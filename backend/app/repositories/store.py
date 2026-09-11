from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import ConfigurationError, OperationFailure, PyMongoError, ServerSelectionTimeoutError


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
        try:
            self.client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            self.client.admin.command("ping")
            self.db = self.client[database]
            self._indexes()
        except OperationFailure as exc:
            raise RuntimeError("MongoDB authentication or authorization failed") from exc
        except ConfigurationError as exc:
            raise RuntimeError("MongoDB connection string or DNS configuration is invalid") from exc
        except ServerSelectionTimeoutError as exc:
            raise RuntimeError("MongoDB network, DNS, TLS, or timeout failure") from exc
        except PyMongoError as exc:
            raise RuntimeError("MongoDB connection failed") from exc

    def _indexes(self) -> None:
        self.db.users.create_index("email", unique=True)
        self.db.users.create_index("username", unique=True, sparse=True)
        self.db.users.create_index("username_normalized", unique=True, sparse=True)
        self.db.companies.create_index("name")
        self.db.products.create_index([("category_id", ASCENDING), ("active", ASCENDING)])
        self.db.customers.create_index([("customer_id", ASCENDING), ("name", ASCENDING)])
        self.db.customers.create_index("customer_code", unique=True, sparse=True)
        self.db.carts.create_index([("user_id", ASCENDING), ("customer_id", ASCENDING)], unique=True)
        self.db.customers.create_index([("company_id", ASCENDING), ("name", ASCENDING)])  # legacy bridge
        self.db.customers.create_index("assigned_user_ids")
        self.db.customers.create_index("created_by_user_id")
        self.db.quotations.create_index("quotation_number", unique=True)
        self.db.quotations.create_index([("customer_id", ASCENDING), ("created_at", DESCENDING)])
        self.db.quotations.create_index([("company_id", ASCENDING), ("created_at", DESCENDING)])  # legacy bridge
        self.db.quotations.create_index([("created_by_user_id", ASCENDING), ("created_at", DESCENDING)])
        self.db.quotations.create_index([("currency", ASCENDING), ("status", ASCENDING), ("created_at", DESCENDING)])
        self.db.quotations.create_index([("user_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True, sparse=True)
        self.db.orders.create_index([("customer_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True, sparse=True)
        self.db.orders.create_index([("company_id", ASCENDING), ("idempotency_key", ASCENDING)], unique=True, sparse=True)  # legacy bridge
        self.db.leads.create_index([("customer_id", ASCENDING), ("status", ASCENDING)])
        self.db.leads.create_index([("company_id", ASCENDING), ("status", ASCENDING)])  # legacy bridge
        self.db.reminders.create_index([("assigned_to", ASCENDING), ("due_date", ASCENDING)])
        self.db.audit_logs.create_index("created_at")
        self.db.otp_challenges.create_index("expires_at", expireAfterSeconds=0)
        self.db.pending_signups.create_index("expires_at", expireAfterSeconds=0)
        self.db.email_logs.create_index([("created_at", DESCENDING), ("status", ASCENDING)])
        self.db.oauth_states.create_index("expires_at", expireAfterSeconds=0)
        self.db.oauth_states.create_index("transaction_hash", unique=True, sparse=True)
        self.db.devices.create_index([("user_id", ASCENDING), ("token_hash", ASCENDING)], unique=True)
        self.db.devices.create_index([("user_id", ASCENDING), ("device_status", ASCENDING)])
        self.db.login_approvals.create_index("expires_at", expireAfterSeconds=0)
        self.db.login_approvals.create_index("token_hash", unique=True)
        self.db.login_approvals.create_index([("user_id", ASCENDING), ("status", ASCENDING)])
        self.db.orders.create_index("quotation_id")
        self.db.payments.create_index([("order_id", ASCENDING), ("status", ASCENDING)])
        self.db.incentives.create_index([("salesperson_id", ASCENDING), ("status", ASCENDING)])
        self.db.incentives.create_index("order_id", unique=True)
        self.db.credit_notes.create_index([("order_id", ASCENDING), ("created_at", DESCENDING)])

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
