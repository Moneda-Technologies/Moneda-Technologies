"""Canonical product-catalogue access rules shared by every product endpoint."""

from __future__ import annotations

from typing import Any


# These IDs/names are retained only so historical quotations can still render;
# they must never participate in a new product selection or price calculation.
LEGACY_PRODUCT_IDS = frozenset({"image_print_master_bl_gr"})
LEGACY_PRODUCT_NAMES = frozenset({
    "image print master bl / gr", "image print master bl/gr", "print master bl / gr",
    "mark3zet", "polipack aa", "polipack wa",
})


def is_legacy_product(row: dict[str, Any]) -> bool:
    product_id = str(row.get("_id") or row.get("id") or "").strip().lower()
    name = " ".join(str(row.get("name") or "").split()).strip().lower()
    if product_id in LEGACY_PRODUCT_IDS or name in LEGACY_PRODUCT_NAMES:
        return True
    return product_id in {"mark3zet", "polipack-aa", "polipack-wa"}


def get_active_catalog_products(store: Any, family_id: str | None = None) -> list[dict[str, Any]]:
    """Return only active, canonical products in stable name order."""
    query: dict[str, Any] = {"active": True}
    if family_id:
        query["category_id"] = family_id
    rows, _ = store.list("products", query, limit=100_000, sort="name", direction=1)
    rows = [row for row in rows if not is_legacy_product(row)]
    # Underpacking has one source-owned product.  Any older/custom duplicate
    # remains available only to historical records, never to selectors/pricing.
    if family_id == "mpacks":
        rows = [row for row in rows if row.get("_id") == "mtech-mpack"]
    return rows


def get_active_catalog_product(store: Any, product_id: Any) -> dict[str, Any] | None:
    """Resolve a canonical active ID; arbitrary/legacy IDs never resolve."""
    requested = str(product_id or "").strip()
    if not requested:
        return None
    row = store.find_one("products", {"_id": requested, "active": True})
    if not row or is_legacy_product(row):
        return None
    if row.get("category_id") == "mpacks" and row.get("_id") != "mtech-mpack":
        return None
    return row
