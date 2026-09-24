"""预警生命周期服务：检测、确认、发布、升级、解除、作废。

状态与转移规则见 :mod:`app.seismic.statemachine`。本模块负责：

* 状态、版本、转移历史、被拒绝操作的持久化（重启不丢失）；
* 乐观版本控制：请求携带的 expected_version 过期则拒绝转移；
* 幂等检测：相同事件（external_event_key）的重复检测直接返回既有预警；
* 权限校验：发布、解除、作废等动作需要对应权限。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.security import Principal
from app.database import get_connection, transaction
from app.seismic.statemachine import (
    ACTION_LABELS,
    STATE_LABELS,
    AlertAction,
    AlertState,
    allowed_actions,
    is_terminal,
    next_state,
    rejection_message,
    require_permission,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS seismic_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_event_key TEXT NOT NULL UNIQUE,
    event_id INTEGER REFERENCES seismic_events(id) ON DELETE SET NULL,
    title TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN ('detected','pending_confirmation','published','escalated','released','voided')),
    version INTEGER NOT NULL DEFAULT 1,
    alert_level INTEGER NOT NULL DEFAULT 1,
    trigger_source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seismic_alert_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES seismic_alerts(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    from_state TEXT NOT NULL DEFAULT '',
    to_state TEXT NOT NULL DEFAULT '',
    trigger_source TEXT NOT NULL DEFAULT '',
    actor_user_id INTEGER,
    actor_name TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    request_id TEXT NOT NULL DEFAULT '',
    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
    reject_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_transitions_alert ON seismic_alert_transitions(alert_id, id);
CREATE INDEX IF NOT EXISTS idx_alerts_state ON seismic_alerts(state, updated_at);
"""

def ensure_alert_schema() -> None:
    get_connection().executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AlertService:
    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self.connection = connection or get_connection()
        ensure_alert_schema()

    # ------------------------------------------------------------------ 查询

    def _require_alert(self, connection: sqlite3.Connection, alert_id: int) -> sqlite3.Row:
        alert = connection.execute("SELECT * FROM seismic_alerts WHERE id=?", (alert_id,)).fetchone()
        if alert is None:
            raise NotFoundError("预警不存在")
        return alert

    def get_alert(self, alert_id: int) -> dict[str, Any]:
        """返回当前状态、最后一次合法转移和最近一次被拒绝操作的原因。"""
        alert = self._require_alert(self.connection, alert_id)
        result = dict(alert)
        result["state_label"] = STATE_LABELS[AlertState(alert["state"])]
        result["is_terminal"] = is_terminal(AlertState(alert["state"]))
        result["allowed_actions"] = [item.value for item in allowed_actions(AlertState(alert["state"]))]

        last_legal = self.connection.execute(
            "SELECT * FROM seismic_alert_transitions WHERE alert_id=? AND accepted=1 ORDER BY id DESC LIMIT 1",
            (alert_id,),
        ).fetchone()
        result["last_transition"] = self._transition_view(last_legal)

        last_rejected = self.connection.execute(
            "SELECT * FROM seismic_alert_transitions WHERE alert_id=? AND accepted=0 ORDER BY id DESC LIMIT 1",
            (alert_id,),
        ).fetchone()
        result["last_rejected"] = self._transition_view(last_rejected)
        return result

    def list_alerts(self, state: str | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        where = ""
        params: list[Any] = []
        if state:
            try:
                AlertState(state)
            except ValueError as exc:
                raise ValidationError(f"未知预警状态：{state}") from exc
            where = " WHERE state=?"
            params.append(state)
        total = self.connection.execute(f"SELECT COUNT(*) FROM seismic_alerts{where}", params).fetchone()[0]
        rows = self.connection.execute(
            f"SELECT * FROM seismic_alerts{where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {"total": total, "data": [dict(row) for row in rows]}

    def list_transitions(self, alert_id: int, limit: int = 100) -> list[dict[str, Any]]:
        self._require_alert(self.connection, alert_id)
        rows = self.connection.execute(
            "SELECT * FROM seismic_alert_transitions WHERE alert_id=? ORDER BY id DESC LIMIT ?",
            (alert_id, limit),
        ).fetchall()
        return [self._transition_view(row) for row in rows]

    @staticmethod
    def _transition_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        view = dict(row)
        view["accepted"] = bool(view["accepted"])
        view["action_label"] = ACTION_LABELS.get(AlertAction(row["action"]), row["action"])
        return view

    # ------------------------------------------------------------------ 检测

    def detect(self, payload: dict[str, Any], principal: Principal | None = None) -> dict[str, Any]:
        """登记自动检测结果。

        相同 ``external_event_key`` 的重复检测请求幂等：预警已存在时直接返回
        当前预警，不写入转移、不提升版本、不改写状态——因此迟到的台站包
        不可能把已解除/作废的预警重新推回生效状态。
        """
        key = payload["external_event_key"]
        source = payload.get("source") or "auto-detector"
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM seismic_alerts WHERE external_event_key=?", (key,)).fetchone()
            if existing is not None:
                result = dict(existing)
                result["idempotent_replay"] = True
                return result

            now = _now()
            level = int(payload.get("initial_level") or 1)
            cursor = connection.execute(
                "INSERT INTO seismic_alerts(external_event_key,event_id,title,region,state,version,alert_level,trigger_source,created_at,updated_at)"
                " VALUES(?,?,?,?,'detected',1,?,?,?,?)",
                (
                    key,
                    payload.get("event_id"),
                    payload.get("title", ""),
                    payload.get("region", ""),
                    level,
                    source,
                    now,
                    now,
                ),
            )
            alert_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO seismic_alert_transitions(alert_id,version,action,from_state,to_state,trigger_source,actor_user_id,actor_name,reason,request_id,accepted,reject_reason,created_at)"
                " VALUES(?,1,'detect','','detected',?,?,?,'',?,1,'',?)",
                (alert_id, source, principal.user_id if principal else None, principal.display_name if principal else "", payload.get("request_id", ""), now),
            )
            return self.get_alert(alert_id)

    # ------------------------------------------------------------------ 转移

    def transition(
        self,
        alert_id: int,
        action: AlertAction,
        *,
        principal: Principal,
        expected_version: int | None = None,
        level: int | None = None,
        reason: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        if action is AlertAction.DETECT:
            raise ValidationError("检测动作只能通过预警登记接口发起")
        error_to_raise: DomainError | None = None
        with transaction(immediate=True) as connection:
            alert = self._require_alert(connection, alert_id)

            # 转移请求幂等：相同 request_id 的成功重放直接返回既有结果。
            if request_id:
                replay = connection.execute(
                    "SELECT * FROM seismic_alert_transitions WHERE alert_id=? AND request_id=? AND accepted=1 ORDER BY id DESC LIMIT 1",
                    (alert_id, request_id),
                ).fetchone()
                if replay is not None:
                    if replay["action"] != action.value:
                        raise ConflictError("同一幂等键不能用于不同动作")
                    result = self.get_alert(alert_id)
                    result["idempotent_replay"] = True
                    result["replayed_transition_id"] = replay["id"]
                    return result

            current = AlertState(alert["state"])
            current_version = int(alert["version"])
            target = next_state(current, action)
            pending_error: DomainError | None = None

            # 1) 过期版本：携带的 expected_version 与当前版本不一致即拒绝。
            if expected_version is not None and expected_version != current_version:
                message = f"版本过期：请求基于版本 {expected_version}，当前版本为 {current_version}"
                self._reject(connection, alert, action, principal, reason, request_id, message)
                pending_error = ConflictError(
                    message,
                    context={"current_version": current_version, "current_state": current.value},
                )

            # 2) 非法状态转移（含终态保护）。
            elif target is None:
                message = rejection_message(current, action)
                self._reject(connection, alert, action, principal, reason, request_id, message)
                pending_error = ConflictError(
                    message,
                    context={"current_state": current.value, "action": action.value, "allowed_actions": [a.value for a in allowed_actions(current)]},
                )

            # 3) 权限校验。
            else:
                denied = require_permission(action, principal)
                if denied is not None:
                    self._reject(connection, alert, action, principal, reason, request_id, denied)
                    pending_error = PermissionDeniedError(denied)

                # 4) 升级级别必须单调提高。
                elif action is AlertAction.ESCALATE:
                    current_level = int(alert["alert_level"])
                    if level is None:
                        message = "升级必须提供高于当前级别的 level"
                        self._reject(connection, alert, action, principal, reason, request_id, message)
                        pending_error = ValidationError(message)
                    elif level <= current_level:
                        message = f"升级级别必须高于当前级别 {current_level}，收到 {level}"
                        self._reject(connection, alert, action, principal, reason, request_id, message)
                        pending_error = ConflictError(message, context={"current_level": current_level, "received_level": level})

            if pending_error is not None:
                # 拒绝记录随本次事务一起提交；异常在事务外抛出以免回滚审计记录。
                error_to_raise = pending_error
            else:
                new_level = int(level) if action is AlertAction.ESCALATE else int(alert["alert_level"])
                now = _now()
                connection.execute(
                    "UPDATE seismic_alerts SET state=?, version=version+1, alert_level=?, updated_at=? WHERE id=?",
                    (target.value, new_level, now, alert_id),
                )
                connection.execute(
                    "INSERT INTO seismic_alert_transitions(alert_id,version,action,from_state,to_state,trigger_source,actor_user_id,actor_name,reason,request_id,accepted,reject_reason,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,1,'',?)",
                    (
                        alert_id,
                        current_version + 1,
                        action.value,
                        current.value,
                        target.value,
                        f"manual:{principal.username}",
                        principal.user_id,
                        principal.display_name,
                        reason,
                        request_id,
                        now,
                    ),
                )

        if error_to_raise is not None:
            raise error_to_raise
        return self.get_alert(alert_id)

    def _reject(
        self,
        connection: sqlite3.Connection,
        alert: sqlite3.Row,
        action: AlertAction,
        principal: Principal | None,
        reason: str,
        request_id: str,
        reject_reason: str,
    ) -> None:
        """记录一次被拒绝的操作，不改变状态与版本。"""
        now = _now()
        connection.execute(
            "INSERT INTO seismic_alert_transitions(alert_id,version,action,from_state,to_state,trigger_source,actor_user_id,actor_name,reason,request_id,accepted,reject_reason,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?)",
            (
                alert["id"],
                int(alert["version"]),
                action.value,
                alert["state"],
                "",
                f"manual:{principal.username}" if principal else "anonymous",
                principal.user_id if principal else None,
                principal.display_name if principal else "",
                reason,
                request_id,
                reject_reason,
                now,
            ),
        )
