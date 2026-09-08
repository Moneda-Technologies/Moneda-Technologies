from app.customers.metadata import normalize_customer_profile, phone_is_valid, resolve_customer_currency


def test_india_profile_does_not_control_currency_or_tax():
    changes, error = normalize_customer_profile({"company_name": "India Co", "continent": "Asia", "country_code": "IN"})
    assert error is None
    assert changes["preferred_currency"] == "EUR"
    assert "tax_profile" not in changes


def test_region_update_does_not_rewrite_historical_tax_metadata():
    changes, error = normalize_customer_profile({"continent": "Asia", "country_code": "SG", "gst_vat_number": "OLD"})
    assert error is None
    assert changes["gst_vat_number"] == "OLD"
    assert "tax_profile" not in changes


def test_country_must_belong_to_continent():
    changes, error = normalize_customer_profile({"continent": "Europe", "country_code": "IN"})
    assert changes is None
    assert "belong" in error


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
