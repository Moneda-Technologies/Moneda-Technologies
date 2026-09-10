"""Idempotently migrate historical INR quotations to EUR.

Run without ``--apply`` to inspect the records and configured rate.  Pass
``--apply`` only after reviewing the dry-run output.  The migration updates
existing documents in place; it never recreates or deletes quotations.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal

from app import create_app
from app.config import Config
from app.pricing.tax import money


LINE_MONEY_FIELDS = {
    "converted_price", "unit_price", "subtotal", "discount_amount", "taxable_amount",
    "taxable_subtotal", "tax_amount", "gst_amount", "line_total", "total",
    "display_unit_price", "display_subtotal", "display_discount_amount",
    "display_total", "display_final_total", "vat_amount", "product_tax_amount",
    "transport_tax_amount", "transport_total",
}
TOTAL_MONEY_FIELDS = {
    "subtotal", "discount_amount", "taxable_amount", "product_tax_amount",
    "tax_amount", "transport_cost", "transport_tax_amount", "transport_total",
    "grand_total",
}


def _eur(value: object, rate: Decimal) -> object:
    if isinstance(value, bool) or value is None:
        return value
    try:
        return float(money(Decimal(str(value)) / rate))
    except (TypeError, ValueError, ArithmeticError):
        return value


def _convert_line(line: dict, rate: Decimal) -> dict:
    converted = dict(line)
    for field in LINE_MONEY_FIELDS:
        if field in converted:
            converted[field] = _eur(converted[field], rate)
    converted["currency"] = "EUR"
    if converted.get("exchange_rate") is not None:
        converted["exchange_rate"] = 1.0
    return converted


def _convert_totals(totals: dict, rate: Decimal) -> dict:
    converted = dict(totals)
    for field in TOTAL_MONEY_FIELDS:
        if field in converted:
            converted[field] = _eur(converted[field], rate)
    return converted


def _reconcile_line(line: dict) -> dict:
    """Apply the same subtotal/discount/tax/total arithmetic after rounding."""
    result = dict(line)
    if "subtotal" not in result or "discount_amount" not in result:
        return result
    taxable = money(Decimal(str(result.get("subtotal", 0))) - Decimal(str(result.get("discount_amount", 0))))
    if "taxable_amount" in result:
        result["taxable_amount"] = float(taxable)
    if "taxable_subtotal" in result:
        result["taxable_subtotal"] = float(taxable)
    tax_value = result.get("tax_amount", result.get("gst_amount"))
    if tax_value is not None and "line_total" in result:
        total = money(taxable + Decimal(str(tax_value)))
        result["line_total"] = float(total)
        if "total" in result:
            result["total"] = float(total)
    return result


def _reconcile_totals(totals: dict) -> dict:
    result = dict(totals)
    if "subtotal" not in result or "discount_amount" not in result:
        return result
    taxable = money(Decimal(str(result.get("subtotal", 0))) - Decimal(str(result.get("discount_amount", 0))))
    if "taxable_amount" in result:
        result["taxable_amount"] = float(taxable)
    tax_value = result.get("tax_amount", result.get("product_tax_amount", 0))
    transport_total = Decimal(str(result.get("transport_total", result.get("transport_cost", 0)) or 0))
    transport_tax = Decimal(str(result.get("transport_tax_amount", 0) or 0))
    if "grand_total" in result:
        result["grand_total"] = float(money(taxable + Decimal(str(tax_value or 0)) + transport_total + transport_tax))
    return result


def migrate(*, apply: bool) -> tuple[int, int, Decimal]:
    app = create_app({"AUTO_SEED": False})
    with app.app_context():
        store = app.extensions["store"]
        rate, rate_meta = app.extensions["exchange_rate_service"].rate_for("INR")
        rate_decimal = Decimal(str(rate))
        migration_id = "inr-quotations-to-eur-v1"
        rows, _ = store.list("quotations", {"$or": [{"currency": "INR"}, {"currency": "EUR", "currency_migration.migration_id": migration_id}]}, limit=100_000)
        print(f"configured_eur_to_inr_rate={rate_decimal}")
        print(f"inr_quotation_count={sum(1 for row in rows if str(row.get('currency')) == 'INR')}")
        if not rows:
            return 0, 0, rate_decimal
        migrated = 0
        reconciled = 0
        previous_marker = store.find_one("system_migrations", {"_id": migration_id}) or {}
        for row in rows:
            number = row.get("quotation_number") or row.get("_id")
            already_migrated = str(row.get("currency")) == "EUR" and (row.get("currency_migration") or {}).get("migration_id") == migration_id
            lines = [_reconcile_line(_convert_line(line, rate_decimal) if not already_migrated else line) for line in (row.get("lines") or [])]
            totals = _reconcile_totals(_convert_totals(row.get("totals") or {}, rate_decimal) if not already_migrated else row.get("totals") or {})
            migration_record = row.get("currency_migration") if already_migrated else {
                "migration_id": migration_id,
                "source_currency": "INR",
                "target_currency": "EUR",
                "source_eur_to_inr_rate": float(rate_decimal),
                "migrated_at": datetime.now(timezone.utc),
            }
            changes = {
                "currency": "EUR",
                "quotation_currency": "EUR",
                "base_currency": "EUR",
                "exchange_rate": 1.0,
                "exchange_rate_source": "inr_to_eur_migration",
                "converted_price": _eur(row.get("converted_price"), rate_decimal),
                "totals": totals,
                "lines": lines,
                "currency_migration": migration_record,
            }
            print(f"{number}: {'would reconcile' if already_migrated else ('would migrate' if not apply else 'migrating')}" )
            if apply:
                store.update_one("quotations", {"_id": row["_id"]}, changes)
                if already_migrated:
                    reconciled += 1
                else:
                    migrated += 1
        if apply:
            already_count = sum(1 for row in rows if str(row.get("currency")) == "EUR" and (row.get("currency_migration") or {}).get("migration_id") == migration_id)
            store.update_one("system_migrations", {"_id": migration_id}, {
                "migration_id": migration_id,
                "source_currency": "INR",
                "target_currency": "EUR",
                "rate": float(rate_decimal),
                "migrated_count": max(int(previous_marker.get("migrated_count") or 0) + migrated, already_count),
                "reconciled_count": reconciled,
                "applied_at": datetime.now(timezone.utc),
            }, upsert=True)
        print(f"migrated_count={migrated}")
        print(f"reconciled_count={reconciled}")
        print(f"rate_source={rate_meta.get('source', 'configured')}")
        return len(rows), migrated, rate_decimal


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write the in-place migration")
    args = parser.parse_args()
    migrate(apply=args.apply)
