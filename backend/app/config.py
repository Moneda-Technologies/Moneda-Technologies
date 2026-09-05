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
    TESTING = False
    MONGODB_URI = os.getenv("MONGODB_URI", "")
    MONGODB_DATABASE = os.getenv("MONGODB_DATABASE", "moneda")
    DEMO_MODE = env_bool("DEMO_MODE", not bool(MONGODB_URI))
    # JSON is bootstrap input. Production MongoDB is the runtime authority and
    # is never silently reseeded during a web-process restart.
    AUTO_SEED = env_bool("AUTO_SEED", DEMO_MODE)
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
    MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "zoho")
    MAIL_HOST = os.getenv("MAIL_HOST", "smtp.zoho.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = env_bool("MAIL_USE_TLS", True)
    MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
    MAIL_GENERAL_FROM = os.getenv("MAIL_GENERAL_FROM") or os.getenv("MAIL_FROM", "business@monedatechnologies.com")
    MAIL_QUOTATION_FROM = os.getenv("MAIL_QUOTATION_FROM") or MAIL_GENERAL_FROM
    MAIL_ORDER_FROM = os.getenv("MAIL_ORDER_FROM") or MAIL_GENERAL_FROM
    # Backward-compatible alias for integrations that still read MAIL_FROM.
    MAIL_FROM = MAIL_GENERAL_FROM
    MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Moneda Technologies")
    EXCHANGE_RATE_PROVIDER = os.getenv("EXCHANGE_RATE_PROVIDER", "frankfurter")
    EXCHANGE_RATE_API_KEY = os.getenv("EXCHANGE_RATE_API_KEY", "")
    EXCHANGE_RATE_CACHE_SECONDS = int(os.getenv("EXCHANGE_RATE_CACHE_SECONDS", "21600"))
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    WHATSAPP_ENABLED = env_bool("WHATSAPP_ENABLED", False)


class TestConfig(Config):
    TESTING = True
    DEMO_MODE = True
    AUTO_SEED = True
    DEV_AUTH_BYPASS = True
    SECRET_KEY = "test-secret-key"
