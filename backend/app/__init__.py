from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, request, session
from flask_cors import CORS

from app.api.responses import failure, success
from app.auth.service import OtpService
from app.communication.email import EmailService, RecordingEmailProvider, SmtpEmailProvider
from app.communication.whatsapp import DisabledWhatsAppProvider, MockWhatsAppProvider
from app.config import Config
from app.exchange_rates.provider import FrankfurterProvider
from app.exchange_rates.service import ExchangeRateService
from app.extensions import limiter
from app.quotations.service import QuotationService
from app.repositories.store import build_store
from app.services.seed import seed


def create_app(config: type[Config] | dict[str, Any] | None = None) -> Flask:
    workspace_root = Path(__file__).resolve().parents[2]
    app = Flask(__name__, template_folder=str(workspace_root / "templates"))
    app.config.from_object(Config)
    if isinstance(config, dict):
        app.config.update(config)
    elif config:
        app.config.from_object(config)

    CORS(app, origins=[app.config["FRONTEND_ORIGIN"]], supports_credentials=True)
    limiter.init_app(app)
    app.config["PDF_DIRECTORY"].mkdir(parents=True, exist_ok=True)
    app.config["UPLOAD_DIRECTORY"].mkdir(parents=True, exist_ok=True)

    store = build_store(app.config)
    database_health = store.health()
    app.logger.info(
        "database startup connected=%s engine=%s database=%s collections=%s indexes_ready=%s",
        database_health.get("connected"), database_health.get("engine"), database_health.get("database"),
        database_health.get("collections"), database_health.get("indexes_ready"),
    )
    if app.config.get("AUTO_SEED") or app.config.get("TESTING"):
        seed(store, Path(app.config["DATA_DIRECTORY"]), demo_mode=app.config["DEMO_MODE"])
    email_provider = RecordingEmailProvider() if app.config["DEMO_MODE"] or app.config["TESTING"] else SmtpEmailProvider(
        host=app.config["MAIL_HOST"], port=app.config["MAIL_PORT"], use_tls=app.config["MAIL_USE_TLS"],
        username=app.config["MAIL_USERNAME"], password=app.config["MAIL_PASSWORD"],
        from_address=app.config["MAIL_GENERAL_FROM"], from_name=app.config["MAIL_FROM_NAME"],
    )
    exchange_service = ExchangeRateService(store, FrankfurterProvider(), app.config["EXCHANGE_RATE_CACHE_SECONDS"])
    app.extensions["store"] = store
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
        return success({"status": status, "service": "moneda-api", "version": "1.0.0", "database": database}, status=200 if status == "healthy" else 503)

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

    blueprints = (system_bp, auth_bp, catalog_bp, companies_bp, customers_bp, pricing_bp, quotations_bp, crm_bp, orders_bp, reports_bp, admin_bp)
    for blueprint in blueprints:
        app.register_blueprint(blueprint)

    # /api/v1 is the public contract. The original unversioned registration is
    # retained as a compatibility bridge for existing local sessions/tests.
    v1_prefixes = {
        "system": "/api/v1", "auth": "/api/v1/auth", "catalog": "/api/v1",
        "companies": "/api/v1/companies", "customers": "/api/v1/customers",
        "pricing": "/api/v1", "quotations": "/api/v1/quotations",
        "crm": "/api/v1", "orders": "/api/v1", "reports": "/api/v1", "admin": "/api/v1",
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
