from __future__ import annotations

import hashlib
import json
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Any

from app.pricing.tax import money


MASTER_CURRENCY = "EUR"
SHEET_PRICE_PRECISION = Decimal("0.001")


def sheet_money(value: Decimal | str | int | float) -> Decimal:
    """Keep MPack per-sheet display values precise to three decimals."""
    return Decimal(str(value)).quantize(SHEET_PRICE_PRECISION, rounding=ROUND_HALF_UP)


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
    """Legacy compatibility helper; active Moneda pricing is always tax-free."""
    return 0, "no_tax"


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


def resolve_mpack_selection(product: dict[str, Any], configuration: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve one exact, server-owned MPack machine/size/thickness row."""
    if str(product.get("pricing", {}).get("pricing_type", "")) != "per_pack":
        return None
    machine_sizes = product.get("configuration", {}).get("machine_sizes") or []
    if not machine_sizes:
        return None
    manufacturer = " ".join(str(configuration.get("manufacturer", "")).split())
    machine_model = " ".join(str(configuration.get("machine_model", "")).split())
    if not manufacturer or not machine_model:
        raise ValueError("Machine manufacturer and model are required")
    width = decimal_value(configuration.get("width_mm"), "width_mm")
    length = decimal_value(configuration.get("length_mm"), "length_mm")
    thickness_value = configuration.get("thickness_mm")
    if thickness_value in (None, "") and configuration.get("thickness_micron") not in (None, ""):
        thickness_value = decimal_value(configuration.get("thickness_micron"), "thickness_micron") / Decimal("1000")
    thickness = decimal_value(thickness_value, "thickness_mm")
    machine_row = next((
        row for row in machine_sizes
        if str(row.get("manufacturer", "")).casefold() == manufacturer.casefold()
        and str(row.get("machine_model", "")).casefold() == machine_model.casefold()
        and Decimal(str(row.get("width_mm"))) == width
        and Decimal(str(row.get("length_mm"))) == length
    ), None)
    if not machine_row:
        raise ValueError("Size is not available for the selected machine model")
    price_row = next((
        row for row in machine_row.get("prices", [])
        if Decimal(str(row.get("thickness_mm"))) == thickness
    ), None)
    if not price_row:
        raise ValueError("Thickness is not available for the selected machine size")
    return {
        "manufacturer": machine_row["manufacturer"],
        "machine_model": machine_row["machine_model"],
        "width_mm": int(machine_row["width_mm"]),
        "length_mm": int(machine_row["length_mm"]),
        "thickness_mm": float(Decimal(str(price_row["thickness_mm"]))),
        "thickness_micron": int(price_row["thickness_micron"]),
        "sheets_per_box": int(price_row["sheets_per_box"]),
        "price_per_sheet_eur": float(decimal_value(price_row["price_per_sheet_eur"], "price_per_sheet_eur")),
        "price_per_box_eur": float(decimal_value(price_row["price_per_box_eur"], "price_per_box_eur")),
    }


def validate_blanket_machine_selection(
    product: dict[str, Any], configuration: dict[str, Any],
    allowed_machines: list[dict[str, Any]] | None = None,
) -> None:
    """Validate a blanket machine name before pricing.

    Machine names are optional for cut format and mandatory for bar format.
    When a configured machine catalogue exists, an entered name must match a
    known record; the UI is never the authority for this rule.
    """
    if product.get("category_id") != "blankets":
        return
    machine_name = " ".join(str(configuration.get("machine", configuration.get("machine_name", ""))).split())
    machine_id = str(configuration.get("machine_id", "")).strip()
    if configuration.get("format_type") == "bar_format" and not machine_name:
        raise ValueError("Bar Format requires a machine name")
    if not machine_name:
        return

    declared = product.get("configuration", {}).get("machine_options") or []
    if isinstance(declared, dict):
        flattened: list[dict[str, Any]] = []
        for manufacturer_row in declared.get("manufacturers", []):
            models = manufacturer_row.get("models") or []
            if models:
                flattened.extend({
                    "manufacturer": manufacturer_row.get("name") or manufacturer_row.get("manufacturer"),
                    "manufacturer_id": manufacturer_row.get("id") or manufacturer_row.get("manufacturer_id"),
                    "machine_model": model.get("name") or model.get("model"),
                    "model_id": model.get("id") or model.get("model_id"),
                    "id": model.get("id") or model.get("model_id"),
                } for model in models)
            else:
                flattened.append(manufacturer_row)
        declared = flattened
    candidates = declared if declared else (allowed_machines or [])
    # Some installations do not yet have a machine catalogue. In that case a
    # non-empty machine name is still a valid user-supplied configuration; the
    # mandatory bar-format rule above remains enforced server-side.
    if not candidates:
        if machine_id:
            return
        return

    def same(value: Any, expected: str) -> bool:
        return bool(expected) and str(value or "").strip().casefold() == expected.casefold()

    match = next((row for row in candidates if machine_id and str(row.get("id") or row.get("_id") or "") == machine_id), None)
    if machine_id and not match:
        raise ValueError("Selected machine ID is not available for this product")
    if match and machine_name:
        row_name = match.get("name") or (
            f"{match.get('manufacturer', '')} - {match.get('machine_model', match.get('model', ''))}"
        ).strip(" -")
        if not same(row_name, machine_name):
            raise ValueError("Machine ID and machine name do not match")
    if not match:
        match = next((row for row in candidates if same(
        row.get("name") or (
            f"{row.get('manufacturer', '')} - {row.get('machine_model', row.get('model', ''))}"
        ).strip(" -"), machine_name)), None)
    if not match:
        raise ValueError("Selected machine name is not available for this product")


def calculate_master_unit_price(product: dict[str, Any], configuration: dict[str, Any]) -> Decimal:
    mpack_selection = resolve_mpack_selection(product, configuration)
    if mpack_selection:
        # Client-supplied price fields are deliberately ignored.  The exact
        # per-box amount always comes from the seeded official price matrix.
        return money(decimal_value(mpack_selection["price_per_box_eur"], "price_per_box_eur"))
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
        if rules.get("machine_sizes") and str(product.get("pricing", {}).get("pricing_type", "")) == "per_pack":
            resolve_mpack_selection(product, configuration)
            return
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
    # Discounts are always explicit. Quantity, product family, customer and
    # display currency must never silently change the selected value.
    discount = requested_discount
    discount_source = "user_selected" if discount else "default"
    max_discount = Decimal(str(rules.get("privileged_max_percent" if privileged_discount else "default_max_percent", 0)))
    if discount > max_discount:
        raise ValueError(f"Discount exceeds the allowed {max_discount}% maximum")

    validate_configuration(product, configuration)
    mpack_selection = resolve_mpack_selection(product, configuration)
    if mpack_selection:
        configuration.update(mpack_selection)
        price_list = product.get("configuration", {}).get("machine_price_list") or {}
        configuration["price_list_id"] = str(price_list.get("id", ""))
        configuration["price_valid_from"] = str(price_list.get("valid_from", ""))
        configuration["price_valid_until"] = str(price_list.get("valid_until", ""))
    rate = decimal_value(exchange_rate, "exchange_rate")
    if not rate:
        raise ValueError("Exchange rate must be greater than zero")
    base_master = calculate_master_unit_price(product, configuration)
    all_adjustments = [*(adjustments or []), *_rule_adjustments(product, configuration, base_master, business_rules or {})]
    adjustment_master = money(sum((Decimal(str(item.get("amount_master", 0))) for item in all_adjustments), Decimal("0")))
    unit_master = money(base_master + adjustment_master)
    effective_quantity, packaging = _effective_quantity(product, configuration, quantity)
    # Calculate the complete commercial value in EUR first. Display currency
    # conversion happens only after discounting, so FX rounding never changes
    # the authoritative commercial result.
    master_subtotal = money(unit_master * effective_quantity)
    if mpack_selection:
        configuration["total_eur"] = float(master_subtotal)
    master_discount_amount = money(master_subtotal * discount / Decimal("100"))
    master_total = money(master_subtotal - master_discount_amount)
    discounted_unit_master = money(unit_master * (Decimal("100") - discount) / Decimal("100"))
    unit_selected = money(unit_master * rate)
    subtotal = money(master_subtotal * rate)
    discount_amount = money(master_discount_amount * rate)
    discounted = money(master_total * rate)

    result = {
        "product_id": product["_id"], "article_no": product.get("article_no"),
        "product_name": product["name"], "description": product.get("description", ""),
        "sku": product.get("sku"), "configuration": configuration,
        "commercial_unit": product.get("commercial_unit") or (
            "box" if product.get("category_id") == "mpacks" else
            "pc" if product.get("category_id") == "blankets" else
            str(product.get("pricing", {}).get("unit", "unit"))
        ),
        "pricing_type": product.get("pricing", {}).get("pricing_type", product.get("pricing", {}).get("type")),
        "pricing_unit": product.get("pricing", {}).get("unit"),
        "requested_quantity": quantity, "quantity": effective_quantity,
        "currency": currency, "master_currency": MASTER_CURRENCY, "exchange_rate": float(rate),
        "base_unit_price_master": float(base_master), "adjustments": all_adjustments,
        "adjustment_amount_master": float(adjustment_master), "master_unit_price": float(unit_master),
        # Explicit snapshot fields used by quotations and downstream systems.
        "master_price_eur": float(unit_master), "converted_price": float(unit_selected),
        "master_subtotal": float(master_subtotal), "master_discount_amount": float(master_discount_amount),
        "master_total": float(master_total), "master_final_total": float(master_total),
        "master_discounted_unit_price": float(discounted_unit_master),
        "display_unit_price": float(unit_selected),
        "display_subtotal": float(subtotal), "display_discount_amount": float(discount_amount),
        "display_total": float(discounted), "display_final_total": float(discounted),
        "unit_price": float(unit_selected), "subtotal": float(subtotal),
        "requested_discount_percent": float(requested_discount), "discount_percent": float(discount),
        "discount_reason": None, "discount_source": discount_source, "discount_amount": float(discount_amount),
        "line_total": float(discounted), "total": float(discounted),
    }
    if packaging:
        result["packaging"] = packaging
    if mpack_selection:
        result["price_per_sheet_eur"] = mpack_selection["price_per_sheet_eur"]
        result["price_per_box_eur"] = mpack_selection["price_per_box_eur"]
        result["discounted_price_per_sheet_eur"] = float(sheet_money(Decimal(str(mpack_selection["price_per_sheet_eur"])) * (Decimal("100") - discount) / Decimal("100")))
        result["discounted_price_per_box_eur"] = float(money(Decimal(str(mpack_selection["price_per_box_eur"])) * (Decimal("100") - discount) / Decimal("100")))
        result["sheets_per_box"] = mpack_selection["sheets_per_box"]
        result["price_list"] = product.get("configuration", {}).get("machine_price_list") or {}
    if product.get("pricing", {}).get("pricing_type") in {"per_sqm", "formula"}:
        result["area_sqm"] = float(area_sqm(configuration))
    return result


def with_display_currency(
    line: dict[str, Any], currency: str, exchange_rate: Decimal | str | int | float,
) -> dict[str, Any]:
    """Render a server-created EUR commercial snapshot in a reference currency."""
    if str(line.get("master_currency", "")).upper() != MASTER_CURRENCY:
        raise ValueError("Cart pricing snapshot must use EUR master values")
    rate = decimal_value(exchange_rate, "exchange_rate")
    if not rate:
        raise ValueError("Exchange rate must be greater than zero")
    required = (
        "master_unit_price", "master_subtotal", "master_discount_amount",
        "master_final_total",
    )
    if any(line.get(field) is None for field in required):
        raise ValueError("Cart pricing snapshot is missing EUR commercial values")

    unit = money(Decimal(str(line["master_unit_price"])) * rate)
    subtotal = money(Decimal(str(line["master_subtotal"])) * rate)
    discount = money(Decimal(str(line["master_discount_amount"])) * rate)
    final_total = money(Decimal(str(line["master_final_total"])) * rate)
    result = dict(line)
    result.update({
        "currency": currency, "display_currency": currency,
        "quotation_currency": MASTER_CURRENCY, "exchange_rate": float(rate),
        "converted_price": float(unit), "display_unit_price": float(unit),
        "display_subtotal": float(subtotal),
        "display_discount_amount": float(discount),
        "display_total": float(final_total), "display_final_total": float(final_total),
        # Compatibility fields are display-only. Authoritative consumers use
        # the explicitly named master_* fields.
        "unit_price": float(unit), "subtotal": float(subtotal),
        "discount_amount": float(discount), "line_total": float(final_total),
        "total": float(final_total),
    })
    result.pop("final_total", None)
    for key in (
        "taxable_amount", "tax_rate", "tax_mode", "tax_amount", "taxable_subtotal",
        "gst_applicable", "gst_rate", "gst_amount", "is_gst_inclusive", "vat_amount",
    ):
        result.pop(key, None)
    return result


def calculate_quote_totals(
    lines: list[dict[str, Any]], transport_cost: Any = 0,
    *, transport_tax_rate: Any = 0, transport_tax_mode: str = "no_tax",
) -> dict[str, float]:
    subtotal = money(sum((Decimal(str(line["subtotal"])) for line in lines), Decimal("0")))
    discount = money(sum((Decimal(str(line["discount_amount"])) for line in lines), Decimal("0")))
    line_total = money(sum((Decimal(str(line["line_total"])) for line in lines), Decimal("0")))
    transport = money(transport_cost or 0)
    return {
        "subtotal": float(subtotal), "discount_amount": float(discount),
        "transport_cost": float(transport), "transport_total": float(transport),
        "grand_total": float(money(line_total + transport)),
    }
