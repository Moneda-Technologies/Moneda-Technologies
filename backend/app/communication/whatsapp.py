from __future__ import annotations

from typing import Any


class WhatsAppProvider:
    def send_message(self, *, phone: str, message: str) -> dict[str, Any]:
        raise NotImplementedError

    def send_quotation(self, *, phone: str, quotation_number: str, pdf_url: str) -> dict[str, Any]:
        raise NotImplementedError


class DisabledWhatsAppProvider(WhatsAppProvider):
    def send_message(self, *, phone: str, message: str) -> dict[str, Any]:
        return {"status": "disabled", "phone": phone}

    def send_quotation(self, *, phone: str, quotation_number: str, pdf_url: str) -> dict[str, Any]:
        return {"status": "disabled", "quotation_number": quotation_number}


class MockWhatsAppProvider(WhatsAppProvider):
    """Local adapter that records intent without claiming real delivery."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def send_message(self, *, phone: str, message: str) -> dict[str, Any]:
        row = {"id": f"mock-wa-{len(self.messages) + 1}", "status": "mocked", "phone": phone, "message": message}
        self.messages.append(row)
        return row

    def send_quotation(self, *, phone: str, quotation_number: str, pdf_url: str) -> dict[str, Any]:
        row = {
            "id": f"mock-wa-{len(self.messages) + 1}", "status": "mocked", "phone": phone,
            "quotation_number": quotation_number, "pdf_url": pdf_url,
        }
        self.messages.append(row)
        return row
