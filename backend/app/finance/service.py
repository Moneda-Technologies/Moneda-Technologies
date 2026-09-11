from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.repositories.store import utcnow


INCENTIVE_ELIGIBLE_ROLES = frozenset({"admin", "manager_sales_admin", "user"})
INCENTIVE_PERCENTAGES = frozenset(index / 2 for index in range(0, 13))
DEFAULT_INCENTIVE_PERCENTAGE = 0.0


def money(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (TypeError, ValueError):
        return 0.0


def incentive_rate_for_user(user: dict[str, Any] | None) -> float:
    """Return the configured salesperson rate, constrained to the business range."""
    if str((user or {}).get("role_id") or "") not in INCENTIVE_ELIGIBLE_ROLES:
        return 0.0
    try:
        rate = float((user or {}).get("incentive_percentage") or 0)
    except (TypeError, ValueError):
        return 0.0
    return rate if rate in INCENTIVE_PERCENTAGES else 0.0


def create_incentive_for_order(store: Any, order: dict[str, Any], salesperson: dict[str, Any] | None) -> dict[str, Any]:
    """Create the one immutable rate snapshot associated with an OC."""
    # Conversion requests can be retried after a network timeout.  The order
    # identifier is the natural idempotency key for its incentive snapshot;
    # never create a second incentive for the same Order Confirmation.
    order_id = order.get("_id")
    existing = store.find_one("incentives", {"order_id": order_id}) if order_id else None
    if existing:
        return existing
    amount = money(order.get("order_amount", (order.get("totals") or {}).get("grand_total")))
    rate = incentive_rate_for_user(salesperson)
    gross = money(amount * rate / 100)
    now = utcnow()
    document = {
        "order_id": order.get("_id"),
        "oc_id": order.get("_id"),
        "quotation_id": order.get("quotation_id"),
        "customer_id": order.get("customer_id"),
        "salesperson_id": order.get("salesperson_id"),
        "salesperson_snapshot": order.get("salesperson_snapshot") or {},
        "order_number": order.get("order_number"),
        "oc_number": order.get("order_number"),
        "order_amount": amount,
        "incentive_percentage_snapshot": rate,
        "gross_incentive_amount": gross,
        "payment_confirmation_date": None,
        "incentive_activation_date": None,
        "incentive_due_date": None,
        "paid_amount": 0.0,
        "credit_note_deduction": 0.0,
        "net_payable_incentive": gross,
        "status": "PENDING PAYMENT",
        "created_at": now,
        "updated_at": now,
    }
    return store.insert_one("incentives", document)


def activate_incentive(store: Any, order_id: str, confirmed_at) -> dict[str, Any] | None:
    incentive = store.find_one("incentives", {"order_id": order_id})
    if not incentive:
        return None
    # Payment confirmation may be retried.  Activation is a one-way, repeatable
    # transition and must not rewrite an already activated/paid snapshot.
    if incentive.get("incentive_activation_date") or incentive.get("payment_confirmation_date"):
        return incentive
    due = confirmed_at + timedelta(days=30)
    return store.update_one("incentives", {"_id": incentive["_id"]}, {
        "status": "ACTIVE",
        "payment_confirmation_date": confirmed_at,
        "incentive_activation_date": confirmed_at,
        "incentive_due_date": due,
    })


def recompute_incentive_totals(store: Any, incentive_id: str) -> dict[str, Any] | None:
    incentive = store.find_one("incentives", {"_id": incentive_id})
    if not incentive:
        return None
    gross = money(incentive.get("gross_incentive_amount"))
    deductions = money(incentive.get("credit_note_deduction"))
    paid = money(incentive.get("paid_amount"))
    net = money(gross - deductions)
    remaining = money(net - paid)
    status = str(incentive.get("status") or "PENDING PAYMENT")
    if status not in {"PENDING PAYMENT", "ACTIVE", "DUE", "OVERDUE"}:
        status = "PAID" if remaining <= 0 else status
    elif remaining <= 0 and incentive.get("payment_confirmation_date"):
        status = "PAID"
    elif incentive.get("payment_confirmation_date"):
        due = incentive.get("incentive_due_date")
        if due and utcnow() >= due:
            status = "DUE" if utcnow().date() == due.date() else "OVERDUE"
        else:
            status = "ACTIVE"
    return store.update_one("incentives", {"_id": incentive_id}, {
        "credit_note_deduction": deductions,
        "net_payable_incentive": net,
        "paid_amount": paid,
        "remaining_amount": remaining,
        "status": status,
    })
