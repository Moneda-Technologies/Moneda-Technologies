from __future__ import annotations

from datetime import datetime, timezone
import secrets
from typing import Any


class EmailDeliveryError(RuntimeError):
    def __init__(self, message: str, *, stage: str, diagnostic_id: str,
                 error_code: str = "MESSAGE_SUBMISSION_FAILED") -> None:
        super().__init__(message)
        self.stage = stage
        self.diagnostic_id = diagnostic_id
        self.error_code = error_code


def email_diagnostic_id() -> str:
    return f"email-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(3)}"


class EmailProvider:
    def send(self, *, to: list[str], subject: str, html: str,
             attachments: list[dict[str, Any]] | None = None,
             from_address: str | None = None, cc: list[str] | None = None,
             bcc: list[str] | None = None, request_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class EmailService(EmailProvider):
    """The only application boundary for transactional email delivery."""

    def __init__(self, provider: EmailProvider) -> None:
        self.provider = provider

    def send(self, **kwargs: Any) -> dict[str, Any]:
        return self.provider.send(**kwargs)

    def _send_otp(self, *, to: list[str], html: str, purpose: str,
                  request_id: str | None = None) -> dict[str, Any]:
        subjects = {
            "signup": "Moneda Technologies - Signup verification code",
            "login": "Moneda Technologies - Secure login code",
            "reset": "Moneda Technologies - Password reset code",
        }
        return self.send(
            to=to, subject=subjects.get(purpose, "Moneda Technologies - Verification code"),
            html=html, request_id=request_id,
        )

    def send_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="verification", request_id=request_id)

    def send_signup_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="signup", request_id=request_id)

    def send_login_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="login", request_id=request_id)

    def send_reset_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="reset", request_id=request_id)

    def send_quotation(self, *, to: list[str], subject: str, html: str,
                       attachments: list[dict[str, Any]] | None = None,
                       cc: list[str] | None = None, bcc: list[str] | None = None,
                       request_id: str | None = None) -> dict[str, Any]:
        return self.send(
            to=to, subject=subject, html=html, attachments=attachments,
            cc=cc, bcc=bcc, request_id=request_id,
        )

    def send_order_confirmation(self, *, to: list[str], subject: str, html: str,
                                request_id: str | None = None) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html, request_id=request_id)

    def send_order_status(self, *, to: list[str], subject: str, html: str,
                          request_id: str | None = None) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html, request_id=request_id)

    def send_test_email(self, *, to: list[str], request_id: str | None = None) -> dict[str, Any]:
        return self.send(
            to=to, subject="Moneda Technologies Zoho Mail API test",
            html=("<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
                  "<p>This message confirms that the Zoho Mail API integration is connected.</p></div>"),
            request_id=request_id,
        )

    # Compatibility names retained for existing order callers.
    def send_order_team_notification(self, *, to: list[str], subject: str, html: str,
                                     request_id: str | None = None) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html, request_id=request_id)

    def send_order_team_email(self, **kwargs: Any) -> dict[str, Any]:
        return self.send_order_team_notification(**kwargs)

    def send_packing_update(self, **kwargs: Any) -> dict[str, Any]:
        return self.send_order_status(**kwargs)

    def send_shipment_update(self, **kwargs: Any) -> dict[str, Any]:
        return self.send_order_status(**kwargs)

    def send_delivery_update(self, **kwargs: Any) -> dict[str, Any]:
        return self.send_order_status(**kwargs)


class RecordingEmailProvider(EmailProvider):
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send(self, *, to: list[str], subject: str, html: str,
             attachments: list[dict[str, Any]] | None = None,
             from_address: str | None = None, cc: list[str] | None = None,
             bcc: list[str] | None = None, request_id: str | None = None) -> dict[str, Any]:
        message = {
            "id": f"recording-{len(self.messages) + 1}", "from": from_address,
            "to": to, "cc": cc or [], "bcc": bcc or [], "subject": subject,
            "html": html, "attachments": attachments or [], "provider": "recording",
            "stage": "message_submission",
        }
        self.messages.append(message)
        return message
