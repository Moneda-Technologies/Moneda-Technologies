from __future__ import annotations

from typing import Any

from app.services.business_logic import pricing_client_type


DEFAULT_PRICE_LISTS: tuple[dict[str, Any], ...] = (
    {
        "_id": "blankets", "display_name": "Blankets", "description": "Blanket commercial price list",
        "category": "blankets", "classification": "GLOBAL", "client_type": None,
        "document_url": "https://workdrive.zohoexternal.in/embed/dgl7a5a1296292fb94375948159d0621cc4af?toolbar=false&appearance=light&themecolor=green",
        "active": True, "sort_order": 10, "metadata": {"title": "Blankets Price List"},
    },
    {
        "_id": "underpacking-dealer", "display_name": "Underpacking — Dealer", "description": "Dealer underpacking price list",
        "category": "mpacks", "classification": "DEALER", "client_type": "DEALER",
        "document_url": "https://workdrive.zohoexternal.in/embed/dgl7a89ce96e61ed145e3a7c154116977cde4?toolbar=false&appearance=light&themecolor=green",
        "active": True, "sort_order": 20, "metadata": {"title": "Underpacking Dealer Price List"},
    },
    {
        "_id": "underpacking-distributor", "display_name": "Underpacking — Distributor", "description": "Distributor underpacking price list",
        "category": "mpacks", "classification": "DISTRIBUTOR", "client_type": "WHOLESALER",
        "document_url": "https://workdrive.zohoexternal.in/embed/dgl7a43f6bd264f9e4a5f80d8b9d2242d7d25?toolbar=false&appearance=light&themecolor=green",
        "active": True, "sort_order": 30, "metadata": {"title": "Underpacking Distributor Price List"},
    },
)


def list_price_lists(store: Any, *, active_only: bool = True) -> list[dict[str, Any]]:
    persisted, _ = store.list("price_list_definitions", {}, limit=500)
    merged = {row["_id"]: {**row} for row in DEFAULT_PRICE_LISTS}
    for row in persisted:
        if row.get("_id"):
            merged[str(row["_id"])] = {**merged.get(str(row["_id"]), {}), **row}
    rows = list(merged.values())
    if active_only:
        rows = [row for row in rows if row.get("active", True)]
    return sorted(rows, key=lambda row: (int(row.get("sort_order", 999)), str(row.get("display_name", ""))))


def price_list_by_id(store: Any, price_list_id: str | None) -> dict[str, Any] | None:
    if not price_list_id:
        return None
    return next((row for row in list_price_lists(store, active_only=False) if row.get("_id") == price_list_id), None)


def resolve_effective_price_list(store: Any, customer: dict[str, Any], category_id: str,
                                 shipping_address_id: str | None = None) -> dict[str, Any]:
    shipping = next((row for row in customer.get("shipping_addresses", [])
                     if isinstance(row, dict) and str(row.get("id")) == str(shipping_address_id)), None)
    candidates: list[tuple[str, Any]] = []
    if shipping:
        candidates.extend([
            ("shipping_category_override", (shipping.get("category_price_list_ids") or {}).get(category_id)),
            ("shipping_default_override", shipping.get("default_price_list_id")),
        ])
    candidates.extend([
        ("customer_category_assignment", (customer.get("category_price_list_ids") or {}).get(category_id)),
        ("customer_default_assignment", customer.get("default_price_list_id")),
    ])
    for source, list_id in candidates:
        definition = price_list_by_id(store, str(list_id) if list_id else None)
        if definition and definition.get("active", True):
            return _resolved(definition, source, customer)
    account_type = str(customer.get("account_type") or "").upper()
    client_type = pricing_client_type(customer)
    fallback_id = "underpacking-dealer" if client_type == "DEALER" else "underpacking-distributor"
    if category_id == "blankets":
        fallback_id = "blankets"
    definition = price_list_by_id(store, fallback_id)
    if definition:
        return _resolved(definition, "client_type_fallback", customer)
    return {"price_list_id": None, "display_name": "EUR master pricing", "source": "global_fallback",
            "classification": account_type or client_type, "client_type": client_type, "master_currency": "EUR"}


def _resolved(definition: dict[str, Any], source: str, customer: dict[str, Any]) -> dict[str, Any]:
    return {
        "price_list_id": definition.get("_id"), "display_name": definition.get("display_name"),
        "source": source, "classification": definition.get("classification"),
        "client_type": definition.get("client_type") or pricing_client_type(customer),
        "master_currency": "EUR", "category": definition.get("category"),
    }
