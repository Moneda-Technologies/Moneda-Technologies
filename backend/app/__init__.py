from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any

from flask import Flask, request, session
from flask_cors import CORS
from redis import Redis
from redis.exceptions import RedisError

from app.api.responses import failure, success
from app.auth.service import OtpService
from app.communication.email import EmailService, RecordingEmailProvider
from app.communication.zoho import ZohoMailApiProvider, ZohoMailOAuth
from app.communication.whatsapp import DisabledWhatsAppProvider, MockWhatsAppProvider
from app.config import Config
from app.exchange_rates.provider import FrankfurterProvider
from app.exchange_rates.service import ExchangeRateService
from app.extensions import limiter
from app.quotations.service import QuotationService
from app.repositories.store import build_store
from app.services.seed import seed


_OAUTH_QUERY_SECRET = re.compile(r"([?&](?:code|state)=)[^&\s\"]+", re.IGNORECASE)


class _OAuthAccessLogFilter(logging.Filter):
    """Prevent the web-server access log from recording callback credentials."""

    @staticmethod
    def _redact(value: Any) -> Any:
        return _OAUTH_QUERY_SECRET.sub(r"\1[REDACTED]", value) if isinstance(value, str) else value

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._redact(value) for key, value in record.args.items()}
        else:
            record.msg = self._redact(record.msg)
        return True


def create_app(config: type[Config] | dict[str, Any] | None = None) -> Flask:
    workspace_root = Path(__file__).resolve().parents[2]
    app = Flask(__name__, template_folder=str(workspace_root / "templates"))
    werkzeug_logger = logging.getLogger("werkzeug")
    if not any(isinstance(item, _OAuthAccessLogFilter) for item in werkzeug_logger.filters):
        werkzeug_logger.addFilter(_OAuthAccessLogFilter())
    # Request-stage diagnostics are operational information and must remain
    # visible even when the development server runs with the reloader off.
    app.logger.setLevel("INFO")
    app.config.from_object(Config)
    if isinstance(config, dict):
        app.config.update(config)
    elif config:
        app.config.from_object(config)

    CORS(app, origins=[app.config["FRONTEND_ORIGIN"]], supports_credentials=True)
    app.config["PDF_DIRECTORY"].mkdir(parents=True, exist_ok=True)
    app.config["UPLOAD_DIRECTORY"].mkdir(parents=True, exist_ok=True)

    intended_engine = "memory" if app.config["DEMO_MODE"] or app.config["TESTING"] else "mongodb"
    app.logger.info(
        "Moneda API starting environment=%s demo_mode=%s database_engine=%s database=%s api_port=%s",
        "testing" if app.config["TESTING"] else app.config["ENVIRONMENT"],
        app.config["DEMO_MODE"], intended_engine,
        "demo-memory" if intended_engine == "memory" else app.config["MONGODB_DATABASE"], app.config["PORT"],
    )
    if intended_engine == "mongodb" and not app.config.get("MONGODB_URI"):
        app.logger.error("MongoDB startup configuration failed: MONGODB_URI is missing while DEMO_MODE=false")
        raise RuntimeError("MONGODB_URI is required when DEMO_MODE is disabled")
    try:
        store = build_store(app.config)
    except RuntimeError as exc:
        app.logger.error("database startup failed engine=%s database=%s reason=%s", intended_engine, app.config["MONGODB_DATABASE"], str(exc))
        raise
    database_health = store.health()
    app.logger.info(
        "database startup connected=%s engine=%s database=%s collections=%s indexes_ready=%s",
        database_health.get("connected"), database_health.get("engine"), database_health.get("database"),
        database_health.get("collections"), database_health.get("indexes_ready"),
    )
    rate_limit_uri = str(app.config.get("RATELIMIT_STORAGE_URI") or "")
    # Flask-Limiter supports an in-process memory backend for local
    # development. Only explicitly configured Redis URLs require a network
    # connection and a startup ping; memory:// must never probe localhost:6379.
    if rate_limit_uri.startswith(("redis://", "rediss://")):
        rate_limit_backend = "redis"
    elif rate_limit_uri.startswith("memory://") or not rate_limit_uri:
        rate_limit_backend = "memory"
    else:
        rate_limit_backend = rate_limit_uri.split(":", 1)[0].lower() or "unknown"
    app.config["RATE_LIMIT_BACKEND"] = rate_limit_backend
    if rate_limit_uri.startswith(("redis://", "rediss://")):
        try:
            Redis.from_url(rate_limit_uri, socket_connect_timeout=3, socket_timeout=3).ping()
        except RedisError as exc:
            app.logger.error("Redis rate-limit storage connection failed for configured endpoint")
            raise RuntimeError("Redis rate-limit storage is unavailable") from exc
        app.logger.info("rate-limit storage connected engine=redis")
    else:
        app.logger.info("rate-limit storage backend=%s", rate_limit_backend)
    limiter.init_app(app)
    if app.config.get("AUTO_SEED") or app.config.get("TESTING"):
        seed(store, Path(app.config["DATA_DIRECTORY"]), demo_mode=app.config["DEMO_MODE"])
    configured_provider = str(app.config.get("EMAIL_PROVIDER") or "zoho_mail_api").strip().lower()
    if configured_provider != "zoho_mail_api":
        raise RuntimeError("EMAIL_PROVIDER must be zoho_mail_api; SMTP fallback is not supported")
    zoho_oauth = ZohoMailOAuth(app.config, store)
    email_provider = (
        RecordingEmailProvider() if app.config["DEMO_MODE"] or app.config["TESTING"]
        else ZohoMailApiProvider(app.config, store, oauth=zoho_oauth)
    )
    email_status = zoho_oauth.status()
    app.logger.info(
        "email startup provider=zoho_mail_api configuration=%s oauth=%s account=%s account_id=%s api_domain=%s",
        "VALID" if email_status["configured"] else "INVALID",
        "CONNECTED" if email_status["connected"] else "NOT CONNECTED",
        email_status.get("account_email") or "unknown",
        email_status.get("account_id") or "UNKNOWN",
        email_status.get("api_domain") or "unknown",
    )
    exchange_service = ExchangeRateService(store, FrankfurterProvider(), app.config["EXCHANGE_RATE_CACHE_SECONDS"])
    app.extensions["store"] = store
    app.extensions["zoho_oauth"] = zoho_oauth
    app.extensions["email_provider"] = email_provider
    app.extensions["email_service"] = EmailService(email_provider)
    app.extensions["whatsapp_provider"] = MockWhatsAppProvider() if app.config["DEMO_MODE"] or app.config["TESTING"] else DisabledWhatsAppProvider()
    app.extensions["exchange_rate_service"] = exchange_service
    app.extensions["otp_service"] = OtpService(store, app.extensions["email_service"])
    app.extensions["quotation_service"] = QuotationService(store, exchange_service)

    @app.get("/health")
    def root_health():
        """Deployment-friendly health endpoint without connection secrets."""
        database = app.extensions["store"].health()
        status = "healthy" if database.get("connected") else "degraded"
        message = "Moneda API is running" if status == "healthy" else "Moneda API is running with degraded database connectivity"
        return success({
            "status": status, "service": "moneda-api", "version": "1.0.0",
            "database": database,
            "rate_limit": {"backend": app.config["RATE_LIMIT_BACKEND"]},
            "email": {
                "provider": "zoho_mail_api", "configured": zoho_oauth.status()["configured"],
                "connected": zoho_oauth.status()["connected"],
                "account_id": zoho_oauth.status().get("account_id"),
            },
        }, message, status=200 if status == "healthy" else 503)

    from app.admin.routes import bp as admin_bp
    from app.api.system import bp as system_bp
    from app.auth.routes import bp as auth_bp
    from app.catalog.routes import bp as catalog_bp
    from app.companies.routes import bp as companies_bp
    from app.crm.routes import bp as crm_bp
    from app.customers.routes import bp as customers_bp
    from app.orders.routes import bp as orders_bp
    from app.pricing.routes import bp as pricing_bp
    from app.quotations.routes import bp as quotations_bp
    from app.reports.routes import bp as reports_bp
    from app.integrations.routes import bp as integrations_bp

    blueprints = (system_bp, auth_bp, catalog_bp, companies_bp, customers_bp, pricing_bp, quotations_bp, crm_bp, orders_bp, reports_bp, admin_bp, integrations_bp)
    for blueprint in blueprints:
        app.register_blueprint(blueprint)

    # /api/v1 is the public contract. The original unversioned registration is
    # retained as a compatibility bridge for existing local sessions/tests.
    v1_prefixes = {
        "system": "/api/v1", "auth": "/api/v1/auth", "catalog": "/api/v1",
        "companies": "/api/v1/companies", "customers": "/api/v1/customers",
        "pricing": "/api/v1", "quotations": "/api/v1/quotations",
        "crm": "/api/v1", "orders": "/api/v1", "reports": "/api/v1", "admin": "/api/v1", "integrations": "/api/v1",
    }
    for blueprint in blueprints:
        app.register_blueprint(blueprint, url_prefix=v1_prefixes[blueprint.name], name=f"{blueprint.name}_v1")

    @app.before_request
    def csrf_origin_guard():
        if request.method not in {"POST", "PATCH", "PUT", "DELETE"} or not session.get("user_id"):
            return None
        origin = request.headers.get("Origin")
        allowed = {app.config["FRONTEND_ORIGIN"], app.config["APP_BASE_URL"]}
        if origin and origin not in allowed:
            return failure("Request origin is not allowed", status=403)
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.errorhandler(404)
    def not_found(_error):
        return failure("Endpoint not found", status=404)

    @app.errorhandler(413)
    def too_large(_error):
        return failure("Uploaded file is too large", status=413)

    @app.errorhandler(429)
    def rate_limited(_error):
        return failure("Too many requests. Please try again later.", status=429)

    @app.errorhandler(500)
    def internal_error(_error):
        app.logger.exception("Unhandled request failure")
        return failure("An unexpected server error occurred", status=500)

    return app
