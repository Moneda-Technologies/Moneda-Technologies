from __future__ import annotations

import re
import secrets
from datetime import timedelta

from flask import Blueprint, current_app, request, session
from pydantic import ValidationError
from werkzeug.security import check_password_hash, generate_password_hash

from app.api.responses import failure, success
from app.auth.schemas import OtpRequest, OtpVerify, PasswordLogin, SignupComplete, SignupStart, SignupVerifyEmail
from app.auth.service import OtpError, verify_password
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.extensions import limiter
from app.middleware.access import current_user, login_required
from app.services.audit import audit
from app.repositories.store import ensure_utc, utcnow


bp = Blueprint("auth", __name__, url_prefix="/api/auth")


def _request_id(prefix: str) -> str:
    return f"{prefix}-{utcnow():%Y%m%d}-{secrets.token_hex(3)}"


def _exception_class(exc: BaseException) -> str:
    """Return the dependency exception class without exposing its payload."""
    return type(exc.__cause__ or exc).__name__


def _email_error_message(exc: EmailDeliveryError) -> str:
    if exc.error_code == "OAUTH_NOT_CONNECTED":
        return "Email service is not connected. Please contact the administrator."
    if exc.error_code == "OTP_SENDER_ALIAS_UNAVAILABLE":
        return "OTP email is not ready because its Zoho sender alias is unavailable."
    return "Unable to send verification email."


def _signup_validation_failure(exc: ValidationError, request_id: str):
    messages = {
        ("name", "missing"): "Name is required.",
        ("name", "string_too_short"): "Name must contain at least 2 characters.",
        ("username", "missing"): "Username is required.",
        ("username", "string_too_short"): "Username must contain at least 3 characters.",
        ("username", "string_pattern_mismatch"): "Username may contain only letters, numbers, periods, underscores, and hyphens.",
        ("email", "missing"): "Email address is required.",
    }
    details = []
    for item in exc.errors(include_url=False):
        field = str(item.get("loc", ["request"])[-1])
        error_type = str(item.get("type", "validation_error"))
        if field == "email" and error_type != "missing":
            message = "Enter a valid email address."
        else:
            message = messages.get((field, error_type), str(item.get("msg", "Invalid value.")))
        details.append({"field": field, "message": message, "type": error_type})
    current_app.logger.info("signup_start request_id=%s validation=fail fields=%s", request_id, ",".join(item["field"] for item in details))
    return failure(details[0]["message"] if details else "Signup details are invalid.", details, 422, error="validation_error", request_id=request_id)


def _pending_signup(store, pending_id: str):
    row = store.find_one("pending_signups", {"_id": pending_id})
    expires_at = ensure_utc(row.get("expires_at")) if row else None
    if not row or not expires_at or expires_at <= utcnow():
        if row:
            store.update_one("pending_signups", {"_id": pending_id}, {"expired": True})
        return None
    return row


@bp.post("/login")
@limiter.limit("10 per 15 minutes")
def password_login():
    """Sign in with a username/user id and password."""
    try:
        raw = request.get_json(silent=True)
        raw = raw if isinstance(raw, dict) else {}
        # Accept the explicit API name as well as common form names so older
        # clients can migrate without changing the authentication contract.
        identifier = raw.get("identifier") or raw.get("username") or raw.get("user_id") or raw.get("id")
        payload = PasswordLogin.model_validate({"identifier": identifier, "password": raw.get("password")})
        user = verify_password(current_app.extensions["store"], payload.identifier, payload.password)
        if not user:
            current_app.logger.info("auth session established authenticated=%s method=%s", False, "password")
            audit("auth.login_failed", "user", metadata={"method": "password"})
            return failure("Invalid username or password", status=401)
        session.clear()
        session["user_id"] = user["_id"]
        session["role_id"] = user["role_id"]
        session.permanent = True
        current_app.logger.info("auth session established authenticated=%s method=%s", bool(session.get("user_id")), "password")
        audit("auth.login", "user", str(user["_id"]), {"method": "password"})
        return success({"next_step": "company-selection", "selection_context": "customer"}, "Signed in successfully")
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)


@bp.post("/request-otp")
@limiter.limit("5 per 15 minutes")
def request_otp():
    try:
        payload = OtpRequest.model_validate(request.get_json(silent=True) or {})
        delivered = current_app.extensions["otp_service"].request(str(payload.email), payload.purpose)
        if not delivered:
            return failure("Verification email could not be sent. Please try again.", status=503, error="otp_delivery_failed")
        audit("otp.request", "user", metadata={"purpose": payload.purpose})
        return success(message="If the account is eligible, a verification code has been sent")
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)
    except OtpError as exc:
        return failure(str(exc), status=429)
    except EmailDeliveryError as exc:
        current_app.logger.error(
            "email delivery failed diag=%s type=otp stage=%s provider=zoho_mail_api result=FAIL error_code=%s",
            exc.diagnostic_id, exc.stage, exc.error_code,
        )
        return failure(_email_error_message(exc), status=503, error=exc.error_code, diagnostic_id=exc.diagnostic_id, stage=exc.stage)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("email delivery failed diag=%s type=otp stage=email_service result=FAIL", diagnostic_id)
        return failure("Verification email could not be sent.", status=503, error="otp_delivery_failed", diagnostic_id=diagnostic_id, stage="email_service")


@bp.post("/signup/start")
@limiter.limit("5 per 15 minutes")
def signup_start():
    request_id = _request_id("signup")
    current_app.logger.info(
        "signup_start HTTP request received method=%s path=%s request_id=%s",
        request.method, request.path, request_id,
    )
    raw = request.get_json(silent=True)
    raw = raw if isinstance(raw, dict) else {}
    current_app.logger.info(
        "signup_start request_id=%s name_present=%s username_present=%s email_present=%s",
        request_id, bool(str(raw.get("name", "")).strip()), bool(str(raw.get("username", "")).strip()),
        bool(str(raw.get("email", "")).strip()),
    )
    try:
        payload = SignupStart.model_validate(raw)
    except ValidationError as exc:
        return _signup_validation_failure(exc, request_id)
    current_app.logger.info("[%s] signup stage=validation result=PASS", request_id)
    store = current_app.extensions["store"]
    if payload.previous_pending_signup_id:
        previous = store.find_one("pending_signups", {"_id": payload.previous_pending_signup_id})
        if previous:
            store.update_one("pending_signups", {"_id": previous["_id"]}, {"expired": True, "expires_at": utcnow()})
            previous_challenge = store.find_one("otp_challenges", {"pending_signup_id": previous["_id"], "purpose": "signup", "used": False})
            if previous_challenge:
                store.update_one("otp_challenges", {"_id": previous_challenge["_id"]}, {"used": True})
    email = str(payload.email).lower().strip()
    username = payload.username.strip()
    username_normalized = username.casefold()
    if store.find_one("users", {"email": email}):
        current_app.logger.info("[%s] signup stage=email_check result=FAIL reason=email_in_use", request_id)
        return failure("An account already exists for this email.", status=409, error="email_in_use", request_id=request_id)
    current_app.logger.info("[%s] signup stage=email_check result=PASS", request_id)
    if store.find_one("users", {"username_normalized": username_normalized}) or store.find_one("users", {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}):
        current_app.logger.info("[%s] signup stage=username_check result=FAIL reason=username_in_use", request_id)
        return failure("Username already in use.", status=409, error="username_in_use", request_id=request_id)
    current_app.logger.info("[%s] signup stage=username_check result=PASS", request_id)
    store.delete_one("pending_signups", {"email": email})
    pending = store.insert_one("pending_signups", {
        "name": payload.name.strip(), "username": username, "username_normalized": username_normalized,
        "email": email, "email_verified": False, "expires_at": utcnow() + timedelta(minutes=20),
        "request_id": request_id,
    })
    current_app.logger.info("[%s] signup stage=pending_signup result=PASS", request_id)
    try:
        delivered = current_app.extensions["otp_service"].request(
            email, "signup", request_id=request_id,
            metadata={"pending_signup_id": pending["_id"], "name": payload.name.strip()},
        )
        if not delivered:
            current_app.logger.error("[%s] signup stage=otp_delivery result=FAIL reason=provider_did_not_confirm_delivery", request_id)
            return failure("Unable to send verification email.", status=503, error="otp_delivery_failed", request_id=request_id)
    except OtpError as exc:
        current_app.logger.info("[%s] signup stage=otp_policy result=FAIL exception_class=%s", request_id, type(exc).__name__)
        return failure(str(exc), status=429)
    except EmailDeliveryError as exc:
        current_app.logger.error(
            "[%s] signup_stage=otp_delivery email_stage=%s provider_stage=%s result=FAIL "
            "exception_class=%s error_code=%s diagnostic_id=%s provider=zoho_mail_api",
            request_id, exc.stage, exc.stage, _exception_class(exc), exc.error_code, exc.diagnostic_id,
        )
        return failure(_email_error_message(exc), status=503, error=exc.error_code, diagnostic_id=exc.diagnostic_id, stage=exc.stage, request_id=request_id)
    except Exception as exc:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception(
            "[%s] signup_stage=otp_delivery email_stage=email_service result=FAIL exception_class=%s "
            "error_code=MESSAGE_SUBMISSION_FAILED diagnostic_id=%s",
            request_id, _exception_class(exc), diagnostic_id,
        )
        return failure("Unable to send verification email.", status=503, error="otp_delivery_failed", diagnostic_id=diagnostic_id, stage="email_service", request_id=request_id)
    current_app.logger.info("[%s] signup stage=complete validation=PASS pending_signup=PASS otp_persistence=PASS email_submission=PASS", request_id)
    return success({"pending_signup_id": pending["_id"], "otp_sent": True, "masked_email": f"{email[:1]}***@{email.split('@', 1)[1]}", "request_id": request_id}, "Verification code sent")


@bp.post("/signup/verify-email")
@limiter.limit("10 per 15 minutes")
def signup_verify_email():
    try:
        payload = SignupVerifyEmail.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)
    store = current_app.extensions["store"]
    pending = _pending_signup(store, payload.pending_signup_id)
    if not pending:
        return failure("Your signup session has expired. Please start again.", status=410, error="signup_expired")
    try:
        current_app.extensions["otp_service"].verify(pending["email"], "signup", payload.otp, pending_signup_id=pending["_id"])
    except OtpError as exc:
        return failure(str(exc), status=400)
    store.update_one("pending_signups", {"_id": pending["_id"]}, {"email_verified": True, "verified_at": utcnow()})
    return success({"pending_signup_id": pending["_id"], "email_verified": True}, "Email verified")


@bp.post("/signup/complete")
@limiter.limit("5 per 15 minutes")
def signup_complete():
    try:
        payload = SignupComplete.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)
    if payload.password != payload.confirm_password:
        return failure("Password confirmation does not match.", status=422, error="password_mismatch")
    pending = _pending_signup(current_app.extensions["store"], payload.pending_signup_id)
    if not pending:
        return failure("Your signup session has expired. Please start again.", status=410, error="signup_expired")
    if not pending.get("email_verified"):
        return failure("Verify your email before creating an account.", status=403, error="email_not_verified")
    store = current_app.extensions["store"]
    if store.find_one("users", {"email": pending["email"]}) or store.find_one("users", {"username_normalized": pending["username_normalized"]}):
        return failure("Username or email is already in use.", status=409, error="account_exists")
    document = {
        "email": pending["email"], "name": pending["name"], "username": pending["username"],
        "username_normalized": pending["username_normalized"], "password_hash": generate_password_hash(payload.password),
        "role_id": "user", "customer_ids": [], "customer_company_ids": [], "company_ids": [],
        "active": True, "email_verified": True,
    }
    try:
        user = store.insert_one("users", document)
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            return failure("Username or email is already in use.", status=409, error="account_exists")
        raise
    store.delete_one("pending_signups", {"_id": pending["_id"]})
    audit("user.create", "user", str(user["_id"]))
    return success({"next_step": "login", "user_id": user["_id"]}, "Account created successfully", 201)


@bp.post("/verify-otp")
@limiter.limit("10 per 15 minutes")
def verify_otp():
    try:
        payload = OtpVerify.model_validate(request.get_json(silent=True) or {})
        user = current_app.extensions["otp_service"].verify(str(payload.email), payload.purpose, payload.code)
        if payload.purpose == "login":
            if not user or not user.get("active", False):
                return failure("Account is unavailable", status=403)
            session.clear()
            session["user_id"] = user["_id"]
            session["role_id"] = user["role_id"]
            session.permanent = True
            current_app.logger.info("auth session established authenticated=%s method=%s", bool(session.get("user_id")), "email_otp")
            audit("auth.login", "user", str(user["_id"]), {"method": "email_otp"})
            return success({"next_step": "company-selection", "selection_context": "customer"}, "Signed in successfully")
        session[f"verified_{payload.purpose}_email"] = str(payload.email).lower()
        return success({"next_step": "profile" if payload.purpose == "signup" else "new_password"})
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)
    except OtpError as exc:
        return failure(str(exc), status=400)


@bp.post("/register")
def register():
    """Retained as a compatibility route, but cannot bypass pending signup."""
    return failure("Use the email verification signup flow before creating an account.", status=410, error="signup_flow_required")


@bp.post("/reset-password")
def reset_password():
    email = session.get("verified_reset_email")
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password", ""))
    if not email:
        return failure("Verify your email before resetting the password", status=403)
    if len(password) < 10:
        return failure("Password must contain at least 10 characters", status=422)
    user = current_app.extensions["store"].update_one("users", {"email": email}, {"password_hash": generate_password_hash(password)})
    if not user:
        return failure("Account is unavailable", status=404)
    session.clear()
    audit("auth.password_reset", "user", str(user["_id"]))
    return success(message="Password changed successfully")


@bp.post("/change-password")
@login_required
def change_password():
    user = current_user() or {}
    payload = request.get_json(silent=True) or {}
    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("new_password", ""))
    if not check_password_hash(str(user.get("password_hash", "")), current_password):
        return failure("Current password is incorrect", status=422)
    if len(new_password) < 10:
        return failure("Password must contain at least 10 characters", status=422)
    if new_password == current_password:
        return failure("New password must be different", status=422)
    current_app.extensions["store"].update_one("users", {"_id": user["_id"]}, {"password_hash": generate_password_hash(new_password)})
    audit("auth.password_change", "user", str(user["_id"]))
    return success(message="Password changed successfully")


@bp.post("/demo")
def demo_login():
    if not current_app.config["DEMO_MODE"]:
        return failure("Demo access is disabled", status=404)
    session.clear()
    session["user_id"] = "user-demo-admin"
    session["role_id"] = "superadmin"
    session.permanent = True
    current_app.logger.info("auth session established authenticated=%s method=%s", bool(session.get("user_id")), "demo")
    return success({"next_step": "company-selection", "selection_context": "customer"}, "Customer selection ready")


@bp.post("/logout")
@login_required
def logout():
    user = current_user()
    audit("auth.logout", "user", str(user["_id"]) if user else None)
    session.clear()
    current_app.logger.info("auth session established authenticated=%s method=%s", False, "logout")
    return success(message="Signed out")
