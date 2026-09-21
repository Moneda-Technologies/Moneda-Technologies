"""Shared quotation/order relationship integrity checks.

The quotation collection stores commercial snapshots.  A quotation is only
"Converted to Order" while a non-deleted working order points back to it.  The
helpers in this module are deliberately small and repository-oriented so they
can be used by startup reconciliation, API reads, and the existing dry-run
maintenance command without importing Flask route modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.repositories.store import utcnow


CONVERTED_STATUSES = {"converted", "converted to order", "converted_to_order"}
PRE_CONVERSION_STATUSES = {"draft", "sent", "viewed", "accepted", "accepted quotation"}
EDITABLE_STATUSES = {"draft", "sent", "viewed", "accepted", "accepted quotation"}
CONVERSION_REFERENCE_FIELDS = ("converted_order_id", "converted_oc_id", "order_id", "oc_id")


def quotation_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "").strip().casefold()


def is_final_order(row: dict[str, Any] | None) -> bool:
    """Classify final/legacy OC rows without depending on order routes."""
    if not row:
        return False
    # Explicit working metadata wins over old status-only fields.
    if str(row.get("record_type") or "").upper() == "ORDER" or str(row.get("order_kind") or "").upper() == "WORKING":
        return False
    lifecycle = str(row.get("lifecycle_state") or "").upper()
    return (
        str(row.get("record_type") or "").upper() == "ORDER_CONFIRMATION"
        or str(row.get("document_type") or "").lower() == "order_confirmation"
        or bool(row.get("finalized") or row.get("locked") or row.get("final_oc_id"))
        or lifecycle in {"FINAL", "FINALIZED"}
        or str(row.get("order_number") or row.get("oc_number") or "").startswith("MT-OC-")
    )


def is_live_working_order(row: dict[str, Any] | None) -> bool:
    if not row or str(row.get("status") or "").strip().casefold() in {"deleted", "cancelled", "canceled"}:
        return False
    return not is_final_order(row)


def has_historical_reference(quotation: dict[str, Any]) -> bool:
    """Return true for quotations already tied to a locked/historical OC."""
    status = quotation_status(quotation)
    return (
        bool(quotation.get("converted_oc_id") or quotation.get("converted_oc_number") or quotation.get("final_oc_id"))
        or "legacy order confirmation" in status
        or status in {"legacy oc", "historical oc"}
    )


def live_working_order(store: Any, quotation: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a live working order through direct or reverse references."""
    for field in ("converted_order_id", "order_id"):
        reference = quotation.get(field)
        if reference:
            parent = store.find_one("orders", {"_id": str(reference)})
            if is_live_working_order(parent) and str(parent.get("quotation_id") or parent.get("source_quotation_id") or parent.get("quote_id") or "") == str(quotation.get("_id") or ""):
                return parent
    quotation_id = str(quotation.get("_id") or "")
    if not quotation_id:
        return None
    for field in ("quotation_id", "source_quotation_id", "quote_id"):
        parent = store.find_one("orders", {field: quotation_id})
        if is_live_working_order(parent):
            return parent
    return None


def parent_reference_state(store: Any, quotation: dict[str, Any]) -> str:
    for field in CONVERSION_REFERENCE_FIELDS:
        reference = quotation.get(field)
        if not reference:
            continue
        parent = store.find_one("orders", {"_id": str(reference)})
        if is_live_working_order(parent):
            return "live_direct"
        if is_final_order(parent):
            return "historical_or_locked_parent"
        return "missing_or_deleted_direct"
    if live_working_order(store, quotation):
        return "live_reverse"
    return "missing_or_deleted_reverse"


def _previous_status(quotation: dict[str, Any]) -> str:
    source = str(quotation.get("source_status_before_conversion") or "").strip()
    if source and source.casefold() in PRE_CONVERSION_STATUSES:
        return source
    history = quotation.get("history") or []
    candidates: list[str] = []
    for event in history:
        if not isinstance(event, dict):
            continue
        value = str(event.get("status") or "").strip()
        if value.casefold() in CONVERTED_STATUSES:
            break
        if value and value.casefold() in PRE_CONVERSION_STATUSES:
            candidates.append(value)
    return candidates[-1] if candidates else "Sent"


def find_stale_conversions(store: Any) -> list[dict[str, Any]]:
    """Find broken working-order references without touching valid history."""
    quotations, _ = store.list("quotations", limit=100_000)
    stale: list[dict[str, Any]] = []
    for quotation in quotations:
        if has_historical_reference(quotation):
            continue
        if quotation_status(quotation) not in CONVERTED_STATUSES and not quotation.get("converted_order_id"):
            continue
        if live_working_order(store, quotation) is not None:
            continue
        stale.append({"quotation": quotation, "relationship": parent_reference_state(store, quotation)})
    return stale


def find_orphan_order_references(store: Any) -> list[dict[str, Any]]:
    """Find active order rows whose source quotation no longer exists."""
    orders, _ = store.list("orders", limit=100_000)
    orphaned: list[dict[str, Any]] = []
    for order in orders:
        if str(order.get("status") or "").strip().casefold() == "deleted":
            continue
        quotation_id = order.get("quotation_id") or order.get("source_quotation_id") or order.get("quote_id")
        if quotation_id and not store.find_one("quotations", {"_id": str(quotation_id)}):
            orphaned.append(order)
    return orphaned


def repair_stale_conversions(store: Any, *, actor: str = "quotation-integrity-startup", now: datetime | None = None) -> dict[str, int]:
    """Repair only missing/deleted working-order parents, once and audibly."""
    current_time = now or utcnow()
    stale = find_stale_conversions(store)
    repaired = 0
    historical_skipped = 0
    archived_links_cleared = 0
    for item in stale:
        quotation = item["quotation"]
        quotation_id = quotation.get("_id")
        if not quotation_id:
            continue
        # A second check prevents a concurrent conversion from being reopened
        # by a read/startup repair that began before the conversion completed.
        if has_historical_reference(quotation) or live_working_order(store, quotation) is not None:
            historical_skipped += 1
            continue
        previous = _previous_status(quotation)
        history = [*(quotation.get("history") or []), {
            "status": previous,
            "at": current_time,
            "by": actor,
            "reason": "Converted working-order parent is missing or deleted",
        }]
        changes: dict[str, Any] = {
            "status": previous,
            "history": history,
            "converted_order_id": None,
            "converted_order_number": None,
            "converted_at": None,
            "integrity_reconciled_at": current_time,
            "integrity_reconciled_reason": "missing_or_deleted_working_order",
        }
        for field in ("order_id", "oc_id"):
            reference = quotation.get(field)
            if reference and not is_live_working_order(store.find_one("orders", {"_id": str(reference)})):
                changes[field] = None
        updated = store.update_one("quotations", {"_id": quotation_id}, changes)
        if not updated:
            continue
        store.insert_one("audit_logs", {
            "action": "quotation.reconciled_after_missing_order",
            "entity": "quotation",
            "entity_type": "quotation",
            "entity_id": quotation_id,
            "quotation_id": quotation_id,
            "previous_status": quotation.get("status"),
            "new_status": previous,
            "relationship": item["relationship"],
            "created_at": current_time,
            "timestamp": current_time,
            "actor": actor,
        })
        repaired += 1
    # Older soft-deleted orders retained their active quotation field. Clear
    # that field now that the audit event is the historical source of truth;
    # otherwise Mongo's unique quotation index can block a legitimate retry.
    orders, _ = store.list("orders", limit=100_000)
    for order in orders:
        if str(order.get("status") or "").strip().casefold() != "deleted":
            continue
        fields = [field for field in ("quotation_id", "source_quotation_id", "quote_id") if order.get(field)]
        if not fields:
            continue
        if store.update_one("orders", {"_id": order.get("_id")}, {}, unset_fields=fields):
            archived_links_cleared += 1
    return {
        "scanned": len(stale), "repaired": repaired,
        "historical_skipped": historical_skipped,
        "archived_links_cleared": archived_links_cleared,
    }
