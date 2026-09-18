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
            if "$gte" in expected and (actual is None or actual < expected["$gte"]):
                return False
            if "$lte" in expected and (actual is None or actual > expected["$lte"]):
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
    def __init__(self, uri: str, database: str) -> None:
        metadata = _mongo_connection_metadata(uri, database)
        stage = "client_creation"
        _LOGGER.info(
            "mongodb startup connection configured=%s scheme=%s host=%s database=%s tls=%s",
            metadata["configured"], metadata["scheme"], metadata["host"], metadata["database"], metadata["tls"],
        )
        try:
            # Keep startup bounded while allowing Atlas SRV discovery and a
            # transient socket reset to recover within the index retry window. These options do not
            # mask a failure: ping and index initialization still have to
            # complete before the application is considered healthy.
            self.client = MongoClient(
                uri,
                serverSelectionTimeoutMS=10_000,
                connectTimeoutMS=5_000,
                socketTimeoutMS=10_000,
                retryWrites=True,
                retryReads=True,
                appname="moneda-api",
            )
            stage = "server_selection_ping"
            self.client.admin.command("ping")
            _LOGGER.info("mongodb startup ping=ok host=%s database=%s", metadata["host"], metadata["database"])
            self.db = self.client[database]
            stage = "index_initialization"
            self._initialize_indexes_with_retry(metadata["database"])
            _LOGGER.info("mongodb startup indexes=ready database=%s", metadata["database"])
        except OperationFailure as exc:
            _LOGGER.error(
                "mongodb startup failed category=authentication_or_authorization stage=%s host=%s database=%s error_type=%s error=%s",
                stage, metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
            )
            raise RuntimeError("MongoDB authentication or authorization failed") from exc
        except ConfigurationError as exc:
            _LOGGER.error(
                "mongodb startup failed category=configuration_or_dns stage=%s host=%s database=%s error_type=%s error=%s",
                stage, metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
            )
            raise RuntimeError("MongoDB connection string or DNS configuration is invalid") from exc
        except ServerSelectionTimeoutError as exc:
            _LOGGER.error(
                "mongodb startup failed category=server_selection_network_tls stage=%s host=%s database=%s error_type=%s error=%s",
                stage, metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
            )
            raise RuntimeError("MongoDB network, DNS, TLS, or timeout failure") from exc
        except AutoReconnect as exc:
            _LOGGER.error(
                "mongodb startup failed category=transient_network_reset stage=%s host=%s database=%s error_type=%s error=%s",
                stage, metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
            )
            raise RuntimeError("MongoDB connection was reset during startup") from exc
        except PyMongoError as exc:
            _LOGGER.error(
                "mongodb startup failed category=driver_or_index stage=%s host=%s database=%s error_type=%s error=%s",
                stage, metadata["host"], metadata["database"], type(exc).__name__, _safe_mongo_error(exc),
            )
            raise RuntimeError("MongoDB connection failed") from exc

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
        self._create_index("payments", [("order_id", ASCENDING), ("status", ASCENDING)])
        self._create_index("user_bank_details", "user_id", unique=True)
        self._create_index("incentives", [("salesperson_id", ASCENDING), ("status", ASCENDING)])
        self._create_index("incentives", "order_id", unique=True)
        self._create_index("credit_notes", [("order_id", ASCENDING), ("created_at", DESCENDING)])
        self._create_index("incentive_adjustments", [("incentive_id", ASCENDING), ("created_at", DESCENDING)])
        # Category is the configuration key.  Drop the pre-category product
        # index when upgrading an existing Mongo database so multiple category
        # rows can coexist for one user.
        self._current_index_operation = "incentive_configurations.user_id_1_product_id_1.index_information"
        if "user_id_1_product_id_1" in self.db.incentive_configurations.index_information():
            self._current_index_operation = "incentive_configurations.user_id_1_product_id_1.drop"
            self.db.incentive_configurations.drop_index("user_id_1_product_id_1")
        self._create_index("incentive_configurations", [("user_id", ASCENDING), ("category_id", ASCENDING)], unique=True)
        # ``allocation_type`` is part of the rule scope: a manager override
        # and a creator rule may legitimately share client/role/category while
        # carrying different rates. Replace the legacy three-field unique
        # index before seeding those distinct scopes.
        incentive_rules = self.db.incentive_rules
        legacy_rule_key = [("client_type", ASCENDING), ("recipient_role", ASCENDING), ("category_id", ASCENDING)]
        self._current_index_operation = "incentive_rules.index_information"
        for name, info in incentive_rules.index_information().items():
            if info.get("unique") and list(info.get("key", [])) == legacy_rule_key:
                self._current_index_operation = f"incentive_rules.{name}.drop"
                incentive_rules.drop_index(name)
        # A database created before the corrected scope index may contain
        # duplicate documents. Preserve the most recently updated active row
        # and archive every other row before enforcing the new constraint.
        grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = {}
        self._current_index_operation = "incentive_rules.find"
        for row in incentive_rules.find({}):
            key = (
                str(row.get("allocation_type") or ""),
                str(row.get("client_type") or ""),
                str(row.get("recipient_role") or ""),
                str(row.get("category_id") or ""),
            )
            grouped.setdefault(key, []).append(row)
        for key, rows in grouped.items():
            if len(rows) < 2:
                continue
            def _rule_timestamp(row: dict[str, Any]) -> datetime:
                value = row.get("updated_at") or row.get("created_at")
                if not isinstance(value, datetime):
                    return datetime.min.replace(tzinfo=timezone.utc)
                return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
            rows.sort(key=lambda row: (
                row.get("active") is not False,
                _rule_timestamp(row),
                str(row.get("_id") or ""),
            ), reverse=True)
            winner = rows[0]
            for duplicate in rows[1:]:
                duplicate_id = duplicate.get("_id")
                if not duplicate_id:
                    continue
                self._current_index_operation = f"incentive_rule_archives.duplicate:{duplicate_id}.replace"
                self.db.incentive_rule_archives.replace_one(
                    {"_id": f"duplicate:{duplicate_id}"},
                    {
                        "_id": f"duplicate:{duplicate_id}",
                        "source_id": duplicate_id,
                        "winner_id": winner.get("_id"),
                        "scope": key,
                        "reason": "duplicate_incentive_rule_scope",
                        "archived_at": utcnow(),
                        "document": duplicate,
                    },
                    upsert=True,
                )
                self._current_index_operation = f"incentive_rules.{duplicate_id}.delete"
                incentive_rules.delete_one({"_id": duplicate_id})
        self._create_index("incentive_rules", [
            ("allocation_type", ASCENDING), ("client_type", ASCENDING),
            ("recipient_role", ASCENDING), ("category_id", ASCENDING),
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
        insert_document = {**copy.deepcopy(document), "created_at": now, "updated_at": now}
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
        return MongoStore(config["MONGODB_URI"], config["MONGODB_DATABASE"])
    raise RuntimeError("MONGODB_URI is required when DEMO_MODE is disabled")
