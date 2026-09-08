from __future__ import annotations

from flask import Blueprint, current_app, request, session

from app.api.responses import success
from app.middleware.access import can_view_all_customers, current_user, customer_record, login_required, permitted_customer_query, selected_customer_id
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("system", __name__, url_prefix="/api")


@bp.get("/health")
def health():
    store = current_app.extensions["store"]
    database = store.health()
    zoho = current_app.extensions["zoho_oauth"].status()
    payload = {
        "status": "healthy" if database.get("connected") else "degraded",
        "service": "moneda-api",
        "version": "1.0.0",
        "database": database,
        "rate_limit": {"backend": current_app.config.get("RATE_LIMIT_BACKEND", "memory")},
        "email": {
            "provider": "zoho_mail_api", "configured": zoho["configured"],
            "connected": zoho["connected"], "status": zoho["status"],
        },
    }
    if not database.get("connected"):
        return success(payload, "Moneda API is running with degraded database connectivity", 503)
    return success(payload, "Moneda API is running")


@bp.get("/config")
def public_config():
    settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    brand_name = settings.get("brand_name") or "Moneda Technologies"
    brand_logo_path = settings.get("brand_logo_path") or "/brand/moneda-logo.svg"
    currencies = settings.get("supported_currencies") or ["EUR", "USD", "INR"]
    email_otp_enabled = bool(
        current_app.config.get("DEMO_MODE") or current_app.config.get("TESTING")
        or current_app.extensions["zoho_oauth"].status()["connected"]
    )
    return success({
        # Existing flat keys remain for current clients. The grouped keys make
        # the public contract explicit without exposing private configuration.
        "brand_name": brand_name, "brand_logo_path": brand_logo_path,
        "supported_currencies": currencies,
        "master_currency": settings.get("master_currency", "EUR"), "demo_mode": current_app.config["DEMO_MODE"],
        "app": {"name": brand_name, "logo_path": brand_logo_path},
        "features": {"email_otp": email_otp_enabled, "signup": True, "email_provider": current_app.config.get("EMAIL_PROVIDER", "zoho_mail_api")},
        "currencies": currencies,
    })


@bp.get("/me")
@login_required
def me():
    user = {**(current_user() or {})}
    user.pop("password_hash", None)
    user.pop("currency_preference", None)
    store = current_app.extensions["store"]
    if can_view_all_customers(user):
        customers = [row for row in store.list("customers", permitted_customer_query(user), limit=500, sort="name", direction=1)[0] if not row.get("is_issuer")]
    else:
        customers = [customer_record(customer_id) for customer_id in (user.get("customer_ids") or user.get("customer_company_ids") or user.get("company_ids", []))]
        customers = [customer for customer in customers if customer and not customer.get("is_issuer")]
    customers = [{**customer, "customer_id": customer.get("_id"), "company_name": customer.get("name")} for customer in customers]
    selected = selected_customer_id()
    active = customer_record(selected)
    settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    current_app.logger.info(
        "active_customer_load active_customer_id=%s active_customer_name=%s active_customer_country_code=%s active_customer_display_currency=%s",
        selected or "null", (active or {}).get("name", "unknown"), (active or {}).get("country_code", "unknown"),
        (active or {}).get("preferred_currency") or (active or {}).get("default_currency", "unknown"),
    )
    return success({
        "user": user,
        "customers": customers,
        "companies": customers,
        "customer_companies": customers,
        "active_customer_id": selected,
        "selected_customer_id": selected,
        "selected_customer_company_id": selected,
        "active_company_id": selected,
        "issuer": settings.get("issuer", {"name": "Moneda Technologies", "email": "business@monedatechnologies.com"}),
    })


@bp.patch("/me")
@login_required
def update_profile():
    user = current_user() or {}
    allowed = {"name", "phone", "notification_preferences", "profile_image"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    row = current_app.extensions["store"].update_one("users", {"_id": user["_id"]}, changes)
    row.pop("password_hash", None)
    audit("profile.update", "user", str(user["_id"]), {"fields": sorted(changes)})
    return success(row, "Profile updated")


@bp.get("/notifications")
@login_required
def notifications():
    user = current_user() or {}
    rows, total = current_app.extensions["store"].list("notifications", {"user_id": user["_id"]}, limit=50)
    return success({"items": rows, "unread": len([row for row in rows if not row.get("read")]), "total": total})


@bp.patch("/notifications/<notification_id>")
@login_required
def mark_notification_read(notification_id: str):
    user = current_user() or {}
    store = current_app.extensions["store"]
    row = store.find_one("notifications", {"_id": notification_id, "user_id": user["_id"]})
    if not row:
        return success(message="Notification already cleared")
    updated = store.update_one("notifications", {"_id": notification_id, "user_id": user["_id"]}, {"read": True, "read_at": utcnow()})
    return success(updated, "Notification marked read")


@bp.get("/openapi.json")
def openapi():
    return success({
        "openapi": "3.1.0", "info": {"title": "Moneda Technologies API", "version": "1.0.0"},
        "servers": [{"url": "/api/v1"}],
        "paths": {
            "/auth/login": {"post": {"summary": "Sign in with a username or user id and password"}},
            "/auth/request-otp": {"post": {"summary": "Request email OTP"}},
            "/auth/verify-otp": {"post": {"summary": "Verify email OTP"}},
            "/me": {"get": {"summary": "Current authenticated user"}},
            "/products": {"get": {"summary": "Paginated product catalog"}},
            "/catalog/families": {"get": {"summary": "Three active calculator families"}},
            "/catalog/product-types": {"get": {"summary": "Canonical product type metadata"}},
            "/catalog/blankets/categories": {"get": {"summary": "Seven blanket categories"}},
            "/catalog/blankets/options": {"get": {"summary": "Shared blanket configuration options"}},
            "/catalog/blankets/bars": {"get": {"summary": "Reusable blanket bars with EUR/bar pricing"}},
            "/catalog/blankets/products": {"get": {"summary": "Blanket products filtered by category"}},
            "/catalog/mpacks/types": {"get": {"summary": "Canonical Underpacking types"}},
            "/catalog/mpacks/options": {"get": {"summary": "Shared Underpacking options"}},
            "/catalog/mpacks/products": {"get": {"summary": "Underpacking products"}},
            "/catalog/chemicals/categories": {"get": {"summary": "Chemical categories"}},
            "/catalog/chemicals/options": {"get": {"summary": "Shared chemical options"}},
            "/catalog/chemicals/products": {"get": {"summary": "Chemical products"}},
            "/admin/pricing/products": {"get": {"summary": "Search EUR master pricing"}},
            "/admin/pricing/products/{id}": {"get": {"summary": "Price detail"}, "patch": {"summary": "Edit protected EUR master price"}},
            "/admin/pricing/history/{id}": {"get": {"summary": "Immutable price history"}},
            "/cart/items": {"post": {"summary": "Server-price and add a cart item"}},
            "/quotations": {"get": {"summary": "List quotations"}, "post": {"summary": "Create immutable quotation snapshot"}},
            "/quotations/preview": {"post": {"summary": "Validate and calculate an unsaved quotation preview"}},
            "/exchange-rates": {"get": {"summary": "EUR to USD/INR live or cached rates"}},
        },
    })
