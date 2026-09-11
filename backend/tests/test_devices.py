from werkzeug.security import generate_password_hash
import re

from app.devices.service import _notify_superadmins


def _pending_app(app):
    app.config["DEVICE_ACCESS_MODE"] = "approved_devices_only"
    store = app.extensions["store"]
    store.insert_one("users", {
        "_id": "device-user", "username": "device-user", "username_normalized": "device-user",
        "name": "Device User", "email": "device-user@monedatechnologies.com",
        "password_hash": generate_password_hash("Secure123"), "role_id": "user", "active": True,
        "customer_ids": [], "customer_company_ids": [], "company_ids": [],
    })
    return store


def test_new_device_is_pending_and_approval_unlocks_session(app):
    store = _pending_app(app)
    client = app.test_client()
    login = client.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"})
    assert login.status_code == 200
    assert login.json["data"]["device_status"] == "pending"
    assert client.get("/api/v1/customers").status_code == 403
    device = store.find_one("devices", {"user_id": "device-user"})
    assert device and device["device_status"] == "pending" and "token_hash" in device
    store.update_one("devices", {"_id": device["_id"]}, {"device_status": "approved"})
    assert client.get("/api/v1/me").json["data"]["user"]["_id"] == "device-user"
    assert client.get("/api/v1/customers").status_code == 200


def test_revoked_device_loses_access_immediately(app):
    store = _pending_app(app)
    client = app.test_client()
    client.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"})
    device = store.find_one("devices", {"user_id": "device-user"})
    store.update_one("devices", {"_id": device["_id"]}, {"device_status": "approved"})
    assert client.get("/api/v1/customers").status_code == 200
    store.update_one("devices", {"_id": device["_id"]}, {"device_status": "revoked"})
    assert client.get("/api/v1/customers").status_code == 403


def test_superadmin_is_not_blocked_by_device_gate(app):
    app.config["DEVICE_ACCESS_MODE"] = "approved_devices_only"
    store = app.extensions["store"]
    store.insert_one("users", {
        "_id": "superadmin-new-device", "username": "superadmin-new-device", "username_normalized": "superadmin-new-device",
        "name": "Superadmin", "email": "superadmin-new@monedatechnologies.com",
        "password_hash": generate_password_hash("Secure123"), "role_id": "superadmin", "active": True,
        "customer_ids": [], "customer_company_ids": [], "company_ids": [], "device_access_mode": "approved_devices_only",
    })
    client = app.test_client()
    response = client.post("/api/v1/auth/login", json={"identifier": "superadmin-new-device", "password": "Secure123"})
    assert response.status_code == 200
    assert response.json["data"]["application_access"] is True
    assert client.get("/api/v1/me").json["data"]["application_access"] is True


def test_emergency_access_requires_config_and_real_superadmin_password(app):
    store = app.extensions["store"]
    store.insert_one("users", {
        "_id": "emergency-superadmin", "username": "emergency-superadmin", "username_normalized": "emergency-superadmin",
        "name": "Superadmin", "email": "emergency-superadmin@monedatechnologies.com",
        "password_hash": generate_password_hash("Secure123"), "role_id": "superadmin", "active": True,
        "customer_ids": [], "company_ids": [], "device_access_mode": "approved_devices_only",
    })
    app.config.update(SUPERADMIN_EMERGENCY_ACCESS_ENABLED=True, SUPERADMIN_EMERGENCY_KEY="rotate-me")
    client = app.test_client()
    assert client.post("/api/v1/auth/superadmin/emergency", json={"identifier": "emergency-superadmin", "password": "bad", "emergency_key": "rotate-me"}).status_code == 401
    success = client.post("/api/v1/auth/superadmin/emergency", json={"identifier": "emergency-superadmin", "password": "Secure123", "emergency_key": "rotate-me"})
    assert success.status_code == 200
    assert client.get("/api/v1/me").json["data"]["application_access"] is True


def test_new_pending_device_notifies_superadmins_and_first_email_decision_wins(app):
    store = _pending_app(app)
    client = app.test_client()
    client.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"})
    security_messages = app.extensions["email_provider"].messages
    assert [item["subject"] for item in security_messages] == ["New Moneda device approval required"]
    assert not any(item["subject"] == "User login detected" for item in security_messages)
    message = next(item for item in app.extensions["email_provider"].messages if item["subject"] == "New Moneda device approval required")
    assert "device-user@monedatechnologies.com" in message["html"]
    token = re.search(r"/device-approval/([^?'\"]+)", message["html"]).group(1)
    # The email action requires an authenticated Superadmin and is atomic.
    client.post("/api/v1/auth/demo", json={})
    approved = client.post(f"/api/v1/device-approval/{token}", json={"action": "approve"})
    assert approved.status_code == 200
    second = client.post(f"/api/v1/device-approval/{token}", json={"action": "deny"})
    assert second.status_code == 409
    assert store.find_one("devices", {"user_id": "device-user"})["device_status"] == "approved"


def test_approval_email_is_idempotent_for_one_login_attempt(app):
    store = _pending_app(app)
    client = app.test_client()
    assert client.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"}).status_code == 200
    device = store.find_one("devices", {"user_id": "device-user"})
    attempt = store.find_one("login_approvals", {"user_id": "device-user", "status": "pending"})
    assert device and attempt
    assert attempt.get("approval_email_sent_at")
    before = len([item for item in app.extensions["email_provider"].messages if item["subject"] == "New Moneda device approval required"])
    # A duplicate handler invocation for the same attempt must not enqueue a
    # second approval email. The token is deliberately not reused here; the
    # idempotency guard is keyed by the server-side attempt id.
    with app.app_context():
        _notify_superadmins({"_id": "device-user", "name": "Device User", "email": "device-user@monedatechnologies.com"}, device, attempt={"_id": attempt["_id"], "token": "not-sent"})
    after = len([item for item in app.extensions["email_provider"].messages if item["subject"] == "New Moneda device approval required"])
    assert after == before == 1


def test_login_approval_email_has_working_one_time_links_and_isolated_attempts(app):
    store = _pending_app(app)
    client = app.test_client()
    first = client.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"})
    assert first.status_code == 200
    first_message = next(item for item in app.extensions["email_provider"].messages if item["subject"] == "New Moneda device approval required")
    assert "Accept Login" in first_message["html"] and "Decline Login" in first_message["html"]
    first_token = re.search(r"/device-approval/([^?'\"]+)", first_message["html"]).group(1)
    # A second authenticated attempt gets a different server-side transaction.
    second = client.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"})
    assert second.status_code == 200
    messages = [item for item in app.extensions["email_provider"].messages if item["subject"] == "New Moneda device approval required"]
    second_token = re.search(r"/device-approval/([^?'\"]+)", messages[-1]["html"]).group(1)
    assert first_token != second_token
    assert store.count("login_approvals", {"user_id": "device-user", "status": "pending"}) == 2
    accepted = app.test_client().get(f"/api/v1/device-approval/{first_token}?action=approve")
    assert accepted.status_code == 200
    assert store.count("login_approvals", {"user_id": "device-user", "status": "approved"}) == 1
    assert app.test_client().get(f"/api/v1/device-approval/{first_token}?action=deny").status_code == 409


def test_denial_requires_reason_and_reinstate_returns_to_pending_with_history(app):
    store = _pending_app(app)
    target = app.test_client()
    assert target.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"}).status_code == 200
    admin = app.test_client()
    assert admin.post("/api/v1/auth/demo", json={}).status_code == 200
    listing = admin.get("/api/v1/admin/users/device-user/devices")
    assert listing.status_code == 200
    device_id = listing.json["data"]["items"][0]["device_id"]
    assert admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/reject", json={}).status_code == 422
    denied = admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/reject", json={"reason": "Accidental rejection"})
    assert denied.status_code == 200
    row = store.find_one("devices", {"user_id": "device-user", "device_ref": device_id})
    assert row and row["device_status"] == "denied" and row["denial_reason"] == "Accidental rejection"
    assert target.get("/api/v1/me/device-access").json["data"]["device_status"] == "denied"
    assert admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/reinstate", json={"reason": "   "}).status_code == 422
    reinstated = admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/reinstate", json={"reason": "Retrying approval"})
    assert reinstated.status_code == 200
    row = store.find_one("devices", {"_id": row["_id"]})
    assert row and row["device_status"] == "pending" and row["reinstatement_reason"] == "Retrying approval"
    assert any(item["action"] == "DEVICE_DENIED" for item in row.get("device_history", []))
    assert any(item["action"] == "DEVICE_REINSTATED" for item in row.get("device_history", []))
    assert target.get("/api/v1/me/device-access").json["data"]["reinstatement_reason"] == "Retrying approval"
    assert admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/approve", json={}).status_code == 200
    assert admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/revoke", json={}).status_code == 422
    revoked = admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/revoke", json={"reason": "Security review"})
    assert revoked.status_code == 200
    row = store.find_one("devices", {"_id": row["_id"]})
    assert row and row["device_status"] == "revoked" and row["revoke_reason"] == "Security review"
    assert target.get("/api/v1/me/device-access").json["data"]["device_status"] == "revoked"
    assert admin.post(f"/api/v1/admin/users/device-user/devices/{device_id}/reinstate", json={"reason": "Security review complete"}).status_code == 200
    row = store.find_one("devices", {"_id": row["_id"]})
    assert row and row["device_status"] == "pending"
    assert any(item["action"] == "DEVICE_REVOKED" for item in row.get("device_history", []))
    assert any("Device reinstated" in item["subject"] or "Device reinstated" in item.get("html", "") for item in app.extensions["email_provider"].messages)


def test_denied_device_delete_requires_reason_and_preserves_audit(app):
    store = _pending_app(app)
    target = app.test_client()
    assert target.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"}).status_code == 200
    admin = app.test_client()
    assert admin.post("/api/auth/demo", json={}).status_code == 200
    device = store.find_one("devices", {"user_id": "device-user"})
    assert device
    store.update_one("devices", {"_id": device["_id"]}, {"device_ref": "DVC-DENIED", "device_status": "denied"})
    assert admin.delete("/api/v1/admin/users/device-user/devices/DVC-DENIED", json={}).status_code == 422
    deleted = admin.delete("/api/v1/admin/users/device-user/devices/DVC-DENIED", json={"reason": "Old rejected device entry"})
    assert deleted.status_code == 200
    assert store.find_one("devices", {"_id": device["_id"]}) is None
    events, _ = store.list("audit_logs", {"action": "DEVICE_DELETED"}, limit=20)
    assert any(event.get("metadata", {}).get("previous_status") == "denied" for event in events)


def test_device_details_are_safe_structured_and_audited(app):
    store = _pending_app(app)
    target = app.test_client()
    assert target.post("/api/v1/auth/login", json={"identifier": "device-user", "password": "Secure123"}).status_code == 200
    device = store.find_one("devices", {"user_id": "device-user"})
    assert device and "last_ip" in device
    admin = app.test_client()
    assert admin.post("/api/v1/auth/demo", json={}).status_code == 200
    response = admin.get("/api/v1/admin/users/device-user/devices")
    assert response.status_code == 200
    item = response.json["data"]["items"][0]
    assert item["device_type"] in {"Desktop", "Tablet", "Mobile"}
    assert "browser_name" in item and "os_name" in item and "location" in item
    assert "last_ip" not in item and "token_hash" not in item
    events, _ = store.list("audit_logs", {"action": "DEVICE_DETAILS_VIEWED"}, limit=20)
    assert any(event.get("entity_id") == "device-user" for event in events)


def test_forwarded_client_ip_requires_trusted_proxy(app):
    from app.devices.geolocation import client_ip
    app.config["TRUSTED_PROXY_IPS"] = "127.0.0.1"
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}, headers={"X-Forwarded-For": "198.51.100.24, 127.0.0.1"}):
        assert client_ip() == "198.51.100.24"
    with app.test_request_context("/", environ_base={"REMOTE_ADDR": "198.51.100.24"}, headers={"X-Forwarded-For": "203.0.113.5"}):
        assert client_ip() == "198.51.100.24"
