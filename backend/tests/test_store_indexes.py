from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import AutoReconnect, OperationFailure

from app.repositories import store as store_module
from app.repositories.store import MongoStore, _is_legacy_incentive_rule_index


def _store_without_connecting() -> MongoStore:
    return MongoStore.__new__(MongoStore)


def test_index_initialization_succeeds_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store_without_connecting()
    calls: list[int] = []
    sleeps: list[float] = []
    store._indexes = lambda: calls.append(1)  # type: ignore[method-assign]
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    store._initialize_indexes_with_retry("moneda")

    assert calls == [1]
    assert sleeps == []


def test_index_initialization_retries_one_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store_without_connecting()
    failures = [AutoReconnect("socket reset")]
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky_indexes() -> None:
        calls.append(1)
        if failures:
            raise failures.pop(0)

    store._indexes = flaky_indexes  # type: ignore[method-assign]
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    store._initialize_indexes_with_retry("moneda")

    assert len(calls) == 2
    assert sleeps == [store_module._INDEX_INIT_BACKOFF_SECONDS]


def test_index_initialization_retries_multiple_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store_without_connecting()
    failures = [AutoReconnect("reset 1"), AutoReconnect("reset 2")]
    calls: list[int] = []
    sleeps: list[float] = []

    def flaky_indexes() -> None:
        calls.append(1)
        if failures:
            raise failures.pop(0)

    store._indexes = flaky_indexes  # type: ignore[method-assign]
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    store._initialize_indexes_with_retry("moneda")

    assert len(calls) == 3
    assert sleeps == [
        store_module._INDEX_INIT_BACKOFF_SECONDS,
        store_module._INDEX_INIT_BACKOFF_SECONDS * 2,
    ]


def test_index_initialization_exhausted_transient_failures_are_not_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store_without_connecting()
    calls: list[int] = []

    def always_fails() -> None:
        calls.append(1)
        raise AutoReconnect("reset")

    store._indexes = always_fails  # type: ignore[method-assign]
    monkeypatch.setattr(store_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(AutoReconnect):
        store._initialize_indexes_with_retry("moneda")

    assert len(calls) == store_module._INDEX_INIT_MAX_ATTEMPTS


def test_non_transient_index_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store_without_connecting()
    calls: list[int] = []
    error = OperationFailure("invalid index specification", 67)

    def always_fails() -> None:
        calls.append(1)
        raise error

    store._indexes = always_fails  # type: ignore[method-assign]
    monkeypatch.setattr(store_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(OperationFailure):
        store._initialize_indexes_with_retry("moneda")

    assert calls == [1]


def test_index_helper_preserves_ttl_and_unique_options() -> None:
    class FakeCollection:
        def __init__(self) -> None:
            self.calls: list[tuple[object, dict[str, object]]] = []

        def create_index(self, keys: object, **options: object) -> str:
            self.calls.append((keys, options))
            return "test_index"

    login_approvals = FakeCollection()
    users = FakeCollection()

    class FakeDb:
        def __init__(self) -> None:
            self.login_approvals = login_approvals
            self.users = users

        def __getitem__(self, collection: str) -> FakeCollection:
            return getattr(self, collection)

    store = _store_without_connecting()
    store.db = FakeDb()

    store._create_index("login_approvals", "expires_at", expireAfterSeconds=0)
    store._create_index("users", "email", unique=True)

    assert login_approvals.calls == [("expires_at", {"expireAfterSeconds": 0})]
    assert users.calls == [("email", {"unique": True})]


@pytest.mark.parametrize(
    "index_info",
    [
        {
            "unique": True,
            "key": [
                ("client_type", 1),
                ("recipient_role", 1),
                ("category_id", 1),
            ],
        },
        {
            "unique": True,
            "key": [
                ("allocation_type", 1),
                ("client_type", 1),
                ("recipient_role", 1),
                ("category_id", 1),
            ],
        },
    ],
)
def test_incentive_index_migration_recognizes_both_legacy_unique_shapes(index_info: dict[str, object]) -> None:
    assert _is_legacy_incentive_rule_index(index_info)


@pytest.mark.parametrize(
    "index_info",
    [
        {
            "unique": False,
            "key": [
                ("allocation_type", 1),
                ("client_type", 1),
                ("recipient_role", 1),
                ("category_id", 1),
            ],
        },
        {
            "unique": True,
            "key": [
                ("rule_kind", 1),
                ("allocation_type", 1),
                ("client_type", 1),
                ("recipient_role", 1),
                ("category_id", 1),
                ("customer_id", 1),
                ("product_id", 1),
            ],
        },
    ],
)
def test_incentive_index_migration_does_not_drop_current_or_non_unique_indexes(index_info: dict[str, object]) -> None:
    assert not _is_legacy_incentive_rule_index(index_info)


class _FakeCollection:
    def __init__(self, indexes: dict[str, dict[str, object]] | None = None, documents: list[dict[str, Any]] | None = None) -> None:
        self.indexes = indexes or {}
        self.documents = documents or []
        self.dropped: list[str] = []
        self.archived: list[dict[str, Any]] = []

    def create_index(self, keys: object, **options: object) -> str:
        if isinstance(keys, str):
            key = [(keys, 1)]
            name = keys + "_1"
        else:
            key = [(str(field), int(direction)) for field, direction in keys]  # type: ignore[misc]
            name = "_".join(f"{field}_{direction}" for field, direction in key)
        self.indexes[name] = {"key": key, **options}
        return name

    def index_information(self) -> dict[str, dict[str, object]]:
        return dict(self.indexes)

    def drop_index(self, name: str) -> None:
        self.dropped.append(name)
        self.indexes.pop(name, None)

    def find(self, query: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = query or {}
        if query.get("rule_kind", {}).get("$exists") is False:
            return [row for row in self.documents if "rule_kind" not in row]
        return list(self.documents)

    def update_one(self, query: dict[str, Any], changes: dict[str, Any]) -> None:
        row = next(row for row in self.documents if row.get("_id") == query.get("_id"))
        row.update(changes.get("$set", {}))

    def delete_one(self, query: dict[str, Any]) -> None:
        self.documents[:] = [row for row in self.documents if row.get("_id") != query.get("_id")]

    def replace_one(self, _query: dict[str, Any], document: dict[str, Any], *, upsert: bool = False) -> None:
        if upsert:
            self.archived.append(document)

    def aggregate(self, _pipeline: list[dict[str, Any]]) -> Any:
        return iter(())


class _FakeDb:
    def __init__(self, incentive_rules: _FakeCollection) -> None:
        self._collections: dict[str, _FakeCollection] = {"incentive_rules": incentive_rules}

    def __getattr__(self, name: str) -> _FakeCollection:
        return self._collections.setdefault(name, _FakeCollection())

    def __getitem__(self, name: str) -> _FakeCollection:
        return self.__getattr__(name)


def test_incentive_rule_migration_replaces_legacy_index_and_preserves_default_and_maximum() -> None:
    legacy_name = "allocation_type_1_client_type_1_recipient_role_1_category_id_1"
    documents = [
        {
            "_id": "default-dealer-blankets",
            "allocation_type": "creator",
            "client_type": "DEALER",
            "recipient_role": "user",
            "category_id": "blankets",
            "rate": 3.0,
            "active": True,
        },
        {
            "_id": "maximum-dealer-blankets",
            "rule_kind": "maximum",
            "allocation_type": "creator",
            "client_type": "DEALER",
            "recipient_role": "user",
            "category_id": "blankets",
            "customer_id": "*",
            "product_id": "*",
            "rate": 5.0,
            "maximum_rate": 5.0,
            "active": True,
        },
    ]
    incentive_rules = _FakeCollection({
        "_id_": {"key": [("_id", 1)]},
        legacy_name: {
            "key": [
                ("allocation_type", 1),
                ("client_type", 1),
                ("recipient_role", 1),
                ("category_id", 1),
            ],
            "unique": True,
        },
    }, documents)
    store = _store_without_connecting()
    store.db = _FakeDb(incentive_rules)

    store._indexes()

    assert legacy_name in incentive_rules.dropped
    new_index = next(info for info in incentive_rules.indexes.values() if _is_legacy_incentive_rule_index(info) is False and info.get("unique") and "rule_kind" in dict(info["key"]))
    assert [field for field, _direction in new_index["key"]] == [
        "rule_kind", "allocation_type", "client_type", "recipient_role",
        "category_id", "customer_id", "product_id",
    ]
    assert {row["rule_kind"] for row in documents} == {"default", "maximum"}
    assert {row["rate"] for row in documents} == {3.0, 5.0}
    assert next(row for row in documents if row["rule_kind"] == "maximum")["maximum_rate"] == 5.0

    # A second complete index pass is idempotent and retains both documents.
    store._indexes()
    assert len(documents) == 2
    assert not _is_legacy_incentive_rule_index(next(info for info in incentive_rules.indexes.values() if info.get("unique") and "rule_kind" in dict(info["key"])))


def test_incentive_rule_migration_refuses_to_delete_duplicate_documents() -> None:
    legacy_name = "allocation_type_1_client_type_1_recipient_role_1_category_id_1"
    documents = [
        {
            "_id": "duplicate-1",
            "rule_kind": "default",
            "allocation_type": "creator",
            "client_type": "DEALER",
            "recipient_role": "user",
            "category_id": "blankets",
            "customer_id": "*",
            "product_id": "*",
            "rate": 3.0,
        },
        {
            "_id": "duplicate-2",
            "rule_kind": "default",
            "allocation_type": "creator",
            "client_type": "DEALER",
            "recipient_role": "user",
            "category_id": "blankets",
            "customer_id": "*",
            "product_id": "*",
            "rate": 4.0,
        },
    ]
    incentive_rules = _FakeCollection(
        {
            "_id_": {"key": [("_id", 1)]},
            legacy_name: {
                "key": [
                    ("allocation_type", 1),
                    ("client_type", 1),
                    ("recipient_role", 1),
                    ("category_id", 1),
                ],
                "unique": True,
            },
        },
        documents,
    )
    store = _store_without_connecting()
    store.db = _FakeDb(incentive_rules)

    with pytest.raises(RuntimeError, match="explicit audited repair"):
        store._indexes()

    assert [row["_id"] for row in documents] == ["duplicate-1", "duplicate-2"]
    assert legacy_name in incentive_rules.indexes
    assert incentive_rules.dropped == []
