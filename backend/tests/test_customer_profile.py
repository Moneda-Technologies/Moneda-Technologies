from app.customers.countries import countries
from app.customers.metadata import normalize_customer_profile, phone_is_valid, resolve_customer_currency


def test_india_profile_defaults_display_currency_without_enabling_tax():
    changes, error = normalize_customer_profile({"company_name": "India Co", "continent": "Asia", "country_code": "IN"})
    assert error is None
    assert changes["preferred_currency"] == "INR"
    assert "tax_profile" not in changes


def test_region_update_does_not_rewrite_historical_tax_metadata():
    changes, error = normalize_customer_profile({"continent": "Asia", "country_code": "SG", "gst_vat_number": "OLD"})
    assert error is None
    assert changes["gst_vat_number"] == "OLD"
    assert "tax_profile" not in changes


def test_country_region_mismatch_is_rejected():
    changes, error = normalize_customer_profile({"continent": "Europe", "country_code": "IN"})
    assert changes is None
    assert error == "COUNTRY_CONTINENT_MISMATCH"


def test_country_derives_region_when_region_is_omitted():
    changes, error = normalize_customer_profile({"country_code": "BR"})
    assert error is None
    assert changes["continent"] == "South America"
    assert changes["region"]["continent"] == "South America"


def test_country_name_and_code_must_refer_to_same_country():
    changes, error = normalize_customer_profile({"country_code": "IN", "country_name": "Germany"})
    assert changes is None
    assert error == "COUNTRY_INVALID"


def test_invalid_country_code_is_rejected():
    changes, error = normalize_customer_profile({"country_code": "ZZ"})
    assert changes is None
    assert error == "COUNTRY_INVALID"


def test_country_catalogue_is_complete_and_sorted():
    rows = countries()
    assert len(rows) == 250
    assert [row["name"] for row in rows] == sorted((row["name"] for row in rows), key=str.casefold)
    lookup = {row["code"]: row for row in rows}
    required_names = {
        "India", "Germany", "United States", "United Kingdom", "France", "Italy", "Spain", "Brazil",
        "Argentina", "Chile", "Mexico", "Canada", "Nigeria", "South Africa", "Egypt", "Saudi Arabia",
        "United Arab Emirates", "Singapore", "Malaysia", "Indonesia", "Japan", "China", "Australia", "New Zealand",
    }
    assert required_names <= {row["name"] for row in rows}
    assert lookup["IN"] == {
        "code": "IN", "name": "India", "region": "Asia", "currency_code": "INR",
        "default_display_currency": "INR", "phone_country_code": "+91",
    }
    assert lookup["US"]["region"] == "North America"
    assert lookup["DE"]["default_display_currency"] == "EUR"
    assert lookup["BR"]["region"] == "South America"


def test_custom_payment_days_are_structured():
    changes, error = normalize_customer_profile({"payment_terms": "Custom", "custom_payment_days": "45"})
    assert error is None
    assert changes["custom_payment_days"] == 45
    assert changes["payment_terms_display"] == "Custom: 45 days"


def test_non_custom_payment_clears_custom_days():
    changes, error = normalize_customer_profile({"payment_terms": "30 Days from receipt", "custom_payment_days": 45})
    assert error is None
    assert "custom_payment_days" not in changes


def test_phone_rejects_alphabetic_input():
    changes, error = normalize_customer_profile({"phone": "abc123"})
    assert changes is None
    assert "Phone" in error


def test_phone_validation_accepts_common_international_formats_and_rejects_obvious_values():
    assert all(phone_is_valid(value) for value in (
        "+91 98769 96112", "+1 212 555 1234", "+44 20 1234 5678", "9876996112",
    ))
    assert not phone_is_valid("abc123")
    assert not phone_is_valid("+1")
    assert not phone_is_valid("1" * 16)


def test_customer_contact_fields_accept_explicit_unknown_marker():
    changes, error = normalize_customer_profile({
        "company_name": "Unknown Contacts Ltd", "contact_name": "Buyer", "email": "-", "phone": "-",
        "country_code": "DE", "payment_terms": "Advance", "address": "Berlin",
    }, require_complete=True)
    assert error is None
    assert changes["email"] == "-"
    assert changes["phone"] == "-"
    assert phone_is_valid("-") is False
    assert phone_is_valid("-", allow_unknown=True) is True


def test_legacy_india_country_name_is_supported():
    changes, error = normalize_customer_profile({"country": "India", "preferred_currency": "INR"})
    assert error is None
    assert changes["country_code"] == "IN"


def test_currency_override_is_customer_scoped_and_authorized():
    class Store:
        def __init__(self): self.updated = None
        def find_one(self, *_args): return {"_id": "customer-1"}
        def update_one(self, *_args): self.updated = _args
    store = Store()
    customer = {"_id": "customer-1", "preferred_currency": "EUR"}
    assert resolve_customer_currency(customer, "USD", store, {"permissions": []}) == "EUR"
    assert resolve_customer_currency(customer, "USD", store, {"permissions": ["customers.update"]}) == "USD"
    assert store.updated is not None
