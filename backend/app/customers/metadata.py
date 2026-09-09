"""Authoritative customer region, display-currency, and payment metadata."""

from __future__ import annotations

import re

from email_validator import EmailNotValidError, validate_email

from app.customers.countries import countries_by_code, countries_by_name


PAYMENT_TERMS = ("Advance", "POD", "30 Days from receipt", "60 Days", "Custom")
VALIDATION_MESSAGES = {
    "CUSTOMER_NAME_REQUIRED": "Customer company name is required",
    "PRIMARY_CONTACT_REQUIRED": "Primary contact name is required",
    "CUSTOMER_EMAIL_REQUIRED": "Email address is required",
    "CUSTOMER_EMAIL_INVALID": "Enter a valid customer email address",
    "CUSTOMER_PHONE_REQUIRED": "Phone number is required",
    "CUSTOMER_PHONE_INVALID": "Phone contains invalid characters",
    "CONTINENT_REQUIRED": "Region / Continent is required",
    "CONTINENT_INVALID": "Region / Continent is not recognized",
    "COUNTRY_REQUIRED": "Country is required",
    "COUNTRY_INVALID": "Select a valid country from the country list",
    "COUNTRY_CONTINENT_MISMATCH": "Country must belong to the selected region / continent",
    "CUSTOMER_CURRENCY_REQUIRED": "Display currency is required",
    "PAYMENT_TERMS_REQUIRED": "Payment terms are required",
    "CUSTOM_PAYMENT_DAYS_REQUIRED": "Custom payment term must be a positive whole number of days",
    "CUSTOMER_ADDRESS_REQUIRED": "Address is required",
}


def validation_message(code: str) -> str:
    return VALIDATION_MESSAGES.get(code, code)


def customer_gst_applicable(customer: dict) -> bool:
    """Deprecated compatibility hook; customer location never activates tax."""
    return False
SUPPORTED_CURRENCIES = ("EUR", "USD", "INR")
COUNTRIES_BY_CODE = countries_by_code()
COUNTRIES_BY_NAME = countries_by_name()
VALID_REGIONS = {country["region"].casefold() for country in COUNTRIES_BY_CODE.values()}


def country_for(continent: str | None, country_code: str | None, country_name: str | None = None) -> dict | None:
    """Resolve a country and reject mismatched country/name/region values."""
    code = str(country_code or "").strip().upper()
    name = str(country_name or "").strip().casefold()
    by_code = COUNTRIES_BY_CODE.get(code) if code else None
    by_name = COUNTRIES_BY_NAME.get(name) if name else None
    if (code and not by_code) or (name and not by_name):
        return None
    if by_code and by_name and by_code["code"] != by_name["code"]:
        return None
    country = by_code or by_name
    if not country:
        return None
    requested_region = str(continent or "").strip().casefold()
    if requested_region and requested_region != country["region"].casefold():
        return None
    return country


def phone_is_valid(value: str, *, allow_unknown: bool = False) -> bool:
    if not value:
        return True
    if allow_unknown and value == "-":
        return True
    if not re.fullmatch(r"\+?[0-9().\-\s]{2,29}", value):
        return False
    digits = re.sub(r"\D", "", value)
    return 3 <= len(digits) <= 15


def resolve_customer_currency(customer: dict, requested: str | None, store, user: dict | None = None) -> str:
    """Resolve the customer's display/reference currency only."""
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


def normalize_customer_profile(payload: dict, existing: dict | None = None, *, require_complete: bool = False) -> tuple[dict | None, str | None]:
    """Normalize region, display-currency, and payment fields."""
    existing = existing or {}
    changes = dict(payload)
    required = lambda key: require_complete or key in changes
    name = str(changes.get("name") or changes.get("company_name") or existing.get("name") or "").strip()
    if required("name") and not name:
        return None, "CUSTOMER_NAME_REQUIRED"
    if name:
        changes["name"] = name
        changes["company_name"] = name
    contact = str(changes.get("contact_name") if "contact_name" in changes else existing.get("contact_name") or "").strip()
    if required("contact_name") and not contact:
        return None, "PRIMARY_CONTACT_REQUIRED"
    if "contact_name" in changes or require_complete:
        changes["contact_name"] = contact
    email = str(changes.get("email") if "email" in changes else existing.get("email") or "").strip()
    if required("email") and not email:
        return None, "CUSTOMER_EMAIL_REQUIRED"
    if email and email != "-":
        try:
            changes["email"] = validate_email(email, check_deliverability=False).normalized
        except EmailNotValidError:
            return None, "CUSTOMER_EMAIL_INVALID"
    phone = str(changes.get("phone") if "phone" in changes else existing.get("phone") or "").strip()
    if required("phone") and not phone:
        return None, "CUSTOMER_PHONE_REQUIRED"
    if phone and not phone_is_valid(phone, allow_unknown=True):
        return None, "CUSTOMER_PHONE_INVALID" if require_complete else "Phone contains invalid characters"
    if "phone" in changes or require_complete:
        changes["phone"] = phone
    address = str(changes.get("address") if "address" in changes else existing.get("address") or "").strip()
    if required("address") and not address:
        return None, "CUSTOMER_ADDRESS_REQUIRED"
    if "address" in changes or require_complete:
        changes["address"] = address
    continent = str(changes.get("continent") or (existing.get("region") or {}).get("continent") or "").strip()
    if continent and continent.casefold() not in VALID_REGIONS:
        return None, "CONTINENT_INVALID"
    country_code = str(changes.get("country_code") or (existing.get("region") or {}).get("country_code") or "").strip().upper()
    country_name = str(changes.get("country_name") or changes.get("country") or (existing.get("region") or {}).get("country_name") or existing.get("country") or "").strip()
    country = country_for(continent, country_code, country_name)
    if require_complete and not (country_code or country_name):
        return None, "COUNTRY_REQUIRED"
    if continent or country_code or country_name:
        if not country:
            unfiltered = country_for("", country_code, country_name)
            if unfiltered and continent and unfiltered["region"].casefold() != continent.casefold():
                return None, "COUNTRY_CONTINENT_MISMATCH"
            return None, "COUNTRY_INVALID"
        continent = country["region"]
        changes["continent"] = continent
        changes["country_code"] = country["code"]
        changes["country_name"] = country["name"]
        changes["country"] = country["name"]
        changes["region"] = {"continent": continent, "country_code": country["code"], "country_name": country["name"]}
    default_currency = country["default_display_currency"] if country else "EUR"
    currency = str(changes.get("preferred_currency") or changes.get("default_currency") or existing.get("preferred_currency") or default_currency).upper()
    if currency not in SUPPORTED_CURRENCIES:
        return None, "Unsupported customer currency"
    changes["preferred_currency"] = currency
    changes["default_currency"] = currency
    payment = str(changes.get("payment_terms") or existing.get("payment_terms") or "").strip()
    if require_complete and not payment:
        return None, "PAYMENT_TERMS_REQUIRED"
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
            return None, "CUSTOM_PAYMENT_DAYS_REQUIRED"
        changes["payment_terms_display"] = f"Custom: {changes['custom_payment_days']} days"
    else:
        changes.pop("custom_payment_days", None)
        changes["payment_terms_display"] = payment
    return changes, None
