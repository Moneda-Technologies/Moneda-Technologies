from __future__ import annotations

from werkzeug.security import check_password_hash

from app.communication.email import EmailDeliveryError


VALID_INVITATION = {
    "name": "Invited User",
    "username": "invited.user",
    "email": "invited.user@monedatechnologies.com",
    "password": "Secure123",
    "confirm_password": "Secure123",
    "role_id": "user",
}


def test_superadmin_invites_user_with_hashed_password_and_email(app, authenticated):
    previous_total = authenticated.get("/api/v1/admin/users").json["data"]["total"]
    response = authenticated.post("/api/v1/admin/users", json=VALID_INVITATION)

    assert response.status_code == 201
    assert response.json["message"] == "User invited successfully."
    assert response.json["data"]["invitation"]["email_sent"] is True
    assert "password_hash" not in response.json["data"]["user"]

    stored = app.extensions["store"].find_one("users", {"username_normalized": "invited.user"})
    assert stored is not None
    assert stored["role_id"] == "user"
    assert stored["customer_ids"] == []
    assert stored["invitation_email_status"] == "sent"
    assert "password" not in stored
    assert "confirm_password" not in stored
    assert stored["password_hash"] != VALID_INVITATION["password"]
    assert check_password_hash(stored["password_hash"], VALID_INVITATION["password"])

    messages = app.extensions["email_provider"].messages
    invitation = messages[-1]
    assert invitation["to"] == [VALID_INVITATION["email"]]
    assert invitation["subject"] == "You've been invited to Moneda Technologies"
    assert VALID_INVITATION["username"] in invitation["html"]
    assert VALID_INVITATION["password"] in invitation["html"]
    assert stored["password_hash"] not in invitation["html"]
    assert "User" in invitation["html"]
    assert "http://localhost:3005/login" in invitation["html"]

    audit = app.extensions["store"].find_one("audit_logs", {"action": "user.invite", "entity_id": stored["_id"]})
    assert audit is not None
    assert audit["metadata"]["actor_user_id"] == "user-demo-admin"
    assert audit["metadata"]["target_user_id"] == stored["_id"]
    assert audit["metadata"]["role_id"] == "user"
    assert "password" not in audit["metadata"]
    assert "password_hash" not in audit["metadata"]

    listing = authenticated.get("/api/v1/admin/users")
    assert listing.json["data"]["total"] == previous_total + 1
    listed = next(user for user in listing.json["data"]["items"] if user["_id"] == stored["_id"])
    assert listed["role_id"] == "user"
    assert listed["customer_access_global"] is False
    assert listed["customer_access_count"] == 0
    assert "password_hash" not in listed


def test_invited_user_can_authenticate_without_forced_password_change(app, authenticated):
    created = authenticated.post("/api/v1/admin/users", json=VALID_INVITATION)
    assert created.status_code == 201

    invited_client = app.test_client()
    login = invited_client.post("/api/v1/auth/login", json={
        "identifier": VALID_INVITATION["username"],
        "password": VALID_INVITATION["password"],
    })
    assert login.status_code == 200
    assert "force_password_change" not in login.json["data"]
    assert "password_change" not in str(login.json).lower()


def test_invite_validation_and_duplicate_submission(app, authenticated):
    mismatch = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION, "confirm_password": "Different123",
    })
    assert mismatch.status_code == 422
    assert mismatch.json["message"] == "Passwords do not match."
    assert app.extensions["store"].find_one("users", {"username_normalized": "invited.user"}) is None

    weak = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION, "password": "alllowercase1", "confirm_password": "alllowercase1",
    })
    assert weak.status_code == 422
    assert weak.json["error"] == "password_policy"

    invalid_email = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION, "email": "not-an-email",
    })
    assert invalid_email.status_code == 422
    assert invalid_email.json["error"] == "invalid_email"

    outside_domain = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION, "email": "invited@example.com",
    })
    assert outside_domain.status_code == 422
    assert outside_domain.json["error"] == "signup_email_domain_not_allowed"

    invalid_role = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION, "role_id": "invented-role",
    })
    assert invalid_role.status_code == 422
    assert invalid_role.json["error"] == "invalid_role"

    assert authenticated.post("/api/v1/admin/users", json=VALID_INVITATION).status_code == 201
    duplicate_username = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION,
        "username": "INVITED.USER",
        "email": "second.user@monedatechnologies.com",
    })
    assert duplicate_username.status_code == 409
    assert duplicate_username.json["error"] == "username_in_use"

    duplicate_email = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION,
        "username": "second.user",
    })
    assert duplicate_email.status_code == 409
    assert duplicate_email.json["error"] == "email_in_use"


def test_non_superadmin_cannot_assign_superadmin_role(app, authenticated):
    app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, {"role_id": "admin"})
    response = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION, "role_id": "superadmin",
    })
    assert response.status_code == 403
    assert response.json["error"] == "superadmin_required"
    assert app.extensions["store"].find_one("users", {"username_normalized": "invited.user"}) is None


def test_invited_user_uses_existing_customer_assignment_model(app, authenticated):
    response = authenticated.post("/api/v1/admin/users", json={
        **VALID_INVITATION,
        "customer_ids": ["customer-demo-1", "customer-demo-1"],
    })
    assert response.status_code == 201
    stored = app.extensions["store"].find_one("users", {"username_normalized": "invited.user"})
    assert stored["customer_ids"] == ["customer-demo-1"]
    customer = app.extensions["store"].find_one("customers", {"_id": "customer-demo-1"})
    assert stored["_id"] in customer["assigned_user_ids"]

    listing = authenticated.get("/api/v1/admin/users")
    listed = next(user for user in listing.json["data"]["items"] if user["_id"] == stored["_id"])
    assert listed["customer_access_global"] is False
    assert listed["assigned_customer_ids"] == ["customer-demo-1"]
    assert listed["customer_access_count"] == 1


def test_email_failure_keeps_created_user_and_reports_actual_state(app, authenticated, monkeypatch):
    def fail_delivery(**_kwargs):
        raise EmailDeliveryError(
            "Provider unavailable",
            stage="message_submission",
            diagnostic_id="email-test-failure",
            error_code="OAUTH_NOT_CONNECTED",
        )

    monkeypatch.setattr(app.extensions["email_service"], "send_user_invitation", fail_delivery)
    response = authenticated.post("/api/v1/admin/users", json=VALID_INVITATION)

    assert response.status_code == 201
    assert response.json["message"] == "User account created, but the invitation email could not be sent."
    assert response.json["data"]["invitation"] == {
        "email_sent": False,
        "status": "failed",
        "diagnostic_id": "email-test-failure",
        "error_code": "OAUTH_NOT_CONNECTED",
    }
    stored = app.extensions["store"].find_one("users", {"username_normalized": "invited.user"})
    assert stored is not None
    assert stored["invitation_email_status"] == "failed"
    assert "password" not in stored
