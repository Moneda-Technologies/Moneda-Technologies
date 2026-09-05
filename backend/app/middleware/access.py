from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from flask import current_app, g, session

from app.api.responses import failure


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


def login_required(fn: F) -> F:
    @wraps(fn)
    def wrapped(*args: Any, **kwargs: Any):
        if not current_user():
            return failure("Authentication required", status=401)
        return fn(*args, **kwargs)
    return wrapped  # type: ignore[return-value]


def permission_required(permission: str) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any):
            user = current_user()
            if not user:
                return failure("Authentication required", status=401)
            if permission not in user.get("permissions", []):
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


def enforce_customer(customer_id: str | None) -> bool:
    user = current_user()
    if not user or not customer_id:
        return False
    if not customer_record(customer_id):
        return False
    if user.get("role_id") == "superadmin":
        return True
    permitted = user.get("customer_ids") or user.get("customer_company_ids") or user.get("company_ids", [])
    return customer_id in permitted


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
