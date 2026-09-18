"""Shared business rules for hierarchy, customer types and commercial snapshots.

This module intentionally contains no Flask route code.  It is used by the
customer, pricing, quotation and order layers so those layers cannot silently
grow different interpretations of manager access or client type.
"""

from __future__ import annotations

from typing import Any


CLIENT_TYPES = ("WHOLESALER", "DEALER", "CUSTOMER")
CUSTOMER_INCENTIVE_PERCENTAGES = tuple(index / 2 for index in range(1, 41))
# Price lists intentionally have a smaller, account-type vocabulary than the
# legacy incentive/client-type field.  Keep CLIENT_TYPES as a compatibility
# bridge for historical documents and existing incentive rules, while all new
# pricing resolution goes through this canonical pair.
PRICE_LIST_ACCOUNT_TYPES = ("DISTRIBUTOR", "DEALER")
MANAGER_ROLE_IDS = {"manager", "manager_sales_admin"}
GLOBAL_ROLE_IDS = {"admin", "superadmin"}


def validate_customer_incentive_config(
    enabled: Any,
    bearer_name: Any = None,
    designation: Any = None,
    percentage: Any = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the customer-level incentive configuration.

    This is deliberately shared by the customer API and incentive resolver so
    a UI payload can never be the source of truth for a customer allocation.
    """
    is_enabled = bool(enabled) if not isinstance(enabled, str) else enabled.strip().lower() in {"1", "true", "yes", "on"}
    if not is_enabled:
        return {
            "have_to_give_incentive": False,
            "incentive_bearer_name": None,
            "incentive_designation": None,
            "customer_incentive_percentage": None,
        }, None
    bearer = str(bearer_name or "").strip()
    role = str(designation or "").strip()
    if not bearer:
        return None, "Incentive Bearer Name is required when customer incentive is enabled"
    if not role:
        return None, "Designation is required when customer incentive is enabled"
    try:
        rate = float(percentage)
    except (TypeError, ValueError):
        return None, "Incentive Percentage must be between 0.5% and 20.0% in 0.5% steps"
    if rate not in CUSTOMER_INCENTIVE_PERCENTAGES:
        return None, "Incentive Percentage must be between 0.5% and 20.0% in 0.5% steps"
    return {
        "have_to_give_incentive": True,
        "incentive_bearer_name": bearer,
        "incentive_designation": role,
        "customer_incentive_percentage": rate,
    }, None


def normalize_client_type(value: Any) -> str:
    value = str(value or "").strip().upper()
    return value if value in CLIENT_TYPES else "WHOLESALER"


def customer_client_type(customer: dict[str, Any] | None) -> str:
    customer = customer or {}
    return normalize_client_type(customer.get("client_type"))


def normalize_price_list_account_type(value: Any, *, fallback: str | None = "DISTRIBUTOR") -> str | None:
    """Normalize the account type used by the Price Lists resolver.

    WHOLESALER is the historical spelling of DISTRIBUTOR and CUSTOMER was a
    legacy fallback namespace.  They are accepted only at this compatibility
    boundary; the Price Lists UI and new customer payloads expose Distributor
    and Dealer exclusively.
    """
    value = str(value or "").strip().upper()
    if value in PRICE_LIST_ACCOUNT_TYPES:
        return value
    if value in {"WHOLESALER", "CUSTOMER"}:
        return "DISTRIBUTOR"
    return fallback


def customer_account_type(customer: dict[str, Any] | None) -> str | None:
    customer = customer or {}
    explicit = customer.get("account_type")
    if explicit:
        return normalize_price_list_account_type(explicit, fallback=None)
    return normalize_price_list_account_type(customer.get("client_type"), fallback=None)


def pricing_client_type(customer: dict[str, Any] | None) -> str:
    """Compatibility value consumed by the existing pricing engine."""
    return "DEALER" if customer_account_type(customer) == "DEALER" else "WHOLESALER"


def is_manager(user: dict[str, Any] | None) -> bool:
    return str((user or {}).get("role_id") or "") in MANAGER_ROLE_IDS


def is_global_user(user: dict[str, Any] | None) -> bool:
    user = user or {}
    return str(user.get("role_id") or "") in GLOBAL_ROLE_IDS


def valid_manager(store, manager_id: Any) -> dict[str, Any] | None:
    manager_id = str(manager_id or "").strip()
    if not manager_id:
        return None
    manager = store.find_one("users", {"_id": manager_id, "active": {"$ne": False}})
    return manager if manager and is_manager(manager) else None


def manager_for_user(store, user: dict[str, Any] | None) -> dict[str, Any] | None:
    user = user or {}
    return valid_manager(store, user.get("manager_id"))


def managed_user_ids(store, manager_id: Any) -> list[str]:
    manager_id = str(manager_id or "").strip()
    if not manager_id:
        return []
    rows, _ = store.list("users", {"manager_id": manager_id, "active": {"$ne": False}}, limit=100_000)
    return [str(row["_id"]) for row in rows if row.get("_id")]


def accessible_user_ids(store, user: dict[str, Any] | None) -> list[str]:
    """Return the user IDs whose customer relationships a user may manage."""
    user = user or {}
    own = str(user.get("_id") or "").strip()
    if not own:
        return []
    if is_global_user(user):
        rows, _ = store.list("users", {"active": {"$ne": False}}, limit=100_000)
        return [str(row["_id"]) for row in rows if row.get("_id")]
    if is_manager(user):
        return list(dict.fromkeys([own, *managed_user_ids(store, own)]))
    return [own]


def customer_ids_for_user(store, user: dict[str, Any] | None, *, include_created: bool = True) -> list[str]:
    """Resolve assigned/owned customers, including a manager's direct team."""
    user = user or {}
    if is_global_user(user):
        rows, _ = store.list("customers", {"active": {"$ne": False}, "status": {"$ne": "archived"}}, limit=100_000)
        return [str(row["_id"]) for row in rows if row.get("_id") and not row.get("is_issuer")]
    ids: list[str] = []
    for user_id in accessible_user_ids(store, user):
        clauses: list[dict[str, Any]] = [{"assigned_user_ids": user_id}]
        if include_created:
            clauses.append({"created_by_user_id": user_id})
        rows, _ = store.list("customers", {
            "active": {"$ne": False}, "status": {"$ne": "archived"}, "$or": clauses,
        }, limit=100_000)
        ids.extend(str(row["_id"]) for row in rows if row.get("_id") and not row.get("is_issuer"))
    return list(dict.fromkeys(ids))


def manager_snapshot(store, user: dict[str, Any] | None) -> dict[str, Any] | None:
    manager = manager_for_user(store, user)
    if not manager:
        return None
    return {"_id": manager.get("_id"), "name": manager.get("name"), "email": manager.get("email"), "role_id": manager.get("role_id")}


def incentive_base_amount(order: dict[str, Any]) -> float:
    """The canonical incentive base; never derive it from a display currency."""
    # Newer OCs keep their authoritative commercial total under
    # ``master_totals.grand_total`` while older documents used the flat
    # ``net_total_eur``/``total_eur`` fields.  Read the persisted EUR/master
    # values in order, and only then fall back to line snapshots.  Never use a
    # display-currency total here.
    candidates: list[Any] = [order.get("net_total_eur"), order.get("total_eur")]
    for container_key in ("master_totals", "totals"):
        container = order.get(container_key)
        if isinstance(container, dict):
            candidates.append(container.get("grand_total"))
            candidates.append(container.get("net_total"))
    candidates.append(order.get("order_amount"))
    for value in candidates:
        try:
            if value is not None and str(value).strip() != "":
                return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    lines = order.get("lines") or order.get("items") or []
    if isinstance(lines, list):
        total = 0.0
        for line in lines:
            if not isinstance(line, dict):
                continue
            value = line.get("master_final_total", line.get("line_total"))
            try:
                total += max(0.0, float(value or 0))
            except (TypeError, ValueError):
                continue
        return total
    return 0.0


def resolve_incentive_rate(
    store: Any,
    *,
    recipient: dict[str, Any] | None,
    client_type: Any,
    category_id: str,
    customer_id: Any = None,
    allocation_type: str = "creator",
    prefer_persisted_rules: bool = False,
) -> float | None:
    """Resolve one incentive rate using the canonical precedence chain.

    User/category overrides remain the first-class compatibility layer.  The
    persisted rules then provide customer, client-type/category, client-type,
    role and global fallbacks.  Returning ``None`` means no configured rate;
    callers can decide whether that should block a transaction.
    """
    recipient = recipient or {}
    client_type = normalize_client_type(client_type)
    category_id = str(category_id or "").strip().lower()
    role_id = str(recipient.get("role_id") or "").strip()
    if role_id not in {"admin", "manager_sales_admin", "manager", "user"}:
        return 0.0
    configured = recipient.get("incentive_rates")
    if not prefer_persisted_rules:
        if isinstance(configured, dict) and category_id in configured:
            try:
                value = float(configured[category_id])
            except (TypeError, ValueError):
                return None
            return value if value in {index / 2 for index in range(0, 13)} else None
        if isinstance(configured, dict) and configured:
            return None
        if "incentive_rates" not in recipient and allocation_type != "manager_override":
            return None
    elif isinstance(configured, dict) and configured:
        # Older user snapshots were seeded with an all-zero map even when the
        # administrator had configured the persisted role/client rule.  Treat
        # that all-zero map as an empty legacy value so the database rule can
        # resolve the rate.  A non-zero legacy map remains a deliberate
        # compatibility override for existing installations and tests; this
        # keeps historical category-specific rates stable while correcting the
        # stale 0% case.
        try:
            configured_values = [float(value) for value in configured.values()]
        except (TypeError, ValueError):
            configured_values = []
        if configured_values and any(value != 0 for value in configured_values):
            if category_id in configured:
                try:
                    value = float(configured[category_id])
                except (TypeError, ValueError):
                    return None
                return value if value in {index / 2 for index in range(0, 13)} else None
            return None
    manager_role = "manager_sales_admin" if role_id == "manager" else role_id
    candidates = []
    if customer_id:
        # Customer rules intentionally come first and may be scoped narrowly or
        # broadly.  This keeps the precedence stable when an administrator
        # adds a customer-wide override later.
        candidates.extend([
            {"customer_id": str(customer_id), "client_type": client_type, "recipient_role": manager_role, "category_id": category_id},
            {"customer_id": str(customer_id), "client_type": client_type, "recipient_role": manager_role, "category_id": "*"},
            {"customer_id": str(customer_id), "client_type": client_type, "category_id": category_id},
            {"customer_id": str(customer_id), "category_id": category_id},
            {"customer_id": str(customer_id)},
        ])
    candidates.extend([
        {"client_type": client_type, "recipient_role": manager_role, "category_id": category_id},
        {"client_type": client_type, "recipient_role": manager_role, "category_id": "*"},
        {"client_type": client_type, "category_id": category_id},
        {"client_type": client_type},
        {"client_type": "*", "recipient_role": manager_role, "category_id": category_id},
        {"client_type": "*", "recipient_role": manager_role, "category_id": "*"},
        {"recipient_role": manager_role, "category_id": category_id},
        {"recipient_role": manager_role, "category_id": "*"},
        {"category_id": category_id},
        {"category_id": "*"},
    ])
    for query in candidates:
        query["active"] = True
        if allocation_type:
            query["allocation_type"] = allocation_type
        rule = store.find_one("incentive_rules", query)
        # Older seeded rules did not carry allocation_type.  They remain valid
        # for manager overrides and existing installations only.
        if not rule and allocation_type == "manager_override":
            query.pop("allocation_type", None)
            rule = store.find_one("incentive_rules", query)
        if not rule:
            continue
        try:
            value = float(rule.get("rate"))
        except (TypeError, ValueError):
            continue
        if value in {index / 2 for index in range(0, 13)}:
            return value

    if prefer_persisted_rules and isinstance(configured, dict) and category_id in configured:
        try:
            value = float(configured[category_id])
        except (TypeError, ValueError):
            return None
        return value if value in {index / 2 for index in range(0, 13)} else None
    if prefer_persisted_rules and isinstance(configured, dict) and configured:
        # A non-empty user-specific map is an explicit allow-list.  Do not
        # silently fill an omitted category from a global default.
        return None
    # A legacy user without the new map must retain the existing requirement
    # to configure a category, rather than silently receiving a new default.
    if prefer_persisted_rules and "incentive_rates" not in recipient and allocation_type != "manager_override":
        return None
    return None
