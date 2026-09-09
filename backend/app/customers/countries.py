"""Load the single country catalogue shared by customer APIs and validation."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


COUNTRY_CATALOGUE_PATH = Path(__file__).resolve().parents[3] / "data" / "countries.json"


@lru_cache(maxsize=1)
def countries() -> tuple[dict, ...]:
    payload = json.loads(COUNTRY_CATALOGUE_PATH.read_text(encoding="utf-8"))
    rows = payload.get("countries") or []
    if not isinstance(rows, list):
        raise RuntimeError("Country catalogue must contain a countries list")

    normalized: list[dict] = []
    seen: set[str] = set()
    for raw in rows:
        code = str(raw.get("code") or "").strip().upper()
        name = str(raw.get("name") or "").strip()
        region = str(raw.get("region") or "").strip()
        if len(code) != 2 or not code.isalpha() or not name or not region:
            raise RuntimeError(f"Invalid country catalogue entry: {code or name or 'unknown'}")
        if code in seen:
            raise RuntimeError(f"Duplicate country code in catalogue: {code}")
        seen.add(code)
        normalized.append({
            "code": code,
            "name": name,
            "region": region,
            "currency_code": str(raw.get("currency_code") or "").strip().upper(),
            "default_display_currency": str(raw.get("default_display_currency") or "USD").strip().upper(),
            "phone_country_code": str(raw.get("phone_country_code") or "").strip(),
        })
    return tuple(sorted(normalized, key=lambda row: row["name"].casefold()))


@lru_cache(maxsize=1)
def countries_by_code() -> dict[str, dict]:
    return {row["code"]: row for row in countries()}


@lru_cache(maxsize=1)
def countries_by_name() -> dict[str, dict]:
    lookup = {row["name"].casefold(): row for row in countries()}
    lookup.update({"usa": countries_by_code()["US"], "uk": countries_by_code()["GB"]})
    return lookup
