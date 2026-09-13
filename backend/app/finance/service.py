from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.repositories.store import utcnow
from app.services.business_logic import incentive_base_amount, manager_snapshot, normalize_client_type, resolve_incentive_rate


INCENTIVE_ELIGIBLE_ROLES = frozenset({"admin", "manager_sales_admin", "user"})
INCENTIVE_PERCENTAGES = frozenset(index / 2 for index in range(0, 13))
# Incentives are configured against the product family/category, never against
# an individual product.  These IDs match the authoritative catalogue family
# IDs used by the products collection.
INCENTIVE_CATEGORIES = {
    "blankets": "Blanket",
    "mpacks": "Underpacking",
    "chemicals": "Chemical",
}
DEFAULT_INCENTIVE_PERCENTAGE = 0.0

PAYMENT_STATUS_AWAITING = "AWAITING SUPERADMIN CONFIRMATION"
PAYMENT_STATUS_CONFIRMED = "CONFIRMED"
PAYMENT_STATUS_REJECTED = "REJECTED"
PENDING_PAYMENT_STATUSES = frozenset({
    "PAYMENT RECORDED",
    "PENDING CONFIRMATION",
    "PENDING_CONFIRMATION",
    "AWAITING BANK CONFIRMATION",
    PAYMENT_STATUS_AWAITING,
})


class IncentiveConfigurationError(ValueError):
    """Raised when a salesperson has no valid rate for an OC category."""

    def __init__(self, category_id: str, category_name: str | None = None, product_name: str | None = None):
        self.category_id = category_id
        self.category_name = category_name or INCENTIVE_CATEGORIES.get(category_id, category_id)
        # Keep product_id/product_name aliases for older API clients and audit
        # consumers, while the validation message is category-specific.
        self.product_id = category_id
        self.product_name = product_name or self.category_name
        super().__init__(f"Configure an incentive percentage for the {self.category_name} category before creating the Order Confirmation")


def money(value: Any) -> float:
    try:
        return float(Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except (TypeError, ValueError):
        return 0.0


def normalized_payment_status(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().replace("_", " ").split())


def is_confirmed_payment(payment: dict[str, Any]) -> bool:
    return normalized_payment_status(payment.get("status")) == PAYMENT_STATUS_CONFIRMED


def payment_amount(payment: dict[str, Any]) -> float:
    return money(payment.get("amount", payment.get("payment_amount")))


def _safe_due_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def payment_rollup(order: dict[str, Any], payments: list[dict[str, Any]]) -> dict[str, Any]:
    """Calculate invoice state from confirmed bank receipts only.

    Recorded, awaiting, and rejected payments are deliberately excluded from
    paid totals and customer credit. This is the authoritative payment state
    machine used by both Banking and Order Confirmation views.
    """
    confirmed = [row for row in payments if is_confirmed_payment(row)]
    confirmed_received = money(sum(payment_amount(row) for row in confirmed))
    invoice_amount = money(order.get("order_amount", (order.get("totals") or {}).get("grand_total", 0)))
    remaining_balance = money(max(invoice_amount - confirmed_received, 0))
    customer_credit = money(max(confirmed_received - invoice_amount, 0))
    pending_count = sum(1 for row in payments if normalized_payment_status(row.get("status")) in {
        normalized_payment_status(status) for status in PENDING_PAYMENT_STATUSES
    })
    due_date = _safe_due_date(order.get("due_date"))
    overdue = bool(due_date and due_date < datetime.now(timezone.utc).date() and remaining_balance > 0)

    if invoice_amount > 0 and confirmed_received >= invoice_amount:
        invoice_status = "PAID"
    elif confirmed_received > 0:
        invoice_status = "OVERDUE" if overdue else "PARTIALLY_PAID"
    elif pending_count:
        # An OC with a payment under review remains pending until a privileged
        # confirmation changes the confirmed receipt total.
        invoice_status = "PENDING"
    else:
        invoice_status = "OVERDUE" if overdue else "PENDING"

    return {
        "invoice_amount": invoice_amount,
        "confirmed_received": confirmed_received,
        "total_confirmed_payments": confirmed_received,
        "remaining_balance": remaining_balance,
        "balance": remaining_balance,
        "customer_credit": customer_credit,
        "pending_payment_count": pending_count,
        "invoice_status": invoice_status,
        "payment_status": invoice_status,
    }


def sync_order_payment_state(store: Any, order_id: str) -> dict[str, Any] | None:
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return None
    payments, _ = store.list(
        "payments",
        {"$or": [{"order_id": order_id}, {"oc_id": order_id}]},
        limit=100_000,
    )
    rollup = payment_rollup(order, payments)
    order_changes = {
        "payment_status": rollup["payment_status"],
        "confirmed_received": rollup["confirmed_received"],
        "total_confirmed_payments": rollup["total_confirmed_payments"],
        "remaining_balance": rollup["remaining_balance"],
        "customer_credit": rollup["customer_credit"],
        "pending_payment_count": rollup["pending_payment_count"],
        "financial_locked": rollup["payment_status"] == "PAID",
    }
    updated = store.update_one("orders", {"_id": order_id}, order_changes) or {**order, **order_changes}
    if order.get("quotation_id"):
        store.update_one("quotations", {"_id": order.get("quotation_id")}, {
            "payment_status": rollup["payment_status"],
            "confirmed_received": rollup["confirmed_received"],
            "remaining_balance": rollup["remaining_balance"],
            "customer_credit": rollup["customer_credit"],
            "financial_locked": rollup["payment_status"] == "PAID",
        })
    return updated


def repair_payment_states(store: Any) -> dict[str, int]:
    """Repair legacy PAID rows and synchronize every affected OC.

    A legacy PAID row is treated as confirmed only when it carries explicit
    confirmation evidence. Otherwise it is returned to the Superadmin review
    queue; arbitrary client-derived PAID values are never trusted.
    """
    rows, _ = store.list("payments", limit=100_000)
    repaired = 0
    order_ids: set[str] = set()
    now = utcnow()
    for row in rows:
        order_id = str(row.get("order_id") or row.get("oc_id") or "")
        if order_id:
            order_ids.add(order_id)
        if normalized_payment_status(row.get("status")) != "PAID":
            continue
        confirmed = bool(row.get("confirmed_at") and row.get("confirmed_by_user_id"))
        status = PAYMENT_STATUS_CONFIRMED if confirmed else PAYMENT_STATUS_AWAITING
        store.update_one("payments", {"_id": row.get("_id")}, {
            "status": status,
            "submitted_at": row.get("submitted_at") or row.get("created_at") or now,
            "audit": [*(row.get("audit") or []), {
                "action": "payment_state_repaired",
                "from": "PAID",
                "to": status,
                "at": now,
            }],
        })
        repaired += 1
    for order_id in order_ids:
        sync_order_payment_state(store, order_id)
    return {"payments": repaired, "orders": len(order_ids)}


def incentive_rate_for_user(user: dict[str, Any] | None) -> float:
    """Return the configured salesperson rate, constrained to the business range."""
    if str((user or {}).get("role_id") or "") not in INCENTIVE_ELIGIBLE_ROLES:
        return 0.0
    try:
        rate = float((user or {}).get("incentive_percentage") or 0)
    except (TypeError, ValueError):
        return 0.0
    return rate if rate in INCENTIVE_PERCENTAGES else 0.0


def incentive_rate_for_category(user: dict[str, Any] | None, category_id: str, *, allow_legacy: bool = True) -> float | None:
    """Resolve the configured rate for a user's product category."""
    user = user or {}
    if str(user.get("role_id") or "") not in INCENTIVE_ELIGIBLE_ROLES:
        return 0.0
    rates = user.get("incentive_rates")
    if isinstance(rates, dict) and category_id in rates:
        try:
            rate = float(rates[category_id])
        except (TypeError, ValueError):
            return None
        return rate if rate in INCENTIVE_PERCENTAGES else None
    if allow_legacy and "incentive_rates" not in user and "incentive_percentage" in user:
        # Compatibility for accounts created before the product matrix was
        # introduced. A zero/invalid legacy value means no configuration, not
        # an implicit zero-rate incentive.
        legacy_rate = incentive_rate_for_user(user)
        return legacy_rate if legacy_rate in INCENTIVE_PERCENTAGES else None
    return None


def incentive_rate_for_product(user: dict[str, Any] | None, product_id: str, *, allow_legacy: bool = True) -> float | None:
    """Compatibility wrapper; callers should resolve a product's category first."""
    return incentive_rate_for_category(user, product_id, allow_legacy=allow_legacy)


def _line_amount(line: dict[str, Any]) -> float:
    return money(line.get("line_total", line.get("total", line.get("amount", 0))))


def build_incentive_lines(store: Any, order: dict[str, Any], salesperson: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Create product lines with category-level rate snapshots for an OC."""
    lines = order.get("products_snapshot") or order.get("lines") or []
    snapshots: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not isinstance(line, dict):
            continue
        product_id = str(line.get("product_id") or line.get("sku") or "").strip()
        product = store.find_one("products", {"_id": product_id}) if product_id else None
        product_name = str(line.get("product_name") or (product or {}).get("name") or product_id or f"Product {index + 1}")
        category_id = str(line.get("category_id") or line.get("category") or (product or {}).get("category_id") or "").strip().lower()
        category_id = {"blanket": "blankets", "underpacking": "mpacks", "chemical": "chemicals"}.get(category_id, category_id)
        # Historical/hand-created records can contain a line without a
        # product/category.  Preserve those records, but every catalogue
        # product must resolve to one of the three incentive categories.
        if not category_id:
            if product:
                raise IncentiveConfigurationError("unknown", "product category", product_name)
            continue
        if category_id not in INCENTIVE_CATEGORIES:
            raise IncentiveConfigurationError(category_id, category_id, product_name)
        rate = resolve_incentive_rate(
            store, recipient=salesperson, client_type=order.get("client_type_at_creation"),
            category_id=category_id, customer_id=order.get("customer_id"), allocation_type="creator",
        )
        if rate is None:
            # Preserve the pre-restructure validation behavior for legacy
            # users while allowing new users (which carry an empty map) to
            # resolve the configured client-type defaults.
            rate = incentive_rate_for_category(salesperson, category_id, allow_legacy=True)
        if rate is None:
            raise IncentiveConfigurationError(category_id, INCENTIVE_CATEGORIES[category_id], product_name)
        amount = _line_amount(line)
        snapshots.append({
            "oc_line_id": str(line.get("_id") or line.get("line_id") or index),
            "product_id": product_id or None,
            "product_name": product_name,
            "category_id": category_id,
            "category_name": INCENTIVE_CATEGORIES[category_id],
            "product_amount": amount,
            "incentive_rate_snapshot": rate,
            "incentive_amount": money(amount * rate / 100),
            "recipient_user_id": (salesperson or {}).get("_id"),
            "recipient_role": str((salesperson or {}).get("role_id") or "user"),
        })
    return snapshots


def manager_rate_for_category(store: Any, manager: dict[str, Any], client_type: str, category_id: str, customer_id: Any = None) -> float:
    value = resolve_incentive_rate(
        store, recipient=manager, client_type=client_type, category_id=category_id,
        customer_id=customer_id, allocation_type="manager_override",
    )
    return float(value or 0.0)


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
    incentive_lines = build_incentive_lines(store, order, salesperson)
    creator_gross = money(sum(money(line.get("incentive_amount")) for line in incentive_lines))
    creator = store.find_one("users", {"_id": order.get("created_by_user_id")}) or salesperson or {}
    manager = store.find_one("users", {"_id": order.get("manager_id_at_creation")}) if order.get("manager_id_at_creation") else None
    client_type = normalize_client_type(order.get("client_type_at_creation"))
    manager_lines: list[dict[str, Any]] = []
    if manager and str(creator.get("role_id") or "") == "user":
        for index, line in enumerate(incentive_lines):
            rate = manager_rate_for_category(store, manager, client_type, str(line.get("category_id") or ""), order.get("customer_id"))
            amount_line = money(line.get("product_amount"))
            manager_lines.append({
                **line, "oc_line_id": f"{line.get('oc_line_id', index)}:manager",
                "recipient_user_id": manager.get("_id"), "recipient_role": manager.get("role_id"),
                "incentive_rate_snapshot": rate, "incentive_amount": money(amount_line * rate / 100),
            })
        incentive_lines.extend(manager_lines)
    gross = money(sum(money(line.get("incentive_amount")) for line in incentive_lines))
    # Keep the legacy scalar for existing reports, while the line snapshots
    # are now the authoritative calculation.
    rates = {float(line.get("incentive_rate_snapshot") or 0) for line in incentive_lines}
    rate = next(iter(rates)) if len(rates) == 1 else None
    now = utcnow()
    document = {
        "order_id": order.get("_id"),
        "oc_id": order.get("_id"),
        "quotation_id": order.get("quotation_id"),
        "customer_id": order.get("customer_id"),
        "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or order.get("company_snapshot") or {},
        "salesperson_id": order.get("salesperson_id"),
        "salesperson_snapshot": order.get("salesperson_snapshot") or {},
        "creator_user_id": creator.get("_id") or order.get("created_by_user_id"),
        "creator_role_snapshot": creator.get("role_id") or order.get("created_by_role"),
        "manager_user_id": manager.get("_id") if manager else order.get("manager_id_at_creation"),
        "manager_snapshot": order.get("manager_at_creation") or (manager_snapshot(store, creator) if manager else None),
        "client_type_snapshot": client_type,
        "incentive_base": "OC_NET_AMOUNT",
        "order_number": order.get("order_number"),
        "oc_number": order.get("order_number"),
        "order_amount": amount,
        "incentive_percentage_snapshot": rate,
        "incentive_lines": incentive_lines,
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
    created = store.insert_one("incentives", document)
    allocations = []
    for line in incentive_lines:
        allocation = store.insert_one("incentive_allocations", {
            "incentive_id": created.get("_id"), "order_id": order.get("_id"),
            "quotation_id": order.get("quotation_id"), "invoice_id": order.get("invoice_id"),
            "creator_user_id": created.get("creator_user_id"), "manager_user_id": created.get("manager_user_id"),
            "client_id": order.get("customer_id"),
            "recipient_user_id": line.get("recipient_user_id"), "recipient_role": line.get("recipient_role"),
            "client_type_snapshot": client_type, "base": "OC_NET_AMOUNT",
            "incentive_base": "OC_NET_AMOUNT", "rate": line.get("incentive_rate_snapshot"),
            "incentive_rate": line.get("incentive_rate_snapshot"), "amount": line.get("incentive_amount"),
            "incentive_amount": line.get("incentive_amount"),
            "status": "PENDING", "created_at": now,
        })
        allocations.append(allocation)
    return {**created, "allocations": allocations}


def validate_incentive_configuration(store: Any, order: dict[str, Any], salesperson: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Run the category-wise preflight before an OC is inserted."""
    return build_incentive_lines(store, order, salesperson)


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
        "financial_locked": True,
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
