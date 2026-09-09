from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta
from html import escape
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from app.communication.email import EmailProvider
from app.repositories.store import Store, ensure_utc, utcnow


logger = logging.getLogger(__name__)


class OtpError(ValueError):
    pass


class OtpService:
    def __init__(self, store: Store, email_provider: EmailProvider) -> None:
        self.store = store
        self.email_provider = email_provider

    def request(self, email: str, purpose: str, *, metadata: dict[str, Any] | None = None,
                request_id: str | None = None, return_challenge: bool = False) -> bool | dict[str, Any]:
        trace_id = request_id or "otp-untracked"
        email = email.lower().strip()
        user = self.store.find_one("users", {"email": email})
        if purpose in {"login", "reset"} and not user:
            return False
        if purpose == "signup" and user:
            return False
        logger.info("[%s] otp stage=eligibility result=PASS purpose=%s", trace_id, purpose)
        previous = self.store.find_one("otp_challenges", {"email": email, "purpose": purpose, "used": False})
        resend_after = self._normalized_datetime(previous, "resend_after", trace_id) if previous else None
        if resend_after and resend_after > utcnow():
            raise OtpError("Please wait before requesting another code")
        code = f"{secrets.randbelow(1_000_000):06d}"
        logger.info("[%s] otp stage=generation result=PASS purpose=%s", trace_id, purpose)
        challenge = {
            "email": email, "purpose": purpose, "code_hash": generate_password_hash(code),
            "expires_at": utcnow() + timedelta(minutes=10), "resend_after": utcnow() + timedelta(seconds=60),
            "attempts": 0, "max_attempts": 5, "used": False,
        }
        if metadata:
            challenge.update({key: value for key, value in metadata.items() if key not in {"code", "otp", "code_hash"}})
        if previous:
            self.store.update_one("otp_challenges", {"_id": previous["_id"]}, {"used": True})
        row = self.store.insert_one("otp_challenges", challenge)
        logger.info("[%s] otp stage=persistence result=PASS purpose=%s", trace_id, purpose)
        try:
            display_name = escape(str((metadata or {}).get("name") or "there"))
            html = ("<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
                    f"<p>Hello {display_name},</p>"
                    f"<p>Your Moneda Technologies verification code is <strong>{code}</strong>.</p>"
                    "<p>It expires in 10 minutes. If you did not request it, ignore this email.</p></div>")
            method_name = {
                "signup": "send_signup_otp", "login": "send_login_otp", "reset": "send_reset_otp",
                "email_change": "send_email_change_otp",
            }.get(purpose, "send_otp")
            send_otp = getattr(self.email_provider, method_name, None) or getattr(self.email_provider, "send_otp", None)
            result = (send_otp(to=[email], html=html, request_id=request_id) if callable(send_otp) else
                      self.email_provider.send(to=[email], subject="Moneda Technologies — Email Verification Code", html=html, request_id=request_id))
            logger.info("[%s] otp stage=email_submission result=PASS purpose=%s", trace_id, purpose)
            self.store.insert_one("email_logs", {
                "recipient": email, "subject": "Moneda Technologies — Email Verification Code", "message_type": f"otp_{purpose}",
                "status": "sent", "provider_id": result.get("id"), "diagnostic_id": result.get("diagnostic_id"),
                "stage": result.get("stage", "message_submission"), "channel": "email", "created_at": utcnow(),
            })
        except Exception as exc:
            logger.error(
                "[%s] otp stage=email_submission result=FAIL email_stage=%s exception_class=%s error_code=%s",
                trace_id, getattr(exc, "stage", "email_service"), type(exc.__cause__ or exc).__name__,
                getattr(exc, "error_code", "MESSAGE_SUBMISSION_FAILED"),
            )
            self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"used": True})
            self.store.insert_one("email_logs", {
                "recipient": email, "subject": "Moneda Technologies — Email Verification Code", "message_type": f"otp_{purpose}",
                "status": "failed", "diagnostic_id": getattr(exc, "diagnostic_id", None),
                "stage": getattr(exc, "stage", "email_service"),
                "error_code": getattr(exc, "error_code", "MESSAGE_SUBMISSION_FAILED"),
                "channel": "email", "created_at": utcnow(),
            })
            raise
        return row if return_challenge else True

    def verify(self, email: str, purpose: str, code: str, *, pending_signup_id: str | None = None) -> dict[str, Any] | None:
        row = self.verify_challenge(email, purpose, code, pending_signup_id=pending_signup_id)
        return self.store.find_one("users", {"email": email.lower().strip()}) if row else None

    def verify_challenge(self, email: str, purpose: str, code: str, *, pending_signup_id: str | None = None,
                         extra_query: dict[str, Any] | None = None) -> dict[str, Any]:
        query: dict[str, Any] = {"email": email.lower().strip(), "purpose": purpose, "used": False}
        if pending_signup_id:
            query["pending_signup_id"] = pending_signup_id
        query.update(extra_query or {})
        row = self.store.find_one("otp_challenges", query)
        expires_at = self._normalized_datetime(row, "expires_at", "otp-verify") if row else None
        if not row or not expires_at or expires_at < utcnow():
            raise OtpError("The verification code is invalid or expired")
        attempts = int(row.get("attempts", 0)) + 1
        if attempts > int(row.get("max_attempts", 5)):
            self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"used": True})
            raise OtpError("Maximum verification attempts exceeded")
        if not check_password_hash(row["code_hash"], code):
            self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"attempts": attempts})
            raise OtpError("The verification code is invalid or expired")
        self.store.update_one("otp_challenges", {"_id": row["_id"]}, {"attempts": attempts, "used": True})
        return row

    @staticmethod
    def _normalized_datetime(row: dict[str, Any] | None, field: str, trace_id: str) -> datetime | None:
        value = (row or {}).get(field)
        normalized = ensure_utc(value) if isinstance(value, datetime) else None
        if isinstance(value, datetime) and value.tzinfo is None:
            logger.info("[%s] otp_datetime_normalization field=%s source=mongo normalized=true", trace_id, field)
        return normalized


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
