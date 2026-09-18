from __future__ import annotations

import re
import secrets
import hashlib
import hmac
from datetime import timedelta

from flask import Blueprint, current_app, g, request, session
from markupsafe import escape
from pydantic import ValidationError
from werkzeug.security import check_password_hash, generate_password_hash

from app.api.responses import failure, success
from app.auth.schemas import OtpRequest, OtpVerify, PasswordLogin, SignupComplete, SignupStart, SignupVerifyEmail
from app.auth.policy import (
    SIGNUP_EMAIL_DOMAIN_MESSAGE,
    SIGNUP_PASSWORD_POLICY_MESSAGE,
    is_allowed_signup_email,
    is_valid_signup_password,
    normalize_signup_email,
)
from app.auth.service import OtpError, verify_password
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.extensions import limiter
from app.middleware.access import current_user, login_required
from app.devices.service import establish_device_session, notify_login_attempt, _device_from_request
from app.services.audit import audit
from app.repositories.store import ensure_utc, utcnow


bp = Blueprint("auth", __name__, url_prefix="/api/auth")

# Email OTP is a recovery/login path reserved for the Superadmin account. Keep
# this wording shared by the request and verification guards so the browser
# cannot turn a forbidden request into an OTP challenge.
SUPERADMIN_OTP_ONLY_MESSAGE = (
    "Email OTP login is available only for Superadmin accounts. "
    "Please contact your Superadmin to change or reset your password."
)


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


def _establish_session(user: dict[str, object], *, method: str, event: str = "session_created") -> dict[str, object]:
    """Create the one authoritative persistent Flask session for a user."""
    prior_device = _device_from_request(user)
    prior_status = str((prior_device or {}).get("device_status") or "")
    session.clear()
    session["user_id"] = user["_id"]
    session["role_id"] = user["role_id"]
    session.permanent = True
    g.auth_session_event = event
    current_app.logger.info(
        "session_created user_id=%s method=%s lifetime_days=%s",
        user["_id"], method, current_app.config.get("AUTH_SESSION_LIFETIME_DAYS", 30),
    )
    if event != "session_created":
        current_app.logger.info("%s user_id=%s method=%s", event, user["_id"], method)
    if str(user.get("role_id") or "") == "superadmin":
        audit("superadmin_login", "user", str(user.get("_id")), {"method": method})
    device = establish_device_session(user)
    # This is called only after password/OTP authentication succeeds. The
    # device status is captured before denied/revoked credentials are rotated,
    # so Superadmins receive an accurate security event for that attempt. Pass
    # the current device record so a pending approval can be recognized and
    # does not also receive the generic login email.
    notify_login_attempt(user, device, prior_status or str(device.get("device_status") or "pending"))
    return device


def _session_result(device: dict[str, object], selection: str = "company-selection") -> dict[str, object]:
    if device.get("device_status") != "approved":
        return {"next_step": "device-approval-pending", "device_status": device.get("device_status"), "application_access": False}
    return {"next_step": selection, "selection_context": "customer", "application_access": True}


def _emergency_key_matches(value: str) -> bool:
    configured_hash = str(current_app.config.get("SUPERADMIN_EMERGENCY_KEY_HASH") or "").strip().lower()
    configured_key = str(current_app.config.get("SUPERADMIN_EMERGENCY_KEY") or "")
    if configured_hash:
        return hmac.compare_digest(hashlib.sha256(value.encode("utf-8")).hexdigest(), configured_hash)
    return bool(configured_key) and hmac.compare_digest(value, configured_key)


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


def _signup_complete_validation_failure(exc: ValidationError, request_id: str):
    """Return safe, field-level completion errors without exposing input values."""
    details = []
    for item in exc.errors(include_url=False):
        field = str(item.get("loc", ["request"])[-1])
        error_type = str(item.get("type", "validation_error"))
        if field == "password":
            message = "Password is required." if error_type == "missing" else "Password must be at least 8 characters."
        elif field == "confirm_password":
            message = "Password confirmation is required." if error_type == "missing" else "Password confirmation must be at least 8 characters."
        elif field == "pending_signup_id":
            message = "Your signup session is required. Please start again."
        else:
            message = "The signup details are invalid."
        details.append({"field": field, "message": message, "type": error_type})
    current_app.logger.info(
        "signup_complete stage=request_validation result=FAIL request_id=%s error_code=SIGNUP_COMPLETE_VALIDATION_ERROR fields=%s",
        request_id,
        ",".join(item["field"] for item in details) or "request",
    )
    return failure(
        details[0]["message"] if details else "The signup details are invalid.",
        details,
        422,
        error="validation_error",
        error_code="SIGNUP_COMPLETE_VALIDATION_ERROR",
        stage="request_validation",
        request_id=request_id,
    )


def _pending_signup(store, pending_id: str):
    row = store.find_one("pending_signups", {"_id": pending_id})
    expires_at = ensure_utc(row.get("expires_at")) if row else None
    if not row or row.get("consumed") or not expires_at or expires_at <= utcnow():
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
        # Password authentication is a complete login method. Do not require
        # an email address or silently convert a successful password login
        # into an OTP challenge; email OTP is an explicit alternative exposed
        # by /request-otp and /verify-otp.
        device = _establish_session(user, method="password")
        audit("auth.login", "user", str(user["_id"]), {
            "method": "password",
            "role": str(user.get("role_id") or "unknown"),
        })
        return success(
            _session_result(device),
            "Signed in successfully" if device.get("application_access") else "Device approval pending",
        )
    except ValidationError as exc:
        return failure("Validation failed", exc.errors(include_url=False), 422)


@bp.post("/superadmin/emergency")
@limiter.limit("5 per 15 minutes")
def superadmin_emergency_login():
    """Optional server-only recovery login, still requiring the real password."""
    raw = request.get_json(silent=True) or {}
    identifier = raw.get("identifier") or raw.get("username") or raw.get("user_id")
    password = str(raw.get("password") or "")
    emergency_key = str(raw.get("emergency_key") or "")
    store = current_app.extensions["store"]
    user = verify_password(store, str(identifier or ""), password) if identifier and password else None
    enabled = bool(current_app.config.get("SUPERADMIN_EMERGENCY_ACCESS_ENABLED"))
    if not enabled or not user or str(user.get("role_id")) != "superadmin" or not user.get("active", False) or not _emergency_key_matches(emergency_key):
        current_app.logger.warning("superadmin_emergency_access result=FAIL authenticated_superadmin=%s enabled=%s", bool(user and user.get("role_id") == "superadmin"), enabled)
        audit("superadmin_emergency_access_failed", "user", str(user.get("_id")) if user and user.get("role_id") == "superadmin" else None)
        return failure("Emergency access was not accepted", status=401)
    device = _establish_session(user, method="superadmin_emergency", event="superadmin_login")
    audit("superadmin_emergency_access_used", "user", str(user["_id"]))
    return success(_session_result(device), "Signed in successfully")


@bp.post("/request-otp")
@limiter.limit("5 per 15 minutes")
def request_otp():
    try:
        payload = OtpRequest.model_validate(request.get_json(silent=True) or {})
        if payload.purpose == "signup" and not is_allowed_signup_email(payload.email, current_app.config.get("ALLOWED_SIGNUP_EMAIL_DOMAINS")):
            return failure(SIGNUP_EMAIL_DOMAIN_MESSAGE, status=422, error="signup_email_domain_not_allowed")
        if payload.purpose == "reset":
            # Password recovery is deliberately a Superadmin-only flow.  Do
            # not create an OTP challenge for ordinary accounts.  The public
            # response remains generic so an email probe cannot enumerate
            # accounts; a known non-Superadmin request is audited and routed
            # to the administrators through the existing server email path.
            store = current_app.extensions["store"]
            target = store.find_one("users", {"email": str(payload.email).strip().lower(), "active": {"$ne": False}})
            if not target:
                return success({"next_step": "contact_superadmin"}, message="Please contact your Superadmin to update your password.")
            if str(target.get("role_id") or "") != "superadmin":
                actor_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",", 1)[0].strip()
                requested_at = utcnow()
                target_id = str(target.get("_id") or "unknown")
                target_name = str(target.get("name") or "A user")
                target_email = str(target.get("email") or "")
                target_username = str(target.get("username") or target.get("username_normalized") or "")
                target_role = str(target.get("role_id") or "unknown")
                audit("auth.password_reset.blocked", "user", target_id, {
                    "reason": "superadmin_required", "source": "forgot_password",
                    "target_username": target_username, "target_email": target_email,
                    "target_role": target_role, "requested_at": requested_at,
                })
                current_app.logger.info("password_reset_request blocked=true target_role=non_superadmin ip_present=%s", bool(actor_ip))
                admins, _ = store.list("users", {"role_id": "superadmin", "active": True}, limit=100)
                recipients = [str(row.get("email") or "").strip().lower() for row in admins if "@" in str(row.get("email") or "")]
                for admin in admins:
                    if admin.get("_id"):
                        store.insert_one("notifications", {
                            "user_id": admin["_id"], "type": "password_reset_requested",
                            "title": "Password update requested",
                            "message": f"{target_name} ({target_role}) requested a password update · Superadmin action required",
                            "read": False, "created_at": requested_at,
                        })
                if recipients:
                    try:
                        current_app.extensions["email_service"].send(
                            to=recipients,
                            subject="Moneda Technologies — Password update requested",
                            html=("<h2>Password update requested</h2>"
                                  f"<p><strong>Name:</strong> {escape(target_name)}<br>"
                                  f"<strong>User ID:</strong> {escape(target_id)}<br>"
                                  f"<strong>Username:</strong> {escape(target_username or '—')}<br>"
                                  f"<strong>Email:</strong> {escape(target_email)}<br>"
                                  f"<strong>Role:</strong> {escape(target_role)}<br>"
                                  f"<strong>Requested:</strong> {escape(requested_at.isoformat())}<br>"
                                  "<strong>Source:</strong> Forgot Password</p>"
                                  "<p>Use the Users &amp; Access screen to set the new password. "
                                  "No reset token was issued to this account.</p>"),
                            request_id=_request_id("password-admin"),
                            purpose="general",
                        )
                    except Exception:
                        current_app.logger.exception("password_reset_admin_notification result=FAIL")
                return success({"next_step": "contact_superadmin"}, message="Please contact your Superadmin to update your password.")
        if payload.purpose == "login":
            store = current_app.extensions["store"]
            normalized_email = str(payload.email).strip().lower()
            target = store.find_one("users", {"email": normalized_email, "active": {"$ne": False}})
            if target and str(target.get("role_id") or "") != "superadmin":
                # This guard intentionally runs before OtpService.request so
                # no challenge, email, or pending-login session is created.
                audit("auth.login_otp.blocked", "user", str(target.get("_id") or "unknown"), {"reason": "superadmin_required"})
                session.pop("pending_login_user_id", None)
                session.pop("pending_login_email", None)
                session.pop("pending_superadmin_login_user_id", None)
                session.pop("pending_superadmin_login_email", None)
                return failure(SUPERADMIN_OTP_ONLY_MESSAGE, status=403, error="superadmin_otp_required")
            # Email OTP is a separate, explicitly selected login method. Keep
            # the account identity in the server session while the code is
            # pending; never trust a browser-provided user id at verification.
            session.clear()
            if target:
                session["pending_login_user_id"] = str(target["_id"])
                session["pending_login_email"] = normalized_email
                # Retain the legacy keys for one release so an in-flight
                # browser can finish a challenge started by an older build.
                session["pending_superadmin_login_user_id"] = str(target["_id"])
                session["pending_superadmin_login_email"] = normalized_email
                session.permanent = True
            else:
                # Do not reveal whether an address is registered. The UI can
                # proceed to its verification screen and the verifier will
                # reject an unknown address without creating a session.
                audit("auth.login_otp.requested", "user", metadata={"known_account": False})
                return success(message="If the account is eligible, a verification code has been sent")
        delivered = current_app.extensions["otp_service"].request(str(payload.email), payload.purpose)
        if not delivered:
            if payload.purpose == "login":
                session.clear()
            return failure("Verification email could not be sent. Please try again.", status=503, error="otp_delivery_failed")
        audit("otp.request", "user", metadata={"purpose": payload.purpose})
        if payload.purpose == "reset":
            return success({"next_step": "verify_code"}, message="If the account is eligible, a verification code has been sent")
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
    email = normalize_signup_email(payload.email)
    if not email or not is_allowed_signup_email(email, current_app.config.get("ALLOWED_SIGNUP_EMAIL_DOMAINS")):
        current_app.logger.info("[%s] signup stage=email_domain result=FAIL reason=domain_not_allowed", request_id)
        return failure(SIGNUP_EMAIL_DOMAIN_MESSAGE, status=422, error="signup_email_domain_not_allowed", request_id=request_id)
    if payload.previous_pending_signup_id:
        previous = store.find_one("pending_signups", {"_id": payload.previous_pending_signup_id})
        if previous:
            store.update_one("pending_signups", {"_id": previous["_id"]}, {"expired": True, "expires_at": utcnow()})
            previous_challenge = store.find_one("otp_challenges", {"pending_signup_id": previous["_id"], "purpose": "signup", "used": False})
            if previous_challenge:
                store.update_one("otp_challenges", {"_id": previous_challenge["_id"]}, {"used": True})
    username = payload.username.strip()
    username_normalized = username.casefold()
    if store.find_one("users", {"email": email}):
        current_app.logger.info("[%s] signup stage=email_check result=FAIL reason=email_in_use", request_id)
        return failure("An account already exists for this email.", status=409, error="email_in_use", request_id=request_id)
    current_app.logger.info("[%s] signup stage=email_check result=PASS", request_id)
    if store.find_one("users", {"username_normalized": username_normalized}) or store.find_one("users", {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}}):
        current_app.logger.info("[%s] signup stage=username_check result=FAIL reason=username_in_use", request_id)
        return failure("That username is already taken. Please choose another username.", status=409, error="username_in_use", request_id=request_id)
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
        current_app.logger.info("signup_verify_email stage=pending_signup_lookup result=FAIL reason=expired_or_consumed")
        return failure("Your signup session has expired. Please start again.", status=410, error="signup_expired")
    try:
        current_app.extensions["otp_service"].verify(pending["email"], "signup", payload.otp, pending_signup_id=pending["_id"])
    except OtpError as exc:
        current_app.logger.info("signup_verify_email stage=otp_verification result=FAIL reason=invalid_or_expired")
        return failure(str(exc), status=400)
    updated = store.update_one("pending_signups", {"_id": pending["_id"]}, {"email_verified": True, "verified_at": utcnow()})
    if not updated:
        current_app.logger.error("signup_verify_email stage=otp_verification_state result=FAIL reason=persistence")
        return failure("Email verification could not be saved. Please try again.", status=500, error="signup_state_persistence_failed")
    current_app.logger.info("signup_verify_email stage=otp_verification_state result=PASS email_verified=true")
    return success({"pending_signup_id": pending["_id"], "email_verified": True}, "Email verified")


@bp.post("/signup/complete")
@limiter.limit("5 per 15 minutes")
def signup_complete():
    request_id = _request_id("signup-complete")
    raw = request.get_json(silent=True)
    raw = raw if isinstance(raw, dict) else {}
    current_app.logger.info(
        "signup_complete request_received request_id=%s content_type=%s pending_signup_id_present=%s "
        "password_present=%s confirm_password_present=%s fields=%s",
        request_id,
        request.content_type or "none",
        bool(str(raw.get("pending_signup_id", "")).strip()),
        bool(raw.get("password")),
        bool(raw.get("confirm_password")),
        ",".join(sorted(str(key) for key in raw.keys())) or "none",
    )
    try:
        payload = SignupComplete.model_validate(raw)
    except ValidationError as exc:
        return _signup_complete_validation_failure(exc, request_id)
    current_app.logger.info(
        "signup_complete stage=request_validation result=PASS request_id=%s pending_signup_id_present=%s",
        request_id,
        bool(payload.pending_signup_id),
    )
    if payload.password != payload.confirm_password:
        current_app.logger.info("signup_complete stage=password_validation result=FAIL request_id=%s error_code=PASSWORD_MISMATCH", request_id)
        return failure("Password confirmation does not match.", status=422, error="password_mismatch", error_code="PASSWORD_MISMATCH", stage="password_validation", request_id=request_id)
    if not is_valid_signup_password(payload.password):
        current_app.logger.info("signup_complete stage=password_validation result=FAIL request_id=%s error_code=PASSWORD_POLICY", request_id)
        return failure(SIGNUP_PASSWORD_POLICY_MESSAGE, status=422, error="password_policy", error_code="PASSWORD_POLICY", stage="password_validation", request_id=request_id)
    current_app.logger.info("signup_complete stage=password_validation result=PASS request_id=%s", request_id)
    current_app.logger.info("signup_complete stage=pending_signup_lookup result=START request_id=%s", request_id)
    pending = _pending_signup(current_app.extensions["store"], payload.pending_signup_id)
    if not pending:
        current_app.logger.info("signup_complete stage=pending_signup_lookup result=FAIL request_id=%s error_code=SIGNUP_EXPIRED", request_id)
        return failure("Your signup session has expired. Please start again.", status=410, error="signup_expired", error_code="SIGNUP_EXPIRED", stage="pending_signup_lookup", request_id=request_id)
    current_app.logger.info("signup_complete stage=pending_signup_lookup result=PASS request_id=%s", request_id)
    if not pending.get("email_verified"):
        current_app.logger.info("signup_complete stage=email_verification_state result=FAIL request_id=%s error_code=EMAIL_NOT_VERIFIED", request_id)
        return failure("Verify your email before creating an account.", status=403, error="email_not_verified", error_code="EMAIL_NOT_VERIFIED", stage="email_verification_state", request_id=request_id)
    current_app.logger.info("signup_complete stage=email_verification_state result=PASS request_id=%s", request_id)
    if not is_allowed_signup_email(pending.get("email"), current_app.config.get("ALLOWED_SIGNUP_EMAIL_DOMAINS")):
        current_app.logger.info("signup_complete stage=email_domain result=FAIL request_id=%s error_code=SIGNUP_EMAIL_DOMAIN_NOT_ALLOWED", request_id)
        return failure(SIGNUP_EMAIL_DOMAIN_MESSAGE, status=422, error="signup_email_domain_not_allowed", error_code="SIGNUP_EMAIL_DOMAIN_NOT_ALLOWED", stage="email_domain", request_id=request_id)
    store = current_app.extensions["store"]
    current_app.logger.info("signup_complete stage=duplicate_user_check result=START request_id=%s", request_id)
    duplicate = store.find_one("users", {"email": pending["email"]}) or store.find_one("users", {"username_normalized": pending["username_normalized"]})
    if duplicate:
        current_app.logger.info("signup_complete stage=duplicate_user_check result=FAIL request_id=%s error_code=ACCOUNT_EXISTS", request_id)
        return failure("Username or email is already in use.", status=409, error="account_exists", error_code="ACCOUNT_EXISTS", stage="duplicate_user_check", request_id=request_id)
    current_app.logger.info("signup_complete stage=duplicate_user_check result=PASS request_id=%s", request_id)
    document = {
        "email": pending["email"], "name": pending["name"], "username": pending["username"],
        "username_normalized": pending["username_normalized"], "password_hash": generate_password_hash(payload.password),
        "role_id": "user", "customer_ids": [], "customer_company_ids": [], "company_ids": [],
        "active": True, "email_verified": True, "device_access_mode": current_app.config.get("DEVICE_ACCESS_MODE", "approved_devices_only"),
    }
    try:
        user = store.insert_one("users", document)
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            current_app.logger.info("signup_complete stage=user_creation result=FAIL request_id=%s error_code=ACCOUNT_EXISTS", request_id)
            return failure("Username or email is already in use.", status=409, error="account_exists", error_code="ACCOUNT_EXISTS", stage="user_creation", request_id=request_id)
        current_app.logger.exception("signup_complete stage=user_creation result=FAIL request_id=%s error_code=SIGNUP_COMPLETION_FAILED", request_id)
        return failure("Account could not be created. Please try again.", status=500, error="signup_completion_failed", error_code="SIGNUP_COMPLETION_FAILED", stage="user_creation", request_id=request_id)
    current_app.logger.info("signup_complete stage=user_creation result=PASS request_id=%s", request_id)
    try:
        device = _establish_session(user, method="signup", event="signup_auto_login")
    except Exception:
        if session.get("user_id") == user["_id"]:
            session.clear()
        store.delete_one("users", {"_id": user["_id"]})
        current_app.logger.exception("signup_complete stage=session_creation result=FAIL request_id=%s error_code=SIGNUP_COMPLETION_FAILED", request_id)
        return failure("Account could not be created. Please try again.", status=500, error="signup_completion_failed", error_code="SIGNUP_COMPLETION_FAILED", stage="session_creation", request_id=request_id)
    current_app.logger.info("signup_complete stage=session_creation result=PASS request_id=%s", request_id)
    try:
        if not store.delete_one("pending_signups", {"_id": pending["_id"]}):
            store.update_one("pending_signups", {"_id": pending["_id"]}, {"consumed": True, "expires_at": utcnow()})
    except Exception:
        # The permanent user and authenticated session are already established;
        # invalidate the pending record as far as the store permits without
        # turning a successful signup into a misleading failure response.
        current_app.logger.exception("signup_pending_cleanup result=FAIL")
    audit("user.create", "user", str(user["_id"]))
    current_app.logger.info("signup_complete stage=complete result=PASS request_id=%s authenticated=true", request_id)
    return success(
        {**_session_result(device, "customer-selection"), "authenticated": True, "user_id": user["_id"]},
        "Account created successfully",
        201,
    )


@bp.post("/verify-otp")
@limiter.limit("10 per 15 minutes")
def verify_otp():
    try:
        payload = OtpVerify.model_validate(request.get_json(silent=True) or {})
        if payload.purpose == "login":
            store = current_app.extensions["store"]
            normalized_email = str(payload.email).strip().lower()
            target = store.find_one("users", {"email": normalized_email, "active": {"$ne": False}})
            if target and str(target.get("role_id") or "") != "superadmin":
                # Re-check eligibility immediately before OTP verification as
                # a defense against a forged/replayed pending-login session.
                audit("auth.login_otp.blocked", "user", str(target.get("_id") or "unknown"), {"reason": "superadmin_required_at_verify"})
                session.pop("pending_login_user_id", None)
                session.pop("pending_login_email", None)
                session.pop("pending_superadmin_login_user_id", None)
                session.pop("pending_superadmin_login_email", None)
                return failure(SUPERADMIN_OTP_ONLY_MESSAGE, status=403, error="superadmin_otp_required")
            pending_id = str(session.get("pending_login_user_id") or session.get("pending_superadmin_login_user_id") or "")
            pending_email = str(session.get("pending_login_email") or session.get("pending_superadmin_login_email") or "").strip().lower()
            if (not target
                    or pending_id != str(target.get("_id") or "")
                    or pending_email != normalized_email):
                audit("auth.login_otp.blocked", "user", str((target or {}).get("_id") or "unknown"), {"reason": "otp_request_required"})
                return failure("Request a new email verification code.", status=403, error="login_otp_pending")
        if payload.purpose == "reset":
            reset_target = current_app.extensions["store"].find_one(
                "users", {"email": str(payload.email).strip().lower(), "active": {"$ne": False}}
            )
            if reset_target and str(reset_target.get("role_id") or "") != "superadmin":
                audit("auth.password_reset.blocked", "user", str(reset_target.get("_id") or "unknown"), {"reason": "superadmin_required_at_verify"})
                session.pop("verified_reset_email", None)
                return failure("Please contact your Superadmin to update your password.", status=403, error="superadmin_password_reset_required")
        user = current_app.extensions["otp_service"].verify(str(payload.email), payload.purpose, payload.code)
        if payload.purpose == "reset" and (not user or str(user.get("role_id") or "") != "superadmin"):
            audit("auth.password_reset.blocked", "user", str((user or {}).get("_id") or "unknown"), {"reason": "superadmin_required"})
            return failure("Please contact your Superadmin to update your password.", status=403, error="superadmin_password_reset_required")
        if payload.purpose == "login":
            if not user or not user.get("active", False):
                return failure("Account is unavailable", status=403)
            if (str(session.get("pending_login_user_id") or session.get("pending_superadmin_login_user_id") or "") != str(user.get("_id") or "")
                    or str(session.get("pending_login_email") or session.get("pending_superadmin_login_email") or "").strip().lower() != str(user.get("email") or "").strip().lower()):
                return failure("Request a new email verification code.", status=403, error="login_otp_pending")
            session.pop("pending_login_user_id", None)
            session.pop("pending_login_email", None)
            session.pop("pending_superadmin_login_user_id", None)
            session.pop("pending_superadmin_login_email", None)
            device = _establish_session(user, method="email_otp")
            audit("auth.login", "user", str(user["_id"]), {"method": "email_otp"})
            return success(_session_result(device), "Signed in successfully" if device.get("application_access") else "Device approval pending")
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
    store = current_app.extensions["store"]
    existing = store.find_one("users", {"email": email, "active": {"$ne": False}})
    if not existing or str(existing.get("role_id") or "") != "superadmin":
        session.pop("verified_reset_email", None)
        audit("auth.password_reset.blocked", "user", str((existing or {}).get("_id") or "unknown"), {"reason": "superadmin_required"})
        return failure("Please contact your Superadmin to update your password.", status=403, error="superadmin_password_reset_required")
    user = store.update_one("users", {"_id": existing["_id"]}, {"password_hash": generate_password_hash(password), "password_changed_at": utcnow()})
    if not user:
        return failure("Account is unavailable", status=404)
    session.clear()
    audit("auth.password_reset", "user", str(user["_id"]))
    return success(message="Password changed successfully")


@bp.post("/change-password")
@login_required
def change_password():
    user = current_user() or {}
    if str(user.get("role_id") or "") != "superadmin":
        audit("auth.password_change.blocked", "user", str(user.get("_id") or "unknown"), {"reason": "superadmin_required"})
        return failure("Please contact your Superadmin to update your password.", status=403, error="superadmin_password_change_required")
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
    device = _establish_session({"_id": "user-demo-admin", "role_id": "superadmin"}, method="demo")
    return success(_session_result(device), "Customer selection ready")


@bp.post("/logout")
@login_required
def logout():
    user = current_user()
    audit("auth.logout", "user", str(user["_id"]) if user else None)
    session.clear()
    current_app.logger.info("session_revoked user_id=%s reason=explicit_logout", user.get("_id") if user else "unknown")
    return success(message="Signed out")
