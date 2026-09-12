from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.repositories.store import utcnow


INCENTIVE_ELIGIBLE_ROLES = frozenset({"admin", "manager_sales_admin", "user"})
INCENTIVE_PERCENTAGES = frozenset(float(index) for index in range(1, 7))
# Incentives are configured against the product family/category, never against
# an individual product.  These IDs match the authoritative catalogue family
# IDs used by the products collection.
INCENTIVE_CATEGORIES = {
    "blankets": "Blanket",
    "mpacks": "Underpacking",
    "chemicals": "Chemical",
}
DEFAULT_INCENTIVE_PERCENTAGE = 0.0


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
        })
    return snapshots


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
    return store.insert_one("incentives", document)


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
