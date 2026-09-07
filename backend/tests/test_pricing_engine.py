import pytest

from app.pricing.engine import PricingUnavailable, calculate_line, calculate_master_unit_price, calculate_quote_totals


def base_product(pricing_type="fixed", price=100, category_id="test"):
    return {
        "_id": "p1", "name": "Test Product", "sku": "P1", "category_id": category_id,
        "pricing": {"pricing_type": pricing_type, "price": price, "master_currency": "EUR", "unit": "piece"},
        "tax": {"mode": None, "rate": None, "override_enabled": False},
        "discount_rules": {"enabled": True, "default_max_percent": 5, "privileged_max_percent": 10},
        "configuration": {},
    }


def test_fixed_price_flow_with_discount_conversion_and_tax():
    line = calculate_line(base_product(), {}, quantity=2, discount_percent=5, currency="INR", exchange_rate=80,
                          company_tax_rate=18, company_tax_mode="exclusive")
    assert line["unit_price"] == 8000
    assert line["subtotal"] == 16000
    assert line["discount_amount"] == 800
    assert line["tax_amount"] == 2736
    assert line["line_total"] == 17936


def test_india_tax_mode_switches_between_exclusive_and_inclusive():
    exclusive = calculate_line(base_product(price=1000), {}, quantity=1, discount_percent=0, currency="INR", exchange_rate=1,
                               company_tax_rate=18, company_tax_mode="exclusive", apply_tax=True, tax_mode_override="exclusive")
    inclusive = calculate_line(base_product(price=1000), {}, quantity=1, discount_percent=0, currency="INR", exchange_rate=1,
                               company_tax_rate=18, company_tax_mode="exclusive", apply_tax=True, tax_mode_override="inclusive")
    assert exclusive["tax_amount"] == 180
    assert exclusive["line_total"] == 1180
    assert inclusive["tax_amount"] == 152.54
    assert inclusive["taxable_amount"] == 847.46
    assert inclusive["line_total"] == 1000


def test_area_normalization_from_inches():
    price = calculate_master_unit_price(base_product("per_sqm", 10), {"length": 10, "width": 10, "dimension_unit": "inch"})
    assert float(price) == 0.65


def test_mpack_formula():
    price = calculate_master_unit_price(base_product("formula", 75), {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 200})
    assert float(price) == 150


def test_discount_permission_limit():
    with pytest.raises(ValueError, match="5% maximum"):
        calculate_line(base_product(), {}, quantity=1, discount_percent=5.5, currency="EUR", exchange_rate=1,
                       company_tax_rate=18, company_tax_mode="exclusive")
    line = calculate_line(base_product(), {}, quantity=1, discount_percent=10, currency="EUR", exchange_rate=1,
                          company_tax_rate=18, company_tax_mode="exclusive", privileged_discount=True)
    assert line["discount_percent"] == 10


def test_pending_pricing_is_explicit():
    with pytest.raises(PricingUnavailable):
        calculate_master_unit_price(base_product(price=None), {})


def test_quote_totals_include_transport_once():
    line = calculate_line(base_product(), {}, quantity=1, discount_percent=0, currency="EUR", exchange_rate=1,
                          company_tax_rate=18, company_tax_mode="exclusive")
    totals = calculate_quote_totals([line], 10)
    assert totals == {"subtotal": 100.0, "discount_amount": 0.0, "taxable_amount": 100.0,
                      "product_tax_amount": 18.0, "tax_amount": 18.0, "transport_cost": 10.0, "transport_tax_amount": 0.0,
                      "transport_total": 10.0, "grand_total": 128.0}


def test_quantity_does_not_inject_bulk_discount_but_cut_format_surcharge_remains_server_rule():
    settings = {
        "discount_rules": {"bulk_rolls": {"enabled": True, "minimum_quantity": 10, "discount_percent": 2.5, "applies_to_categories": ["blankets"]}},
        "surcharge_rules": {"cut_format": {"enabled": True, "percent": 5, "applies_to_categories": ["blankets"]}},
    }
    line = calculate_line(
        base_product("per_sqm", 100, "blankets"),
        {"length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
        quantity=10, discount_percent=0, currency="EUR", exchange_rate=1,
        company_tax_rate=0, company_tax_mode="no_tax", business_rules=settings,
    )
    assert line["master_unit_price"] == 105
    assert line["requested_discount_percent"] == 0
    assert line["discount_percent"] == 0
    assert line["discount_source"] == "default"
    assert line["discount_reason"] is None
    assert line["line_total"] == 1050


def test_only_enabled_product_tax_overrides_company_tax():
    product = base_product()
    product["tax"] = {"mode": "no_tax", "rate": 0, "override_enabled": True}
    line = calculate_line(product, {}, quantity=1, discount_percent=0, currency="EUR", exchange_rate=1,
                          company_tax_rate=18, company_tax_mode="exclusive")
    assert line["tax_rate"] == 0
    assert line["tax_mode"] == "no_tax"
    assert line["line_total"] == 100
