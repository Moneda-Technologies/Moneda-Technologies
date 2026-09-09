from __future__ import annotations

from datetime import datetime, timezone
import logging
import secrets
from typing import Any


logger = logging.getLogger(__name__)

EMAIL_PURPOSE_CONFIG = {
    "otp": "MAIL_OTP_FROM",
    "quotation": "MAIL_QUOTATION_FROM",
    "order": "MAIL_ORDER_FROM",
    "general": "MAIL_GENERAL_FROM",
}
EMAIL_PURPOSE_DEFAULTS = {
    "otp": {"from_name": "Moneda OTP", "from_address": "otp@monedatechnologies.com"},
    "quotation": {"from_name": "Moneda Quotations", "from_address": "quotations@monedatechnologies.com"},
    "order": {"from_name": "Moneda Orders", "from_address": "orders@monedatechnologies.com"},
    "general": {"from_name": "Moneda Technologies", "from_address": "business@monedatechnologies.com"},
}


class EmailSenderRegistry:
    """Authoritative purpose-to-From-address configuration."""

    def __init__(self, config: dict[str, Any]) -> None:
        general_default = str(
            config.get("MAIL_GENERAL_FROM")
            or config.get("ZOHO_FROM_ADDRESS")
            or "business@monedatechnologies.com"
        ).strip().lower()
        self._senders = {}
        for purpose, key in EMAIL_PURPOSE_CONFIG.items():
            default = EMAIL_PURPOSE_DEFAULTS[purpose]
            address = general_default if purpose == "general" else default["from_address"]
            self._senders[purpose] = {
                "from_name": str(config.get(f"{key}_NAME") or default["from_name"]).strip(),
                "from_address": str(config.get(key) or address).strip().lower(),
            }

    def resolve(self, purpose: str) -> str:
        normalized = str(purpose or "general").strip().lower()
        if normalized not in self._senders:
            raise ValueError(f"Unsupported email purpose: {normalized}")
        return self._senders[normalized]["from_address"]

    def resolve_identity(self, purpose: str) -> dict[str, str]:
        normalized = str(purpose or "general").strip().lower()
        if normalized not in self._senders:
            raise ValueError(f"Unsupported email purpose: {normalized}")
        return dict(self._senders[normalized])

    def items(self) -> list[tuple[str, str]]:
        return [(purpose, identity["from_address"]) for purpose, identity in self._senders.items()]

    def identity_items(self) -> list[tuple[str, dict[str, str]]]:
        return [(purpose, dict(identity)) for purpose, identity in self._senders.items()]

    def purpose_for(self, address: str) -> str | None:
        normalized = str(address or "").strip().lower()
        return next((purpose for purpose, identity in self._senders.items() if identity["from_address"] == normalized), None)

    @staticmethod
    def unavailable_code(purpose: str) -> str:
        return {
            "otp": "OTP_SENDER_ALIAS_UNAVAILABLE",
            "quotation": "QUOTATION_SENDER_ALIAS_UNAVAILABLE",
            "order": "ORDER_SENDER_ALIAS_UNAVAILABLE",
        }.get(purpose, "GENERAL_SENDER_UNAVAILABLE")


class CustomerRecipientRegistry:
    """Central customer-facing CC/BCC policy; disabled recipients stay inactive."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.cc = [
            {"address": str(config.get("MAIL_CUSTOMER_CC_BUSINESS") or "business@monedatechnologies.com").strip().lower(), "enabled": True},
            {"address": str(config.get("MAIL_CUSTOMER_CC_VBHUTA") or "vbhuta@monedatechnologies.com").strip().lower(), "enabled": str(config.get("MAIL_CUSTOMER_CC_VBHUTA_ENABLED", False)).lower() in {"1", "true", "yes", "on"}},
            {"address": str(config.get("MAIL_CUSTOMER_CC_ADMIN") or "admin@monedatechnologies.com").strip().lower(), "enabled": str(config.get("MAIL_CUSTOMER_CC_ADMIN_ENABLED", False)).lower() in {"1", "true", "yes", "on"}},
        ]
        self.bcc = [{
            "address": str(config.get("MAIL_CUSTOMER_BCC_OPERATIONS") or "operations@chemo.in").strip().lower(),
            "enabled": str(config.get("MAIL_CUSTOMER_BCC_OPERATIONS_ENABLED", True)).lower() in {"1", "true", "yes", "on"},
        }]

    def resolved(self) -> dict[str, list[str]]:
        return {
            "cc": [item["address"] for item in self.cc if item["enabled"]],
            "bcc": [item["address"] for item in self.bcc if item["enabled"]],
        }

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            normalized = str(value or "").strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return result

    def resolved_for_quotation(self, *, to: list[str], sender_user_email: str) -> dict[str, list[str]]:
        """Apply the central quotation routing policy without trusting frontend recipients."""
        base = self.resolved()
        to_values = self._dedupe(to)
        to_set = set(to_values)
        cc = [value for value in self._dedupe([*base["cc"], sender_user_email]) if value not in to_set]
        cc_set = set(cc)
        bcc = [value for value in self._dedupe(base["bcc"]) if value not in to_set and value not in cc_set]
        return {"cc": cc, "bcc": bcc}

    def display(self) -> dict[str, list[dict[str, Any]]]:
        return {"cc": [dict(item) for item in self.cc], "bcc": [dict(item) for item in self.bcc]}

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
             bcc: list[str] | None = None, from_name: str | None = None,
             request_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError


class EmailService(EmailProvider):
    """The only application boundary for transactional email delivery."""

    def __init__(self, provider: EmailProvider, config: dict[str, Any]) -> None:
        self.provider = provider
        self.senders = EmailSenderRegistry(config)
        self.recipients = CustomerRecipientRegistry(config)

    def send(self, *, purpose: str = "general", **kwargs: Any) -> dict[str, Any]:
        identity = self.senders.resolve_identity(purpose)
        sender = identity["from_address"]
        kwargs["from_name"] = identity["from_name"]
        customer_facing = bool(kwargs.pop("customer_facing", False))
        diagnostic_id = kwargs.get("request_id") or "email-untracked"
        recipient_domains = sorted({
            address.rsplit("@", 1)[-1].lower()
            for address in (kwargs.get("to") or []) if "@" in address
        })
        logger.info(
            "[%s] email_sender_resolution purpose=%s from_domain=%s recipient_domains=%s result=PASS",
            diagnostic_id, purpose, sender.rsplit("@", 1)[-1], ",".join(recipient_domains) or "none",
        )
        logger.info(
            "[%s] email_purpose=%s from_address=%s recipient_count=%s cc_count=%s bcc_count=%s to_customer=%s",
            diagnostic_id, purpose, sender, len(kwargs.get("to") or []), len(kwargs.get("cc") or []),
            len(kwargs.get("bcc") or []), customer_facing,
        )
        kwargs["from_address"] = sender
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
            html=html, request_id=request_id, purpose="otp",
        )

    def send_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="verification", request_id=request_id)

    def send_signup_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="signup", request_id=request_id)

    def send_login_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="login", request_id=request_id)

    def send_reset_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self._send_otp(to=to, html=html, purpose="reset", request_id=request_id)

    def send_email_change_otp(self, *, to: list[str], html: str, request_id: str | None = None) -> dict[str, Any]:
        return self.send(
            to=to, subject="Moneda Technologies - Email change verification code",
            html=html, request_id=request_id, purpose="otp",
        )

    def send_quotation(self, *, to: list[str], subject: str, html: str,
                       attachments: list[dict[str, Any]] | None = None,
                       cc: list[str] | None = None, bcc: list[str] | None = None,
                       sender_user_email: str = "", request_id: str | None = None) -> dict[str, Any]:
        routing = self.recipients.resolved_for_quotation(to=to, sender_user_email=sender_user_email)
        return self.send(
            to=to, subject=subject, html=html, attachments=attachments,
            cc=routing["cc"], bcc=routing["bcc"],
            request_id=request_id, purpose="quotation", customer_facing=True,
        )

    def send_order_confirmation(self, *, to: list[str], subject: str, html: str,
                                request_id: str | None = None) -> dict[str, Any]:
        routing = self.recipients.resolved()
        return self.send(to=to, subject=subject, html=html, cc=routing["cc"], bcc=routing["bcc"], request_id=request_id, purpose="order", customer_facing=True)

    def send_order_status(self, *, to: list[str], subject: str, html: str,
                          request_id: str | None = None) -> dict[str, Any]:
        routing = self.recipients.resolved()
        return self.send(to=to, subject=subject, html=html, cc=routing["cc"], bcc=routing["bcc"], request_id=request_id, purpose="order", customer_facing=True)

    def send_test_email(self, *, to: list[str], request_id: str | None = None) -> dict[str, Any]:
        return self.send(
            to=to, subject="Moneda Technologies Zoho Mail API test",
            html=("<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
                  "<p>This message confirms that the Zoho Mail API integration is connected.</p></div>"),
            request_id=request_id, purpose="general",
        )

    # Compatibility names retained for existing order callers.
    def send_order_team_notification(self, *, to: list[str], subject: str, html: str,
                                     request_id: str | None = None) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html, request_id=request_id, purpose="order")

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
             bcc: list[str] | None = None, from_name: str | None = None,
             request_id: str | None = None) -> dict[str, Any]:
        message = {
            "id": f"recording-{len(self.messages) + 1}", "from": from_address,
            "from_name": from_name,
            "to": to, "cc": cc or [], "bcc": bcc or [], "subject": subject,
            "html": html, "attachments": attachments or [], "provider": "recording",
            "stage": "message_submission",
        }
        self.messages.append(message)
        return message
