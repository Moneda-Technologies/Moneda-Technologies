from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import current_app, g, request, session

from app.api.responses import failure
from app.devices.service import enforce_device_access


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
            if permission not in user.get("permissions", []):
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
    return bool({"admin", "superadmin"}.intersection({str(user.get("role_id") or "")})) or "customers.view_all" in user.get("permissions", [])


def customer_access_ids_for_user(user_id: str | None, *, include_created: bool = True) -> list[str]:
    """Return canonical customer IDs accessible to one non-global user.

    ``customers.assigned_user_ids`` is the authoritative relationship. The
    creator relationship is included so an existing owner cannot lose access
    merely because an older record predates the assignment array.
    """
    if not user_id:
        return []
    store = current_app.extensions["store"]
    clauses: list[dict[str, Any]] = [{"assigned_user_ids": user_id}]
    if include_created:
        clauses.append({"created_by_user_id": user_id})
    rows, _ = store.list("customers", {
        "active": {"$ne": False}, "status": {"$ne": "archived"}, "$or": clauses,
    }, limit=100_000, sort="name", direction=1)
    return list(dict.fromkeys(str(row["_id"]) for row in rows if row.get("_id") and not row.get("is_issuer")))


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
    return bool({"admin", "superadmin"}.intersection({str(user.get("role_id") or "")})) or "quotations.view_all" in user.get("permissions", [])


def permitted_quotation_query(user: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the authorized quotation scope before applying user filters."""
    user = user or current_user() or {}
    if can_view_all_quotations(user):
        return {}
    user_id = user.get("_id")
    return {"$or": [
        {"created_by_user_id": user_id},
        {"user_id": user_id},
        {"prepared_by_user_id": user_id},
        {"salesperson_id": user_id},
    ]}


def permitted_customer_query(user: dict[str, Any] | None = None) -> dict[str, Any]:
    user = user or current_user() or {}
    query: dict[str, Any] = {"active": {"$ne": False}, "status": {"$ne": "archived"}}
    if can_view_all_customers(user):
        return query
    user_id = user.get("_id")
    query["$or"] = [{"assigned_user_ids": user_id}, {"created_by_user_id": user_id}]
    return query


def enforce_customer(customer_id: str | None) -> bool:
    user = current_user()
    if not user or not customer_id:
        return False
    if not customer_record(customer_id):
        return False
    if can_view_all_customers(user):
        return True
    customer = customer_record(customer_id) or {}
    assigned = {str(value) for value in (customer.get("assigned_user_ids") or []) if value}
    return str(user.get("_id") or "") in assigned or str(customer.get("created_by_user_id") or "") == str(user.get("_id") or "")


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
