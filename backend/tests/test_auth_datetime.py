from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from werkzeug.security import generate_password_hash

from app.communication.email import EmailService, RecordingEmailProvider
from app.auth.service import OtpError, OtpService
from app.repositories.store import MemoryStore, ensure_utc, utcnow


def otp_service() -> tuple[OtpService, RecordingEmailProvider, MemoryStore]:
    store = MemoryStore()
    provider = RecordingEmailProvider()
    service = OtpService(store, EmailService(provider, {
        "MAIL_OTP_FROM": "otp@monedatechnologies.com",
        "MAIL_OTP_FROM_NAME": "Moneda OTP",
        "MAIL_GENERAL_FROM": "business@monedatechnologies.com",
    }))
    return service, provider, store


def challenge(*, resend_after: datetime | None = None, expires_at: datetime | None = None) -> dict:
    now = utcnow()
    return {
        "email": "otp-test@example.com", "purpose": "signup", "used": False,
        "code_hash": generate_password_hash("123456"), "attempts": 0, "max_attempts": 5,
        "resend_after": resend_after if resend_after is not None else now - timedelta(seconds=1),
        "expires_at": expires_at if expires_at is not None else now + timedelta(minutes=10),
    }


def test_ensure_utc_normalizes_naive_and_aware_values():
    naive = datetime(2026, 1, 1, 12, 0, 0)
    aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    assert ensure_utc(naive) == aware
    assert ensure_utc(naive).tzinfo == timezone.utc
    assert ensure_utc(aware) == aware
    assert ensure_utc(None) is None


@pytest.mark.parametrize("resend_after", [
    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=1),
    datetime.now(timezone.utc) + timedelta(minutes=1),
])
def test_active_naive_or_aware_resend_after_preserves_cooldown(resend_after):
    service, provider, store = otp_service()
    store.insert_one("otp_challenges", challenge(resend_after=resend_after))

    with pytest.raises(OtpError, match="wait"):
        service.request("otp-test@example.com", "signup")
    assert provider.messages == []


def test_expired_resend_after_allows_new_signup_otp():
    service, provider, store = otp_service()
    store.insert_one("otp_challenges", challenge(resend_after=utcnow() - timedelta(seconds=1)))

    assert service.request("otp-test@example.com", "signup") is True
    assert len(provider.messages) == 1
    assert provider.messages[0]["from"] == "otp@monedatechnologies.com"
    assert provider.messages[0]["cc"] == []
    assert provider.messages[0]["bcc"] == []


@pytest.mark.parametrize("expires_at", [
    datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
    datetime.now(timezone.utc) + timedelta(minutes=5),
])
def test_verify_accepts_naive_or_aware_expiry(expires_at):
    service, _provider, store = otp_service()
    store.insert_one("otp_challenges", challenge(expires_at=expires_at))

    assert service.verify("otp-test@example.com", "signup", "123456") is None
    row = store.find_one("otp_challenges", {"email": "otp-test@example.com", "purpose": "signup"})
    assert row["used"] is True


def test_normal_signup_request_persists_aware_timestamps_and_sends_otp():
    service, provider, store = otp_service()

    assert service.request("new-user@example.com", "signup", metadata={"name": "New User"}) is True
    row = store.find_one("otp_challenges", {"email": "new-user@example.com", "purpose": "signup"})
    assert row is not None
    assert ensure_utc(row["expires_at"]).tzinfo == timezone.utc
    assert ensure_utc(row["resend_after"]).tzinfo == timezone.utc
    assert provider.messages[0]["from_name"] == "Moneda OTP"
