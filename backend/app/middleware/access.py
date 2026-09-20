from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import current_app, g, request, session

from app.api.responses import failure
from app.devices.service import enforce_device_access
from app.services.business_logic import accessible_user_ids, customer_ids_for_user


F = TypeVar("F", bound=Callable[..., Any])

# The selected workspace record is the customer being quoted.  Legacy keys
# remain read-only compatibility bridges while sessions migrate.
CUSTOMER_CONTEXT_KEY = "active_customer_id"
LEGACY_CUSTOMER_CONTEXT_KEYS = ("selected_customer_company_id", "active_company_id")


def selected_customer_id() -> str | None:
    return session.get(CUSTOMER_CONTEXT_KEY) or next((session.get(key) for key in LEGACY_CUSTOMER_CONTEXT_KEYS if session.get(key)), None)


def selected_customer_company_id() -> str | None:
    """Deprecated alias for integrations; core state is active_customer_id."""
    return selected_customer_id()


def customer_company_id_from(payload: dict[str, Any] | None) -> str | None:
    """Deprecated alias; resolve the canonical customer_id field first."""
    body = payload or {}
    value = body.get("customer_id") or body.get("customer_company_id") or body.get("company_id")
    return str(value).strip() if value else None


def customer_id_from(payload: dict[str, Any] | None) -> str | None:
    return customer_company_id_from(payload)


def load_current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = current_app.extensions["store"].find_one("users", {"_id": user_id, "active": True})
    if user:
        role = current_app.extensions["store"].find_one("roles", {"_id": user.get("role_id")}) or {}
        user["permissions"] = role.get("permissions", [])
        user["role_display_name"] = role.get("display_name", user.get("role_id"))
    g.current_user = user
    return user


def current_user() -> dict[str, Any] | None:
    return getattr(g, "current_user", None) or load_current_user()


def login_required(fn: F | None = None, *, allow_pending: bool = False):
    def decorator(handler: F) -> F:
      @wraps(handler)
      def wrapped(*args: Any, **kwargs: Any):
        if not current_user():
            return failure("Authentication required", status=401)
        # Logout and the read-only device status endpoint remain available to
        # an authenticated-but-pending session; all other protected routes are
        # blocked by the server-side device record.
        if not allow_pending and not (request.endpoint or "").endswith(("logout", "device_access")):
            blocked = enforce_device_access(current_user())
            if blocked is not None:
                return blocked
        return handler(*args, **kwargs)
      return wrapped  # type: ignore[return-value]
    if fn is None:
        return decorator
    return decorator(fn)


def permission_required(permission: str) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any):
            user = current_user()
            if not user:
                return failure("Authentication required", status=401)
            blocked = enforce_device_access(user)
            if blocked is not None:
                return blocked
            # Existing installations can have a stale permission array even
            # though the canonical role is still Superadmin. Keep the role
            # boundary authoritative after authentication/device checks.
            is_superadmin = str(user.get("role_id") or "") == "superadmin"
            if permission not in user.get("permissions", []) and not is_superadmin:
                if permission == "quotations.send":
                    current_app.logger.info(
                        "quotation_send_authorization quotation_id=%s user_authorized=false permission=%s result=FAIL",
                        kwargs.get("quotation_id", "unknown"), permission,
                    )
                message = "You do not have permission to send quotations." if permission == "quotations.send" else "You do not have permission to perform this action"
                return failure(message, status=403)
            if permission == "quotations.send":
                current_app.logger.info(
                    "quotation_send_authorization quotation_id=%s user_authorized=true permission=%s result=PASS",
                    kwargs.get("quotation_id", "unknown"), permission,
                )
            return fn(*args, **kwargs)
        return wrapped  # type: ignore[return-value]
    return decorator


def permission_required_any(*permissions: str) -> Callable[[F], F]:
    """Authorize a route with any existing equivalent permission."""
    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any):
            user = current_user()
            if not user:
                return failure("Authentication required", status=401)
            blocked = enforce_device_access(user)
            if blocked is not None:
                return blocked
            if not any(permission in user.get("permissions", []) for permission in permissions):
                return failure("You do not have permission to perform this action", status=403)
            return fn(*args, **kwargs)
        return wrapped  # type: ignore[return-value]
    return decorator


def superadmin_required(fn: F) -> F:
    """Require the canonical Superadmin role for security-sensitive controls."""
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any):
        user = current_user()
        if not user:
            return failure("Authentication required", status=401)
        blocked = enforce_device_access(user)
        if blocked is not None:
            return blocked
        if str(user.get("role_id") or "") != "superadmin":
            return failure("Only a Superadmin can perform this action", status=403, error="superadmin_required")
        return fn(*args, **kwargs)
    return wrapped  # type: ignore[return-value]


def customer_record(customer_id: str | None) -> dict[str, Any] | None:
    if not customer_id:
        return None
    store = current_app.extensions["store"]
    customer = store.find_one("customers", {"_id": customer_id, "active": {"$ne": False}})
    if customer:
        return customer
    # Read-only bridge for pre-migration records in companies.
    company = store.find_one("companies", {"_id": customer_id, "active": True})
    if company:
        return {**company, "_id": customer_id, "customer_id": customer_id, "company_name": company.get("name")}
    return None


def can_view_all_customers(user: dict[str, Any] | None = None) -> bool:
    user = user or current_user() or {}
    role_id = str(user.get("role_id") or "")
    # Manager / sales users are always team-scoped, even if an older role
    # document still carries the legacy customers.view_all permission.
    if role_id in {"manager", "manager_sales_admin", "user"}:
        return False
    return role_id in {"admin", "superadmin"} or "customers.view_all" in user.get("permissions", [])


def customer_access_ids_for_user(user_id: str | None, *, include_created: bool = True) -> list[str]:
    """Return canonical customer IDs accessible to one non-global user.

    ``customers.assigned_user_ids`` is the authoritative relationship. The
    creator relationship is included so an existing owner cannot lose access
    merely because an older record predates the assignment array.
    """
    if not user_id:
        return []
    store = current_app.extensions["store"]
    user = store.find_one("users", {"_id": user_id}) or {"_id": user_id}
    return customer_ids_for_user(store, user, include_created=include_created)


def customer_access_summary(user: dict[str, Any] | None = None) -> dict[str, Any]:
    user = user or current_user() or {}
    if can_view_all_customers(user):
        return {"global": True, "customer_ids": [], "count": None}
    ids = customer_access_ids_for_user(str(user.get("_id")) if user.get("_id") else None)
    return {"global": False, "customer_ids": ids, "count": len(ids)}


def repair_customer_assignments(store) -> int:
    """Backfill only provable stored creator/assignment relationships.

    No name/email guessing is performed. Legacy user arrays are treated as
    explicit relationship records, and creator fields are used only when they
    contain an existing user ID. Existing assignment members are kept.
    """
    rows, _ = store.list("customers", limit=100_000)
    customer_by_id = {
        str(row.get("_id")): row for row in rows
        if row.get("_id") and not row.get("is_issuer")
    }
    users, _ = store.list("users", limit=100_000)
    explicit_by_customer: dict[str, list[str]] = {}
    for user in users:
        user_id = str(user.get("_id") or "").strip()
        if not user_id:
            continue
        legacy_ids: list[Any] = []
        for field in ("customer_ids", "customer_company_ids", "company_ids"):
            values = user.get(field)
            if isinstance(values, list):
                legacy_ids.extend(values)
        for value in legacy_ids:
            customer_id = str(value).strip()
            if customer_id in customer_by_id:
                members = explicit_by_customer.setdefault(customer_id, [])
                if user_id not in members:
                    members.append(user_id)
    changed = 0
    for row in rows:
        customer_id = str(row.get("_id") or "").strip()
        if not customer_id or row.get("is_issuer"):
            continue
        creator = row.get("created_by_user_id") or row.get("owner_user_id") or row.get("created_by")
        if isinstance(creator, dict):
            creator = creator.get("_id") or creator.get("user_id")
        assigned = [str(value) for value in (row.get("assigned_user_ids") or []) if value]
        additions = [value for value in explicit_by_customer.get(customer_id, []) if value not in assigned]
        if isinstance(creator, str) and store.find_one("users", {"_id": creator}) and creator not in assigned:
            additions.append(creator)
        if not additions:
            continue
        updated = list(dict.fromkeys([*assigned, *additions]))
        if store.update_one("customers", {"_id": row.get("_id")}, {"assigned_user_ids": updated}):
            changed += 1
    return changed


def can_view_all_quotations(user: dict[str, Any] | None = None) -> bool:
    """Return whether the central permission model grants global history access."""
    user = user or current_user() or {}
    role_id = str(user.get("role_id") or "")
    # Manager/Sales Admin and User visibility is always scoped by team/customer
    # ownership.  A stale legacy permission must not widen those roles into a
    # company-wide quotation history view.
    if role_id in {"manager", "manager_sales_admin", "user"}:
        return False
    return role_id in {"admin", "superadmin"} or "quotations.view_all" in user.get("permissions", [])


def quotation_scope_user_ids(user: dict[str, Any] | None = None) -> list[str]:
    """Return the creator IDs visible in quotation history for this actor."""
    user = user or current_user() or {}
    if can_view_all_quotations(user):
        return []
    return [str(value) for value in accessible_user_ids(current_app.extensions["store"], user) if value]


def quotation_scope_customer_ids(user: dict[str, Any] | None = None) -> list[str]:
    """Return the customer IDs allowed for quotation history."""
    user = user or current_user() or {}
    if can_view_all_quotations(user):
        return []
    return [str(value) for value in customer_ids_for_user(current_app.extensions["store"], user) if value]


def quotation_is_authorized(quotation: dict[str, Any], user: dict[str, Any] | None = None) -> bool:
    """Apply the same creator/team and customer scope to list and detail routes."""
    user = user or current_user() or {}
    if can_view_all_quotations(user):
        return True
    customer_id = str(quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id") or "")
    if not customer_id or customer_id not in set(quotation_scope_customer_ids(user)):
        return False
    owner_ids = {
        str(quotation.get(field) or "")
        for field in ("created_by_user_id", "user_id", "prepared_by_user_id", "salesperson_id")
        if quotation.get(field)
    }
    # Legacy quotations without a creator snapshot remain visible only when
    # their customer is in the actor's server-side scope.
    return not owner_ids or bool(owner_ids.intersection(quotation_scope_user_ids(user)))


def permitted_quotation_query(user: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the authorized quotation scope before applying user filters."""
    user = user or current_user() or {}
    if can_view_all_quotations(user):
        return {}
    user_ids = quotation_scope_user_ids(user)
    customer_ids = quotation_scope_customer_ids(user)
    creator_clauses = [
        {field: {"$in": user_ids}}
        for field in ("created_by_user_id", "user_id", "prepared_by_user_id", "salesperson_id")
    ]
    # Keep the existing compatibility behavior for legacy rows with no owner
    # snapshot, while still requiring an authorized customer relationship.
    creator_clauses.append({"$and": [
        {"created_by_user_id": {"$exists": False}},
        {"user_id": {"$exists": False}},
        {"prepared_by_user_id": {"$exists": False}},
        {"salesperson_id": {"$exists": False}},
    ]})
    customer_clauses = [
        {field: {"$in": customer_ids}}
        for field in ("customer_id", "customer_company_id", "company_id")
    ]
    if not customer_ids:
        return {"_id": "__no_quotation_access__"}
    return {"$and": [{"$or": creator_clauses}, {"$or": customer_clauses}]}


def permitted_customer_query(user: dict[str, Any] | None = None) -> dict[str, Any]:
    user = user or current_user() or {}
    query: dict[str, Any] = {"active": {"$ne": False}, "status": {"$ne": "archived"}}
    if can_view_all_customers(user):
        return query
    ids = customer_ids_for_user(current_app.extensions["store"], user)
    query["_id"] = {"$in": ids}
    return query


def enforce_customer(customer_id: str | None) -> bool:
    user = current_user()
    if not user or not customer_id:
        return False
    if not customer_record(customer_id):
        return False
    if can_view_all_customers(user):
        return True
    return str(customer_id) in set(customer_ids_for_user(current_app.extensions["store"], user))


def enforce_company(company_id: str | None) -> bool:
    return enforce_customer(company_id)


def enforce_customer_company(customer_company_id: str | None) -> bool:
    """Deprecated alias for canonical customer authorization."""
    return enforce_customer(customer_company_id)


def enforce_active_company(company_id: str | None) -> bool:
    """Legacy alias for active customer-company enforcement."""
    return enforce_active_customer_company(company_id)


def enforce_active_customer_company(customer_company_id: str | None) -> bool:
    """Require permission and the server-side selected customer context."""
    return bool(customer_company_id and selected_customer_id() == customer_company_id and enforce_customer(customer_company_id))


def enforce_active_customer(customer_id: str | None) -> bool:
    return enforce_active_customer_company(customer_id)
