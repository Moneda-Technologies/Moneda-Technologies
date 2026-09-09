"""Import the structured MPack EUR price matrix from the official PDF.

The source is an Excel-generated PDF whose table text retains stable cell
coordinates.  This importer deliberately reads both the per-sheet and per-box
rows and keeps the PDF outside the runtime application.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET


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
    # Table data uses a .5 y-coordinate.  The repeated page watermark/section
    # headings overlap the first table row but use .8, so exclude them.
    selected = []
    for x, word_y, text in sorted(words, key=lambda item: (item[1], item[0])):
        if not (x_min <= x < x_max and y - 5 <= word_y <= y + 30):
            continue
        if abs((word_y % 1) - 0.5) > 0.08:
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


def extract(pdf_path: Path, temporary_parent: Path | None = None) -> dict:
    temporary_parent = temporary_parent or Path.cwd()
    bbox_path = temporary_parent / f".{pdf_path.stem}.bbox.html"
    try:
        subprocess.run(
            ["pdftotext", "-bbox-layout", str(pdf_path), str(bbox_path)],
            check=True,
        )
        root = ET.parse(bbox_path).getroot()
    finally:
        bbox_path.unlink(missing_ok=True)

    namespace = {"x": "http://www.w3.org/1999/xhtml"}
    records: list[dict] = []
    previous_manufacturer = ""
    previous_model = ""

    for page in root.findall(".//x:page", namespace):
        words = [
            (float(word.attrib["xMin"]), float(word.attrib["yMin"]), "".join(word.itertext()))
            for word in page.findall(".//x:word", namespace)
        ]
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

            manufacturer = _row_text(words, 35, 103, y) or previous_manufacturer
            model = _row_text(words, 103, 197, y) or previous_model
            if not manufacturer or not model:
                raise ValueError(f"Missing manufacturer/model at y={y}")
            previous_manufacturer = manufacturer
            previous_model = model

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

    if len(records) != 63:
        raise ValueError(f"Expected 63 machine-size rows, extracted {len(records)}")
    if len({(r["manufacturer"], r["machine_model"], r["width_mm"], r["length_mm"]) for r in records}) != len(records):
        raise ValueError("Extracted machine-size keys are not unique")

    return {
        "schema_version": 1,
        "price_list": {
            "id": "mpack-2026-h2-q3-q4",
            "name": "MPack Price List 2026 H2 / Q3 / Q4",
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
    args = parser.parse_args()
    document = extract(args.source_pdf, args.output_json.parent)
    args.output_json.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(document['machine_sizes'])} machine-size rows to {args.output_json}")


if __name__ == "__main__":
    main()
