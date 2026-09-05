from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_CEILING
from typing import Any

from app.pricing.tax import calculate_tax, money


MASTER_CURRENCY = "EUR"


class PricingUnavailable(ValueError):
    pass


UNIT_TO_METERS = {
    "mm": Decimal("0.001"),
    "cm": Decimal("0.01"),
    "m": Decimal("1"),
    "inch": Decimal("0.0254"),
}

UNIT_PRICING_TYPES = {
    "fixed", "quantity", "per_piece", "per_bar", "per_pack", "per_packet", "per_roll",
}


def decimal_value(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if result < 0:
        raise ValueError(f"{field} cannot be negative")
    return result


def company_tax_values(company: dict[str, Any]) -> tuple[Any, str]:
    if company.get("tax_enabled") is False:
        return 0, "no_tax"
    return company.get("default_tax_rate", 18), company.get("default_tax_mode", "exclusive")


def pricing_value(product: dict[str, Any], configuration: dict[str, Any] | None = None) -> tuple[Decimal, str, str]:
    pricing = product.get("pricing", {})
    raw_price = pricing.get("price", pricing.get("base_price"))
    variant_prices = pricing.get("variant_prices") or {}
    if variant_prices:
        selected = decimal_value((configuration or {}).get("thickness_mm"), "thickness_mm")
        matched = next((value for key, value in variant_prices.items() if Decimal(str(key)) == selected), None)
        if matched is None:
            raise PricingUnavailable("EUR price is pending for the selected thickness")
        raw_price = matched
    status = product.get("pricing_status")
    if status == "on_request":
        raise PricingUnavailable("Price is on request; an administrator must approve a EUR price before calculation")
    if raw_price is None:
        raise PricingUnavailable("EUR price is pending; an administrator must configure this product")
    master_currency = str(pricing.get("master_currency", pricing.get("currency", MASTER_CURRENCY))).upper()
    if master_currency != MASTER_CURRENCY:
        raise ValueError("Product master currency must be EUR")
    return decimal_value(raw_price, "price"), str(pricing.get("pricing_type", pricing.get("type", "fixed"))), str(pricing.get("unit", "unit"))


def area_sqm(configuration: dict[str, Any]) -> Decimal:
    unit = str(configuration.get("dimension_unit", configuration.get("unit", "mm"))).lower()
    if unit not in UNIT_TO_METERS:
        raise ValueError("Unsupported dimension unit")
    length = decimal_value(configuration.get("length"), "length") * UNIT_TO_METERS[unit]
    width = decimal_value(configuration.get("width"), "width") * UNIT_TO_METERS[unit]
    if not length or not width:
        raise ValueError("Length and width must be greater than zero")
    return length * width


def calculate_master_unit_price(product: dict[str, Any], configuration: dict[str, Any]) -> Decimal:
    base, pricing_type, _unit = pricing_value(product, configuration)
    if pricing_type in UNIT_PRICING_TYPES:
        return money(base)
    if pricing_type == "per_sqm":
        return money(base * area_sqm(configuration))
    if pricing_type == "formula":
        thickness = decimal_value(configuration.get("thickness_micron"), "thickness_micron")
        if not thickness:
            raise ValueError("Thickness must be greater than zero")
        return money(base * thickness / Decimal("100") * area_sqm(configuration))
    if pricing_type == "per_litre":
        size = decimal_value(configuration.get("size_litre", 1), "size_litre")
        if not size:
            raise ValueError("Container size must be greater than zero")
        return money(base * size)
    if pricing_type == "per_kg":
        weight = decimal_value(configuration.get("weight_kg", 1), "weight_kg")
        return money(base * weight)
    if pricing_type == "per_meter":
        default_length = product.get("configuration", {}).get("length_per_unit_m", 1)
        length = decimal_value(configuration.get("length_m", default_length), "length_m")
        return money(base * length)
    raise ValueError("Unsupported product pricing type")


def resolve_product_adjustments(store: Any, product: dict[str, Any], configuration: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve trusted cross-product adjustments, such as blanket barring."""
    if product.get("category_id") != "blankets":
        return []
    format_type = configuration.get("format_type")
    if format_type == "cut_format":
        # Cut format is valid on its own. Ignore stale bar values from older
        # clients instead of rejecting an otherwise complete configuration.
        return []
    if format_type != "bar_format":
        return []
    bar_1_id = str(configuration.get("bar_1_id", "")).strip()
    if not bar_1_id:
        raise ValueError("Bar Format requires Bar 1")
    bar_2_id = str(configuration.get("bar_2_id", "")).strip()
    use_second_bar = configuration.get("use_second_bar")
    # Older clients sent two IDs without the explicit checkbox field.
    if use_second_bar is None:
        use_second_bar = bool(bar_2_id and bar_2_id != bar_1_id)
    if use_second_bar and not bar_2_id:
        raise ValueError("Select the second bar or leave Different second bar disabled")
    if use_second_bar and bar_2_id == bar_1_id:
        raise ValueError("Different second bar must use a different bar")
    bar_ids = [bar_1_id, bar_2_id if use_second_bar else bar_1_id]
    resolved: list[dict[str, Any]] = []
    for bar_id in bar_ids:
        bar = store.find_one("blanket_bars", {"_id": bar_id, "active": True})
        if not bar:
            raise ValueError("Selected bar is unavailable")
        unit_price = calculate_master_unit_price(bar, {})
        existing = next((row for row in resolved if row["product_id"] == bar_id), None)
        if existing:
            existing["quantity"] += 1
            existing["amount_master"] = float(money(unit_price * existing["quantity"]))
        else:
            resolved.append({
                "type": "barring", "product_id": bar["_id"], "article_no": bar.get("article_no"),
                "label": bar["name"], "quantity": 1,
                "unit_price_master": float(unit_price), "amount_master": float(unit_price),
            })
    return resolved


def validate_configuration(product: dict[str, Any], configuration: dict[str, Any]) -> None:
    rules = product.get("configuration", {})
    configurator = rules.get("configurator")
    if product.get("category_id") == "blankets":
        thicknesses = [Decimal(str(value)) for value in rules.get("thicknesses_mm", [])]
        if thicknesses:
            selected = decimal_value(configuration.get("thickness_mm"), "thickness_mm")
            if selected not in thicknesses:
                raise ValueError("Thickness is not available for the selected product")
        formats = rules.get("format_types", [])
        if not formats:
            formats = [
                *( ["cut_format"] if rules.get("supports_cut_format") is True else [] ),
                *( ["bar_format"] if rules.get("supports_bar_format") is True else [] ),
            ]
        if formats and configuration.get("format_type") not in formats:
            raise ValueError("Format is not available for the selected product")
    elif configurator == "mpack":
        valid = [Decimal(str(row.get("value"))) for row in rules.get("thicknesses", [])]
        if valid and decimal_value(configuration.get("thickness_micron"), "thickness_micron") not in valid:
            raise ValueError("Thickness is not available for the selected Underpacking product")
    elif configurator == "chemical":
        formats = rules.get("formats", [])
        selected_id = str(configuration.get("format_id", ""))
        selected = next((row for row in formats if row.get("id") == selected_id), None)
        if not selected:
            raise ValueError("Package is not available for the selected chemical")
        if Decimal(str(selected.get("size_litre"))) != decimal_value(configuration.get("size_litre"), "size_litre"):
            raise ValueError("Package size does not match the selected chemical package")


def configuration_fingerprint(product: dict[str, Any], configuration: dict[str, Any]) -> str:
    configuration = dict(configuration)
    if product.get("category_id") == "blankets" and configuration.get("format_type") == "cut_format":
        for key in ("bar_1_id", "bar_2_id", "use_second_bar"):
            configuration.pop(key, None)
    elif product.get("category_id") == "blankets" and configuration.get("format_type") == "bar_format":
        bar_1_id = str(configuration.get("bar_1_id", "")).strip()
        bar_2_id = str(configuration.get("bar_2_id", "")).strip()
        use_second_bar = configuration.get("use_second_bar")
        if use_second_bar is False or (use_second_bar is None and (not bar_2_id or bar_2_id == bar_1_id)):
            configuration["bar_2_id"] = bar_1_id
    configured_fields = product.get("configuration", {}).get("fingerprint_fields")
    if configured_fields:
        canonical = {key: configuration.get(key) for key in configured_fields if key in configuration}
    else:
        canonical = configuration
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effective_quantity(product: dict[str, Any], configuration: dict[str, Any], quantity: int) -> tuple[int, dict[str, Any]]:
    pricing_type = str(product.get("pricing", {}).get("pricing_type", product.get("pricing", {}).get("type", "fixed")))
    if pricing_type != "per_litre" or not product.get("configuration", {}).get("complete_containers"):
        return quantity, {}
    requested = configuration.get("requested_litres")
    if requested in (None, ""):
        return quantity, {}
    requested_litres = decimal_value(requested, "requested_litres")
    size_litre = decimal_value(configuration.get("size_litre"), "size_litre")
    if not requested_litres or not size_litre:
        raise ValueError("Requested litres and container size must be greater than zero")
    containers_per_batch = int((requested_litres / size_litre).to_integral_value(rounding=ROUND_CEILING))
    effective = containers_per_batch * quantity
    return effective, {
        "requested_litres_per_batch": float(requested_litres),
        "container_size_litre": float(size_litre),
        "containers_per_batch": containers_per_batch,
        "batches": quantity,
    }


def _rule_adjustments(
    product: dict[str, Any], configuration: dict[str, Any], base_master: Decimal,
    business_rules: dict[str, Any],
) -> list[dict[str, Any]]:
    rule = business_rules.get("surcharge_rules", {}).get("cut_format", {})
    categories = rule.get("applies_to_categories", [])
    if not rule.get("enabled") or configuration.get("format_type") != "cut_format" or product.get("category_id") not in categories:
        return []
    percent = decimal_value(rule.get("percent", 0), "cut format surcharge")
    amount = money(base_master * percent / Decimal("100"))
    return [{
        "type": "surcharge", "code": "cut_format", "label": "Cut-format charge",
        "percent": float(percent), "amount_master": float(amount),
    }]


def _effective_discount(
    product: dict[str, Any], quantity: int, requested: Decimal, business_rules: dict[str, Any],
) -> tuple[Decimal, str | None]:
    rule = business_rules.get("discount_rules", {}).get("bulk_rolls", {})
    if (
        rule.get("enabled")
        and product.get("category_id") in rule.get("applies_to_categories", [])
        and quantity >= int(rule.get("minimum_quantity", 10))
    ):
        automatic = decimal_value(rule.get("discount_percent", 0), "bulk discount")
        if automatic > requested:
            return automatic, f"Automatic bulk discount for {quantity} rolls"
    return requested, None


def calculate_line(
    product: dict[str, Any],
    configuration: dict[str, Any],
    *,
    quantity: int,
    discount_percent: Decimal | str | int | float,
    currency: str,
    exchange_rate: Decimal | str | int | float,
    company_tax_rate: Decimal | str | int | float,
    company_tax_mode: str,
    privileged_discount: bool = False,
    adjustments: list[dict[str, Any]] | None = None,
    business_rules: dict[str, Any] | None = None,
    apply_tax: bool = True,
    tax_mode_override: str | None = None,
) -> dict[str, Any]:
    configuration = dict(configuration)
    if product.get("category_id") == "blankets" and configuration.get("format_type") == "cut_format":
        for key in ("bar_1_id", "bar_2_id", "use_second_bar"):
            configuration.pop(key, None)
    if quantity < 1 or quantity > 100_000:
        raise ValueError("Quantity must be between 1 and 100000")
    rules = product.get("discount_rules", {})
    requested_discount = decimal_value(discount_percent, "discount_percent")
    if not rules.get("enabled", True) and requested_discount:
        raise ValueError("Discount is not available for this product")
    discount_step = Decimal(str(rules.get("step", 0.5)))
    if discount_step and requested_discount % discount_step:
        raise ValueError(f"Discount must use {discount_step}% increments")
    discount, discount_reason = _effective_discount(product, quantity, requested_discount, business_rules or {})
    max_discount = Decimal(str(rules.get("privileged_max_percent" if privileged_discount else "default_max_percent", 0)))
    if discount > max_discount:
        raise ValueError(f"Discount exceeds the allowed {max_discount}% maximum")

    validate_configuration(product, configuration)
    rate = decimal_value(exchange_rate, "exchange_rate")
    if not rate:
        raise ValueError("Exchange rate must be greater than zero")
    base_master = calculate_master_unit_price(product, configuration)
    all_adjustments = [*(adjustments or []), *_rule_adjustments(product, configuration, base_master, business_rules or {})]
    adjustment_master = money(sum((Decimal(str(item.get("amount_master", 0))) for item in all_adjustments), Decimal("0")))
    unit_master = money(base_master + adjustment_master)
    unit_selected = money(unit_master * rate)
    effective_quantity, packaging = _effective_quantity(product, configuration, quantity)
    subtotal = money(unit_selected * effective_quantity)
    discount_amount = money(subtotal * discount / Decimal("100"))
    discounted = money(subtotal - discount_amount)

    tax_config = product.get("tax", {})
    has_override = bool(tax_config.get("override_enabled"))
    product_rate = tax_config.get("rate")
    tax_rate = product_rate if has_override and product_rate is not None else company_tax_rate
    product_mode = tax_config.get("mode")
    tax_mode = tax_mode_override or (product_mode if has_override and product_mode else company_tax_mode)
    if tax_mode not in {"exclusive", "inclusive", "no_tax"}:
        raise ValueError("Tax mode must be exclusive, inclusive or no_tax")
    tax = calculate_tax(discounted, tax_rate if apply_tax else 0, tax_mode if apply_tax else "no_tax")
    result = {
        "product_id": product["_id"], "article_no": product.get("article_no"),
        "product_name": product["name"], "description": product.get("description", ""),
        "sku": product.get("sku"), "configuration": configuration,
        "pricing_type": product.get("pricing", {}).get("pricing_type", product.get("pricing", {}).get("type")),
        "pricing_unit": product.get("pricing", {}).get("unit"),
        "requested_quantity": quantity, "quantity": effective_quantity,
        "currency": currency, "master_currency": MASTER_CURRENCY, "exchange_rate": float(rate),
        "base_unit_price_master": float(base_master), "adjustments": all_adjustments,
        "adjustment_amount_master": float(adjustment_master), "master_unit_price": float(unit_master),
        # Explicit snapshot fields used by quotations and downstream systems.
        "master_price_eur": float(unit_master), "converted_price": float(unit_selected),
        "unit_price": float(unit_selected), "subtotal": float(subtotal),
        "requested_discount_percent": float(requested_discount), "discount_percent": float(discount),
        "discount_reason": discount_reason, "discount_amount": float(discount_amount),
        "taxable_amount": float(tax.net), "tax_rate": float(tax.rate), "tax_mode": tax.mode,
        "tax_amount": float(tax.tax), "line_total": float(tax.total),
    }
    if packaging:
        result["packaging"] = packaging
    if product.get("pricing", {}).get("pricing_type") in {"per_sqm", "formula"}:
        result["area_sqm"] = float(area_sqm(configuration))
    return result


def calculate_quote_totals(
    lines: list[dict[str, Any]], transport_cost: Any = 0,
    *, transport_tax_rate: Any = 0, transport_tax_mode: str = "no_tax",
) -> dict[str, float]:
    subtotal = money(sum((Decimal(str(line["subtotal"])) for line in lines), Decimal("0")))
    discount = money(sum((Decimal(str(line["discount_amount"])) for line in lines), Decimal("0")))
    taxable = money(sum((Decimal(str(line["taxable_amount"])) for line in lines), Decimal("0")))
    tax = money(sum((Decimal(str(line["tax_amount"])) for line in lines), Decimal("0")))
    line_total = money(sum((Decimal(str(line["line_total"])) for line in lines), Decimal("0")))
    transport = money(transport_cost or 0)
    transport_tax = calculate_tax(transport, transport_tax_rate, transport_tax_mode)
    transport_is_taxable = transport_tax.mode != "no_tax" and transport_tax.rate > 0
    combined_taxable = money(taxable + (transport_tax.net if transport_is_taxable else Decimal("0")))
    combined_tax = money(tax + transport_tax.tax)
    return {
        "subtotal": float(subtotal), "discount_amount": float(discount),
        "taxable_amount": float(combined_taxable), "product_tax_amount": float(tax),
        "tax_amount": float(combined_tax),
        "transport_cost": float(transport), "transport_tax_amount": float(transport_tax.tax),
        "transport_total": float(transport_tax.total),
        "grand_total": float(money(line_total + transport_tax.total)),
    }
