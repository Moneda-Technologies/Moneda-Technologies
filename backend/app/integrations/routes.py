from __future__ import annotations

import logging
from urllib.parse import parse_qs, urlencode, urlparse

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, current_app, redirect, request, session

from app.api.responses import failure, success
from app.communication.email import EmailDeliveryError
from app.communication.zoho import ZohoIntegrationError, ZohoMailOAuth, integration_diagnostic_id
from app.middleware.access import current_user, permission_required
from app.services.audit import audit


logger = logging.getLogger(__name__)
bp = Blueprint("integrations", __name__, url_prefix="/api")


def _oauth() -> ZohoMailOAuth:
    return current_app.extensions["zoho_oauth"]


def _settings_redirect(result: str, *, diagnostic_id: str | None = None,
                       stage: str | None = None, error_code: str | None = None) -> str:
    query = {"zoho": result}
    if diagnostic_id:
        query["diagnostic_id"] = diagnostic_id
    if stage:
        query["stage"] = stage
    if error_code:
        query["error_code"] = error_code
    base = str(current_app.config.get("APP_BASE_URL") or "http://localhost:3005").rstrip("/")
    return f"{base}/settings?{urlencode(query)}"


def _email_failure(exc: EmailDeliveryError):
    if exc.error_code == "OAUTH_NOT_CONNECTED":
        message = "Email service is not connected. Please contact the administrator."
    else:
        message = "Zoho test email could not be delivered."
    alias_errors = {
        "SENDER_INVALID", "OTP_SENDER_ALIAS_UNAVAILABLE",
        "QUOTATION_SENDER_ALIAS_UNAVAILABLE", "ORDER_SENDER_ALIAS_UNAVAILABLE",
        "GENERAL_SENDER_UNAVAILABLE",
    }
    status = 429 if exc.error_code == "ZOHO_MAIL_API_RATE_LIMIT" else 422 if exc.error_code in alias_errors else 503
    return failure(
        message, status=status, error=exc.error_code,
        diagnostic_id=exc.diagnostic_id, stage=exc.stage,
    )


@bp.get("/integrations/zoho/status")
@permission_required("settings.manage")
def zoho_status():
    return success(_oauth().status(refresh_aliases=True), "Zoho Mail integration status")


@bp.get("/integrations/zoho/connect")
@permission_required("settings.manage")
def zoho_connect():
    oauth = _oauth()
    diagnostic_id = integration_diagnostic_id()
    user = current_user() or {}
    if not oauth.configured():
        logger.error(
            "zoho oauth start request_id=%s diagnostic_id=%s stage=oauth_configuration result=FAIL error_code=OAUTH_CONFIGURATION_ERROR",
            request.headers.get("X-Request-ID", "oauth-start"), diagnostic_id,
        )
        return failure(
            "Zoho OAuth client configuration is incomplete", status=503,
            error="OAUTH_CONFIGURATION_ERROR", diagnostic_id=diagnostic_id,
            stage="oauth_configuration",
        )
    state, transaction_id, code_challenge = oauth.create_state_transaction(
        str(user["_id"]), return_path="/settings",
    )
    # Zoho's server-based callback examples do not guarantee a returned
    # state parameter. Keep only an opaque, signed-session transaction pointer
    # in the browser; MongoDB remains authoritative for the actual transaction.
    session["zoho_oauth_transaction"] = transaction_id
    authorization_url = oauth.authorization_url(state, code_challenge=code_challenge)
    parsed_authorization_url = urlparse(authorization_url)
    authorization_query = parse_qs(parsed_authorization_url.query, keep_blank_values=True)
    encoded_state = authorization_query.get("state", [""])[0]
    if not state or encoded_state != state:
        session.pop("zoho_oauth_transaction", None)
        exc = ZohoIntegrationError(
            "OAUTH_CONFIGURATION_ERROR", "Zoho authorization URL did not contain the generated state",
            stage="authorization", diagnostic_id=diagnostic_id,
        )
        _oauth().record_error(exc)
        logger.error(
            "oauth_start authorization_url_state_present=false diagnostic_id=%s error_code=%s",
            diagnostic_id, exc.code,
        )
        return failure(
            "Zoho OAuth authorization could not be started", status=503,
            error=exc.code, diagnostic_id=diagnostic_id, stage=exc.stage,
        )
    logger.info(
        "oauth_start authorization_endpoint_host=%s redirect_uri=%s state_present=true state_length=%s diagnostic_id=%s",
        parsed_authorization_url.netloc, authorization_query.get("redirect_uri", [""])[0], len(state), diagnostic_id,
    )
    logger.info(
        "oauth_authorization_redirect state_present=true redirect_uri=%s diagnostic_id=%s",
        authorization_query.get("redirect_uri", [""])[0], diagnostic_id,
    )
    return redirect(authorization_url)


@bp.get("/integrations/zoho/callback")
def zoho_callback():
    diagnostic_id = integration_diagnostic_id()
    request_id = request.headers.get("X-Request-ID", "oauth-callback")
    supplied_state = str(request.args.get("state") or "")
    transaction_id = str(session.get("zoho_oauth_transaction") or "")
    logger.info(
        "zoho oauth callback request_id=%s diagnostic_id=%s stage=oauth_callback result=RECEIVED state_present=%s",
        request_id, diagnostic_id, bool(supplied_state),
    )
    user_id = session.get("user_id")
    if supplied_state:
        state_record = _oauth().consume_state_record(
            supplied_state, user_id, transaction_id=transaction_id,
        )
        validation_mode = "returned_state"
    else:
        # Zoho's documented server-based callback can omit state. Do not
        # accept that callback blindly: require the signed-session pointer to
        # match an unexpired, user-bound, one-time MongoDB transaction.
        state_record = _oauth().consume_transaction_record(transaction_id, user_id)
        validation_mode = "server_transaction"
    if not state_record:
        logger.error(
            "zoho oauth callback request_id=%s diagnostic_id=%s stage=state_validation result=FAIL error_code=OAUTH_STATE_ERROR state_rejected=true",
            request_id, diagnostic_id,
        )
        return redirect(_settings_redirect("error", diagnostic_id=diagnostic_id, stage="state_validation", error_code="OAUTH_STATE_ERROR"))
    logger.info(
        "zoho oauth callback request_id=%s diagnostic_id=%s stage=state_validation result=PASS state_validated=true validation_mode=%s",
        request_id, diagnostic_id, validation_mode,
    )
    session.pop("zoho_oauth_transaction", None)
    try:
        code_verifier = _oauth().code_verifier_from_transaction(
            state_record, diagnostic_id=diagnostic_id,
        )
    except ZohoIntegrationError as exc:
        _oauth().record_error(exc)
        logger.error(
            "zoho oauth callback request_id=%s diagnostic_id=%s stage=%s result=FAIL error_code=%s exception_class=%s",
            request_id, diagnostic_id, exc.stage, exc.code, type(exc).__name__,
        )
        return redirect(_settings_redirect("error", diagnostic_id=diagnostic_id, stage=exc.stage, error_code=exc.code))
    if not _oauth().callback_context_valid(
        location=request.args.get("location"), accounts_server=request.args.get("accounts-server"),
    ):
        exc = ZohoIntegrationError(
            "OAUTH_CALLBACK_ERROR", "Zoho callback data-center does not match the configured India account server",
            stage="oauth_callback", diagnostic_id=diagnostic_id,
        )
        _oauth().record_error(exc)
        logger.error(
            "zoho oauth callback request_id=%s diagnostic_id=%s stage=%s result=FAIL error_code=%s",
            request_id, diagnostic_id, exc.stage, exc.code,
        )
        return redirect(_settings_redirect("error", diagnostic_id=diagnostic_id, stage=exc.stage, error_code=exc.code))
    if request.args.get("error"):
        exc = ZohoIntegrationError(
            "OAUTH_CALLBACK_ERROR", "Zoho authorization was not granted",
            stage="oauth_callback", diagnostic_id=diagnostic_id,
        )
        _oauth().record_error(exc)
        logger.error(
            "zoho oauth callback request_id=%s diagnostic_id=%s stage=oauth_callback result=FAIL error_code=%s",
            request_id, diagnostic_id, exc.code,
        )
        return redirect(_settings_redirect("error", diagnostic_id=diagnostic_id, stage=exc.stage, error_code=exc.code))
    code = str(request.args.get("code") or "").strip()
    if not code:
        exc = ZohoIntegrationError(
            "OAUTH_CALLBACK_ERROR", "Zoho authorization code is missing",
            stage="oauth_callback", diagnostic_id=diagnostic_id,
        )
        _oauth().record_error(exc)
        return redirect(_settings_redirect("error", diagnostic_id=diagnostic_id, stage=exc.stage, error_code=exc.code))
    try:
        _oauth().exchange_code(code, code_verifier=code_verifier, diagnostic_id=diagnostic_id)
    except ZohoIntegrationError as exc:
        _oauth().record_error(exc)
        logger.error(
            "zoho oauth callback request_id=%s diagnostic_id=%s stage=%s result=FAIL error_code=%s exception_class=%s",
            request_id, diagnostic_id, exc.stage, exc.code, type(exc.__cause__ or exc).__name__,
        )
        return redirect(_settings_redirect("error", diagnostic_id=diagnostic_id, stage=exc.stage, error_code=exc.code))
    audit("integration.zoho.connect", "integration", "zoho_mail")
    return redirect(_settings_redirect("connected"))


@bp.post("/integrations/zoho/test")
@permission_required("settings.manage")
def zoho_test():
    recipient = str(
        (request.get_json(silent=True) or {}).get("to")
        or current_app.config.get("MAIL_TEST_TO") or ""
    ).strip().lower()
    try:
        recipient = validate_email(recipient, check_deliverability=False).normalized
    except EmailNotValidError:
        return failure("Provide a valid test recipient email", status=422, error="RECIPIENT_INVALID")
    request_id = f"zoho-test-{integration_diagnostic_id()}"
    try:
        result = current_app.extensions["email_service"].send_test_email(
            to=[recipient], request_id=request_id,
        )
    except EmailDeliveryError as exc:
        return _email_failure(exc)
    checks = result.get("checks") or [
        {"stage": stage, "result": "PASS"}
        for stage in ("configuration", "oauth", "token_refresh", "account_id", "sender", "api_request", "message_submission")
    ]
    audit("integration.zoho.test", "integration", "zoho_mail", {"diagnostic_id": result.get("diagnostic_id")})
    return success({
        "sent": True, "provider": result.get("provider", "zoho_mail_api"),
        "diagnostic_id": result.get("diagnostic_id"), "stage": result.get("stage"),
        "checks": checks,
    }, "Zoho test email submitted")


@bp.post("/integrations/zoho/disconnect")
@permission_required("settings.manage")
def zoho_disconnect():
    result = _oauth().disconnect()
    audit("integration.zoho.disconnect", "integration", "zoho_mail", {"remote_revoked": result["revoked"]})
    return success(result, "Zoho Mail disconnected")
