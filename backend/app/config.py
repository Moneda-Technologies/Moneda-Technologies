from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# python-dotenv correctly preserves deployment environment variables, but an
# inherited empty Mongo URI must not mask the configured project value while
# DEMO_MODE is disabled. Non-empty process values continue to take precedence.
if os.getenv("MONGODB_URI") == "":
    os.environ.pop("MONGODB_URI", None)
load_dotenv(PROJECT_ROOT / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_path(name: str, default: Path) -> Path:
    value = Path(os.getenv(name, str(default)))
    return value if value.is_absolute() else PROJECT_ROOT / value


class Config:
    # ``SECRET_KEY`` is the public configuration name.  Keep the historical
    # FLASK_SECRET_KEY alias so existing deployments continue to work.
    SECRET_KEY = os.getenv("SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or "development-only-change-me"
    DEBUG = env_bool("FLASK_DEBUG", False)
    ENVIRONMENT = os.getenv("FLASK_ENV", "development")
    PORT = int(os.getenv("PORT", "5005"))
    TESTING = False
    MONGODB_URI = os.getenv("MONGODB_URI", "")
    MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "moneda")
    # Memory storage is an explicit demo-only choice. Missing MongoDB
    # configuration must never silently turn a normal environment into demo.
    DEMO_MODE = env_bool("DEMO_MODE", False)
    # JSON is bootstrap input. Production MongoDB is the runtime authority and
    # is never silently reseeded during a web-process restart.
    AUTO_SEED = env_bool("AUTO_SEED", False)
    DEV_AUTH_BYPASS = env_bool("DEV_AUTH_BYPASS", False)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_PATH = "/"
    SESSION_COOKIE_NAME = "session"
    SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", False)
    SESSION_REFRESH_EACH_REQUEST = True
    PERMANENT_SESSION_LIFETIME = 60 * 60 * 12
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024
    JSON_SORT_KEYS = False
    FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:3005")
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:3005")
    DATA_DIRECTORY = env_path("DATA_DIRECTORY", PROJECT_ROOT / "data")
    PDF_DIRECTORY = env_path("PDF_DIRECTORY", PROJECT_ROOT / "generated" / "quotations")
    UPLOAD_DIRECTORY = env_path("UPLOAD_DIRECTORY", PROJECT_ROOT / "uploads")
    MAIL_TEST_TO = os.getenv("MAIL_TEST_TO", "")
    # Zoho Mail API OAuth.  These values are server-side only; never expose
    # client secret or refresh token through the public configuration API.
    ZOHO_CLIENT_ID = os.getenv("ZOHO_CLIENT_ID", "")
    ZOHO_CLIENT_SECRET = os.getenv("ZOHO_CLIENT_SECRET", "")
    ZOHO_REFRESH_TOKEN = os.getenv("ZOHO_REFRESH_TOKEN", "")
    ZOHO_ACCOUNT_ID = os.getenv("ZOHO_ACCOUNT_ID", "")
    ZOHO_ACCOUNTS_BASE_URL = os.getenv("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.in")
    ZOHO_OAUTH_REDIRECT_URI = os.getenv("ZOHO_OAUTH_REDIRECT_URI", "http://localhost:5005/api/v1/integrations/zoho/callback")
    ZOHO_MAIL_API_BASE_URL = os.getenv("ZOHO_MAIL_API_BASE_URL", "")
    ZOHO_FROM_ADDRESS = os.getenv("ZOHO_FROM_ADDRESS", "business@monedatechnologies.com")
    # Sender identities are aliases on the single Zoho OAuth mailbox. Keep the
    # purpose mapping here so application workflows never hardcode addresses.
    MAIL_OTP_FROM = os.getenv("MAIL_OTP_FROM", "otp@monedatechnologies.com")
    MAIL_OTP_FROM_NAME = os.getenv("MAIL_OTP_FROM_NAME", "Moneda OTP")
    MAIL_QUOTATION_FROM = os.getenv("MAIL_QUOTATION_FROM", "quotations@monedatechnologies.com")
    MAIL_QUOTATION_FROM_NAME = os.getenv("MAIL_QUOTATION_FROM_NAME", "Moneda Quotations")
    MAIL_ORDER_FROM = os.getenv("MAIL_ORDER_FROM", "orders@monedatechnologies.com")
    MAIL_ORDER_FROM_NAME = os.getenv("MAIL_ORDER_FROM_NAME", "Moneda Orders")
    MAIL_GENERAL_FROM = os.getenv("MAIL_GENERAL_FROM", ZOHO_FROM_ADDRESS)
    MAIL_GENERAL_FROM_NAME = os.getenv("MAIL_GENERAL_FROM_NAME", "Moneda Technologies")
    MAIL_CUSTOMER_CC_BUSINESS = os.getenv("MAIL_CUSTOMER_CC_BUSINESS", "business@monedatechnologies.com")
    MAIL_CUSTOMER_CC_VBHUTA = os.getenv("MAIL_CUSTOMER_CC_VBHUTA", "vbhuta@monedatechnologies.com")
    MAIL_CUSTOMER_CC_VBHUTA_ENABLED = env_bool("MAIL_CUSTOMER_CC_VBHUTA_ENABLED", False)
    MAIL_CUSTOMER_CC_ADMIN = os.getenv("MAIL_CUSTOMER_CC_ADMIN", "admin@monedatechnologies.com")
    MAIL_CUSTOMER_CC_ADMIN_ENABLED = env_bool("MAIL_CUSTOMER_CC_ADMIN_ENABLED", False)
    MAIL_CUSTOMER_BCC_OPERATIONS = os.getenv("MAIL_CUSTOMER_BCC_OPERATIONS", "operations@chemo.in")
    MAIL_CUSTOMER_BCC_OPERATIONS_ENABLED = env_bool("MAIL_CUSTOMER_BCC_OPERATIONS_ENABLED", True)
    # A stable, deployment-only key is preferred. SECRET_KEY is the secure
    # backwards-compatible fallback so existing deployments can connect.
    INTEGRATION_ENCRYPTION_KEY = os.getenv("INTEGRATION_ENCRYPTION_KEY") or SECRET_KEY
    EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "zoho_mail_api")
    EXCHANGE_RATE_PROVIDER = os.getenv("EXCHANGE_RATE_PROVIDER", "frankfurter")
    EXCHANGE_RATE_API_KEY = os.getenv("EXCHANGE_RATE_API_KEY", "")
    EXCHANGE_RATE_CACHE_SECONDS = int(os.getenv("EXCHANGE_RATE_CACHE_SECONDS", "21600"))
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    WHATSAPP_ENABLED = env_bool("WHATSAPP_ENABLED", False)


class TestConfig(Config):
    TESTING = True
    DEMO_MODE = True
    AUTO_SEED = True
    # Tests must never inherit or mutate a developer/production MongoDB.
    MONGODB_URI = ""
    MONGODB_DATABASE = "moneda-test-memory"
    RATELIMIT_STORAGE_URI = "memory://"
    DEV_AUTH_BYPASS = True
    SECRET_KEY = "test-secret-key"
