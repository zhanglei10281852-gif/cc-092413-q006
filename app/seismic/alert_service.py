"""预警生命周期持久化服务。

在 :mod:`app.seismic.alert_state` 定义的状态机之上，提供：

* 带版本号（乐观锁）的状态持久化，过期版本的转移被拒绝；
* 相同事件重复检测请求幂等，迟到的台站包不会复活已解除/作废的预警；
* 记录每次合法转移（触发来源、确认/操作人）与每次被拒绝操作的原因；
* 状态全部落库，进程重启后不丢失。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.seismic.alert_state import (
    ALLOWED_TARGETS,
    SOURCE_AUTOMATIC,
    SOURCE_MANUAL,
    AlertStatus,
    CanFn,
    check_transition,
    resolve_status,
)

ALERT_SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES seismic_events(id) ON DELETE RESTRICT,
    request_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN ('detected','pending','published','escalated','released','voided')),
    version INTEGER NOT NULL DEFAULT 1,
    severity INTEGER NOT NULL DEFAULT 1,
    detected_source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_alert_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES seismic_alerts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    action TEXT NOT NULL,
    trigger_source TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_user_id INTEGER,
    reason TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(alert_id, version)
);
CREATE TABLE IF NOT EXISTS seismic_alert_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER,
    event_id INTEGER,
    from_status TEXT,
    to_status TEXT,
    expected_version INTEGER,
    actual_version INTEGER,
    action TEXT,
    trigger_source TEXT,
    actor TEXT,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_event ON seismic_alerts(event_id);
CREATE INDEX IF NOT EXISTS idx_alert_tr_alert ON seismic_alert_transitions(alert_id, version);
CREATE INDEX IF NOT EXISTS idx_alert_rej_alert ON seismic_alert_rejections(alert_id, id);
"""

# 操作动作 -> 目标状态
ACTION_TARGETS: dict[str, AlertStatus] = {
    "submit_confirm": AlertStatus.PENDING,
    "publish": AlertStatus.PUBLISHED,
    "escalate": AlertStatus.ESCALATED,
    "release": AlertStatus.RELEASED,
    "void": AlertStatus.VOIDED,
}


def ensure_alert_schema(connection: sqlite3.Connection | None = None) -> None:
    connection = connection or get_connection()
    connection.executescript(ALERT_SCHEMA)


class AlertService:
    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        ensure_alert_schema(self.connection)

    # ------------------------------------------------------------------ #
    # 自动检测（幂等）
    # ------------------------------------------------------------------ #
    def detect(
        self,
        event_id: int,
        *,
        request_key: str | None = None,
        trigger_source: str = SOURCE_AUTOMATIC,
        actor: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """登记一次检测。相同事件（同一 request_key）的重复检测幂等返回原预警。

        迟到的台站包复用同一 request_key，只会取回当前预警，绝不会把
        已经解除/作废的预警推回生效状态。
        """
        event = self.connection.execute("SELECT id FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("事件不存在")
        # 默认按事件维度去重：同一事件的自动检测重复请求天然幂等
        key = request_key or f"event:{event_id}"
        actor_name = actor or ("station" if trigger_source == SOURCE_AUTOMATIC else "operator")
        now = to_storage(self.clock.now())
        detail_json = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
        with transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM seismic_alerts WHERE request_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if int(existing["event_id"]) != event_id:
                    raise ConflictError(
                        "同一检测键已用于其他事件",
                        context={"reason_code": "key_conflict"},
                    )
                result = self._view(connection, int(existing["id"]))
                result["idempotent"] = True
                return result
            cursor = connection.execute(
                "INSERT INTO seismic_alerts(event_id,request_key,status,version,severity,detected_source,created_at,updated_at)"
                " VALUES(?,?, 'detected', 1, 1, ?, ?, ?)",
                (event_id, key, trigger_source, now, now),
            )
            alert_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO seismic_alert_transitions(alert_id,version,from_status,to_status,action,trigger_source,actor,reason,detail_json,created_at)"
                " VALUES(?, 1, 'detected','detected','detect',?,?, '', ?, ?)",
                (alert_id, trigger_source, actor_name, detail_json, now),
            )
            result = self._view(connection, alert_id)
            result["idempotent"] = False
            return result

    # ------------------------------------------------------------------ #
    # 带版本号的状态转移
    # ------------------------------------------------------------------ #
    def transition(
        self,
        alert_id: int,
        *,
        action: str,
        expected_version: int | None = None,
        trigger_source: str = SOURCE_MANUAL,
        principal: Principal | None = None,
        reason: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = ACTION_TARGETS.get(action)
        if target is None:
            raise ConflictError(
                f"未知预警操作：{action}",
                context={"reason_code": "unknown_action", "allowed": sorted(ACTION_TARGETS)},
            )
        can: CanFn = principal.can if principal is not None else (lambda _permission: False)
        actor_name = principal.display_name if principal is not None else (
            "station" if trigger_source == SOURCE_AUTOMATIC else "anonymous"
        )
        actor_user_id = principal.user_id if principal is not None else None

        rejection: dict[str, Any] | None = None
        with transaction(immediate=True) as connection:
            alert = connection.execute(
                "SELECT * FROM seismic_alerts WHERE id=?", (alert_id,)
            ).fetchone()
            if alert is None:
                raise NotFoundError("预警不存在")
            current = resolve_status(alert["status"])
            actual_version = int(alert["version"])

            # 1) 过期版本（乐观锁）
            if expected_version is not None and expected_version != actual_version:
                rejection = {
                    "alert_id": alert_id,
                    "event_id": int(alert["event_id"]),
                    "from_status": current.value,
                    "to_status": target.value,
                    "expected_version": expected_version,
                    "actual_version": actual_version,
                    "action": action,
                    "trigger_source": trigger_source,
                    "actor": actor_name,
                    "reason_code": "stale_version",
                    "reason": f"版本已过期：请求基于 v{expected_version}，当前为 v{actual_version}",
                }
            else:
                # 2) 状态白名单 + 3) 权限（不合法抛领域异常，转译为拒绝记录）
                try:
                    check_transition(current, target, trigger_source=trigger_source, can=can)
                except (ConflictError, PermissionDeniedError) as exc:
                    rejection = {
                        "alert_id": alert_id,
                        "event_id": int(alert["event_id"]),
                        "from_status": current.value,
                        "to_status": target.value,
                        "expected_version": expected_version,
                        "actual_version": actual_version,
                        "action": action,
                        "trigger_source": trigger_source,
                        "actor": actor_name,
                        "reason_code": exc.context.get("reason_code", "rejected"),
                        "reason": str(exc),
                        "_error": exc,
                    }
                else:
                    new_version = actual_version + 1
                    now = to_storage(self.clock.now())
                    severity = int(alert["severity"]) + (1 if action == "escalate" else 0)
                    detail_json = json.dumps(detail or {}, ensure_ascii=False, sort_keys=True)
                    cursor = connection.execute(
                        "INSERT INTO seismic_alert_transitions(alert_id,version,from_status,to_status,action,trigger_source,actor,actor_user_id,reason,detail_json,created_at)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (alert_id, new_version, current.value, target.value, action,
                         trigger_source, actor_name, actor_user_id, reason, detail_json, now),
                    )
                    connection.execute(
                        "UPDATE seismic_alerts SET status=?, version=?, severity=?, updated_at=? WHERE id=?",
                        (target.value, new_version, severity, now, alert_id),
                    )

        if rejection is not None:
            error = rejection.pop("_error", None)
            self._record_rejection(rejection)
            if error is not None:
                raise error
            raise ConflictError(rejection["reason"], context={"reason_code": rejection["reason_code"]})

        return self.get_alert(alert_id)

    def _record_rejection(self, rejection: dict[str, Any]) -> None:
        now = to_storage(self.clock.now())
        detail = {
            "expected_version": rejection.get("expected_version"),
            "actual_version": rejection.get("actual_version"),
        }
        with transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO seismic_alert_rejections(alert_id,event_id,from_status,to_status,expected_version,actual_version,"
                "action,trigger_source,actor,reason_code,reason,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rejection.get("alert_id"), rejection.get("event_id"),
                    rejection.get("from_status"), rejection.get("to_status"),
                    rejection.get("expected_version"), rejection.get("actual_version"),
                    rejection.get("action"), rejection.get("trigger_source"),
                    rejection.get("actor"), rejection["reason_code"], rejection["reason"],
                    json.dumps(detail, ensure_ascii=False), now,
                ),
            )

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get_alert(self, alert_id: int) -> dict[str, Any]:
        return self._view(self.connection, alert_id)

    def _view(self, connection: sqlite3.Connection, alert_id: int) -> dict[str, Any]:
        alert = connection.execute("SELECT * FROM seismic_alerts WHERE id=?", (alert_id,)).fetchone()
        if alert is None:
            raise NotFoundError("预警不存在")
        data = dict(alert)
        transition_rows = connection.execute(
            "SELECT * FROM seismic_alert_transitions WHERE alert_id=? ORDER BY version", (alert_id,)
        ).fetchall()
        history = [dict(row) for row in transition_rows]
        data["history"] = history
        data["last_transition"] = history[-1] if history else None
        current = resolve_status(data["status"])
        data["allowed_targets"] = sorted(status.value for status in ALLOWED_TARGETS[current])
        rejection_row = connection.execute(
            "SELECT * FROM seismic_alert_rejections WHERE alert_id=? ORDER BY id DESC LIMIT 1", (alert_id,)
        ).fetchone()
        if rejection_row is not None:
            rejection = dict(rejection_row)
            rejection.pop("alert_id", None)
            rejection.pop("event_id", None)
            data["last_rejection"] = rejection
        else:
            data["last_rejection"] = None
        return data

    def list_for_event(self, event_id: int) -> list[dict[str, Any]]:
        event = self.connection.execute("SELECT id FROM seismic_events WHERE id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFoundError("事件不存在")
        rows = self.connection.execute(
            "SELECT id FROM seismic_alerts WHERE event_id=? ORDER BY id", (event_id,)
        ).fetchall()
        return [self.get_alert(int(row["id"])) for row in rows]
