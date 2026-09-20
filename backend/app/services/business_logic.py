"""Shared business rules for hierarchy, customer types and commercial snapshots.

This module intentionally contains no Flask route code.  It is used by the
customer, pricing, quotation and order layers so those layers cannot silently
grow different interpretations of manager access or client type.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

# Incentive configuration is intentionally data-driven.  These values describe
# the state machine and validation vocabulary only; rates remain persisted in
# MongoDB and are never hard-coded into pricing or quotation code.
INCENTIVE_CONFIGURATION_STATUSES = frozenset({
    "ENABLED", "DISABLED", "INHERIT", "NOT_CONFIGURED",
})
INCENTIVE_CONFIGURATION_SCOPES = frozenset({
    "customer", "product", "category", "client_type", "person", "default",
})
INCENTIVE_HALF_STEPS = frozenset(index / 2 for index in range(0, 201))


class IncentiveConfigurationValidationError(ValueError):
    """Raised when an incentive configuration violates its maximum/shape."""


def _incentive_status(value: Any, *, default: str = "ENABLED") -> str:
    status = str(value or default).strip().upper().replace(" ", "_")
    if status == "ACTIVE":
        status = "ENABLED"
    if status == "INACTIVE":
        status = "DISABLED"
    return status if status in INCENTIVE_CONFIGURATION_STATUSES else default


def _incentive_rate(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise IncentiveConfigurationValidationError("Incentive rate must be numeric") from None
    if rate < 0 or rate not in INCENTIVE_HALF_STEPS:
        raise IncentiveConfigurationValidationError("Incentive rate must use 0.5% steps between 0% and 100%")
    return rate


def _configuration_rate(row: dict[str, Any]) -> float | None:
    return _incentive_rate(row.get("rate", row.get("incentive_percentage", row.get("default_rate"))))


def _configuration_scope(row: dict[str, Any]) -> str:
    explicit = str(row.get("scope") or "").strip().lower()
    if explicit in INCENTIVE_CONFIGURATION_SCOPES:
        return explicit
    if row.get("customer_id"):
        return "customer"
    if row.get("product_id"):
        return "product"
    if row.get("category_id") and str(row.get("category_id")) != "*":
        return "category"
    if row.get("client_type") and str(row.get("client_type")) != "*":
        return "client_type"
    # A row that belongs to one recipient but has no narrower business
    # dimension is that person's default.  Treat it as ``person`` so it wins
    # over the global/role fallback in the documented precedence chain.
    if row.get("user_id") or row.get("recipient_user_id"):
        return "person"
    return "default"


def _effective_configuration(row: dict[str, Any], effective_date: Any = None) -> bool:
    """Return whether an optional effective date window includes the request."""
    if effective_date is None:
        effective_date = datetime.now(timezone.utc)
    if isinstance(effective_date, str):
        try:
            effective_date = datetime.fromisoformat(effective_date.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not hasattr(effective_date, "tzinfo"):
        return True
    for field, is_start in (("effective_from", True), ("effective_to", False)):
        raw = row.get(field)
        if not raw:
            continue
        try:
            boundary = raw if hasattr(raw, "tzinfo") else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if boundary.tzinfo is None:
                boundary = boundary.replace(tzinfo=timezone.utc)
            if is_start and effective_date < boundary:
                return False
            if not is_start and effective_date > boundary:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _configuration_matches(
    row: dict[str, Any], *, user_id: str, role_id: str, manager_role: str,
    client_type: str, category_id: str, product_id: str | None,
    customer_id: str | None, allocation_type: str,
) -> bool:
    row_user = str(row.get("user_id") or row.get("recipient_user_id") or "").strip()
    if row_user and row_user != user_id:
        return False
    row_role = str(row.get("role") or row.get("recipient_role") or "").strip().lower()
    if row_role and row_role not in {role_id, manager_role, "*"}:
        return False
    configured_allocation = str(row.get("allocation_type") or "").strip().lower()
    if configured_allocation and configured_allocation != allocation_type:
        return False
    row_client = str(row.get("client_type") or "").strip().upper()
    if row_client and row_client not in {client_type, "*"}:
        return False
    row_category = str(row.get("category_id") or "").strip().lower()
    if row_category and row_category not in {category_id, "*"}:
        return False
    row_product = str(row.get("product_id") or "").strip()
    if row_product and row_product != str(product_id or ""):
        return False
    row_customer = str(row.get("customer_id") or "").strip()
    if row_customer and row_customer != str(customer_id or ""):
        return False
    return True


def _rule_matches(
    row: dict[str, Any], *, client_type: str, category_id: str,
    product_id: str | None, customer_id: str | None,
    role_id: str, manager_role: str, allocation_type: str,
    include_maximum: bool = False,
) -> bool:
    if row.get("active") is False:
        return False
    if not _effective_configuration(row):
        return False
    kind = str(row.get("rule_kind") or "default").strip().lower()
    if not include_maximum and kind in {"maximum", "max", "ceiling"}:
        return False
    configured_allocation = str(row.get("allocation_type") or "").strip().lower()
    if configured_allocation and configured_allocation != allocation_type:
        return False
    row_role = str(row.get("recipient_role") or "").strip().lower()
    if row_role and row_role not in {role_id, manager_role, "*"}:
        return False
    row_client = str(row.get("client_type") or "").strip().upper()
    if row_client and row_client not in {client_type, "*"}:
        return False
    row_category = str(row.get("category_id") or "").strip().lower()
    if row_category and row_category not in {category_id, "*"}:
        return False
    row_product = str(row.get("product_id") or "").strip()
    if row_product and row_product != str(product_id or ""):
        return False
    row_customer = str(row.get("customer_id") or "").strip()
    if row_customer and row_customer != str(customer_id or ""):
        return False
    return True


def resolve_incentive_maximum(
    store: Any, *, recipient: dict[str, Any] | None, client_type: Any,
    category_id: str, customer_id: Any = None, product_id: Any = None,
    allocation_type: str = "creator",
) -> float | None:
    """Return the most-specific persisted incentive ceiling, if configured.

    Maximum rows live alongside normal rules but are explicitly marked with
    ``rule_kind``.  Keeping this lookup here ensures the write API and the OC
    resolver enforce exactly the same ceiling semantics.
    """
    recipient = recipient or {}
    role_id = str(recipient.get("role_id") or "").strip().lower()
    if role_id not in {"admin", "manager_sales_admin", "manager", "user"}:
        return None
    manager_role = "manager_sales_admin" if role_id == "manager" else role_id
    normalized_client = normalize_client_type(client_type)
    normalized_category = str(category_id or "").strip().lower()
    normalized_product = str(product_id or "").strip() or None
    normalized_customer = str(customer_id or "").strip() or None
    normalized_allocation = str(allocation_type or "creator").strip().lower()
    rules, _ = store.list("incentive_rules", {}, limit=100_000)
    maximum_rows = [
        row for row in rules
        if str(row.get("rule_kind") or "").strip().lower() in {"maximum", "max", "ceiling"}
        and _rule_matches(
            row, client_type=normalized_client, category_id=normalized_category,
            product_id=normalized_product, customer_id=normalized_customer,
            role_id=role_id, manager_role=manager_role,
            allocation_type=normalized_allocation, include_maximum=True,
        )
    ]

    def specificity(row: dict[str, Any]) -> tuple[int, int, str]:
        return (
            4 if row.get("customer_id") else 3 if row.get("product_id") else 2 if row.get("category_id") not in (None, "", "*") else 1 if row.get("client_type") not in (None, "", "*") else 0,
            1 if str(row.get("recipient_role") or "*").lower() in {role_id, manager_role} else 0,
            str(row.get("updated_at") or row.get("created_at") or ""),
        )

    maximum_rows.sort(key=specificity, reverse=True)
    if not maximum_rows:
        return None
    row = maximum_rows[0]
    maximum = _incentive_rate(row.get("maximum_rate"))
    return maximum if maximum is not None else _configuration_rate(row)


def resolve_incentive_configuration(
    store: Any, *, recipient: dict[str, Any] | None, client_type: Any,
    category_id: str, customer_id: Any = None, product_id: Any = None,
    allocation_type: str = "creator", effective_date: Any = None,
) -> dict[str, Any]:
    """Resolve an optional individual incentive without mutating history.

    Maximum rules are ceilings; individual rows are actual configuration.  A
    disabled row is terminal, while inherit/not-configured rows continue down
    the documented specificity chain.
    """
    recipient = recipient or {}
    role_id = str(recipient.get("role_id") or "").strip().lower()
    if role_id not in {"admin", "manager_sales_admin", "manager", "user"}:
        return {"eligible": False, "status": "NOT_CONFIGURED", "rate": None, "source": "NONE", "maximum_rate": None}
    user_id = str(recipient.get("_id") or "").strip()
    manager_role = "manager_sales_admin" if role_id == "manager" else role_id
    client_type = normalize_client_type(client_type)
    category_id = str(category_id or "").strip().lower()
    product_id = str(product_id or "").strip() or None
    customer_id = str(customer_id or "").strip() or None
    allocation_type = str(allocation_type or "creator").strip().lower()
    configs, _ = store.list("incentive_configurations", {}, limit=100_000)
    configs = [row for row in configs if _effective_configuration(row, effective_date) and _configuration_matches(
        row, user_id=user_id, role_id=role_id, manager_role=manager_role,
        client_type=client_type, category_id=category_id, product_id=product_id,
        customer_id=customer_id, allocation_type=allocation_type,
    )]
    person_candidates = [
        ("customer", lambda row: str(row.get("customer_id") or "").strip() == str(customer_id or "") and bool(customer_id)),
        ("product", lambda row: str(row.get("product_id") or "").strip() == str(product_id or "") and bool(product_id)),
        ("category", lambda row: str(row.get("category_id") or "").strip().lower() == category_id),
        ("client_type", lambda row: str(row.get("client_type") or "").strip().upper() == client_type),
        ("person", lambda row: bool(user_id) and str(row.get("user_id") or row.get("recipient_user_id") or "").strip() == user_id),
        ("default", lambda row: not any(row.get(key) for key in ("customer_id", "product_id", "category_id", "client_type"))),
    ]
    selected: dict[str, Any] | None = None
    selected_from_person_configuration = False
    source = "NONE"
    for scope, matcher in person_candidates:
        matches = [row for row in configs if _configuration_scope(row) == scope and matcher(row)]
        if not matches:
            continue
        row = sorted(matches, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[0]
        status = _incentive_status(row.get("status"), default="ENABLED")
        if status == "DISABLED":
            return {"eligible": True, "status": status, "rate": 0.0, "source": f"PERSON_{scope.upper()}", "rule_id": row.get("_id"), "maximum_rate": None}
        if status in {"INHERIT", "NOT_CONFIGURED"}:
            continue
        selected, source = row, f"PERSON_{scope.upper()}"
        selected_from_person_configuration = True
        break

    # Legacy user maps are treated as person/category configuration for
    # backwards compatibility.  A non-empty map is an explicit allow-list;
    # omitted categories therefore do not silently inherit a role default.
    legacy_rates = recipient.get("incentive_rates")
    legacy_values: list[float] = []
    if isinstance(legacy_rates, dict):
        for value in legacy_rates.values():
            try:
                legacy_values.append(float(value))
            except (TypeError, ValueError):
                continue
    if selected is None and legacy_values and any(value != 0 for value in legacy_values):
        if category_id in legacy_rates:
            selected = {"_id": None, "rate": legacy_rates.get(category_id), "base": "OC_NET_AMOUNT"}
            source = "PERSON_CATEGORY"
        else:
            return {"eligible": True, "status": "NOT_CONFIGURED", "rate": None, "source": "NONE", "maximum_rate": None}

    rules, _ = store.list("incentive_rules", {}, limit=100_000)
    applicable = [row for row in rules if _rule_matches(
        row, client_type=client_type, category_id=category_id, product_id=product_id,
        customer_id=customer_id, role_id=role_id, manager_role=manager_role,
        allocation_type=allocation_type,
    )]
    def specificity(row: dict[str, Any]) -> tuple[int, int, str]:
        return (
            4 if row.get("customer_id") else 3 if row.get("product_id") else 2 if row.get("category_id") not in (None, "", "*") else 1 if row.get("client_type") not in (None, "", "*") else 0,
            1 if str(row.get("recipient_role") or "*").lower() in {role_id, manager_role} else 0,
            str(row.get("updated_at") or row.get("created_at") or ""),
        )
    applicable.sort(key=specificity, reverse=True)
    rule = applicable[0] if applicable else None
    if selected is None and rule is not None:
        selected = rule
        source = "CUSTOMER_ROLE" if rule.get("customer_id") else "PRODUCT_ROLE" if rule.get("product_id") else "CATEGORY_ROLE" if rule.get("category_id") not in (None, "", "*") else "CLIENT_TYPE_ROLE" if rule.get("client_type") not in (None, "", "*") else "ROLE_DEFAULT"
    if selected is None:
        return {"eligible": True, "status": "NOT_CONFIGURED", "rate": None, "source": "NONE", "maximum_rate": None}
    status = _incentive_status(selected.get("status"), default="ENABLED")
    if status == "DISABLED":
        return {"eligible": True, "status": status, "rate": 0.0, "source": source, "rule_id": selected.get("_id"), "maximum_rate": None}
    rate = _configuration_rate(selected)
    if rate is None:
        return {"eligible": True, "status": "NOT_CONFIGURED", "rate": None, "source": source, "rule_id": selected.get("_id"), "maximum_rate": None}

    maximum = resolve_incentive_maximum(
        store, recipient=recipient, client_type=client_type,
        category_id=category_id, customer_id=customer_id,
        product_id=product_id, allocation_type=allocation_type,
    )
    if maximum is None:
        maximum = _incentive_rate(selected.get("maximum_rate"))
    if maximum is None:
        # Legacy rules had no explicit ceiling; keeping the selected rate as
        # the fallback preserves existing behaviour while new configuration
        # rows can opt into a real maximum rule.
        maximum = _configuration_rate(selected)
    # Maximum ceilings are mandatory for persisted, person-specific
    # configuration rows.  Legacy role/category rules pre-date the optional
    # configuration model and remain valid fallbacks until an administrator
    # explicitly migrates them to an individual configuration.  Do not make a
    # historical/default rule suddenly fail an otherwise valid OC because a
    # newly seeded ceiling is lower than that legacy fallback.
    if selected_from_person_configuration and maximum is not None and rate > maximum:
        raise IncentiveConfigurationValidationError(f"Incentive rate {rate:g}% exceeds the configured maximum of {maximum:g}%")
    if not selected_from_person_configuration and maximum is not None and rate > maximum:
        maximum = rate
    return {
        "eligible": True, "status": "ENABLED", "rate": rate, "source": source,
        "rule_id": selected.get("_id"), "maximum_rate": maximum,
        "incentive_base_type": selected.get("base") or selected.get("incentive_base_type") or "OC_NET_AMOUNT",
        "effective_from": selected.get("effective_from"), "effective_to": selected.get("effective_to"),
    }


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
    prefer_persisted_rules: bool = True,
) -> float | None:
    """Resolve one incentive rate using the canonical precedence chain.

    User/category overrides remain the first-class compatibility layer.  The
    persisted rules then provide customer, client-type/category, client-type,
    role and global fallbacks.  Returning ``None`` means no configured rate;
    callers can decide whether that should block a transaction.
    """
    if prefer_persisted_rules:
        resolved = resolve_incentive_configuration(
            store,
            recipient=recipient,
            client_type=client_type,
            category_id=category_id,
            customer_id=customer_id,
            allocation_type=allocation_type,
        )
        if resolved.get("status") == "DISABLED":
            return 0.0
        return resolved.get("rate")
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
            return value if value in INCENTIVE_HALF_STEPS else None
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
                return value if value in INCENTIVE_HALF_STEPS else None
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
        if value in INCENTIVE_HALF_STEPS:
            return value

    if prefer_persisted_rules and isinstance(configured, dict) and category_id in configured:
        try:
            value = float(configured[category_id])
        except (TypeError, ValueError):
            return None
        return value if value in INCENTIVE_HALF_STEPS else None
    if prefer_persisted_rules and isinstance(configured, dict) and configured:
        # A non-empty user-specific map is an explicit allow-list.  Do not
        # silently fill an omitted category from a global default.
        return None
    # A legacy user without the new map must retain the existing requirement
    # to configure a category, rather than silently receiving a new default.
    if prefer_persisted_rules and "incentive_rates" not in recipient and allocation_type != "manager_override":
        return None
    return None
