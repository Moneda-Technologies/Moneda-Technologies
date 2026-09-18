from __future__ import annotations

def complete_password_otp_login(app, client, identifier: str, password: str):
    """Sign in through the production password authentication path.

    Password login is intentionally independent from email OTP. The second
    tuple value is retained for existing device tests that only assert a
    successful response object.
    """
    login = client.post("/api/v1/auth/login", json={"identifier": identifier, "password": password})
    assert login.status_code == 200
    assert login.json["data"]["next_step"] in {"company-selection", "customer-selection", "device-approval-pending"}
    return login, login
