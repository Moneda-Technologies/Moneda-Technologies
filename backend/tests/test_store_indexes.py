from __future__ import annotations

import pytest
from pymongo.errors import AutoReconnect, OperationFailure

from app.repositories import store as store_module
from app.repositories.store import MongoStore


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
