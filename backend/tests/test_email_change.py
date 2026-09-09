from __future__ import annotations

from werkzeug.security import generate_password_hash
from app.repositories.store import utcnow
from datetime import timedelta


def signed_in(app, client, user_id="email-change-user"):
    app.extensions["store"].insert_one("users", {
        "_id": user_id,
        "name": "Email Change User",
        "email": f"{user_id}@monedatechnologies.com",
        "username": user_id,
        "username_normalized": user_id,
        "password_hash": generate_password_hash("Secure123"),
        "role_id": "user", "active": True, "email_verified": True,
        "customer_ids": [], "customer_company_ids": [], "company_ids": [],
    })
    response = client.post("/api/v1/auth/login", json={"identifier": user_id, "password": "Secure123"})
    assert response.status_code == 200


def test_email_change_requires_allowed_domain_and_verification(app, client, monkeypatch):
    signed_in(app, client)
    assert client.post("/api/v1/profile/email-change/request", json={"email": "new@gmail.com"}).status_code == 422

    monkeypatch.setattr("app.auth.service.secrets.randbelow", lambda _limit: 123456)
    requested = client.post("/api/v1/profile/email-change/request", json={"email": " NewUser@chemo.in "})
    assert requested.status_code == 200
    user = app.extensions["store"].find_one("users", {"_id": "email-change-user"})
    assert user["email"] == "email-change-user@monedatechnologies.com"
    assert user["pending_email"] == "newuser@chemo.in"

    wrong = client.post("/api/v1/profile/email-change/verify", json={"code": "000000"})
    assert wrong.status_code == 400
    assert app.extensions["store"].find_one("users", {"_id": "email-change-user"})["email"] == "email-change-user@monedatechnologies.com"

    verified = client.post("/api/v1/profile/email-change/verify", json={"code": "123456"})
    assert verified.status_code == 200
    updated = app.extensions["store"].find_one("users", {"_id": "email-change-user"})
    assert updated["email"] == "newuser@chemo.in"
    assert "pending_email" not in updated


def test_email_change_resend_and_cancel_preserve_current_email(app, client, monkeypatch):
    signed_in(app, client, "email-change-resend")
    monkeypatch.setattr("app.auth.service.secrets.randbelow", lambda _limit: 654321)
    assert client.post("/api/v1/profile/email-change/request", json={"email": "first@chemo.in"}).status_code == 200
    challenge = app.extensions["store"].find_one("otp_challenges", {"purpose": "email_change", "user_id": "email-change-resend", "used": False})
    app.extensions["store"].update_one("otp_challenges", {"_id": challenge["_id"]}, {"resend_after": utcnow() - timedelta(seconds=1)})
    assert client.post("/api/v1/profile/email-change/resend", json={}).status_code == 200
    cancelled = client.post("/api/v1/profile/email-change/cancel", json={})
    assert cancelled.status_code == 200
    user = app.extensions["store"].find_one("users", {"_id": "email-change-resend"})
    assert user["email"] == "email-change-resend@monedatechnologies.com"
    assert "pending_email" not in user


def test_email_change_rejects_duplicate_email(app, client):
    signed_in(app, client, "email-change-duplicate")
    app.extensions["store"].insert_one("users", {
        "_id": "email-change-owner", "name": "Existing", "email": "existing@chemo.in",
        "username": "existing", "username_normalized": "existing", "role_id": "user", "active": True,
    })
    response = client.post("/api/v1/profile/email-change/request", json={"email": "EXISTING@CHEMO.IN"})
    assert response.status_code == 409
