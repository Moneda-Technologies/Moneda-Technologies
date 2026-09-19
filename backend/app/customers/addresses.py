from __future__ import annotations

from typing import Any
from uuid import uuid4


ADDRESS_FIELDS = (
    "label", "recipient_name", "company_name", "address_line_1", "address_line_2",
    "city", "state", "postal_code", "country_code", "country_name",
)


def normalize_address(value: Any, *, address_id: str | None = None, active: bool = True,
                      is_default: bool = False) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    row = {field: str(raw.get(field) or "").strip() for field in ADDRESS_FIELDS}
    row["id"] = str(raw.get("id") or address_id or uuid4())
    row["active"] = bool(raw.get("active", active))
    row["is_default"] = bool(raw.get("is_default", is_default))
    return row


def legacy_billing_address(customer: dict[str, Any]) -> dict[str, Any]:
    existing = customer.get("billing_address_record")
    if isinstance(existing, dict):
        return normalize_address(existing, address_id=str(existing.get("id") or "billing"), active=True, is_default=True)
    text = str(customer.get("billing_address") or customer.get("address") or "").strip()
    return normalize_address({
        "id": "billing", "label": "Billing", "company_name": customer.get("name") or customer.get("company_name"),
        "recipient_name": customer.get("contact_name"), "address_line_1": text,
        "city": customer.get("city"), "state": customer.get("state"), "postal_code": customer.get("postal_code"),
        "country_code": customer.get("country_code"), "country_name": customer.get("country_name") or customer.get("country"),
    }, address_id="billing", active=True, is_default=True)


def customer_shipping_addresses(customer: dict[str, Any]) -> list[dict[str, Any]]:
    rows = customer.get("shipping_addresses")
    if isinstance(rows, list) and rows:
        normalized = [normalize_address(row) for row in rows if isinstance(row, dict)]
    else:
        text = str(customer.get("shipping_address") or customer.get("address") or "").strip()
        if not text:
            return []
        normalized = [normalize_address({
            "id": "shipping-legacy", "label": "Primary shipping", "company_name": customer.get("name") or customer.get("company_name"),
            "recipient_name": customer.get("contact_name"), "address_line_1": text,
            "city": customer.get("city"), "state": customer.get("state"), "postal_code": customer.get("postal_code"),
            "country_code": customer.get("country_code"), "country_name": customer.get("country_name") or customer.get("country"),
            "active": True, "is_default": True,
        })]
    active_defaults = [row for row in normalized if row["active"] and row["is_default"]]
    if len(active_defaults) != 1:
        first_active = next((row for row in normalized if row["active"]), None)
        for row in normalized:
            row["is_default"] = row is first_active
    return normalized


def customer_address_view(customer: dict[str, Any]) -> dict[str, Any]:
    return {
        "billing_address_record": legacy_billing_address(customer),
        "shipping_addresses": customer_shipping_addresses(customer),
    }

