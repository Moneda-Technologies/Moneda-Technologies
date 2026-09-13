"""Canonical layout for static JSON bootstrap data.

The resolver keeps a read-only fallback to the historical flat layout so an
older deployment can be upgraded without a startup failure. New checkouts
use the grouped layout below.
"""

from __future__ import annotations

from pathlib import Path


DATA_FILE_PATHS: dict[str, Path] = {
    "product_types.json": Path("catalog") / "product_types.json",
    "blanket_categories.json": Path("catalog") / "blankets" / "blanket_categories.json",
    "blanket_options.json": Path("catalog") / "blankets" / "blanket_options.json",
    "blanket_bars.json": Path("catalog") / "blankets" / "blanket_bars.json",
    "blankets.json": Path("catalog") / "blankets" / "blankets.json",
    "mpack_types.json": Path("catalog") / "underpacking" / "mpack_types.json",
    "mpack_options.json": Path("catalog") / "underpacking" / "mpack_options.json",
    "mpacks.json": Path("catalog") / "underpacking" / "mpacks.json",
    "chemical_categories.json": Path("catalog") / "chemicals" / "chemical_categories.json",
    "chemical_options.json": Path("catalog") / "chemicals" / "chemical_options.json",
    "chemicals.json": Path("catalog") / "chemicals" / "chemicals.json",
    "machines.json": Path("machines") / "machines.json",
    "pricing_eur.json": Path("pricing") / "pricing_eur.json",
    "dealer_underpacking_pricing.json": Path("pricing") / "dealer_underpacking_pricing.json",
    "mpack_price_list_2026_h2.json": Path("pricing") / "mpack_price_list_2026_h2.json",
    "tax_rules.json": Path("tax") / "tax_rules.json",
    "countries.json": Path("geography") / "countries.json",
}


def data_file(data_directory: Path, filename: str) -> Path:
    """Return the grouped file path, falling back to the legacy flat path."""
    grouped = data_directory / DATA_FILE_PATHS.get(filename, Path(filename))
    if grouped.exists():
        return grouped
    return data_directory / filename
