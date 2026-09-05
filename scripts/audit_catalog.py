"""Fail fast when the canonical Moneda JSON graph violates its contracts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FILES = {
    "product_types.json", "blanket_categories.json", "blanket_options.json",
    "blanket_bars.json", "blankets.json", "mpack_types.json", "mpack_options.json",
    "mpacks.json", "chemical_categories.json", "chemical_options.json",
    "chemicals.json", "pricing_eur.json", "tax_rules.json",
}
FAMILIES = ["blankets", "mpacks", "chemicals"]
PRICE_FIELDS = {"price", "price_eur", "pricing", "variant_prices", "dimension_prices", "package_prices"}


def load(name: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        row: dict[str, Any] = {}
        for key, value in pairs:
            assert key not in row, f"Duplicate key {key!r} in {name}"
            row[key] = value
        return row
    return json.loads((DATA / name).read_text(encoding="utf-8"), object_pairs_hook=unique)


def assert_no_embedded_price(value: Any, path: str) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            assert key not in PRICE_FIELDS, f"Pricing field {key!r} leaked into {path}"
            assert_no_embedded_price(nested, path)
    elif isinstance(value, list):
        for nested in value:
            assert_no_embedded_price(nested, path)


def valid_price(value: Any) -> bool:
    return value is None or not isinstance(value, bool) and isinstance(value, (int, float)) and value >= 0


def main() -> None:
    active_files = {path.name for path in DATA.glob("*.json")}
    assert active_files == FILES, f"Data directory must contain exactly the 13 canonical files: {sorted(active_files)}"
    documents = {name: load(name) for name in FILES}
    assert [row["id"] for row in documents["product_types.json"]["product_types"]] == FAMILIES

    blanket_categories = {row["id"] for row in documents["blanket_categories.json"]["categories"]}
    assert len(blanket_categories) == 7
    bars = documents["blanket_bars.json"]["bars"]
    bar_ids = {row["id"] for row in bars}
    assert len(bar_ids) == len(bars) == 6
    defaults = documents["blanket_options.json"]["rules"]["default_bar_ids"]
    assert len(defaults) == 2 and set(defaults) <= bar_ids
    for row in bars:
        assert row["unit"] == "bar" and valid_price(row.get("price_eur"))

    blankets_doc = documents["blankets.json"]
    family_products = {
        "blankets": blankets_doc["products"] + blankets_doc["underlay_products"],
        "mpacks": documents["mpacks.json"]["products"],
        "chemicals": documents["chemicals.json"]["products"],
    }
    all_ids = [row["id"] for products in family_products.values() for row in products]
    assert len(all_ids) == len(set(all_ids)), "Product IDs must be globally unique"
    for name in ("blankets.json", "mpacks.json", "chemicals.json"):
        assert_no_embedded_price(documents[name], name)

    for row in family_products["blankets"]:
        assert set(row.get("category_ids", [])) <= blanket_categories
    mpack_types = {row["id"] for row in documents["mpack_types.json"]["types"]}
    assert all(row["type_id"] in mpack_types for row in family_products["mpacks"])
    chemical_categories = {row["id"] for row in documents["chemical_categories.json"]["categories"]}
    assert all(row["category_id"] in chemical_categories for row in family_products["chemicals"])

    pricing = documents["pricing_eur.json"]
    assert pricing["currency"] == "EUR"
    for family, products in family_products.items():
        family_prices = pricing["products"][family]
        assert set(family_prices) == {row["id"] for row in products}, f"Incomplete {family} pricing references"
        for product_id, row in family_prices.items():
            assert valid_price(row.get("price_eur")), f"Invalid base price: {product_id}"
            for field in ("variant_prices_eur", "dimension_prices_eur", "package_prices_eur"):
                assert all(valid_price(value) for value in row.get(field, {}).values()), f"Invalid {field}: {product_id}"

    expected_variants = {
        "mtech_web_x_press_g3": [1.7, 1.96], "mtech_web_x_press_g8": [1.93, 1.96],
        "mtech_kleber_bf": [0.95, 1.05, 1.95], "mtech_underlay_lc": [1.0],
        "mtech_underlay_ch": [0.6, 0.8, 1.0],
    }
    by_id = {row["id"]: row for row in family_products["blankets"]}
    for product_id, expected in expected_variants.items():
        assert [row["thickness_mm"] for row in by_id[product_id]["variants"]] == expected

    tax = documents["tax_rules.json"]
    assert tax["rates"] == [0, 5, 12, 18]
    assert tax["product_override_only"] is True and tax["quotation_override_allowed"] is False
    print(f"Catalog audit passed: 13 files, 3 families, 6 bars, {len(all_ids)} unique products.")


if __name__ == "__main__":
    main()
