from __future__ import annotations


def event_payload(external_id: str = "EQ-ALERT-001"):
    return {
        "external_id": external_id,
        "origin_time": "2026-09-24T12:00:00+00:00",
        "latitude": 30.1,
        "longitude": 103.2,
        "depth_km": 12.0,
        "magnitude": 6.2,
        "magnitude_type": "ML",
        "source": "station-auto",
    }


def make_event(client, external_id="EQ-ALERT-001"):
    created = client.post("/api/seismic/events", json=event_payload(external_id))
    assert created.status_code == 201, created.text
    return created.json()["id"]


def make_auditor(client, admin, username="auditor1"):
    created = client.post(
        "/api/users",
        json={"username": username, "password": "Auditor!23456", "display_name": "审计员", "role_codes": ["auditor"]},
        headers=admin["headers"],
    )
    assert created.status_code == 201, created.text
    login = client.post("/api/auth/login", json={"username": username, "password": "Auditor!23456", "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


def test_full_alert_lifecycle_detect_confirm_publish_escalate_release(client, admin):
    event_id = make_event(client)
    detected = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={"trigger_source": "automatic"})
    assert detected.status_code == 201, detected.text
    alert = detected.json()
    assert alert["status"] == "detected"
    assert alert["version"] == 1
    assert alert["idempotent"] is False
    alert_id = alert["id"]

    # 待确认
    pending = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1, "trigger_source": "manual"},
        headers=admin["headers"],
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    assert pending.json()["version"] == 2

    # 发布
    published = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "publish", "expected_version": 2, "reason": "值班员确认发布"},
        headers=admin["headers"],
    )
    assert published.status_code == 200
    body = published.json()
    assert body["status"] == "published"
    assert body["version"] == 3
    assert body["last_transition"]["action"] == "publish"
    assert body["last_transition"]["actor"] == "系统管理员"
    assert body["last_transition"]["trigger_source"] == "manual"

    # 升级（可逐级）
    escalated = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "escalate", "expected_version": 3},
        headers=admin["headers"],
    )
    assert escalated.status_code == 200
    body = escalated.json()
    assert body["status"] == "escalated"
    assert body["version"] == 4
    assert body["severity"] == 2

    # 解除（终态）
    released = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "release", "expected_version": 4, "reason": "震情结束"},
        headers=admin["headers"],
    )
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert released.json()["version"] == 5


def test_duplicate_detect_is_idempotent(client):
    event_id = make_event(client, "EQ-ALERT-002")
    first = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={"trigger_source": "automatic"}).json()
    second = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={"trigger_source": "automatic"}).json()
    assert first["id"] == second["id"]
    assert second["idempotent"] is True
    assert second["version"] == 1


def test_late_station_packet_does_not_revive_released_alert(client, admin):
    event_id = make_event(client, "EQ-ALERT-003")
    alert = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={"trigger_source": "automatic"}).json()
    alert_id = alert["id"]
    for action, version in [("submit_confirm", 1), ("publish", 2), ("release", 3)]:
        resp = client.post(
            f"/api/seismic/alerts/{alert_id}/transition",
            json={"action": action, "expected_version": version},
            headers=admin["headers"],
        )
        assert resp.status_code == 200, resp.text

    # 迟到的台站包再次上报检测：幂等返回，状态仍是 released，不复活
    late = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={"trigger_source": "automatic"})
    assert late.status_code == 201
    assert late.json()["status"] == "released"
    assert late.json()["idempotent"] is True

    # 直接尝试从 released 推回 published 被拒绝
    revive = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "publish", "expected_version": 4},
        headers=admin["headers"],
    )
    assert revive.status_code == 409
    assert revive.json()["error"]["context"]["reason_code"] == "terminal_state"

    view = client.get(f"/api/seismic/alerts/{alert_id}", headers=admin["headers"]).json()
    assert view["status"] == "released"
    assert view["last_rejection"]["reason_code"] == "terminal_state"


def test_stale_version_transition_is_rejected(client, admin):
    event_id = make_event(client, "EQ-ALERT-004")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1},
        headers=admin["headers"],
    )
    # 仍用过期的 v1 去发布（当前已是 v2）
    stale = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "publish", "expected_version": 1},
        headers=admin["headers"],
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["context"]["reason_code"] == "stale_version"

    view = client.get(f"/api/seismic/alerts/{alert_id}", headers=admin["headers"]).json()
    assert view["status"] == "pending"  # 未被错误推进
    assert view["version"] == 2
    assert view["last_rejection"]["reason_code"] == "stale_version"
    assert view["last_rejection"]["expected_version"] == 1
    assert view["last_rejection"]["actual_version"] == 2


def test_illegal_transition_is_rejected(client, admin):
    event_id = make_event(client, "EQ-ALERT-005")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    # detected 不能直接 release
    resp = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "release", "expected_version": 1},
        headers=admin["headers"],
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["context"]["reason_code"] == "illegal_transition"
    view = client.get(f"/api/seismic/alerts/{alert_id}", headers=admin["headers"]).json()
    assert view["status"] == "detected"
    assert view["last_rejection"]["reason_code"] == "illegal_transition"


def test_publish_and_release_require_permission(client, admin):
    event_id = make_event(client, "EQ-ALERT-006")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    auditor = make_auditor(client, admin)

    # 审计员没有确认/发布权限
    denied = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1},
        headers=auditor,
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["context"]["reason_code"] == "missing_permission"

    view = client.get(f"/api/seismic/alerts/{alert_id}", headers=admin["headers"]).json()
    assert view["status"] == "detected"
    assert view["last_rejection"]["reason_code"] == "missing_permission"


def test_transition_requires_authentication(client):
    event_id = make_event(client, "EQ-ALERT-007")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    resp = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1},
    )
    assert resp.status_code == 401


def test_void_from_pending_is_terminal(client, admin):
    event_id = make_event(client, "EQ-ALERT-008")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1},
        headers=admin["headers"],
    )
    voided = client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "void", "expected_version": 2, "reason": "误报"},
        headers=admin["headers"],
    )
    assert voided.status_code == 200
    assert voided.json()["status"] == "voided"
    assert voided.json()["allowed_targets"] == []


def test_query_returns_status_last_transition_and_history(client, admin):
    event_id = make_event(client, "EQ-ALERT-009")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1},
        headers=admin["headers"],
    )
    view = client.get(f"/api/seismic/alerts/{alert_id}", headers=admin["headers"]).json()
    assert view["status"] == "pending"
    assert view["last_transition"]["action"] == "submit_confirm"
    assert {item["action"] for item in view["history"]} == {"detect", "submit_confirm"}
    assert "published" in view["allowed_targets"]
    assert "voided" in view["allowed_targets"]
    assert view["last_rejection"] is None

    listed = client.get(f"/api/seismic/events/{event_id}/alerts", headers=admin["headers"]).json()
    assert len(listed["data"]) == 1
    assert listed["data"][0]["id"] == alert_id


def test_alert_state_survives_restart(client, admin):
    from app.database import close_connection
    event_id = make_event(client, "EQ-ALERT-010")
    alert_id = client.post(f"/api/seismic/events/{event_id}/alerts/detect", json={}).json()["id"]
    client.post(
        f"/api/seismic/alerts/{alert_id}/transition",
        json={"action": "submit_confirm", "expected_version": 1},
        headers=admin["headers"],
    )

    # 模拟重启：关闭并重建连接（指向同一个数据库文件）
    close_connection()
    view = client.get(f"/api/seismic/alerts/{alert_id}", headers=admin["headers"]).json()
    assert view["status"] == "pending"
    assert view["version"] == 2
    assert view["last_transition"]["action"] == "submit_confirm"
