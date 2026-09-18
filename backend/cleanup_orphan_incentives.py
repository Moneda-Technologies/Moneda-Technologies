"""Find and safely retire incentives whose originating OC no longer exists.

The default mode is a dry run.  Apply mode requires an explicit confirmation
flag and soft-cancels only unpaid, non-payout-linked snapshots; it never
physically deletes financial history and never changes a valid OC snapshot.

Examples (from ``backend``):

    python cleanup_orphan_incentives.py --dry-run
    python cleanup_orphan_incentives.py --apply --confirm-orphans
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Any

from app import create_app
from app.finance.service import has_payout_link, incentive_transaction_id, money, normalized_payment_status


def _parent(store: Any, incentive: dict[str, Any]) -> dict[str, Any] | None:
    transaction_id = incentive_transaction_id(incentive)
    if not transaction_id:
        return None
    return store.find_one("orders", {"_id": transaction_id})


def find_orphans(store: Any) -> list[dict[str, Any]]:
    rows, _ = store.list("incentives", limit=100_000)
    orphaned = []
    for row in rows:
        parent = _parent(store, row)
        if not parent or str(parent.get("status") or "").casefold() == "deleted":
            orphaned.append(row)
    return orphaned


def _recipient(incentive: dict[str, Any]) -> str:
    values = {
        str(incentive.get(field) or "")
        for field in ("recipient_user_id", "salesperson_id", "manager_user_id", "creator_user_id")
        if incentive.get(field)
    }
    for line in incentive.get("incentive_lines") or []:
        if isinstance(line, dict) and line.get("recipient_user_id"):
            values.add(str(line["recipient_user_id"]))
    return ", ".join(sorted(values)) or "—"


def _financially_locked(store: Any, incentive: dict[str, Any]) -> bool:
    if normalized_payment_status(incentive.get("status")) == "PAID" or money(incentive.get("paid_amount")) > 0:
        return True
    if has_payout_link(incentive):
        return True
    allocations, _ = store.list("incentive_allocations", {"incentive_id": incentive.get("_id")}, limit=100_000)
    return any(
        normalized_payment_status(row.get("status")) == "PAID"
        or money(row.get("paid_amount")) > 0
        or has_payout_link(row)
        for row in allocations
    )


def cleanup(*, apply: bool, confirm: bool) -> tuple[int, int, int]:
    app = create_app({"AUTO_SEED": False})
    with app.app_context():
        store = app.extensions["store"]
        orphans = find_orphans(store)
        print(f"ORPHAN INCENTIVES FOUND: {len(orphans)}")
        for row in orphans:
            print(
                f"incentive_id={row.get('_id')} oc_id={incentive_transaction_id(row) or '—'} "
                f"recipient={_recipient(row)} amount={money(row.get('gross_incentive_amount')):.2f} "
                f"status={row.get('status') or '—'}"
            )
        if not apply:
            print("DRY RUN ONLY: no records changed")
            return len(orphans), 0, 0
        if not confirm:
            raise SystemExit("Refusing cleanup without --confirm-orphans")
        cancelled = 0
        skipped_locked = 0
        now = datetime.now(timezone.utc)
        for row in orphans:
            if _financially_locked(store, row):
                skipped_locked += 1
                print(f"SKIPPED FINANCIALLY LOCKED: {row.get('_id')}")
                continue
            reason = "Originating Order Confirmation no longer exists"
            updated = store.update_one("incentives", {"_id": row.get("_id")}, {
                "status": "CANCELLED",
                "cancelled_at": now,
                "cancelled_by": "orphan-incentive-cleanup",
                "cancelled_reason": reason,
                "financial_locked": True,
                "audit": [*(row.get("audit") or []), {
                    "action": "orphan_cleanup",
                    "from": row.get("status") or "PENDING PAYMENT",
                    "to": "CANCELLED",
                    "reason": reason,
                    "by": "orphan-incentive-cleanup",
                    "at": now,
                }],
            })
            if updated:
                cancelled += 1
                for allocation in store.list("incentive_allocations", {"incentive_id": row.get("_id")}, limit=100_000)[0]:
                    if normalized_payment_status(allocation.get("status")) in {"PAID", "CANCELLED"} or money(allocation.get("paid_amount")) > 0 or has_payout_link(allocation):
                        continue
                    store.update_one("incentive_allocations", {"_id": allocation.get("_id")}, {
                        "status": "CANCELLED",
                        "cancelled_at": now,
                        "cancelled_by": "orphan-incentive-cleanup",
                        "cancelled_reason": reason,
                    })
        print(f"ORPHAN INCENTIVES CANCELLED: {cancelled}")
        print(f"FINANCIALLY LOCKED ORPHANS SKIPPED: {skipped_locked}")
        return len(orphans), cancelled, skipped_locked


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report orphan records without changing them")
    mode.add_argument("--apply", action="store_true", help="soft-cancel eligible orphan records")
    parser.add_argument("--confirm-orphans", action="store_true", help="required with --apply")
    args = parser.parse_args()
    cleanup(apply=args.apply, confirm=args.confirm_orphans)
