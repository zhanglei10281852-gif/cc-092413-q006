from __future__ import annotations

import pytest


def detect_payload(key: str = "EW-2026-0001", **overrides):
    payload = {
        "external_event_key": key,
        "title": "康定附近 5.8 级地震",
        "region": "四川甘孜",
        "initial_level": 1,
        "source": "station-network-A",
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def alert_id(client) -> int:
    response = client.post("/api/seismic/alerts/detect", json=detect_payload())
    assert response.status_code == 201, response.text
    return response.json()["id"]


@pytest.fixture()
def no_role_user(client, admin):
    created = client.post(
        "/api/users",
        json={"username": "watcher", "password": "Watch!234567", "display_name": "值班观察员", "role_codes": []},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": "watcher", "password": "Watch!234567", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def transition(client, alert_id, headers, action, **fields):
    payload = {"action": action, **fields}
    return client.post(f"/api/seismic/alerts/{alert_id}/transitions", json=payload, headers=headers)


def test_detect_is_idempotent_for_same_event(client, alert_id):
    first = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert first["state"] == "detected"
    assert first["version"] == 1
    assert first["last_transition"]["action"] == "detect"
    assert first["last_transition"]["trigger_source"] == "station-network-A"

    duplicate = client.post("/api/seismic/alerts/detect", json=detect_payload())
    assert duplicate.status_code == 201
    body = duplicate.json()
    assert body["id"] == alert_id
    assert body["idempotent_replay"] is True
    # 幂等重放不提升版本、不追加转移记录。
    again = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert again["version"] == 1
    transitions = client.get(f"/api/seismic/alerts/{alert_id}/transitions").json()["data"]
    assert [item["action"] for item in transitions] == ["detect"]


def test_full_lifecycle_detect_confirm_publish_escalate_release(client, alert_id, admin):
    r = transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1, reason="台站三源一致")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "pending_confirmation"
    assert r.json()["version"] == 2

    r = transition(client, alert_id, admin["headers"], "confirm_publish", expected_version=2, reason="值班长确认")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "published"
    assert r.json()["version"] == 3

    r = transition(client, alert_id, admin["headers"], "escalate", expected_version=3, level=2, reason="影响扩大")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "escalated"
    assert body["version"] == 4
    assert body["alert_level"] == 2

    r = transition(client, alert_id, admin["headers"], "release", expected_version=4, reason="震情结束")
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "released"
    assert r.json()["is_terminal"] is True

    view = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert view["last_transition"]["action"] == "release"
    assert view["last_transition"]["actor_name"]
    assert view["allowed_actions"] == []


def test_late_detection_packet_cannot_revive_released_alert(client, alert_id, admin):
    transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1)
    transition(client, alert_id, admin["headers"], "confirm_publish", expected_version=2)
    transition(client, alert_id, admin["headers"], "release", expected_version=3)
    released = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert released["state"] == "released"

    # 迟到的台站包：重复检测同一事件，必须幂等且状态保持解除。
    late = client.post("/api/seismic/alerts/detect", json=detect_payload(source="station-network-B"))
    assert late.status_code == 201
    assert late.json()["idempotent_replay"] is True

    # 终态上的任何转移也必须被拒绝，并记录原因。
    r = transition(client, alert_id, admin["headers"], "escalate", expected_version=4, level=3)
    assert r.status_code == 409
    assert "终态" in r.json()["error"]["message"]

    view = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert view["state"] == "released"
    assert view["version"] == 4
    assert view["last_rejected"]["action"] == "escalate"
    assert "终态" in view["last_rejected"]["reject_reason"]


def test_stale_version_transition_is_rejected(client, alert_id, admin):
    transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1)
    # 另一请求仍拿着版本 1 试图发布。
    stale = transition(client, alert_id, admin["headers"], "confirm_publish", expected_version=1)
    assert stale.status_code == 409
    detail = stale.json()["error"]
    assert "版本过期" in detail["message"]
    assert detail["context"]["current_version"] == 2

    view = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert view["state"] == "pending_confirmation"
    assert view["version"] == 2
    assert view["last_rejected"]["reject_reason"].startswith("版本过期")


def test_illegal_transition_is_rejected(client, alert_id, admin):
    # detected 上不能直接发布。
    r = transition(client, alert_id, admin["headers"], "confirm_publish", expected_version=1)
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["context"]["current_state"] == "detected"
    assert "request_confirmation" in body["context"]["allowed_actions"]

    view = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert view["state"] == "detected"
    assert view["last_rejected"]["accepted"] is False
    assert "不允许执行" in view["last_rejected"]["reject_reason"]


def test_escalation_level_must_increase(client, alert_id, admin):
    transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1)
    transition(client, alert_id, admin["headers"], "confirm_publish", expected_version=2)
    r = transition(client, alert_id, admin["headers"], "escalate", expected_version=3, level=1)
    assert r.status_code == 409
    assert "高于当前级别" in r.json()["error"]["message"]


def test_publish_and_release_require_permission(client, alert_id, no_role_user, admin):
    # 未认证请求被拒绝。
    unauthenticated = client.post(
        f"/api/seismic/alerts/{alert_id}/transitions",
        json={"action": "request_confirmation", "expected_version": 1},
    )
    assert unauthenticated.status_code == 401

    # 已登录但没有预警权限的值班员不能提交确认。
    denied = transition(client, alert_id, no_role_user, "request_confirmation", expected_version=1)
    assert denied.status_code == 403
    assert "seismic.alerts.confirm" in denied.json()["error"]["message"]

    # 被拒绝的操作要记录确认人信息，且状态不变。
    view = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert view["state"] == "detected"
    assert view["last_rejected"]["actor_name"] == "值班观察员"
    assert view["last_rejected"]["reject_reason"].startswith("缺少权限")

    # 有权限的用户仍可正常推进。
    ok = transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1)
    assert ok.status_code == 200


def test_transition_request_is_idempotent_by_request_id(client, alert_id, admin):
    first = transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1, request_id="req-abc")
    assert first.status_code == 200
    replay = transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=2, request_id="req-abc")
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["version"] == 2


def test_void_terminates_alert(client, admin):
    created = client.post("/api/seismic/alerts/detect", json=detect_payload("EW-2026-0002"))
    target = created.json()["id"]
    r = transition(client, target, admin["headers"], "void", expected_version=1, reason="误报")
    assert r.status_code == 200
    assert r.json()["state"] == "voided"
    assert r.json()["is_terminal"] is True

    revive = client.post("/api/seismic/alerts/detect", json=detect_payload("EW-2026-0002"))
    assert revive.json()["state"] == "voided"
    assert revive.json()["idempotent_replay"] is True


def test_state_survives_connection_restart(client, alert_id, admin):
    transition(client, alert_id, admin["headers"], "request_confirmation", expected_version=1)
    transition(client, alert_id, admin["headers"], "confirm_publish", expected_version=2)
    transition(client, alert_id, admin["headers"], "escalate", expected_version=3, level=3)

    from app.database import close_connection

    # 关闭并重建线程局部连接，模拟进程重启后重新打开同一个数据库文件。
    close_connection()

    view = client.get(f"/api/seismic/alerts/{alert_id}").json()
    assert view["state"] == "escalated"
    assert view["version"] == 4
    assert view["alert_level"] == 3
    assert view["last_transition"]["action"] == "escalate"
    transitions = client.get(f"/api/seismic/alerts/{alert_id}/transitions").json()["data"]
    assert [item["action"] for item in transitions] == ["escalate", "confirm_publish", "request_confirmation", "detect"]

    # 重启后过期版本转移依旧被拒绝。
    stale = transition(client, alert_id, admin["headers"], "release", expected_version=1)
    assert stale.status_code == 409
    assert "版本过期" in stale.json()["error"]["message"]
