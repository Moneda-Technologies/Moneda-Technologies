from __future__ import annotations

from flask import Blueprint, current_app, request, session
from pydantic import ValidationError
from werkzeug.security import check_password_hash, generate_password_hash

from app.api.responses import failure, success
from app.auth.schemas import OtpRequest, OtpVerify, PasswordLogin, Registration
from app.auth.service import OtpError, verify_password
from app.extensions import limiter
from app.middleware.access import current_user, login_required
from app.services.audit import audit


bp = Blueprint("auth", __name__, url_prefix="/api/auth")


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
        current_app.extensions["otp_service"].request(str(payload.email), payload.purpose)
        audit("otp.request", "user", metadata={"purpose": payload.purpose})
        return success(message="If the account is eligible, a verification code has been sent")
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)
    except OtpError as exc:
        return failure(str(exc), status=429)
    except Exception:
        return failure("Verification email could not be sent. Check email provider configuration.", status=503)


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
    email = session.get("verified_signup_email")
    if not email:
        return failure("Verify your email before registration", status=403)
    try:
        payload = Registration.model_validate(request.get_json(silent=True) or {})
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)
    store = current_app.extensions["store"]
    if store.find_one("users", {"email": email}):
        return failure("Account already exists", status=409)
    user = store.insert_one("users", {
        "email": email, "name": payload.name.strip(), "phone": payload.phone.strip(),
        "password_hash": generate_password_hash(payload.password), "role_id": "user",
        "customer_ids": [], "customer_company_ids": [], "company_ids": [], "active": True, "currency_preference": "EUR",
    })
    session.clear()
    session["user_id"] = user["_id"]
    session["role_id"] = user["role_id"]
    current_app.logger.info("auth session established authenticated=%s method=%s", bool(session.get("user_id")), "registration")
    audit("user.create", "user", str(user["_id"]))
    return success({"next_step": "company-selection", "selection_context": "customer"}, "Account created", 201)


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
