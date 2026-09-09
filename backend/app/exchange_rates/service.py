from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.exchange_rates.provider import ExchangeRateProvider
from app.repositories.store import Store, utcnow


logger = logging.getLogger(__name__)


class ExchangeRateUnavailable(RuntimeError):
    pass


class ExchangeRateService:
    def __init__(self, store: Store, provider: ExchangeRateProvider, cache_seconds: int = 21600) -> None:
        self.store = store
        self.provider = provider
        self.cache_seconds = cache_seconds
        self._refresh_lock = threading.Lock()
        self._retry_not_before: datetime | None = None

    @staticmethod
    def _age_seconds(row: dict[str, Any]) -> float:
        value = row.get("fetched_at")
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            return float("inf")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (utcnow() - value).total_seconds()

    def get_rates(self, *, force: bool = False) -> dict[str, Any]:
        targets = ["USD", "INR"]
        cached = {target: self.store.find_one("exchange_rates", {"_id": f"EUR_{target}"}) for target in targets}
        now = utcnow()
        def is_fresh(row: dict[str, Any] | None) -> bool:
            if not self._is_valid_cached_row(row) or row.get("is_fallback"):
                return False
            expires_at = row.get("expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if isinstance(expires_at, datetime):
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                return expires_at > now
            return self._age_seconds(row) < self.cache_seconds

        fresh = all(is_fresh(cached.get(target)) for target in targets)
        if fresh and not force:
            return self._response(cached, stale=False, source="cached")

        # A request may arrive while another request is refreshing. Re-read the
        # cache after acquiring the lock so waiting callers share that result.
        with self._refresh_lock:
            cached = {target: self.store.find_one("exchange_rates", {"_id": f"EUR_{target}"}) for target in targets}
            fresh_after_lock = all(is_fresh(cached.get(target)) for target in targets)
            if fresh_after_lock and (not force or not fresh):
                return self._response(cached, stale=False, source="cached")
            now = utcnow()
            if self._retry_not_before and now < self._retry_not_before:
                if all(self._is_valid_cached_row(cached.get(target)) for target in targets):
                    return self._stored_fallback(cached)
                raise ExchangeRateUnavailable("No live or cached exchange rate is currently available.")
            logger.info("fx_refresh_attempt provider=%s base_currency=EUR status=refreshing", getattr(self.provider, "name", "ECB"))
            try:
                live = self.provider.get_rates("EUR", targets)
                if not isinstance(live, dict) or any(target not in live for target in targets):
                    raise ValueError("Provider did not return all required EUR reference rates")
                fetched_at = utcnow()
                expires_at = fetched_at + timedelta(seconds=self.cache_seconds)
                provider_date = getattr(self.provider, "last_provider_date", None)
                provider_name = str(getattr(self.provider, "name", "ECB"))
                provider_source = str(getattr(self.provider, "source", "Frankfurter API"))
                validated_rates: dict[str, float] = {}
                for target in targets:
                    rate = float(live[target])
                    if rate <= 0:
                        raise ValueError(f"Provider returned an invalid EUR/{target} rate")
                    validated_rates[target] = rate
                for target, rate in validated_rates.items():
                    cached[target] = self.store.update_one("exchange_rates", {"_id": f"EUR_{target}"}, {
                        "base_currency": "EUR", "target_currency": target,
                        "base": "EUR", "target": target, "rate": rate,
                        "provider": provider_name, "provider_source": provider_source,
                        "provider_date": provider_date, "rate_date": provider_date,
                        "provider_fetched_at": fetched_at, "last_successful_refresh_at": fetched_at,
                        "fetched_at": fetched_at, "expires_at": expires_at,
                        "source": "live", "source_status": "latest", "status": "latest", "is_fallback": False,
                    }, upsert=True)
                self._retry_not_before = None
                logger.info("fx_provider_success provider=%s base_currency=EUR rate_date=%s status=latest last_successful_refresh_at=%s", provider_name, provider_date or "unknown", fetched_at.isoformat())
                return self._response(cached, stale=False, source="live")
            except Exception as exc:
                cached_row = next((row for row in cached.values() if row), {})
                logger.warning("fx_provider_failure provider=%s base_currency=EUR rate_date=%s status=refresh_failed last_successful_refresh_at=%s error_type=%s", getattr(self.provider, "name", "ECB"), cached_row.get("provider_date") or cached_row.get("rate_date") or "unknown", cached_row.get("last_successful_refresh_at") or cached_row.get("fetched_at") or "unknown", type(exc).__name__)
                if all(self._is_valid_cached_row(cached.get(target)) for target in targets):
                    self._retry_not_before = utcnow() + timedelta(seconds=max(30, min(self.cache_seconds, 300)))
                    return self._stored_fallback(cached)
                logger.error("fx_no_usable_rate provider=%s base_currency=EUR status=unavailable", getattr(self.provider, "name", "ECB"))
                self._retry_not_before = utcnow() + timedelta(seconds=max(30, min(self.cache_seconds, 300)))
                raise ExchangeRateUnavailable("No live or cached exchange rate is currently available.") from exc

    @staticmethod
    def _is_valid_cached_row(row: dict[str, Any] | None) -> bool:
        if not row:
            return False
        try:
            return (
                str(row.get("base_currency") or row.get("base")) == "EUR"
                and str(row.get("target_currency") or row.get("target")) in {"USD", "INR"}
                and float(row.get("rate")) > 0
                and bool(row.get("fetched_at"))
            )
        except (TypeError, ValueError):
            return False

    def rate_for(self, currency: str) -> tuple[float, dict[str, Any]]:
        if currency == "EUR":
            return 1.0, {"provider": "master", "provider_source": "EUR master", "source": "master", "status": "latest", "fetched_at": utcnow(), "stale": False}
        payload = self.get_rates()
        if currency not in payload["rates"]:
            raise ValueError("Unsupported currency")
        return float(payload["rates"][currency]), payload

    def _stored_fallback(self, rows: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
        result = self._response(rows, stale=True, source="cached")
        result["status"] = "stored_fallback"
        result["source_status"] = "stored_fallback"
        result["warning"] = "Provider temporarily unavailable; using the last stored rate (ECB reference rate)"
        logger.info("fx_using_stored_fallback provider=%s base_currency=EUR rate_date=%s status=stored_fallback last_successful_refresh_at=%s", result.get("provider", "ECB"), result.get("rate_date") or "unknown", result.get("last_successful_refresh_at") or "unknown")
        return result

    @staticmethod
    def _response(rows: dict[str, dict[str, Any] | None], *, stale: bool, source: str) -> dict[str, Any]:
        valid = [row for row in rows.values() if row]
        fetched_values = [row.get("fetched_at") for row in valid if row.get("fetched_at")]
        oldest = min(fetched_values, default=utcnow())
        provider_dates = {target: row.get("provider_date") or row.get("rate_date") for target, row in rows.items() if row}
        successful_values = [row.get("last_successful_refresh_at") or row.get("provider_fetched_at") or row.get("fetched_at") for row in valid]
        return {
            "base": "EUR",
            "rates": {target: row["rate"] for target, row in rows.items() if row},
            "provider": ", ".join(sorted({str(row.get("provider")) for row in valid})),
            "provider_source": ", ".join(sorted({str(row.get("provider_source", "Frankfurter API")) for row in valid})),
            "provider_dates": provider_dates,
            "rate_date": max((value for value in provider_dates.values() if value), default=None),
            "fetched_at": oldest,
            "provider_fetched_at": max((value for value in (row.get("provider_fetched_at") or row.get("fetched_at") for row in valid) if value), default=None),
            "last_successful_refresh_at": max((value for value in successful_values if value), default=None),
            "expires_at": max((row.get("expires_at") for row in valid if row.get("expires_at")), default=None),
            "source": source,
            "status": "latest",
            "stale": stale,
        }
