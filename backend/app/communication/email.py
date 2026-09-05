from __future__ import annotations

from dataclasses import dataclass
import base64
import smtplib
from email.message import EmailMessage
from typing import Any


class EmailDeliveryError(RuntimeError):
    pass


class EmailProvider:
    def send(self, *, to: list[str], subject: str, html: str, attachments: list[dict[str, Any]] | None = None, from_address: str | None = None, cc: list[str] | None = None) -> dict[str, Any]:
        raise NotImplementedError


class EmailService(EmailProvider):
    """Single application boundary for OTP, quotation, and order email delivery."""

    def __init__(self, provider: EmailProvider) -> None:
        self.provider = provider

    def send(self, **kwargs: Any) -> dict[str, Any]:
        return self.provider.send(**kwargs)

    def send_otp(self, *, to: list[str], html: str) -> dict[str, Any]:
        return self.send(to=to, subject="Your Moneda verification code", html=html)

    def send_quotation(self, *, to: list[str], subject: str, html: str, attachments: list[dict[str, Any]] | None = None, from_address: str | None = None, cc: list[str] | None = None) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html, attachments=attachments, from_address=from_address, cc=cc)

    def send_order_team_notification(self, *, to: list[str], subject: str, html: str) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html)

    def send_order_confirmation(self, *, to: list[str], subject: str, html: str) -> dict[str, Any]:
        return self.send(to=to, subject=subject, html=html)


@dataclass
class SmtpEmailProvider(EmailProvider):
    host: str
    port: int
    use_tls: bool
    username: str
    password: str
    from_address: str
    from_name: str = "Moneda Technologies"

    def send(self, *, to: list[str], subject: str, html: str, attachments: list[dict[str, Any]] | None = None, from_address: str | None = None, cc: list[str] | None = None) -> dict[str, Any]:
        if not self.host or not self.username or not self.password or not self.from_address:
            raise EmailDeliveryError("Email configuration missing")
        sender = from_address or self.from_address
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.from_name} <{sender}>" if self.from_name else sender
        message["To"] = ", ".join(to)
        if cc:
            message["Cc"] = ", ".join(cc)
        message.set_content("This message requires an HTML-capable email client.")
        message.add_alternative(html, subtype="html")
        for attachment in attachments or []:
            content = attachment.get("content", b"")
            if isinstance(content, str):
                content = base64.b64decode(content)
            message.add_attachment(content, maintype="application", subtype="pdf", filename=str(attachment.get("filename", "attachment.pdf")))
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as smtp:
                if self.use_tls:
                    smtp.starttls()
                smtp.login(self.username, self.password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError(f"Email delivery failed: {exc}") from exc
        return {"id": f"smtp-{message.get('Message-ID', '')}", "from": sender, "to": to, "subject": subject}


class RecordingEmailProvider(EmailProvider):
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send(self, *, to: list[str], subject: str, html: str, attachments: list[dict[str, Any]] | None = None, from_address: str | None = None, cc: list[str] | None = None) -> dict[str, Any]:
        message = {"id": f"recording-{len(self.messages) + 1}", "from": from_address, "to": to, "cc": cc or [], "subject": subject, "html": html, "attachments": attachments or []}
        self.messages.append(message)
        return message
