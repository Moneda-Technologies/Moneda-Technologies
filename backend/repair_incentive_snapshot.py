"""Repair the known manager-created OC incentive snapshot.

This is an explicit, idempotent maintenance command for the OC created before
the persisted incentive-rule resolver was made authoritative.  It never
deletes financial records.  The default is a dry run; applying the repair
requires both ``--apply`` and ``--confirm-repair``.

Run from ``backend``::

    python repair_incentive_snapshot.py --dry-run
    python repair_incentive_snapshot.py --apply --confirm-repair

The command writes a copy of the affected incentive/allocation records to the
``repair_backups`` collection before changing them and records a repair entry
on the incentive document itself.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from typing import Any

from app.config import Config
from app.finance.service import build_internal_incentive_lines, money
from app.repositories.store import build_store


DEFAULT_ORDER_ID = "58363e86-5afb-4695-bb18-ae7b07340fc3"
DEFAULT_QUOTATION_ID = "d23c6eb4-c924-46a4-bf57-4cfaeda9356d"
REPAIR_KEY = "manager_oc_incentive_rule_precedence_v1"


def _configured_store() -> Any:
    # Construct only the repository.  This deliberately does not start Flask,
    # seed catalogues, send mail, or run any application request handlers.
    return build_store({
        "DEMO_MODE": False,
        "TESTING": False,
        "MONGODB_URI": Config.MONGODB_URI,
        "MONGODB_DATABASE": Config.MONGODB_DATABASE,
    })


def _internal_lines(incentive: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        line for line in incentive.get("incentive_lines") or []
        if isinstance(line, dict)
        and str(line.get("recipient_type") or "").upper() != "CUSTOMER"
        and str(line.get("category_id") or "") != "customer_incentive"
    ]


def _customer_lines(incentive: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        line for line in incentive.get("incentive_lines") or []
        if isinstance(line, dict)
        and (
            str(line.get("recipient_type") or "").upper() == "CUSTOMER"
            or str(line.get("category_id") or "") == "customer_incentive"
        )
    ]


def _allocation_key(incentive_id: str, line: dict[str, Any]) -> str:
    return ":".join(str(value or "-") for value in (
        incentive_id,
        line.get("oc_line_id"),
        line.get("recipient_type"),
        line.get("recipient_user_id") or line.get("customer_id"),
    ))


def _line_match_key(line: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(line.get("oc_line_id") or ""),
        str(line.get("product_id") or ""),
        str(line.get("category_id") or ""),
    )


def _target(store: Any, order_id: str, quotation_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        raise SystemExit(f"Order Confirmation not found: {order_id}")
    if quotation_id and str(order.get("quotation_id") or "") != quotation_id:
        raise SystemExit("Refusing repair: the Order Confirmation does not match the expected quotation")
    incentive = store.find_one("incentives", {"order_id": order_id})
    if not incentive:
        raise SystemExit(f"Incentive snapshot not found for OC: {order_id}")
    allocations, _ = store.list("incentive_allocations", {"incentive_id": incentive.get("_id")}, limit=100_000)
    return order, incentive, allocations


def _build_plan(store: Any, order: dict[str, Any], incentive: dict[str, Any], allocations: list[dict[str, Any]]) -> dict[str, Any]:
    target_internal, creator, manager = build_internal_incentive_lines(store, order, None)
    if not target_internal:
        raise SystemExit("Refusing repair: no internal incentive lines were resolved")

    existing_internal = [
        row for row in allocations
        if str(row.get("recipient_type") or "").upper() != "CUSTOMER"
        and not str(row.get("recipient_role") or "").casefold() == "customer"
    ]
    # The legacy rows do not contain line/product keys.  This command is
    # intentionally scoped to the known OC; require the counts to agree and
    # pair those legacy rows by their deterministic creation order.
    existing_internal.sort(key=lambda row: (row.get("created_at") or datetime.min.replace(tzinfo=timezone.utc), str(row.get("_id") or "")))
    if len(existing_internal) != len(target_internal):
        raise SystemExit(
            f"Refusing repair: expected {len(target_internal)} internal allocations, found {len(existing_internal)}"
        )

    allocations_by_target: list[tuple[dict[str, Any], dict[str, Any]]] = list(zip(existing_internal, target_internal))
    internal_total = money(sum(money(line.get("incentive_amount")) for line in target_internal))
    paid = money(incentive.get("paid_amount"))
    credit = money(incentive.get("credit_note_deduction"))
    net = money(max(internal_total - credit, 0))
    internal_rates = {
        float(line.get("incentive_rate_snapshot"))
        for line in target_internal
        if line.get("incentive_rate_snapshot") is not None
    }
    parent_changes = {
        "creator_user_id": creator.get("_id") or order.get("created_by_user_id"),
        "creator_role_snapshot": creator.get("role_id") or order.get("created_by_role"),
        "creator_snapshot": {
            "_id": creator.get("_id"), "name": creator.get("name"),
            "email": creator.get("email"), "role_id": creator.get("role_id"),
        },
        "manager_user_id": manager.get("_id") if manager else order.get("manager_id_at_creation"),
        "manager_snapshot": order.get("manager_at_creation"),
        "incentive_lines": [*target_internal, *_customer_lines(incentive)],
        "incentive_percentage_snapshot": next(iter(internal_rates)) if len(internal_rates) == 1 else None,
        "gross_incentive_amount": internal_total,
        "incentive_amount": internal_total,
        "net_payable_incentive": net,
        "remaining_amount": money(max(net - paid, 0)),
    }
    allocation_changes: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for old, line in allocations_by_target:
        allocation_changes.append((old, {
            "allocation_key": _allocation_key(str(incentive.get("_id")), line),
            "recipient_user_id": line.get("recipient_user_id"),
            "recipient_role": line.get("recipient_role"),
            "recipient_type": line.get("recipient_type"),
            "recipient_snapshot": line.get("recipient_snapshot"),
            "allocation_type": line.get("allocation_type") or "creator",
            "oc_line_id": line.get("oc_line_id"),
            "product_id": line.get("product_id"),
            "product_name": line.get("product_name"),
            "category_id": line.get("category_id"),
            "category_name": line.get("category_name"),
            "product_amount": line.get("product_amount"),
            "rate": line.get("incentive_rate_snapshot"),
            "incentive_rate": line.get("incentive_rate_snapshot"),
            "amount": line.get("incentive_amount"),
            "incentive_amount": line.get("incentive_amount"),
            "customer_type_snapshot": line.get("customer_type_snapshot") or order.get("client_type_at_creation"),
            "client_type_snapshot": order.get("client_type_at_creation"),
        }))
    return {
        "parent_changes": parent_changes,
        "allocation_changes": allocation_changes,
        "internal_total": internal_total,
        "customer_total": money(sum(money(line.get("incentive_amount")) for line in _customer_lines(incentive))),
    }


def repair(*, apply: bool, confirm: bool, order_id: str, quotation_id: str) -> None:
    store = _configured_store()
    order, incentive, allocations = _target(store, order_id, quotation_id)
    history = incentive.get("repair_history") or []
    if any(isinstance(item, dict) and item.get("key") == REPAIR_KEY for item in history):
        print(f"ALREADY REPAIRED: order_id={order_id} incentive_id={incentive.get('_id')}")
        return
    plan = _build_plan(store, order, incentive, allocations)
    print(f"OC: {order.get('order_number') or order_id}")
    print(f"INCENTIVE: {incentive.get('_id')}")
    print(f"INTERNAL LINES: {len(_internal_lines(incentive))} -> {len(plan['parent_changes']['incentive_lines']) - len(_customer_lines(incentive))}")
    print(f"INTERNAL TOTAL: {money(incentive.get('gross_incentive_amount')):.2f} -> {plan['internal_total']:.2f}")
    print(f"CUSTOMER INCENTIVE PRESERVED: {plan['customer_total']:.2f}")
    if not apply:
        print("DRY RUN ONLY: no records changed")
        return
    if not confirm:
        raise SystemExit("Refusing repair without --confirm-repair")

    now = datetime.now(timezone.utc)
    backup_id = f"{REPAIR_KEY}:{incentive.get('_id')}"
    store.upsert_one("repair_backups", {"_id": backup_id}, {
        "repair_key": REPAIR_KEY,
        "incentive_id": incentive.get("_id"),
        "order_id": order_id,
        "quotation_id": quotation_id,
        "captured_at": now,
        "incentive_before": copy.deepcopy(incentive),
        "allocations_before": copy.deepcopy(allocations),
    })
    repair_entry = {
        "key": REPAIR_KEY,
        "reason": "Use persisted incentive rules for the manager-created OC; legacy per-user zero map was incorrectly authoritative",
        "at": now,
        "before_gross": money(incentive.get("gross_incentive_amount")),
        "after_gross": plan["internal_total"],
        "before_internal_line_count": len(_internal_lines(incentive)),
        "after_internal_line_count": len(plan["parent_changes"]["incentive_lines"]) - len(_customer_lines(incentive)),
    }
    store.update_one("incentives", {"_id": incentive.get("_id")}, {
        **plan["parent_changes"],
        "repair_history": [*history, repair_entry],
        "repaired_at": now,
        "updated_at": now,
    })
    for old, changes in plan["allocation_changes"]:
        store.update_one("incentive_allocations", {"_id": old.get("_id")}, {**changes, "updated_at": now})
    # The OC scalar is a legacy summary field; keep it consistent with the
    # internal payable amount while preserving all payment/customer snapshots.
    store.update_one("orders", {"_id": order_id}, {
        "incentive_amount": plan["internal_total"],
        "incentive_repair_key": REPAIR_KEY,
        "incentive_repaired_at": now,
    })
    print("REPAIR APPLIED: incentive parent, internal allocations, and OC summary updated")
    print(f"BACKUP: repair_backups/{backup_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report the repair without changing records")
    mode.add_argument("--apply", action="store_true", help="apply the audited repair")
    parser.add_argument("--confirm-repair", action="store_true", help="required with --apply")
    parser.add_argument("--order-id", default=DEFAULT_ORDER_ID)
    parser.add_argument("--quotation-id", default=DEFAULT_QUOTATION_ID)
    args = parser.parse_args()
    repair(
        apply=args.apply,
        confirm=args.confirm_repair,
        order_id=args.order_id,
        quotation_id=args.quotation_id,
    )
