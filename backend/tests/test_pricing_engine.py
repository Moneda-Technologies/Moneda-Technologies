import pytest

from app.pricing.engine import PricingUnavailable, calculate_line, calculate_master_unit_price, calculate_quote_totals, with_display_currency


def base_product(pricing_type="fixed", price=100, category_id="test"):
    return {
        "_id": "p1", "name": "Test Product", "sku": "P1", "category_id": category_id,
        "pricing": {"pricing_type": pricing_type, "price": price, "master_currency": "EUR", "unit": "piece"},
        "tax": {"mode": None, "rate": None, "override_enabled": False},
        "discount_rules": {"enabled": True, "default_max_percent": 5, "privileged_max_percent": 10},
        "configuration": {},
    }


def test_fixed_price_flow_with_discount_conversion_and_no_tax():
    line = calculate_line(base_product(), {}, quantity=2, discount_percent=5, currency="INR", exchange_rate=80,
                          company_tax_rate=18, company_tax_mode="exclusive")
    assert line["unit_price"] == 8000
    assert line["subtotal"] == 16000
    assert line["discount_amount"] == 800
    assert "tax_amount" not in line
    assert line["line_total"] == 15200


def test_legacy_tax_inputs_do_not_change_active_pricing():
    exclusive = calculate_line(base_product(price=1000), {}, quantity=1, discount_percent=0, currency="INR", exchange_rate=1,
                               company_tax_rate=18, company_tax_mode="exclusive", apply_tax=True, tax_mode_override="exclusive")
    inclusive = calculate_line(base_product(price=1000), {}, quantity=1, discount_percent=0, currency="INR", exchange_rate=1,
                               company_tax_rate=18, company_tax_mode="exclusive", apply_tax=True, tax_mode_override="inclusive")
    assert exclusive["line_total"] == inclusive["line_total"] == 1000
    assert "tax_amount" not in exclusive
    assert "tax_amount" not in inclusive


def test_discount_is_calculated_in_eur_before_display_conversion():
    inr = calculate_line(base_product(price=100), {}, quantity=1, discount_percent=3, currency="INR", exchange_rate=80,
                         company_tax_rate=18, company_tax_mode="exclusive")
    usd = calculate_line(base_product(price=100), {}, quantity=1, discount_percent=3, currency="USD", exchange_rate=1.2,
                         company_tax_rate=18, company_tax_mode="exclusive")
    assert inr["master_subtotal"] == usd["master_subtotal"] == 100
    assert inr["master_discount_amount"] == usd["master_discount_amount"] == 3
    assert inr["master_total"] == usd["master_total"] == 97
    assert inr["display_total"] == 7760
    assert usd["display_total"] == 116.4


def test_saved_eur_snapshot_can_switch_reference_currency_without_double_conversion():
    original = calculate_line(base_product(price=53), {}, quantity=2, discount_percent=3,
                              currency="INR", exchange_rate=100,
                              company_tax_rate=0, company_tax_mode="no_tax")
    usd = with_display_currency(original, "USD", 1.2)
    back_to_eur = with_display_currency(usd, "EUR", 1)
    assert original["master_subtotal"] == usd["master_subtotal"] == back_to_eur["master_subtotal"] == 106
    assert original["master_final_total"] == usd["master_final_total"] == back_to_eur["master_final_total"] == 102.82
    assert usd["display_final_total"] == 123.38
    assert back_to_eur["display_final_total"] == 102.82
    assert "final_total" not in usd
    assert "tax_amount" not in usd


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
    assert totals == {"subtotal": 100.0, "discount_amount": 0.0,
                      "transport_cost": 10.0, "transport_total": 10.0, "grand_total": 110.0}


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


def test_product_tax_configuration_is_ignored_by_active_pricing():
    product = base_product()
    product["tax"] = {"mode": "no_tax", "rate": 0, "override_enabled": True}
    line = calculate_line(product, {}, quantity=1, discount_percent=0, currency="EUR", exchange_rate=1,
                          company_tax_rate=18, company_tax_mode="exclusive")
    assert "tax_rate" not in line
    assert "tax_mode" not in line
    assert line["line_total"] == 100
