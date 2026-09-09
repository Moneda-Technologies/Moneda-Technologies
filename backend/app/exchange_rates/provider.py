from __future__ import annotations

import logging
from typing import Protocol

import requests


logger = logging.getLogger(__name__)


class ExchangeRateProvider(Protocol):
    name: str
    def get_rates(self, base: str, targets: list[str]) -> dict[str, float]: ...


class FrankfurterProvider:
    # Frankfurter publishes the ECB reference rates. Keep both values
    # explicit so quotations can identify the economic source and the API.
    name = "ECB"
    source = "Frankfurter API"
    api_url = "https://api.frankfurter.dev/v2/rates"

    def __init__(self) -> None:
        self.last_provider_date: str | None = None

    def get_rates(self, base: str, targets: list[str]) -> dict[str, float]:
        if base.upper() != "EUR":
            raise ValueError("Frankfurter ECB reference rates must use EUR as the base currency")
        params = {"base": base, "quotes": ",".join(targets), "providers": "ECB"}
        try:
            response = requests.get(self.api_url, params=params, timeout=8)
            logger.info(
                "exchange provider response provider=ECB endpoint=%s status=%s base=%s targets=%s",
                self.api_url, response.status_code, base, ",".join(targets),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            logger.warning(
                "exchange provider request failed provider=ECB endpoint=%s error_type=%s",
                self.api_url, type(exc).__name__,
            )
            raise
        if not isinstance(payload, list):
            raise ValueError("Frankfurter returned an invalid rates response")
        rows = {str(row.get("quote")): row for row in payload if isinstance(row, dict)}
        missing = [target for target in targets if target not in rows]
        if missing:
            raise ValueError(f"Frankfurter did not return rates for: {', '.join(missing)}")
        dates = {str(row.get("date")) for row in rows.values() if row.get("date")}
        self.last_provider_date = max(dates) if dates else None
        rates = {target: float(rows[target]["rate"]) for target in targets}
        if any(rate <= 0 for rate in rates.values()):
            raise ValueError("Frankfurter returned a non-positive exchange rate")
        return rates
