from __future__ import annotations

import pytest
from pymongo.errors import AutoReconnect, ConfigurationError, OperationFailure

from app.repositories import store as store_module
from app.repositories.store import MongoStartupError, MongoStore, _classify_mongo_startup_error


def test_dns_srv_resolution_timeout_is_transient_network() -> None:
    error = ConfigurationError(
        "The resolution lifetime expired after 5.108 seconds: "
        "DNS operation timed out"
    )

    assert _classify_mongo_startup_error(error, "client_creation") == (
        "transient_network", "dns_resolution", True
    )


def test_malformed_configuration_is_not_retryable() -> None:
    error = ConfigurationError("Invalid URI: mongodb+srv:// has no hostname")

    assert _classify_mongo_startup_error(error, "client_creation") == (
        "configuration", "configuration", False
    )


def test_authentication_failure_is_not_retryable() -> None:
    error = OperationFailure("Authentication failed", code=18)

    assert _classify_mongo_startup_error(error, "server_selection_ping") == (
        "authentication", "authentication", False
    )


def test_valid_configuration_initializes_with_existing_client_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeAdmin:
        def command(self, name: str) -> dict[str, int]:
            assert name == "ping"
            return {"ok": 1}

    class FakeClient:
        admin = FakeAdmin()

        def __getitem__(self, database: str) -> object:
            assert database == "moneda"
            return object()

        def close(self) -> None:
            return None

    def make_client(uri: str, **options: object) -> FakeClient:
        calls.append((uri, options))
        return FakeClient()

    monkeypatch.setattr(store_module, "MongoClient", make_client)
    monkeypatch.setattr(MongoStore, "_initialize_indexes_with_retry", lambda _self, _database: None)

    MongoStore("mongodb+srv://cluster.example.mongodb.net", "moneda", startup_max_attempts=1)

    assert calls == [
        (
            "mongodb+srv://cluster.example.mongodb.net",
            {
                "serverSelectionTimeoutMS": 10_000,
                "connectTimeoutMS": 5_000,
                "socketTimeoutMS": 10_000,
                "retryWrites": True,
                "retryReads": True,
                "appname": "moneda-api",
            },
        )
    ]


def test_startup_retries_connection_reset_with_exponential_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    failures = [AutoReconnect("socket reset")]
    sleeps: list[float] = []
    calls = 0

    def connect_once(self: MongoStore, _uri: str, _database: str, _metadata: dict[str, str]) -> None:
        nonlocal calls
        calls += 1
        if failures:
            self._startup_stage = "server_selection_ping"
            raise failures.pop(0)
        self.client = None
        self.db = object()

    monkeypatch.setattr(MongoStore, "_connect_and_initialize", connect_once)
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    MongoStore("mongodb://localhost:27017", "moneda", startup_max_attempts=3, startup_backoff_seconds=0.5)

    assert calls == 2
    assert sleeps == [0.5]


def test_startup_retry_exhaustion_fails_with_category(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    def always_dns_fails(self: MongoStore, _uri: str, _database: str, _metadata: dict[str, str]) -> None:
        self._startup_stage = "client_creation"
        raise ConfigurationError("The resolution lifetime expired; DNS operation timed out")

    monkeypatch.setattr(MongoStore, "_connect_and_initialize", always_dns_fails)
    monkeypatch.setattr(store_module.time, "sleep", sleeps.append)

    with pytest.raises(MongoStartupError) as raised:
        MongoStore("mongodb+srv://cluster.example.mongodb.net", "moneda", startup_max_attempts=3, startup_backoff_seconds=0.25)

    error = raised.value
    assert error.category == "transient_network"
    assert error.stage == "dns_resolution"
    assert error.attempts == 3
    assert "DNS/network connectivity failed" in str(error)
    assert sleeps == [0.25, 0.5]
