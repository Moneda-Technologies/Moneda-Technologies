"""Audit and safely reconcile quotations whose conversion parent is gone.

The checker is intentionally dry-run by default.  A repair only runs when the
operator supplies both ``--apply`` and ``--confirm-repair``.  Repairs are
soft, auditable state changes: no quotation or order is physically deleted.

Examples (from ``backend``)::

    python check_quotation_integrity.py --dry-run
    python check_quotation_integrity.py --apply --confirm-repair
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from flask import Flask

from app.config import Config
from app.repositories.store import build_store


CONVERTED_STATUSES = {"converted", "converted to order", "converted_to_order"}
PRE_CONVERSION_STATUSES = {"draft", "sent", "viewed", "accepted", "accepted quotation"}
CONVERSION_REFERENCE_FIELDS = ("converted_order_id", "converted_oc_id", "order_id", "oc_id")


def _status(row: dict[str, Any]) -> str:
    return str(row.get("status") or "").strip().casefold()


def _is_live_order(row: dict[str, Any] | None) -> bool:
    return bool(row) and str(row.get("status") or "").strip().casefold() not in {"deleted", "cancelled", "canceled"}


def _live_parent(store: Any, quotation: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve a live OC through direct or reverse quotation references."""
    for field in CONVERSION_REFERENCE_FIELDS:
        reference = quotation.get(field)
        if reference:
            parent = store.find_one("orders", {"_id": str(reference)})
            if _is_live_order(parent):
                return parent
    quotation_id = str(quotation.get("_id") or "")
    if not quotation_id:
        return None
    for field in ("quotation_id", "source_quotation_id", "quote_id"):
        parent = store.find_one("orders", {field: quotation_id})
        if _is_live_order(parent):
            return parent
    return None


def _parent_reference_state(store: Any, quotation: dict[str, Any]) -> str:
    """Return a diagnostic relationship state without exposing full payloads."""
    for field in CONVERSION_REFERENCE_FIELDS:
        reference = quotation.get(field)
        if not reference:
            continue
        parent = store.find_one("orders", {"_id": str(reference)})
        if _is_live_order(parent):
            return "live_direct"
        return "missing_or_deleted_direct"
    if _live_parent(store, quotation):
        return "live_reverse"
    return "missing_or_deleted_reverse"


def _reference_ids(store: Any, quotation: dict[str, Any]) -> tuple[str, str]:
    """Return safe order/OC identifiers for the dry-run report."""
    order_id = str(quotation.get("converted_order_id") or quotation.get("order_id") or "—")
    oc_id = str(quotation.get("converted_oc_id") or quotation.get("oc_id") or "—")
    if order_id != "—" or oc_id != "—":
        return order_id, oc_id
    quotation_id = str(quotation.get("_id") or "")
    for field in ("quotation_id", "source_quotation_id", "quote_id"):
        parent = store.find_one("orders", {field: quotation_id}) if quotation_id else None
        if parent and parent.get("_id"):
            # This application stores Order Confirmations in ``orders``; when
            # the quotation has only a reverse link, expose the same safe ID
            # in both report columns rather than hiding the relationship.
            parent_id = str(parent["_id"])
            return parent_id, parent_id
    return order_id, oc_id


def find_stale_conversions(store: Any) -> list[dict[str, Any]]:
    quotations, _ = store.list("quotations", limit=100_000)
    stale: list[dict[str, Any]] = []
    for quotation in quotations:
        if _status(quotation) not in CONVERTED_STATUSES:
            continue
        if _live_parent(store, quotation) is not None:
            continue
        stale.append({
            "quotation": quotation,
            "relationship": _parent_reference_state(store, quotation),
        })
    return stale


def find_orphan_order_references(store: Any) -> list[dict[str, Any]]:
    """Find OCs that still point at a quotation that no longer exists."""
    orders, _ = store.list("orders", limit=100_000)
    orphaned: list[dict[str, Any]] = []
    for order in orders:
        quotation_id = order.get("quotation_id") or order.get("source_quotation_id") or order.get("quote_id")
        if quotation_id and not store.find_one("quotations", {"_id": str(quotation_id)}):
            orphaned.append(order)
    return orphaned


def _previous_status(quotation: dict[str, Any]) -> str:
    """Recover the last known pre-conversion status, defaulting to Sent."""
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


def _repair(store: Any, item: dict[str, Any], now: datetime) -> bool:
    quotation = item["quotation"]
    quotation_id = quotation.get("_id")
    if not quotation_id:
        return False
    previous = _previous_status(quotation)
    actor = "quotation-integrity-check"
    history = [*(quotation.get("history") or []), {
        "status": previous,
        "at": now,
        "by": actor,
        "reason": "Converted parent Order Confirmation is missing or deleted",
    }]
    changes: dict[str, Any] = {
        "status": previous,
        "history": history,
        "converted_order_id": None,
        "converted_oc_id": None,
        "converted_at": None,
        "integrity_reconciled_at": now,
        "integrity_reconciled_reason": "missing_or_deleted_conversion_parent",
    }
    # Clear only relationship fields that point at a missing/deleted parent;
    # unrelated quotation data and historical snapshots remain untouched.
    for field in ("order_id", "oc_id"):
        reference = quotation.get(field)
        if reference and not _is_live_order(store.find_one("orders", {"_id": str(reference)})):
            changes[field] = None
    updated = store.update_one("quotations", {"_id": quotation_id}, changes)
    if updated:
        store.insert_one("audit_logs", {
            "action": "quotation.reconciled_after_missing_order",
            "entity_type": "quotation",
            "entity_id": quotation_id,
            "quotation_id": quotation_id,
            "previous_status": quotation.get("status"),
            "new_status": previous,
            "relationship": item["relationship"],
            "created_at": now,
            "actor": actor,
        })
    return bool(updated)


def _read_only_app() -> Flask:
    """Build only the repository for this maintenance check.

    ``create_app`` intentionally runs normal startup migrations and catalog
    synchronization.  A dry-run integrity audit must not execute those
    write paths, so this command constructs the configured store directly.
    """
    app = Flask(__name__)
    app.config.from_object(Config)
    app.extensions["store"] = build_store(app.config)
    return app


def check(*, apply: bool, confirm: bool) -> tuple[int, int]:
    app = _read_only_app()
    with app.app_context():
        store = app.extensions["store"]
        stale = find_stale_conversions(store)
        orphaned_orders = find_orphan_order_references(store)
        print(f"STALE CONVERSIONS FOUND: {len(stale)}")
        for item in stale:
            quotation = item["quotation"]
            order_id, oc_id = _reference_ids(store, quotation)
            print(
                f"quotation_id={quotation.get('_id')} quotation_number={quotation.get('quotation_number') or '—'} "
                f"status={quotation.get('status') or '—'} order_id={order_id} oc_id={oc_id} "
                f"linked_record_exists=false relationship={item['relationship']} "
                f"previous_status={_previous_status(quotation)}"
            )
        print(f"ORPHAN ORDER REFERENCES FOUND: {len(orphaned_orders)}")
        for order in orphaned_orders:
            print(
                f"order_id={order.get('_id')} order_number={order.get('order_number') or '—'} "
                f"quotation_id={order.get('quotation_id') or order.get('source_quotation_id') or order.get('quote_id')} "
                f"status={order.get('status') or '—'}"
            )
        if not apply:
            print("DRY RUN ONLY: no records changed")
            return len(stale), 0
        if not confirm:
            raise SystemExit("Refusing repair without --confirm-repair")
        now = datetime.now(timezone.utc)
        repaired = sum(_repair(store, item, now) for item in stale)
        print(f"STALE CONVERSIONS REPAIRED: {repaired}")
        return len(stale), repaired


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report stale conversions without changing them (default)")
    mode.add_argument("--apply", action="store_true", help="restore stale quotations to their pre-conversion state")
    parser.add_argument("--confirm-repair", action="store_true", help="required with --apply")
    args = parser.parse_args()
    check(apply=args.apply, confirm=args.confirm_repair)
