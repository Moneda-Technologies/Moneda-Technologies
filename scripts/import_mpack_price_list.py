"""Import the structured MPack EUR price matrix from the official PDF.

The source is an Excel-generated PDF whose table text retains stable cell
coordinates.  This importer deliberately reads both the per-sheet and per-box
rows and keeps the PDF outside the runtime application.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

try:
    import fitz
except ImportError as exc:  # pragma: no cover - exercised by operators
    raise SystemExit(
        "PyMuPDF is required for MPack PDF imports. Install backend requirements first."
    ) from exc


THICKNESSES = (
    ("0.050", 50, 200),
    ("0.075", 75, 200),
    ("0.100", 100, 100),
    ("0.125", 125, 100),
    ("0.150", 150, 100),
    ("0.175", 175, 100),
    ("0.200", 200, 100),
    ("0.250", 250, 100),
    ("0.300", 300, 100),
    ("0.350", 350, 50),
    ("0.400", 400, 50),
    ("0.500", 500, 50),
)


def _money(text: str) -> float:
    return float(text.removeprefix("\N{EURO SIGN}").replace(",", ""))


def _row_text(words: list[tuple[float, float, str]], x_min: float, x_max: float, y: float) -> str:
    # Manufacturer/model cells are vertically centred beside the two price
    # lines.  Reading the bounded cell (rather than flattened page text) keeps
    # wrapped model names attached to the correct dimensions.
    selected = []
    for x, word_y, text in sorted(words, key=lambda item: (item[1], item[0])):
        if not (x_min <= x < x_max and y - 5 <= word_y <= y + 30):
            continue
        selected.append(text)
    return re.sub(r"\s+", " ", " ".join(selected)).strip()


def _dimension(words: list[tuple[float, float, str]], x_min: float, x_max: float, y: float) -> int:
    values = [
        int(text)
        for x, word_y, text in words
        if x_min <= x < x_max and abs(word_y - y) <= 4 and text.isdigit()
    ]
    if len(values) != 1:
        raise ValueError(f"Expected one dimension at y={y}, found {values}")
    return values[0]


def extract(
    pdf_path: Path,
    temporary_parent: Path | None = None,
    *,
    price_list_id: str = "mpack-2026-h2-q3-q4",
    account_type: str = "DISTRIBUTOR",
) -> dict:
    del temporary_parent  # retained for compatibility with older callers
    account_type = account_type.strip().upper()
    if account_type not in {"DISTRIBUTOR", "DEALER"}:
        raise ValueError("account_type must be DISTRIBUTOR or DEALER")

    document = fitz.open(pdf_path)
    records: list[dict] = []
    previous_manufacturer = ""
    previous_model = ""
    for page in document:
        words = [(float(word[0]), float(word[1]), str(word[4])) for word in page.get_text("words")]
        price_rows: dict[float, list[tuple[float, str]]] = {}
        for x, y, text in words:
            if x >= 275 and y > 200 and text.startswith("\N{EURO SIGN}"):
                price_rows.setdefault(round(y, 2), []).append((x, text))

        sheet_rows = [
            (y, sorted(prices))
            for y, prices in sorted(price_rows.items())
            if len(prices) == len(THICKNESSES) and _money(sorted(prices)[0][1]) < 5
        ]
        for y, sheet_prices in sheet_rows:
            box_prices = sorted(price_rows.get(round(y + 20, 2), []))
            if len(box_prices) != len(THICKNESSES):
                raise ValueError(f"Missing per-box row after sheet-price row y={y}")

            manufacturer = _row_text(words, 35, 103, y)
            model = _row_text(words, 103, 197, y)
            # Excel merges repeated machine-name cells across adjacent size
            # rows in a few places. PyMuPDF correctly exposes that text only
            # once, so inherit the immediately preceding identity while the
            # manufacturer remains the same.
            if not manufacturer:
                manufacturer = previous_manufacturer
            if not model and manufacturer == previous_manufacturer:
                model = previous_model
            if not manufacturer or not model:
                raise ValueError(f"Missing manufacturer/model at y={y}")

            prices = []
            for (thickness, micron, sheets_per_box), (_, sheet), (_, box) in zip(
                THICKNESSES, sheet_prices, box_prices, strict=True
            ):
                prices.append({
                    "thickness_mm": float(thickness),
                    "thickness_micron": micron,
                    "sheets_per_box": sheets_per_box,
                    "price_per_sheet_eur": _money(sheet),
                    "price_per_box_eur": _money(box),
                })

            records.append({
                "manufacturer": manufacturer,
                "machine_model": model,
                "width_mm": _dimension(words, 197, 238, y),
                "length_mm": _dimension(words, 238, 278, y),
                "prices": prices,
            })
            previous_manufacturer = manufacturer
            previous_model = model

    if len(records) != 63:
        raise ValueError(f"Expected 63 machine-size rows, extracted {len(records)}")
    if len({(r["manufacturer"], r["machine_model"], r["width_mm"], r["length_mm"]) for r in records}) != len(records):
        raise ValueError("Extracted machine-size keys are not unique")

    first_page_text = document[0].get_text("text") if len(document) else ""
    source = "RGF USA" if "RGF USA" in first_page_text else "RGF EUROPE" if "RGF EUROPE" in first_page_text else "RGF"
    return {
        "schema_version": 1,
        "price_list": {
            "id": price_list_id,
            "name": f"{source} MPack Price List 2026 H2 / Q3 / Q4",
            "account_type": account_type,
            "source": source,
            "version": "2026 H2 / Q3 / Q4",
            "currency": "EUR",
            "valid_from": "2026-07-01",
            "valid_until": "2026-12-31",
            "source_document": pdf_path.name,
            "quantity_unit": "box",
        },
        "thicknesses": [
            {"thickness_mm": float(mm), "thickness_micron": micron, "sheets_per_box": sheets}
            for mm, micron, sheets in THICKNESSES
        ],
        "machine_sizes": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_pdf", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--account-type", choices=("DISTRIBUTOR", "DEALER"), default="DISTRIBUTOR")
    parser.add_argument("--price-list-id", default="mpack-2026-h2-q3-q4")
    args = parser.parse_args()
    document = extract(
        args.source_pdf,
        args.output_json.parent,
        price_list_id=args.price_list_id,
        account_type=args.account_type,
    )
    args.output_json.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(document['machine_sizes'])} machine-size rows to {args.output_json}")


if __name__ == "__main__":
    main()
