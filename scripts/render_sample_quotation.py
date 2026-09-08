"""Render a representative quotation through the public API for PDF visual QA."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app import create_app
from app.config import TestConfig


def main() -> None:
    app = create_app(TestConfig)
    client = app.test_client()
    assert client.post("/api/v1/auth/demo", json={}).status_code == 200
    app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, {
        "name": "Athul Nair", "email": "athul@example.com", "phone": "+91 98765 43210",
    })
    assert client.post("/api/v1/companies/select", json={"company_id": "customer-demo-1"}).status_code == 200
    cart = client.post(
        "/api/v1/cart/items",
        json={
            "customer_id": "customer-demo-1",
            "product_id": "mtech_active_prime",
            "currency": "EUR",
            "quantity": 10,
            "discount_percent": 0,
            "configuration": {
                "machine": "Heidelberg Speedmaster XL 106",
                "thickness_mm": 1.96,
                "length": 1050,
                "width": 830,
                "dimension_unit": "mm",
                "format_type": "bar_format",
                "bar_1_id": "aluminium",
                "bar_2_id": "aluminium",
            },
        },
    )
    assert cart.status_code == 201, cart.get_json()
    quote = client.post(
        "/api/v1/quotations",
        json={
            "customer_id": "customer-demo-1",
            "currency": "EUR",
            "proforma_validity_days": 30,
            "payment_terms": "Advance",
            "transport_mode": "by_moneda_team",
            "transport_charges": 85,
            "customer_notes": "Please confirm delivery schedule before dispatch.\nHandle with care during unloading.",
        },
    )
    assert quote.status_code == 201, quote.get_json()
    row = quote.get_json()["data"]
    pdf = client.get(f"/api/v1/quotations/{row['_id']}/pdf?preview=true")
    assert pdf.status_code == 200, pdf.get_json()
    destination = ROOT / "output" / "pdf" / f"{row['quotation_number']}-preview.pdf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(pdf.data)
    print(destination)


if __name__ == "__main__":
    main()
