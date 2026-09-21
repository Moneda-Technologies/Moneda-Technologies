"""Audit and safely reconcile quotations whose working-order parent is gone.

The checker is dry-run by default. A repair requires both ``--apply`` and
``--confirm-repair``. The same repository helper is also used during API reads
and backend startup, so a stale record cannot remain visible indefinitely.

Examples (from ``backend``)::

    python check_quotation_integrity.py --dry-run
    python check_quotation_integrity.py --apply --confirm-repair
"""

from __future__ import annotations

import argparse

from flask import Flask

from app.config import Config
from app.quotations.integrity import (
    CONVERSION_REFERENCE_FIELDS,
    CONVERTED_STATUSES,
    EDITABLE_STATUSES,
    PRE_CONVERSION_STATUSES,
    find_orphan_order_references,
    find_stale_conversions,
    is_live_working_order,
    parent_reference_state,
    quotation_status,
    repair_stale_conversions,
)
from app.repositories.store import build_store

__all__ = [
    "CONVERSION_REFERENCE_FIELDS", "CONVERTED_STATUSES", "EDITABLE_STATUSES",
    "PRE_CONVERSION_STATUSES", "find_orphan_order_references", "find_stale_conversions",
    "is_live_working_order", "parent_reference_state", "quotation_status",
    "repair_stale_conversions",
]


def _reference_ids(store, quotation: dict) -> tuple[str, str]:
    order_id = str(quotation.get("converted_order_id") or quotation.get("order_id") or "—")
    oc_id = str(quotation.get("converted_oc_id") or quotation.get("oc_id") or "—")
    if order_id != "—" or oc_id != "—":
        return order_id, oc_id
    quotation_id = str(quotation.get("_id") or "")
    for field in ("quotation_id", "source_quotation_id", "quote_id"):
        parent = store.find_one("orders", {field: quotation_id}) if quotation_id else None
        if parent and parent.get("_id"):
            parent_id = str(parent["_id"])
            return parent_id, parent_id
    return order_id, oc_id


def _read_only_app() -> Flask:
    """Build only the repository; do not run normal application write paths."""
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
                f"linked_record_exists=false relationship={item['relationship']}"
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
        result = repair_stale_conversions(store, actor="quotation-integrity-check")
        print(f"STALE CONVERSIONS REPAIRED: {result['repaired']}")
        return len(stale), result["repaired"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report stale conversions without changing them (default)")
    mode.add_argument("--apply", action="store_true", help="restore stale quotations to their pre-conversion state")
    parser.add_argument("--confirm-repair", action="store_true", help="required with --apply")
    args = parser.parse_args()
    check(apply=args.apply, confirm=args.confirm_repair)
