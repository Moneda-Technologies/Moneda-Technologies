"""Authoritative customer region, currency, tax, and payment metadata."""

from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email


PAYMENT_TERMS = ("Advance", "POD", "30 Days from receipt", "60 Days", "Custom")
SUPPORTED_CURRENCIES = ("EUR", "USD", "INR")

COUNTRIES_BY_CONTINENT = {
    "Africa": (
        {"code": "ZA", "name": "South Africa", "default_currency": "USD"},
        {"code": "NG", "name": "Nigeria", "default_currency": "USD"},
        {"code": "EG", "name": "Egypt", "default_currency": "USD"},
        {"code": "KE", "name": "Kenya", "default_currency": "USD"},
    ),
    "Asia": (
        {"code": "IN", "name": "India", "default_currency": "INR", "gst_applicable": True},
        {"code": "CN", "name": "China", "default_currency": "USD"},
        {"code": "JP", "name": "Japan", "default_currency": "USD"},
        {"code": "SG", "name": "Singapore", "default_currency": "USD"},
        {"code": "AE", "name": "United Arab Emirates", "default_currency": "USD"},
        {"code": "SA", "name": "Saudi Arabia", "default_currency": "USD"},
        {"code": "TH", "name": "Thailand", "default_currency": "USD"},
        {"code": "MY", "name": "Malaysia", "default_currency": "USD"},
    ),
    "Europe": (
        {"code": "DE", "name": "Germany", "default_currency": "EUR"},
        {"code": "FR", "name": "France", "default_currency": "EUR"},
        {"code": "IT", "name": "Italy", "default_currency": "EUR"},
        {"code": "ES", "name": "Spain", "default_currency": "EUR"},
        {"code": "GB", "name": "United Kingdom", "default_currency": "USD"},
        {"code": "NL", "name": "Netherlands", "default_currency": "EUR"},
    ),
    "North America": (
        {"code": "US", "name": "United States", "default_currency": "USD"},
        {"code": "CA", "name": "Canada", "default_currency": "USD"},
        {"code": "MX", "name": "Mexico", "default_currency": "USD"},
    ),
    "South America": (
        {"code": "BR", "name": "Brazil", "default_currency": "USD"},
        {"code": "AR", "name": "Argentina", "default_currency": "USD"},
        {"code": "CL", "name": "Chile", "default_currency": "USD"},
    ),
    "Oceania": (
        {"code": "AU", "name": "Australia", "default_currency": "USD"},
        {"code": "NZ", "name": "New Zealand", "default_currency": "USD"},
    ),
    "Antarctica": (),
}

COUNTRIES_BY_CODE = {country["code"]: {**country, "continent": continent} for continent, countries in COUNTRIES_BY_CONTINENT.items() for country in countries}
COUNTRIES_BY_NAME = {country["name"].casefold(): country for country in COUNTRIES_BY_CODE.values()}
COUNTRIES_BY_NAME.update({"usa": COUNTRIES_BY_CODE["US"], "uk": COUNTRIES_BY_CODE["GB"]})


def country_for(continent: str | None, country_code: str | None, country_name: str | None = None) -> dict | None:
    code = str(country_code or "").strip().upper()
    if code and code in COUNTRIES_BY_CODE:
        country = COUNTRIES_BY_CODE[code]
        return country if not continent or country["continent"] == continent else None
    if country_name:
        country = COUNTRIES_BY_NAME.get(str(country_name).strip().casefold())
        if country and (not continent or country["continent"] == continent):
            return country
    return None


def phone_is_valid(value: str) -> bool:
    return not value or bool(re.fullmatch(r"[+0-9().\-\s]{3,30}", value))


def resolve_customer_currency(customer: dict, requested: str | None, store, user: dict | None = None) -> str:
    """Resolve currency from the customer; persist an authorized workspace override."""
    stored = str(customer.get("preferred_currency") or customer.get("default_currency") or "EUR").upper()
    if stored not in SUPPORTED_CURRENCIES:
        stored = "EUR"
    candidate = str(requested or "").upper()
    if candidate not in SUPPORTED_CURRENCIES or candidate == stored:
        return stored
    user = user or {}
    authorized = user.get("role_id") == "superadmin" or "customers.update" in user.get("permissions", [])
    if not authorized:
        return stored
    customer_id = customer.get("_id") or customer.get("customer_id")
    if customer_id:
        collection = "customers" if store.find_one("customers", {"_id": customer_id}) else "companies"
        store.update_one(collection, {"_id": customer_id}, {"preferred_currency": candidate, "default_currency": candidate})
    return candidate


def normalize_customer_profile(payload: dict, existing: dict | None = None) -> tuple[dict | None, str | None]:
    """Normalize region/tax/payment fields and return (changes, validation error)."""
    existing = existing or {}
    changes = dict(payload)
    if "email" in changes and str(changes.get("email") or "").strip():
        try:
            changes["email"] = validate_email(str(changes["email"]).strip(), check_deliverability=False).normalized
        except EmailNotValidError:
            return None, "Enter a valid customer email address"
    continent = str(changes.get("continent") or (existing.get("region") or {}).get("continent") or "").strip()
    country_code = str(changes.get("country_code") or (existing.get("region") or {}).get("country_code") or "").strip().upper()
    country_name = str(changes.get("country_name") or changes.get("country") or (existing.get("region") or {}).get("country_name") or existing.get("country") or "").strip()
    country = country_for(continent, country_code, country_name)
    if continent or country_code or country_name:
        if not continent and country:
            continent = country["continent"]
        if continent not in COUNTRIES_BY_CONTINENT:
            return None, "Continent / region is not recognized"
        if not country:
            return None, "Country must belong to the selected continent"
        changes["continent"] = country["continent"]
        changes["country_code"] = country["code"]
        changes["country_name"] = country["name"]
        changes["country"] = country["name"]
        changes["region"] = {"continent": country["continent"], "country_code": country["code"], "country_name": country["name"]}
        if "preferred_currency" not in changes and not existing.get("preferred_currency"):
            changes["preferred_currency"] = country["default_currency"]
    currency = str(changes.get("preferred_currency") or changes.get("default_currency") or existing.get("preferred_currency") or "EUR").upper()
    if currency not in SUPPORTED_CURRENCIES:
        return None, "Unsupported customer currency"
    changes["preferred_currency"] = currency
    changes["default_currency"] = currency
    payment = str(changes.get("payment_terms") or existing.get("payment_terms") or "").strip()
    legacy_payment = {"30 days": "30 Days from receipt", "60 days": "60 Days", "15 days": "Custom"}
    if payment.casefold() in legacy_payment:
        payment = legacy_payment[payment.casefold()]
        changes["payment_terms"] = payment
    if payment and payment not in PAYMENT_TERMS:
        return None, "Invalid payment terms"
    custom_days = changes.get("custom_payment_days", existing.get("custom_payment_days"))
    if payment == "Custom":
        try:
            if isinstance(custom_days, bool) or int(custom_days) != float(custom_days) or int(custom_days) < 1:
                raise ValueError
            changes["custom_payment_days"] = int(custom_days)
        except (TypeError, ValueError):
            return None, "Custom payment term must be a positive whole number of days"
        changes["payment_terms_display"] = f"Custom: {changes['custom_payment_days']} days"
    else:
        changes.pop("custom_payment_days", None)
        changes["payment_terms_display"] = payment
    phone = str(changes.get("phone") or "").strip()
    if phone and not phone_is_valid(phone):
        return None, "Phone contains invalid characters"
    changes["phone"] = phone
    is_india = changes.get("country_code") == "IN"
    tax_number = str(changes.get("tax_number") or changes.get("gst_vat_number") or "").strip()
    if is_india:
        changes["gst_vat_number"] = tax_number
        changes["tax_number"] = tax_number
    else:
        changes["gst_vat_number"] = ""
        changes["tax_number"] = ""
    changes["tax_profile"] = {"gst_applicable": is_india, "tax_number": tax_number if is_india else ""}
    return changes, None
