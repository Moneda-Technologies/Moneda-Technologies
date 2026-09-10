"""Server-side public-IP and approximate location helpers for trusted devices."""
from __future__ import annotations

import ipaddress
import threading
import time
from typing import Any

from flask import current_app, request


_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_LOCK = threading.RLock()


def client_ip() -> str | None:
    """Return the client IP, honoring forwarding headers only from trusted proxies."""
    direct = str(request.remote_addr or "").strip()
    trusted = [part.strip() for part in str(current_app.config.get("TRUSTED_PROXY_IPS", "")).split(",") if part.strip()]
    if direct and direct in trusted:
        forwarded = request.headers.get("X-Forwarded-For", "")
        candidates = [part.strip() for part in forwarded.split(",") if part.strip()]
        if candidates:
            return candidates[0]
    return direct or None


def public_ip(value: str | None) -> str | None:
    try:
        parsed = ipaddress.ip_address(str(value or ""))
        return str(parsed) if parsed.is_global else None
    except ValueError:
        return None


def _empty(source: str = "unavailable") -> dict[str, Any]:
    return {"city": None, "state": None, "country": None, "country_code": None,
            "location_source": source, "location_resolved_at": None}


def _lookup_uncached(ip: str) -> dict[str, Any]:
    provider = str(current_app.config.get("IP_GEO_PROVIDER", "none")).strip().lower()
    if provider in {"", "none", "disabled"}:
        return _empty("disabled")
    if provider == "maxmind":
        try:
            import geoip2.database  # type: ignore[import-not-found]
            path = str(current_app.config.get("IP_GEO_DATABASE_PATH", ""))
            if not path:
                return _empty("maxmind_unconfigured")
            with geoip2.database.Reader(path) as reader:
                record = reader.city(ip)
                return {"city": record.city.name, "state": (record.subdivisions.most_specific.name if record.subdivisions else None),
                        "country": record.country.name, "country_code": record.country.iso_code,
                        "location_source": "maxmind", "location_resolved_at": time.time()}
        except Exception:
            current_app.logger.info("device geolocation lookup unavailable provider=maxmind")
            return _empty("maxmind_error")
    if provider == "ipinfo":
        try:
            import requests
            token = str(current_app.config.get("IP_GEO_API_KEY", "")).strip()
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            response = requests.get(f"https://ipinfo.io/{ip}/json", headers=headers, timeout=1.5)
            if response.ok:
                payload = response.json()
                region = str(payload.get("region") or "") or None
                return {"city": payload.get("city"), "state": region, "country": payload.get("country_name") or payload.get("country"),
                        "country_code": payload.get("country"), "location_source": "ipinfo", "location_resolved_at": time.time()}
        except Exception:
            current_app.logger.info("device geolocation lookup unavailable provider=ipinfo")
        return _empty("ipinfo_error")
    return _empty("unsupported_provider")


def resolve(ip: str | None) -> dict[str, Any]:
    if not ip:
        return _empty()
    try:
        parsed = ipaddress.ip_address(ip)
        if parsed.is_private or parsed.is_loopback or parsed.is_reserved:
            return _empty("local_network")
    except ValueError:
        return _empty("invalid_ip")
    ttl = max(0, int(current_app.config.get("IP_GEO_CACHE_SECONDS", 86400)))
    now = time.time()
    with _LOCK:
        cached = _CACHE.get(ip)
        if cached and ttl > 0 and now - cached[0] < ttl:
            return dict(cached[1])
    result = _lookup_uncached(ip)
    with _LOCK:
        _CACHE[ip] = (now, dict(result))
        if len(_CACHE) > 2048:
            _CACHE.pop(next(iter(_CACHE)))
    return result
