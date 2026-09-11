from __future__ import annotations

from uuid import UUID

from flask import Blueprint, current_app, request, session
from pydantic import ValidationError

from app.api.responses import failure, success
from app.auth.policy import SIGNUP_EMAIL_DOMAIN_MESSAGE, is_allowed_signup_email, normalize_signup_email
from app.auth.schemas import EmailChangeRequest, EmailChangeVerify
from app.auth.service import OtpError
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.customers.metadata import phone_is_valid
from app.middleware.access import current_user, customer_access_summary, customer_record, login_required, permitted_customer_query, selected_customer_id
from app.devices.service import device_access_status
from app.repositories.store import ensure_utc, utcnow
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
        "signup_email_domains": sorted(current_app.config.get("ALLOWED_SIGNUP_EMAIL_DOMAINS", ("monedatechnologies.com", "chemo.in"))),
        "currencies": currencies,
    })


@bp.get("/me")
@login_required(allow_pending=True)
def me():
    user_record = current_user() or {}
    device_status = device_access_status(user_record)
    if not device_status.get("application_access"):
        # Bootstrap is intentionally the only authenticated response exposed
        # to a pending device; never include customers or cached app data.
        return success({"user": {"_id": user_record.get("_id"), "name": user_record.get("name"), "role_id": user_record.get("role_id")}, "customers": [], "companies": [], "customer_companies": [], "device_access": device_status, "application_access": False})
    user = {**user_record}
    for field in ("password_hash", "pending_email_verification_id", "pending_email_verification_token_hash", "pending_email_verification_attempts"):
        user.pop(field, None)
    user.pop("currency_preference", None)
    store = current_app.extensions["store"]
    access = customer_access_summary(user)
    if access["global"]:
        customers = [row for row in store.list("customers", permitted_customer_query(user), limit=500, sort="name", direction=1)[0] if not row.get("is_issuer")]
    else:
        customers = [customer_record(customer_id) for customer_id in access["customer_ids"]]
        customers = [customer for customer in customers if customer and not customer.get("is_issuer")]
    user["customer_access_global"] = access["global"]
    user["assigned_customer_ids"] = access["customer_ids"]
    user["customer_access_count"] = access["count"]
    customers = [{**customer, "customer_id": customer.get("_id"), "company_name": customer.get("name")} for customer in customers]
    selected = selected_customer_id()
    if selected and not access["global"] and selected not in access["customer_ids"]:
        for key in ("active_customer_id", "selected_customer_company_id", "active_company_id"):
            session.pop(key, None)
        selected = None
    active = customer_record(selected)
    settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    if "watermark_enabled" not in settings:
        # Backfill the default for deployments whose settings document predates
        # this feature; the server remains the source of truth.
        current_app.extensions["store"].update_one("app_settings", {"_id": "system"}, {"watermark_enabled": True})
        settings["watermark_enabled"] = True
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
        "application_access": True,
        "device_access": device_status,
        "watermark_enabled": bool(settings.get("watermark_enabled", True)),
        "issuer": settings.get("issuer", {"name": "Moneda Technologies", "email": "business@monedatechnologies.com"}),
    })


@bp.get("/me/device-access")
@login_required(allow_pending=True)
def device_access():
    return success(device_access_status(current_user()))


@bp.patch("/me")
@login_required
def update_profile():
    user = current_user() or {}
    allowed = {"name", "phone", "notification_preferences", "profile_image"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    if "name" in changes and len(str(changes["name"]).strip()) < 2:
        return failure("Full name must contain at least 2 characters", status=422)
    if "phone" in changes and not phone_is_valid(str(changes["phone"]).strip()):
        return failure("Enter a valid phone number", status=422)
    row = current_app.extensions["store"].update_one("users", {"_id": user["_id"]}, changes)
    if not row:
        return failure("Profile could not be updated", status=404)
    row = _safe_profile_user(row)
    audit("profile.update", "user", str(user["_id"]), {"fields": sorted(changes)})
    return success(row, "Profile updated")


_EMAIL_CHANGE_FIELDS = [
    "pending_email", "pending_email_verification_id", "pending_email_verification_expires_at",
    "pending_email_verification_attempts", "pending_email_verification_token_hash",
]


def _invalidate_email_change_challenges(store, user_id: str) -> None:
    rows, _ = store.list("otp_challenges", {"purpose": "email_change", "user_id": user_id, "used": False}, limit=100)
    for row in rows:
        store.update_one("otp_challenges", {"_id": row["_id"]}, {"used": True})


def _safe_profile_user(row: dict) -> dict:
    result = {**row}
    for field in ("password_hash", "pending_email_verification_id", "pending_email_verification_token_hash", "pending_email_verification_attempts"):
        result.pop(field, None)
    return result


def _email_change_error(exc: EmailDeliveryError) -> str:
    if exc.error_code == "OAUTH_NOT_CONNECTED":
        return "Email service is not connected. Please contact the administrator."
    if exc.error_code == "OTP_SENDER_ALIAS_UNAVAILABLE":
        return "OTP email is not ready because its Zoho sender alias is unavailable."
    return "Unable to send verification email."


def _start_email_change(user: dict, email: str, *, request_id: str, resend: bool = False):
    store = current_app.extensions["store"]
    normalized = normalize_signup_email(email)
    if not normalized:
        return failure("Enter a valid email address.", status=422, error="invalid_email")
    if not is_allowed_signup_email(normalized, current_app.config.get("ALLOWED_SIGNUP_EMAIL_DOMAINS")):
        return failure(SIGNUP_EMAIL_DOMAIN_MESSAGE, status=422, error="email_domain_not_allowed")
    current = normalize_signup_email(user.get("email")) or str(user.get("email") or "").strip().lower()
    if normalized == current:
        return failure("That is already your current email address.", status=422, error="email_unchanged")
    duplicate = store.find_one("users", {"email": normalized})
    if duplicate and str(duplicate.get("_id")) != str(user.get("_id")):
        return failure("That email address is already associated with another account.", status=409, error="email_in_use")

    # A new address invalidates any earlier transaction. For resend, leave the
    # active challenge in place so OtpService enforces its resend cooldown and
    # replaces it only after a permitted request.
    if not resend:
        _invalidate_email_change_challenges(store, str(user["_id"]))
    try:
        challenge_result = current_app.extensions["otp_service"].request(
            normalized, "email_change", request_id=request_id, return_challenge=True,
            metadata={"user_id": user["_id"], "name": user.get("name", "there")},
        )
        challenge = challenge_result if isinstance(challenge_result, dict) else store.find_one(
            "otp_challenges", {"email": normalized, "purpose": "email_change", "user_id": user["_id"], "used": False},
        )
        if not challenge:
            current_app.logger.error("email_change stage=challenge_persistence result=FAIL request_id=%s", request_id)
            return failure("Email verification could not be started. Please try again.", status=500, error="email_change_persistence_failed")
        updated = store.update_one("users", {"_id": user["_id"]}, {
            "pending_email": normalized,
            "pending_email_verification_id": challenge["_id"],
            "pending_email_verification_expires_at": challenge["expires_at"],
            "pending_email_verification_attempts": 0,
        })
        if not updated:
            store.update_one("otp_challenges", {"_id": challenge["_id"]}, {"used": True})
            return failure("Email verification could not be started. Please try again.", status=500, error="email_change_persistence_failed")
        current_app.logger.info("email_change stage=request result=PASS request_id=%s resend=%s", request_id, resend)
        return success({"pending_email": normalized, "expires_at": challenge["expires_at"]}, "Verification code sent")
    except OtpError as exc:
        return failure(str(exc), status=429, error="otp_policy")
    except EmailDeliveryError as exc:
        current_app.logger.error("email_change stage=otp_delivery result=FAIL request_id=%s error_code=%s diagnostic_id=%s", request_id, exc.error_code, exc.diagnostic_id)
        return failure(_email_change_error(exc), status=503, error=exc.error_code, diagnostic_id=exc.diagnostic_id, stage=exc.stage)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("email_change stage=otp_delivery result=FAIL request_id=%s diagnostic_id=%s", request_id, diagnostic_id)
        return failure("Unable to send verification email.", status=503, error="otp_delivery_failed", diagnostic_id=diagnostic_id)


@bp.post("/profile/email-change/request")
@login_required
def request_email_change():
    try:
        payload = EmailChangeRequest.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return failure("Enter a valid email address.", exc.errors(include_url=False), status=422, error="invalid_email")
    return _start_email_change(current_user() or {}, str(payload.email), request_id=f"email-change-{utcnow():%Y%m%d}-{email_diagnostic_id()[-6:]}")


@bp.post("/profile/email-change/resend")
@login_required
def resend_email_change():
    user = current_user() or {}
    pending = normalize_signup_email(user.get("pending_email"))
    if not pending:
        return failure("There is no pending email change to resend.", status=409, error="email_change_not_pending")
    return _start_email_change(user, pending, resend=True, request_id=f"email-change-resend-{utcnow():%Y%m%d}-{email_diagnostic_id()[-6:]}")


@bp.post("/profile/email-change/verify")
@login_required
def verify_email_change():
    try:
        payload = EmailChangeVerify.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return failure("Enter the six-digit verification code.", exc.errors(include_url=False), status=422, error="invalid_otp")
    user = current_user() or {}
    pending = normalize_signup_email(user.get("pending_email"))
    challenge_id = user.get("pending_email_verification_id")
    if not pending or not challenge_id:
        return failure("There is no pending email change to verify.", status=409, error="email_change_not_pending")
    expires_at = ensure_utc(user.get("pending_email_verification_expires_at"))
    store = current_app.extensions["store"]
    if not expires_at or expires_at <= utcnow():
        _invalidate_email_change_challenges(store, str(user["_id"]))
        store.unset_many("users", {"_id": user["_id"]}, _EMAIL_CHANGE_FIELDS)
        return failure("This verification code has expired. Request a new code.", status=410, error="email_change_expired")
    duplicate = store.find_one("users", {"email": pending})
    if duplicate and str(duplicate.get("_id")) != str(user["_id"]):
        return failure("That email address is already associated with another account.", status=409, error="email_in_use")
    try:
        current_app.extensions["otp_service"].verify_challenge(
            pending, "email_change", payload.code,
            extra_query={"_id": challenge_id, "user_id": user["_id"]},
        )
    except OtpError as exc:
        return failure(str(exc), status=400, error="email_change_otp_invalid")
    try:
        updated = store.update_one("users", {"_id": user["_id"], "pending_email_verification_id": challenge_id}, {
            "email": pending, "email_verified": True,
        }, unset_fields=_EMAIL_CHANGE_FIELDS)
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            return failure("That email address is already associated with another account.", status=409, error="email_in_use")
        raise
    if not updated:
        return failure("This email change is no longer pending. Request a new code.", status=409, error="email_change_not_pending")
    audit("profile.email_change", "user", str(user["_id"]), {"email_verified": True})
    current_app.logger.info("email_change stage=verification result=PASS user_id=%s", user["_id"])
    return success(_safe_profile_user(updated), "Email address updated")


@bp.post("/profile/email-change/cancel")
@login_required
def cancel_email_change():
    user = current_user() or {}
    store = current_app.extensions["store"]
    _invalidate_email_change_challenges(store, str(user["_id"]))
    updated = store.unset_many("users", {"_id": user["_id"]}, _EMAIL_CHANGE_FIELDS)
    audit("profile.email_change_cancel", "user", str(user["_id"]), {"fields_cleared": bool(updated)})
    current = store.find_one("users", {"_id": user["_id"]}) or user
    return success(_safe_profile_user(current), "Email change cancelled")


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


@bp.delete("/notifications/<notification_id>")
@login_required
def delete_notification(notification_id: str):
    """Remove one notification belonging to the authenticated user."""
    try:
        UUID(notification_id)
    except (ValueError, AttributeError, TypeError):
        return failure("Invalid notification id", status=400)
    user = current_user() or {}
    store = current_app.extensions["store"]
    row = store.find_one("notifications", {"_id": notification_id})
    if not row:
        return failure("Notification not found", status=404)
    if str(row.get("user_id")) != str(user.get("_id")):
        return failure("You are not allowed to delete this notification", status=403)
    removed = store.delete_one("notifications", {"_id": notification_id, "user_id": user["_id"]})
    if not removed:
        return failure("Notification not found", status=404)
    return success({"removed": True}, "Notification deleted")


@bp.delete("/notifications")
@login_required
def delete_notifications():
    """Remove all notifications owned by the authenticated user in one operation."""
    user = current_user() or {}
    store = current_app.extensions["store"]
    rows, _ = store.list("notifications", {"user_id": user["_id"]}, limit=100_000)
    removed = sum(1 for row in rows if store.delete_one("notifications", {"_id": row["_id"], "user_id": user["_id"]}))
    return success({"removed": removed}, "Notifications deleted")


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
            "/exchange-rates": {"get": {"summary": "EUR master to USD/INR ECB reference rates"}},
        },
    })
