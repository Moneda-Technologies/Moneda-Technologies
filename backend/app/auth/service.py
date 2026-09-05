from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from app.communication.email import EmailProvider
from app.repositories.store import Store, utcnow


class OtpError(ValueError):
    pass


class OtpService:
    def __init__(self, store: Store, email_provider: EmailProvider) -> None:
        self.store = store
        self.email_provider = email_provider

    def request(self, email: str, purpose: str) -> None:
        email = email.lower().strip()
        user = self.store.find_one("users", {"email": email})
        if purpose in {"login", "reset"} and not user:
            return
        if purpose == "signup" and user:
            return
        previous = self.store.find_one("otp_challenges", {"email": email, "purpose": purpose, "used": False})
        if previous and previous.get("resend_after") and previous["resend_after"] > utcnow():
            raise OtpError("Please wait before requesting another code")
        code = f"{secrets.randbelow(1_000_000):06d}"
        challenge = {
            "email": email, "purpose": purpose, "code_hash": generate_password_hash(code),
            "expires_at": utcnow() + timedelta(minutes=10), "resend_after": utcnow() + timedelta(seconds=60),
            "attempts": 0, "max_attempts": 5, "used": False,
        }
        if previous:
            self.store.update_one("otp_challenges", {"_id": previous["_id"]}, {"used": True})
        row = self.store.insert_one("otp_challenges", challenge)
        try:
            self.email_provider.send(
                to=[email], subject="Your Moneda verification code",
                html=("<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
                      f"<p>Your one-time verification code is <strong>{code}</strong>.</p>"
                      "<p>It expires in 10 minutes. If you did not request it, ignore this email.</p></div>"),
            )
        except Exception:
            self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"used": True})
            raise

    def verify(self, email: str, purpose: str, code: str) -> dict[str, Any] | None:
        row = self.store.find_one("otp_challenges", {"email": email.lower().strip(), "purpose": purpose, "used": False})
        if not row or row["expires_at"] < utcnow():
            raise OtpError("The verification code is invalid or expired")
        attempts = int(row.get("attempts", 0)) + 1
        if attempts > int(row.get("max_attempts", 5)):
            self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"used": True})
            raise OtpError("Maximum verification attempts exceeded")
        if not check_password_hash(row["code_hash"], code):
            self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"attempts": attempts})
            raise OtpError("The verification code is invalid or expired")
        self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"attempts": attempts, "used": True})
        return self.store.find_one("users", {"email": email.lower().strip()})


def find_user_by_identifier(store: Store, identifier: str) -> dict[str, Any] | None:
    """Resolve a login identifier without exposing whether an account exists.

    Usernames are case-insensitive. IDs remain exact, while email is accepted
    as a backwards-compatible identifier for accounts created by the original
    email-OTP flow. A leading ``username`` label is tolerated for copy/paste
    convenience (for example, ``Username Admin``).
    """

    value = identifier.strip()
    if value.lower().startswith("username "):
        value = value[9:].strip()
    if not value:
        return None
    user = store.find_one("users", {"_id": value})
    if user:
        return user
    escaped = re.escape(value)
    user = store.find_one("users", {"username": {"$regex": f"^{escaped}$"}})
    if user:
        return user
    return store.find_one("users", {"email": value.lower()})


def verify_password(store: Store, identifier: str, password: str) -> dict[str, Any] | None:
    user = find_user_by_identifier(store, identifier)
    if not user or not user.get("active", False):
        return None
    password_hash = str(user.get("password_hash", ""))
    if not password_hash or not check_password_hash(password_hash, password):
        return None
    return user
