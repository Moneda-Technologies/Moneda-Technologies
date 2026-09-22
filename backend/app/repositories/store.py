from __future__ import annotations

import copy
import logging
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import (
    AutoReconnect,
    ConfigurationError,
    ConnectionFailure,
    NetworkTimeout,
    OperationFailure,
    PyMongoError,
    ServerSelectionTimeoutError,
)


_MONGO_URI_CREDENTIALS = re.compile(r"(mongodb(?:\+srv)?://)([^@\s]+)@", re.IGNORECASE)
_LOGGER = logging.getLogger(__name__)
_INDEX_INIT_MAX_ATTEMPTS = 3
_INDEX_INIT_BACKOFF_SECONDS = 0.25
_INDEX_TRANSIENT_ERRORS = (AutoReconnect, ConnectionFailure, NetworkTimeout, ServerSelectionTimeoutError)
_STARTUP_MAX_ATTEMPTS = 3
_STARTUP_BACKOFF_SECONDS = 1.0

# Older deployments used either of these unique scopes for incentive rules.
# Neither includes ``rule_kind`` (and one also omitted ``allocation_type``),
# so it prevents the intentional default/maximum pair from coexisting. Keep
# the exact key order here: unrelated unique indexes must never be dropped by
# the startup migration.
_LEGACY_INCENTIVE_RULE_INDEX_KEYS = frozenset({
    (
        ("client_type", ASCENDING),
        ("recipient_role", ASCENDING),
        ("category_id", ASCENDING),
    ),
    (
        ("allocation_type", ASCENDING),
        ("client_type", ASCENDING),
        ("recipient_role", ASCENDING),
        ("category_id", ASCENDING),
    ),
})


def _is_legacy_incentive_rule_index(info: dict[str, Any]) -> bool:
    """Return whether an index is the obsolete incentive-rule uniqueness constraint."""
    return bool(info.get("unique")) and tuple(info.get("key", [])) in _LEGACY_INCENTIVE_RULE_INDEX_KEYS


class MongoStartupError(RuntimeError):
    """A safe, categorized failure raised when MongoDB cannot initialize."""

    def __init__(self, message: str, *, category: str, stage: str, attempts: int) -> None:
        super().__init__(message)
        self.category = category
        self.stage = stage
        self.attempts = attempts


def _is_dns_resolution_error(error: BaseException) -> bool:
    """Identify PyMongo's SRV/DNS timeout without treating every config error as transient."""
    message = str(error).lower()
    return any(marker in message for marker in (
        "resolution lifetime expired",
        "dns operation timed out",
        "temporary failure in name resolution",
        "name or service not known",
        "getaddrinfo failed",
        "no such host",
        "cannot resolve",
    ))


def _classify_mongo_startup_error(error: BaseException, stage: str) -> tuple[str, str, bool]:
    """Return ``(category, diagnostic_stage, retryable)`` for a startup error."""
    if isinstance(error, OperationFailure):
        message = str(error).lower()
        code = getattr(error, "code", None)
        if code in {11, 13, 18, 8000} or any(marker in message for marker in (
            "authentication", "unauthorized", "not authorized", "bad auth",
        )):
            return "authentication", "authentication", False
        return "database_operation", stage, False
    if isinstance(error, ConfigurationError):
        if _is_dns_resolution_error(error):
            return "transient_network", "dns_resolution", True
        return "configuration", "configuration", False
    # Check the most specific transient errors before their PyMongo base classes.
    if isinstance(error, ServerSelectionTimeoutError):
        return "server_unavailable", "server_selection", True
    if isinstance(error, NetworkTimeout):
        return "transient_network", "network_timeout", True
    if isinstance(error, AutoReconnect):
        return "connection_reset", "connection_reset", True
    if isinstance(error, ConnectionFailure):
        return "server_unavailable", "connection", True
    if isinstance(error, ValueError) and stage == "client_creation":
        return "configuration", "configuration", False
    return "driver", stage, False


def _mongo_startup_message(category: str, stage: str, attempts: int) -> str:
    messages = {
        "configuration": "MongoDB connection string/configuration is invalid",
        "transient_network": "MongoDB DNS/network connectivity failed during startup",
        "connection_reset": "MongoDB connection was reset during startup",
        "server_unavailable": "MongoDB server was unavailable during startup",
        "authentication": "MongoDB authentication or authorization failed",
        "database_operation": "MongoDB rejected a startup database operation",
        "driver": "MongoDB startup failed",
    }
    return f"{messages.get(category, messages['driver'])} (category={category}, stage={stage}, attempts={attempts})"


def _mongo_connection_metadata(uri: str, database: str) -> dict[str, str]:
    """Return safe connection metadata; never include MongoDB credentials."""
    try:
        parts = urlsplit(uri)
        return {
            "configured": "true" if bool(uri) else "false",
            "scheme": parts.scheme or "unknown",
            "host": parts.hostname or "unknown",
            "database": database or "unknown",
            "tls": "srv-default" if parts.scheme.lower() == "mongodb+srv" else "uri-controlled",
        }
    except ValueError:
        return {
            "configured": "true" if bool(uri) else "false",
            "scheme": "invalid",
            "host": "unknown",
            "database": database or "unknown",
            "tls": "unknown",
        }


def _safe_mongo_error(error: BaseException) -> str:
    """Keep driver diagnostics useful while redacting credential-bearing URIs."""
    message = _MONGO_URI_CREDENTIALS.sub(r"\1[REDACTED]@", str(error))
    return message[:600]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize MongoDB/PyMongo datetimes to timezone-aware UTC values."""
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class Store(Protocol):
    def list(self, collection: str, query: dict[str, Any] | None = None, *, page: int = 1,
             limit: int = 50, sort: str = "created_at", direction: int = -1) -> tuple[list[dict[str, Any]], int]: ...
    def find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None: ...
    def insert_one(self, collection: str, document: dict[str, Any]) -> dict[str, Any]: ...
    def update_one(self, collection: str, query: dict[str, Any], changes: dict[str, Any], *, upsert: bool = False, unset_fields: list[str] | None = None) -> dict[str, Any] | None: ...
    def upsert_one(self, collection: str, query: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]: ...
    def unset_many(self, collection: str, query: dict[str, Any], fields: list[str]) -> int: ...
    def delete_one(self, collection: str, query: dict[str, Any]) -> bool: ...
    def count(self, collection: str, query: dict[str, Any] | None = None) -> int: ...
    def next_counter(self, name: str) -> int: ...
    def ensure_counter_at_least(self, name: str, value: int) -> None: ...
    def health(self) -> dict[str, Any]: ...


def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(document, clause) for clause in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(document, clause) for clause in expected):
                return False
            continue
        actual: Any = document
        for part in key.split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if isinstance(expected, dict):
            if "$exists" in expected and (actual is not None) != bool(expected["$exists"]):
                return False
            if "$in" in expected:
                values = actual if isinstance(actual, list) else [actual]
                if not any(value in expected["$in"] for value in values):
                    return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$regex" in expected and not re.search(str(expected["$regex"]), str(actual or ""), re.I):
                return False
            if "$gte" in expected:
                try:
                    if actual is None or actual < expected["$gte"]:
                        return False
                except TypeError:
                    return False
            if "$lte" in expected:
                try:
                    if actual is None or actual > expected["$lte"]:
                        return False
                except TypeError:
                    return False
        elif isinstance(actual, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


class MemoryStore:
    """Deterministic dev/test store. Production startup rejects it unless DEMO_MODE is explicit."""

    def __init__(self) -> None:
        self._data: dict[str, list[dict[str, Any]]] = {}
        self._counters: dict[str, int] = {}
        self._lock = threading.RLock()

    def list(self, collection: str, query: dict[str, Any] | None = None, *, page: int = 1,
             limit: int = 50, sort: str = "created_at", direction: int = -1) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            rows = [copy.deepcopy(row) for row in self._data.get(collection, []) if _matches(row, query or {})]
        rows.sort(key=lambda row: str(row.get(sort, "")), reverse=direction < 0)
        total = len(rows)
        start = max(page - 1, 0) * limit
        return rows[start:start + limit], total

    def find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None:
        rows, _ = self.list(collection, query, limit=1)
        return rows[0] if rows else None

    def insert_one(self, collection: str, document: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(document)
        row.setdefault("_id", str(uuid.uuid4()))
        row.setdefault("created_at", utcnow())
        row["updated_at"] = utcnow()
        with self._lock:
            self._data.setdefault(collection, []).append(row)
        return copy.deepcopy(row)

    def update_one(self, collection: str, query: dict[str, Any], changes: dict[str, Any], *, upsert: bool = False, unset_fields: list[str] | None = None) -> dict[str, Any] | None:
        with self._lock:
            for row in self._data.setdefault(collection, []):
                if _matches(row, query):
                    row.update(copy.deepcopy(changes))
                    for field in unset_fields or []:
                        row.pop(field, None)
                    row["updated_at"] = utcnow()
                    return copy.deepcopy(row)
        if upsert:
            return self.insert_one(collection, {**query, **changes})
        return None

    def upsert_one(self, collection: str, query: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
        """Create a document once, preserving it exactly on later calls."""
        with self._lock:
            for row in self._data.setdefault(collection, []):
                if _matches(row, query):
                    return copy.deepcopy(row)
            return self.insert_one(collection, {**query, **document})

    def delete_one(self, collection: str, query: dict[str, Any]) -> bool:
        with self._lock:
            rows = self._data.setdefault(collection, [])
            for index, row in enumerate(rows):
                if _matches(row, query):
                    rows.pop(index)
                    return True
        return False

    def unset_many(self, collection: str, query: dict[str, Any], fields: list[str]) -> int:
        changed = 0
        with self._lock:
            for row in self._data.setdefault(collection, []):
                if _matches(row, query):
                    for field in fields:
                        row.pop(field, None)
                    row["updated_at"] = utcnow()
                    changed += 1
        return changed

    def count(self, collection: str, query: dict[str, Any] | None = None) -> int:
        return self.list(collection, query, limit=100_000)[1]

    def next_counter(self, name: str) -> int:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + 1
            return self._counters[name]

    def ensure_counter_at_least(self, name: str, value: int) -> None:
        with self._lock:
            self._counters[name] = max(self._counters.get(name, 0), int(value))

    def health(self) -> dict[str, Any]:
        return {
            "connected": True,
            "engine": "memory",
            "database": "demo-memory",
            "collections": len(self._data),
            "indexes_ready": True,
        }


class MongoStore:
    def __init__(
        self,
        uri: str,
        database: str,
        *,
        startup_max_attempts: int = _STARTUP_MAX_ATTEMPTS,
        startup_backoff_seconds: float = _STARTUP_BACKOFF_SECONDS,
    ) -> None:
        metadata = _mongo_connection_metadata(uri, database)
        self.client: Any | None = None
        self.db: Any | None = None
        max_attempts = max(1, int(startup_max_attempts))
        backoff_seconds = max(0.0, float(startup_backoff_seconds))
        _LOGGER.info(
            "mongodb startup connection configured=%s scheme=%s host=%s database=%s tls=%s",
            metadata["configured"], metadata["scheme"], metadata["host"], metadata["database"], metadata["tls"],
        )
        for attempt in range(1, max_attempts + 1):
            stage = "client_creation"
            self._startup_stage = stage
            try:
                self._connect_and_initialize(uri, database, metadata)
            except (PyMongoError, ValueError) as exc:
                stage = getattr(self, "_startup_stage", stage)
                category, diagnostic_stage, retryable = _classify_mongo_startup_error(exc, stage)
                if retryable and attempt < max_attempts:
                    delay = backoff_seconds * (2 ** (attempt - 1))
                    _LOGGER.warning(
                        "mongodb startup retry category=%s stage=%s attempt=%s/%s backoff_seconds=%s "
                        "host=%s database=%s error_type=%s error=%s",
                        category, diagnostic_stage, attempt, max_attempts, delay,
                        metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
                    )
                    self._close_client()
                    if delay:
                        time.sleep(delay)
                    continue
                _LOGGER.error(
                    "mongodb startup failed category=%s stage=%s retryable=%s attempts=%s host=%s database=%s "
                    "error_type=%s error=%s",
                    category, diagnostic_stage, retryable, attempt,
                    metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
                )
                self._close_client()
                raise MongoStartupError(
                    _mongo_startup_message(category, diagnostic_stage, attempt),
                    category=category, stage=diagnostic_stage, attempts=attempt,
                ) from exc
            except RuntimeError:
                self._close_client()
                raise
            else:
                _LOGGER.info("mongodb startup complete host=%s database=%s attempts=%s", metadata["host"], database, attempt)
                return

    def _connect_and_initialize(self, uri: str, database: str, metadata: dict[str, str]) -> None:
        """Open a client, prove it is usable, then initialize indexes."""
        # Keep startup bounded while allowing Atlas SRV discovery and a
        # transient socket reset to recover. These options do not mask a
        # failure: ping and index initialization still have to complete.
        self._startup_stage = "client_creation"
        self.client = MongoClient(
            uri,
            serverSelectionTimeoutMS=10_000,
            connectTimeoutMS=5_000,
            socketTimeoutMS=10_000,
            retryWrites=True,
            retryReads=True,
            appname="moneda-api",
        )
        self._startup_stage = "server_selection_ping"
        self.client.admin.command("ping")
        _LOGGER.info("mongodb startup ping=ok host=%s database=%s", metadata["host"], metadata["database"])
        self.db = self.client[database]
        self._startup_stage = "index_initialization"
        self._initialize_indexes_with_retry(database)
        _LOGGER.info("mongodb startup indexes=ready database=%s", database)

    def _close_client(self) -> None:
        client = getattr(self, "client", None)
        if client is None:
            return
        try:
            client.close()
        except Exception:
            # A failed startup must not hide the original, categorized error.
            pass
        self.client = None
        self.db = None

    @staticmethod
    def _index_operation_label(collection: str, keys: Any) -> str:
        if isinstance(keys, str):
            return f"{collection}.{keys}"
        if isinstance(keys, (list, tuple)):
            fields: list[str] = []
            for item in keys:
                if isinstance(item, (list, tuple)) and item:
                    fields.append(str(item[0]))
                else:
                    fields.append(str(item))
            return f"{collection}.{'_'.join(fields)}"
        return f"{collection}.index"

    def _create_index(self, collection: str, keys: Any, **options: Any) -> str:
        """Create one index while recording a safe diagnostic operation label."""
        self._current_index_operation = self._index_operation_label(collection, keys)
        return self.db[collection].create_index(keys, **options)

    def _initialize_indexes_with_retry(self, database: str) -> None:
        """Ensure all indexes, retrying only transient connection failures.

        Index creation is idempotent in MongoDB, so retrying the complete bounded
        initialization pass is safe when a socket is reset halfway through it.
        Non-transient driver, authorization, duplicate-key, and specification
        errors are intentionally allowed to propagate immediately.
        """
        self._current_index_operation = "index_initialization"
        for attempt in range(1, _INDEX_INIT_MAX_ATTEMPTS + 1):
            _LOGGER.info(
                "mongodb index initialization stage=index_initialization attempt=%s/%s database=%s result=started",
                attempt,
                _INDEX_INIT_MAX_ATTEMPTS,
                database,
            )
            try:
                self._indexes()
            except _INDEX_TRANSIENT_ERRORS as exc:
                operation = getattr(self, "_current_index_operation", "index_initialization")
                if attempt >= _INDEX_INIT_MAX_ATTEMPTS:
                    _LOGGER.error(
                        "mongodb index initialization stage=index_initialization database=%s index=%s "
                        "result=failed retry_count=%s "
                        "error_type=%s error=%s",
                        database,
                        operation,
                        attempt,
                        type(exc).__name__,
                        _safe_mongo_error(exc),
                    )
                    raise
                _LOGGER.warning(
                    "mongodb index initialization stage=index_initialization database=%s index=%s "
                    "result=transient_network_error attempt=%s/%s error_type=%s error=%s",
                    database,
                    operation,
                    attempt,
                    _INDEX_INIT_MAX_ATTEMPTS,
                    type(exc).__name__,
                    _safe_mongo_error(exc),
                )
                time.sleep(_INDEX_INIT_BACKOFF_SECONDS * attempt)
            else:
                _LOGGER.info(
                    "mongodb index initialization stage=index_initialization database=%s result=success",
                    database,
                )
                return

    def _indexes(self) -> None:
        self._create_index("users", "email", unique=True)
        self._create_index("users", "username", unique=True, sparse=True)
        self._create_index("users", "username_normalized", unique=True, sparse=True)
        self._create_index("users", "manager_id")
        self._create_index("companies", "name")
        self._create_index("products", [("category_id", ASCENDING), ("active", ASCENDING)])
        self._create_index("customers", [("customer_id", ASCENDING), ("name", ASCENDING)])
        self._create_index("customers", "customer_code", unique=True, sparse=True)
        self._create_index("carts", [("user_id", ASCENDING), ("customer_id", ASCENDING)], unique=True)
        self._create_index("customers", [("company_id", ASCENDING), ("name", ASCENDING)])  # legacy bridge
        self._create_index("customers", "assigned_user_ids")
        self._create_index("customers", "created_by_user_id")
        self._create_index("customers", "client_type")
        self._create_index("quotations", "quotation_number", unique=True)
        self._create_index("quotations", [("customer_id", ASCENDING), ("created_at", DESCENDING)])
        self._create_index("quotations", [("company_id", ASCENDING), ("created_at", DESCENDING)])  # legacy bridge
        self._create_index("quotations", [("created_by_user_id", ASCENDING), ("created_at", DESCENDING)])
        self._create_index("quotations", [("currency", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
        self._create_index("quotations", [("user_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True, sparse=True)
        self._create_index("orders", [("customer_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True, sparse=True)
        self._create_index("orders", [("company_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True, sparse=True)  # legacy bridge
        self._create_index("leads", [("customer_id", ASCENDING), ("status", ASCENDING)])
        self._create_index("leads", [("company_id", ASCENDING), ("status", ASCENDING)])  # legacy bridge
        self._create_index("reminders", [("assigned_to", ASCENDING), ("due_date", ASCENDING)])
        self._create_index("audit_logs", "created_at")
        self._create_index("otp_challenges", "expires_at", expireAfterSeconds=0)
        self._create_index("pending_signups", "expires_at", expireAfterSeconds=0)
        self._create_index("email_logs", [("created_at", DESCENDING), ("status", ASCENDING)])
        self._create_index("oauth_states", "expires_at", expireAfterSeconds=0)
        self._create_index("oauth_states", "transaction_hash", unique=True, sparse=True)
        self._create_index("devices", [("user_id", ASCENDING), ("token_hash", ASCENDING)], unique=True)
        self._create_index("devices", [("user_id", ASCENDING), ("device_status", ASCENDING)])
        self._create_index("login_approvals", "expires_at", expireAfterSeconds=0)
        self._create_index("login_approvals", "token_hash", unique=True)
        self._create_index("login_approvals", [("user_id", ASCENDING), ("status", ASCENDING)])
        # A quotation can produce exactly one Order Confirmation.  The old
        # non-unique lookup index left concurrent workers able to insert two
        # orders before either request observed the other.  Upgrade that
        # index in place, but refuse to hide pre-existing duplicate business
        # records: those need an explicit audited repair.
        self._current_index_operation = "orders.quotation_id.duplicate_check"
        quotation_key = [("quotation_id", ASCENDING)]
        duplicate_quotation = next(self.db.orders.aggregate([
            {"$match": {"quotation_id": {"$exists": True, "$nin": [None, ""]}}},
            {"$group": {"_id": "$quotation_id", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
            {"$limit": 1},
        ]), None)
        if duplicate_quotation:
            raise RuntimeError(
                "Duplicate Order Confirmations exist for quotation "
                f"{duplicate_quotation.get('_id')}; repair them before startup"
            )
        self._current_index_operation = "orders.quotation_id.index_information"
        for name, info in self.db.orders.index_information().items():
            if list(info.get("key", [])) == quotation_key and not info.get("unique"):
                self._current_index_operation = f"orders.{name}.drop"
                self.db.orders.drop_index(name)
        self._create_index("orders", "quotation_id", unique=True, sparse=True)
        self._create_index("order_documents", "order_id", unique=True)
        self._create_index("order_confirmations", "source_order_id", unique=True, sparse=True)
        self._create_index("payments", [("order_id", ASCENDING), ("status", ASCENDING)])
        self._create_index("user_bank_details", "user_id", unique=True)
        self._create_index("incentives", [("salesperson_id", ASCENDING), ("status", ASCENDING)])
        self._create_index("incentives", "order_id", unique=True)
        self._create_index("credit_notes", [("order_id", ASCENDING), ("created_at", DESCENDING)])
        self._create_index("incentive_adjustments", [("incentive_id", ASCENDING), ("created_at", DESCENDING)])
        # Individual incentive configuration rows can be scoped by customer,
        # product, category, client type, or allocation.  The old unique
        # ``user_id + category_id`` index made those legitimate rows collide;
        # migrate them to a deterministic configuration key and retain a
        # non-unique lookup index for compatibility.
        configurations = self.db.incentive_configurations
        self._current_index_operation = "incentive_configurations.migrate_configuration_keys"

        def _configuration_key(row: dict[str, Any]) -> str:
            parts = (
                str(row.get("user_id") or row.get("recipient_user_id") or "*"),
                str(row.get("allocation_type") or "creator").strip().lower(),
                str(row.get("scope") or "category").strip().lower(),
                str(row.get("customer_id") or "*"),
                str(row.get("product_id") or "*"),
                str(row.get("category_id") or "*"),
                str(row.get("client_type") or "*").strip().upper(),
                str(row.get("role") or row.get("recipient_role") or "*").strip().lower(),
            )
            return "|".join(parts)

        seen_configuration_keys: set[str] = set()
        for row in configurations.find({}):
            if row.get("configuration_key"):
                key = str(row.get("configuration_key"))
            else:
                key = _configuration_key(row)
            # Do not overwrite a colliding legacy row.  A stable suffix keeps
            # both records available for an administrator to reconcile.
            if key in seen_configuration_keys:
                key = f"{key}|legacy:{row.get('_id')}"
            seen_configuration_keys.add(key)
            changes: dict[str, Any] = {"configuration_key": key}
            if not row.get("status"):
                changes["status"] = "ENABLED"
            configurations.update_one({"_id": row.get("_id")}, {"$set": changes})
        configuration_index_info = configurations.index_information()
        for name, info in configuration_index_info.items():
            if not info.get("unique"):
                continue
            key = list(info.get("key", []))
            if key == [("user_id", ASCENDING), ("category_id", ASCENDING)] or key == [("user_id", ASCENDING), ("product_id", ASCENDING)]:
                self._current_index_operation = f"incentive_configurations.{name}.drop"
                configurations.drop_index(name)
        self._create_index("incentive_configurations", "configuration_key", unique=True, sparse=True)
        self._create_index("incentive_configurations", [("user_id", ASCENDING), ("category_id", ASCENDING)])
        # ``allocation_type`` is part of the rule scope: a manager override
        # and a creator rule may legitimately share client/role/category while
        # carrying different rates. Replace the legacy three-field unique
        # index before seeding those distinct scopes.
        incentive_rules = self.db.incentive_rules
        # A database created before the corrected scope index may contain
        # duplicate documents. Never delete or overwrite administrator data
        # during startup; require an explicit audited repair before enforcing
        # the new constraint instead. Check before dropping the legacy index so
        # a repair-required startup cannot leave the collection unprotected.
        self._current_index_operation = "incentive_rules.find"
        rule_rows = list(incentive_rules.find({}))
        grouped: dict[tuple[str, str, str, str, str, str, str], list[dict[str, Any]]] = {}
        for row in rule_rows:
            key = (
                str(row.get("rule_kind") or "default").strip().lower(),
                str(row.get("allocation_type") or ""),
                str(row.get("client_type") or ""),
                str(row.get("recipient_role") or ""),
                str(row.get("category_id") or ""),
                str(row.get("customer_id") or ""),
                str(row.get("product_id") or ""),
            )
            grouped.setdefault(key, []).append(row)
        for key, rows in grouped.items():
            if len(rows) < 2:
                continue
            self._current_index_operation = "incentive_rules.duplicate_check"
            raise RuntimeError(
                "Duplicate incentive rule identity requires an explicit audited repair "
                f"before startup: scope={key} document_ids={[row.get('_id') for row in rows]}"
            )
        # Remove only the exact legacy uniqueness shapes. The replacement
        # index below enforces the complete logical rule identity.
        self._current_index_operation = "incentive_rules.index_information"
        for name, info in incentive_rules.index_information().items():
            if _is_legacy_incentive_rule_index(info):
                self._current_index_operation = f"incentive_rules.{name}.drop"
                incentive_rules.drop_index(name)
        # Legacy rows pre-date ``rule_kind`` and are actual/default rules.
        # Adding the field in place keeps their IDs and rates intact while
        # allowing separate maximum/ceiling rows for the same business scope.
        self._current_index_operation = "incentive_rules.migrate_rule_kind"
        for row in rule_rows:
            if "rule_kind" not in row:
                incentive_rules.update_one({"_id": row.get("_id")}, {"$set": {"rule_kind": "default"}})
        self._create_index("incentive_rules", [
            ("rule_kind", ASCENDING), ("allocation_type", ASCENDING),
            ("client_type", ASCENDING), ("recipient_role", ASCENDING),
            ("category_id", ASCENDING), ("customer_id", ASCENDING),
            ("product_id", ASCENDING),
        ], unique=True, sparse=True)
        self._create_index("incentive_allocations", [("incentive_id", ASCENDING), ("recipient_user_id", ASCENDING)])
        self._create_index("incentive_allocations", "allocation_key", unique=True, sparse=True)

    def list(self, collection: str, query: dict[str, Any] | None = None, *, page: int = 1,
             limit: int = 50, sort: str = "created_at", direction: int = -1) -> tuple[list[dict[str, Any]], int]:
        coll = self.db[collection]
        query = query or {}
        total = coll.count_documents(query)
        rows = list(coll.find(query).sort(sort, direction).skip(max(page - 1, 0) * limit).limit(limit))
        return rows, total

    def find_one(self, collection: str, query: dict[str, Any]) -> dict[str, Any] | None:
        return self.db[collection].find_one(query)

    def insert_one(self, collection: str, document: dict[str, Any]) -> dict[str, Any]:
        row = copy.deepcopy(document)
        row.setdefault("_id", str(uuid.uuid4()))
        row.setdefault("created_at", utcnow())
        row["updated_at"] = utcnow()
        self.db[collection].insert_one(row)
        return row

    def update_one(self, collection: str, query: dict[str, Any], changes: dict[str, Any], *, upsert: bool = False, unset_fields: list[str] | None = None) -> dict[str, Any] | None:
        update: dict[str, Any] = {"$set": {**changes, "updated_at": utcnow()}}
        if unset_fields:
            update["$unset"] = {field: "" for field in unset_fields}
        return self.db[collection].find_one_and_update(
            query, update, upsert=upsert,
            return_document=ReturnDocument.AFTER,
        )

    def upsert_one(self, collection: str, query: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
        """Insert a document if its business key is absent; never overwrite it."""
        now = utcnow()
        # Mongo upserts do not copy equality predicates into the inserted row.
        # Persist the business key too, otherwise a restart can seed a second
        # row because the next lookup cannot find the first one.
        equality_fields = {
            key: value for key, value in query.items()
            if not key.startswith("$") and not isinstance(value, dict)
        }
        insert_document = {**equality_fields, **copy.deepcopy(document), "created_at": now, "updated_at": now}
        return self.db[collection].find_one_and_update(
            query,
            {"$setOnInsert": insert_document},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    def unset_many(self, collection: str, query: dict[str, Any], fields: list[str]) -> int:
        result = self.db[collection].update_many(query, {"$unset": {field: "" for field in fields}})
        return int(result.modified_count)

    def delete_one(self, collection: str, query: dict[str, Any]) -> bool:
        return self.db[collection].delete_one(query).deleted_count == 1

    def count(self, collection: str, query: dict[str, Any] | None = None) -> int:
        return self.db[collection].count_documents(query or {})

    def next_counter(self, name: str) -> int:
        row = self.db.quotation_counters.find_one_and_update(
            {"_id": name}, {"$inc": {"sequence": 1}}, upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(row["sequence"])

    def ensure_counter_at_least(self, name: str, value: int) -> None:
        self.db.quotation_counters.update_one(
            {"_id": name}, {"$max": {"sequence": int(value)}}, upsert=True,
        )

    def health(self) -> dict[str, Any]:
        try:
            self.client.admin.command("ping")
            collections = self.db.list_collection_names()
            return {
                "connected": True,
                "engine": "mongodb",
                "database": self.db.name,
                "collections": len(collections),
                "indexes_ready": True,
            }
        except Exception:
            # Never expose connection strings or driver details in the health API.
            return {
                "connected": False,
                "engine": "mongodb",
                "database": self.db.name,
                "collections": 0,
                "indexes_ready": False,
            }


def build_store(config: dict[str, Any]) -> Store:
    if config.get("DEMO_MODE") or config.get("TESTING"):
        return MemoryStore()
    if config.get("MONGODB_URI"):
        return MongoStore(
            config["MONGODB_URI"],
            config["MONGODB_DATABASE"],
            startup_max_attempts=config.get("MONGODB_STARTUP_MAX_ATTEMPTS", _STARTUP_MAX_ATTEMPTS),
            startup_backoff_seconds=config.get("MONGODB_STARTUP_BACKOFF_SECONDS", _STARTUP_BACKOFF_SECONDS),
        )
    raise RuntimeError("MONGODB_URI is required when DEMO_MODE is disabled")
