from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.exchange_rates.provider import ExchangeRateProvider
from app.repositories.store import Store, utcnow


class ExchangeRateUnavailable(RuntimeError):
    pass


class ExchangeRateService:
    def __init__(self, store: Store, provider: ExchangeRateProvider, cache_seconds: int = 21600) -> None:
        self.store = store
        self.provider = provider
        self.cache_seconds = cache_seconds

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
        try:
            live = self.provider.get_rates("EUR", targets)
            fetched_at = utcnow()
            expires_at = fetched_at + timedelta(seconds=self.cache_seconds)
            provider_date = getattr(self.provider, "last_provider_date", None)
            provider_name = str(getattr(self.provider, "name", "ECB"))
            provider_source = str(getattr(self.provider, "source", "Frankfurter API"))
            for target, rate in live.items():
                if rate <= 0:
                    raise ValueError(f"Provider returned an invalid EUR/{target} rate")
                cached[target] = self.store.update_one("exchange_rates", {"_id": f"EUR_{target}"}, {
                    "base_currency": "EUR", "target_currency": target,
                    "base": "EUR", "target": target, "rate": rate,
                    "provider": provider_name, "provider_source": provider_source,
                    "provider_date": provider_date, "fetched_at": fetched_at,
                    "expires_at": expires_at, "source": "live", "source_status": "live", "is_fallback": False,
                }, upsert=True)
            return self._response(cached, stale=False, source="live")
        except Exception as exc:
            if all(self._is_valid_cached_row(cached.get(target)) for target in targets):
                result = self._response(cached, stale=True, source="cached")
                result["warning"] = "Live provider unavailable; showing the last stored rate with its timestamp"
                return result
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
            return 1.0, {"provider": "master", "provider_source": "EUR master", "source": "master", "fetched_at": utcnow(), "stale": False}
        payload = self.get_rates()
        if currency not in payload["rates"]:
            raise ValueError("Unsupported currency")
        return float(payload["rates"][currency]), payload

    @staticmethod
    def _response(rows: dict[str, dict[str, Any] | None], *, stale: bool, source: str) -> dict[str, Any]:
        valid = [row for row in rows.values() if row]
        oldest = min((row["fetched_at"] for row in valid), default=utcnow())
        provider_dates = {target: row.get("provider_date") for target, row in rows.items() if row}
        return {
            "base": "EUR",
            "rates": {target: row["rate"] for target, row in rows.items() if row},
            "provider": ", ".join(sorted({str(row.get("provider")) for row in valid})),
            "provider_source": ", ".join(sorted({str(row.get("provider_source", "Frankfurter API")) for row in valid})),
            "provider_dates": provider_dates,
            "rate_date": max((value for value in provider_dates.values() if value), default=None),
            "fetched_at": oldest,
            "expires_at": max((row.get("expires_at") for row in valid if row.get("expires_at")), default=None),
            "source": source,
            "status": "cached" if source == "cached" else "live",
            "stale": stale,
        }
