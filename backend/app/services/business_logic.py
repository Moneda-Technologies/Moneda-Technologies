"""Shared business rules for hierarchy, customer types and commercial snapshots.

This module intentionally contains no Flask route code.  It is used by the
customer, pricing, quotation and order layers so those layers cannot silently
grow different interpretations of manager access or client type.
"""

from __future__ import annotations

from typing import Any


CLIENT_TYPES = ("WHOLESALER", "DEALER", "CUSTOMER")
MANAGER_ROLE_IDS = {"manager", "manager_sales_admin"}
GLOBAL_ROLE_IDS = {"admin", "superadmin"}


def normalize_client_type(value: Any) -> str:
    value = str(value or "").strip().upper()
    return value if value in CLIENT_TYPES else "WHOLESALER"


def customer_client_type(customer: dict[str, Any] | None) -> str:
    customer = customer or {}
    return normalize_client_type(customer.get("client_type"))


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
    try:
        return max(0.0, float(order.get("net_total_eur", order.get("total_eur", 0)) or 0))
    except (TypeError, ValueError):
        return 0.0


def resolve_incentive_rate(
    store: Any,
    *,
    recipient: dict[str, Any] | None,
    client_type: Any,
    category_id: str,
    customer_id: Any = None,
    allocation_type: str = "creator",
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
    if isinstance(configured, dict) and category_id in configured:
        try:
            value = float(configured[category_id])
        except (TypeError, ValueError):
            return None
        return value if value in {index / 2 for index in range(0, 13)} else None
    if isinstance(configured, dict) and configured:
        # A non-empty user-specific map is an explicit allow-list.  Do not
        # silently fill an omitted category from a global default.
        return None
    # A legacy user without the new map must retain the existing requirement
    # to configure a category, rather than silently receiving a new default.
    if "incentive_rates" not in recipient and allocation_type != "manager_override":
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
    return None
