from __future__ import annotations

from datetime import timedelta
from io import BytesIO

from pypdf import PdfReader

from app.quotations.pdf import _display_date, _line_configuration
from app.quotations.pdf import render_quotation_pdf
from app.repositories.store import utcnow


def test_customer_facing_pdf_template_has_four_columns_equal_stripe_and_conditional_discounts(app):
    now = utcnow()
    base_line = {
        "product_id": "internal-product-id", "article_no": "105", "sku": "INTERNAL-SKU",
        "product_name": "MTech Magnum SF", "description": "Premium Packaging",
        "configuration": {}, "quantity": 10, "unit_price": 5246.15,
        "line_total": 60356.95, "tax_amount": 9206.99, "tax_rate": 18,
    }
    quotation = {
        "quotation_number": "MT-MONEDA-001", "currency": "INR", "created_at": now,
        "expiry_date": now + timedelta(days=30), "proforma_validity_days": 30,
        "issuer_snapshot": {"name": "Moneda Technologies"},
        "customer_company_snapshot": {"name": "Moneda Unicus LLP"},
        "customer_snapshot": {"name": "Moneda Unicus LLP"},
        "salesperson_snapshot": {"name": "Superadmin"}, "payment_terms": "Advance",
        "transport": {"label": "By Consignee", "description": "To be borne by consignee"},
        "exchange_rate": 109.821, "exchange_rate_provider": "Test",
        "lines": [
            {**base_line, "discount_percent": 0},
            {**base_line, "discount_percent": 2.5},
            {**base_line, "discount_percent": 5},
        ],
        "totals": {
            "subtotal": 157384.5, "discount_amount": 3934.61, "taxable_amount": 153449.89,
            "tax_amount": 27620.98, "transport_cost": 0, "transport_tax_amount": 0,
            "grand_total": 181070.87,
        },
        "commercial_conditions": {"incoterms": "ICC INCOTERMS 2020: Ex Works unless specified."}, "notes": "",
    }
    with app.app_context():
        html = app.jinja_env.get_template("quotation/quotation.html").render(
            quotation=quotation, logo_uri="logo.svg", display_date=_display_date,
            line_configuration=_line_configuration,
        )

    assert "article" not in html.lower()
    assert "internal-product-id" not in html and "INTERNAL-SKU" not in html and ">105<" not in html
    assert "Product and description" in html
    assert "<th class=\"center\">Qty</th>" in html
    assert "<th class=\"right\">Unit price</th>" in html
    assert "<th class=\"right\">Total</th>" in html
    assert "2.5% discount" in html and "5% discount" in html and "0% discount" not in html
    assert "33.333333%" in html and "66.666666%" in html
    assert "Converted from the EUR master catalog" not in html
    assert "Rate provider" not in html
    assert "exchange-rate metadata" not in html
    assert "ICC INCOTERMS 2020" not in html
    assert "Ex Works unless specified" not in html

    with app.app_context():
        pdf_bytes = render_quotation_pdf(quotation)
    pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages)
    assert "Converted from the EUR master catalog" not in pdf_text
    assert "Rate provider" not in pdf_text
    assert "ICC INCOTERMS 2020" not in pdf_text
    assert "This document preserves" not in pdf_text
