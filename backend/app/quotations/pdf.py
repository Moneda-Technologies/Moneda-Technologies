from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from flask import current_app, render_template


def _display_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %Y")
    text = str(value or "")
    return text[:10] if text else "-"


def _line_configuration(line: dict[str, Any]) -> str:
    configuration = line.get("configuration") or {}
    parts: list[str] = []
    if configuration.get("machine"):
        parts.append(f"Machine: {configuration['machine']}")
    if configuration.get("length") and configuration.get("width"):
        unit = configuration.get("dimension_unit", "mm")
        parts.append(f"Size: {configuration['length']} x {configuration['width']} {unit}")
    if configuration.get("thickness_micron"):
        parts.append(f"Thickness: {configuration['thickness_micron']} micron")
    if configuration.get("thickness_mm"):
        parts.append(f"Thickness: {float(configuration['thickness_mm']):.2f} mm")
    if configuration.get("format_type"):
        parts.append(f"Format: {str(configuration['format_type']).replace('_', ' ').title()}")
    if configuration.get("length_mm"):
        parts.append(f"Length: {configuration['length_mm']} mm")
    if configuration.get("finish"):
        parts.append(f"Finish: {configuration['finish']}")
    packaging = line.get("packaging") or {}
    if packaging:
        parts.append(
            f"Packaging: {packaging.get('container_count', line.get('quantity'))} x "
            f"{packaging.get('container_size_litre', '')}L"
        )
    for adjustment in line.get("adjustments") or []:
        if adjustment.get("type") in {"product", "barring"}:
            parts.append(f"{adjustment.get('label', 'Adjustment')}: {adjustment.get('quantity', 1)}")
    return " / ".join(parts) or "Standard product configuration"


def _logo_path() -> Path:
    workspace_root = Path(current_app.root_path).parents[1]
    public_root = (workspace_root / "frontend" / "public").resolve()
    default_logo = public_root / "brand" / "moneda-logo.svg"
    settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    configured = str(settings.get("brand_logo_path", "/brand/moneda-logo.svg")).lstrip("/\\")
    candidate = (public_root / configured).resolve()
    if not candidate.is_relative_to(public_root) or not candidate.is_file():
        candidate = default_logo
    return candidate


def _render_reportlab_pdf(quotation: dict[str, Any], logo_path: Path) -> bytes:
    """Portable fallback for Windows hosts without the native Pango runtime."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    palette = {
        "black": colors.HexColor("#1b1b1b"), "red": colors.HexColor("#df3731"),
        "yellow": colors.HexColor("#f2c800"), "muted": colors.HexColor("#70706b"),
        "line": colors.HexColor("#dededa"), "soft": colors.HexColor("#f5f5f2"),
    }
    base = getSampleStyleSheet()
    normal = ParagraphStyle("NormalSmall", parent=base["Normal"], fontName="Helvetica", fontSize=7.2, leading=10, textColor=palette["black"])
    muted = ParagraphStyle("Muted", parent=normal, fontSize=6.4, leading=8.4, textColor=palette["muted"])
    label = ParagraphStyle("Label", parent=normal, fontName="Helvetica-Bold", fontSize=6, leading=7, textColor=palette["muted"], spaceAfter=2)
    heading = ParagraphStyle("Heading", parent=normal, fontName="Helvetica-Bold", fontSize=10, leading=12)
    right = ParagraphStyle("Right", parent=normal, alignment=TA_RIGHT)
    center = ParagraphStyle("Center", parent=normal, alignment=TA_CENTER)
    white = ParagraphStyle("White", parent=normal, textColor=colors.white)
    white_heading = ParagraphStyle("WhiteHeading", parent=heading, textColor=colors.white)
    white_center = ParagraphStyle("WhiteCenter", parent=white, alignment=TA_CENTER)
    white_right = ParagraphStyle("WhiteRight", parent=white, alignment=TA_RIGHT)

    def safe(value: Any) -> str:
        return escape(str(value or "-"))

    def safe_multiline(value: Any) -> str:
        return escape(str(value or "")).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")

    def lines(*values: Any) -> str:
        return "<br/>".join(safe(value) for value in values if value)

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(palette["line"])
        canvas.line(13 * mm, 11 * mm, 197 * mm, 11 * mm)
        canvas.setFont("Helvetica-Bold", 6)
        canvas.setFillColor(palette["muted"])
        canvas.drawString(13 * mm, 7 * mm, "MONEDA TECHNOLOGIES")
        canvas.setFont("Helvetica", 6)
        canvas.drawRightString(197 * mm, 7 * mm, f"Page {document.page}")
        canvas.restoreState()

    stream = BytesIO()
    document = SimpleDocTemplate(stream, pagesize=A4, leftMargin=13 * mm, rightMargin=13 * mm, topMargin=13 * mm, bottomMargin=17 * mm)
    story: list[Any] = []
    logo: Any
    try:
        from svglib.svglib import svg2rlg

        logo = svg2rlg(str(logo_path))
        target_width = 68 * mm
        scale = target_width / logo.width
        logo.width *= scale
        logo.height *= scale
        logo.scale(scale, scale)
    except (ImportError, OSError, ValueError):
        logo = Paragraph("<font size='22'><b>MONEDA</b></font><br/><font size='8'>T E C H N O L O G I E S</font>", normal)
    quote_number = safe(quotation.get("quotation_number"))
    title = Paragraph(
        f"<font size='20'>QUOTATION</font><br/><font color='#df3731'><b>{quote_number}</b></font>"
        f"<br/><font color='#70706b' size='7'>{safe(_display_date(quotation.get('created_at')))}</font>", right,
    )
    header = Table([[logo, title]], colWidths=[118 * mm, 66 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story.append(header)
    stripe = Table([["", "", ""]], colWidths=[184 * mm / 3] * 3, rowHeights=[2.2 * mm])
    stripe.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), palette["black"]), ("BACKGROUND", (1, 0), (1, 0), palette["red"]), ("BACKGROUND", (2, 0), (2, 0), palette["yellow"]), ("PADDING", (0, 0), (-1, -1), 0)]))
    story.extend([stripe, Spacer(1, 6 * mm)])

    meta = [
        ("Proforma validity", f"{int(quotation.get('proforma_validity_days') or quotation.get('validity_days') or 30)} Days"),
        ("Currency", quotation.get("currency")),
        ("Payment terms", quotation.get("payment_terms") or "Advance"),
        ("Transport", (quotation.get("transport") or {}).get("label") or "By Consignee"),
    ]
    meta_table = Table(
        [[Paragraph(safe(key.upper()), label) for key, _ in meta], [Paragraph(safe(value), normal) for _, value in meta]],
        colWidths=[46 * mm] * 4,
    )
    meta_table.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), .5, palette["line"]), ("LINEBELOW", (0, -1), (-1, -1), .5, palette["line"]), ("LINEAFTER", (0, 0), (-2, -1), .5, palette["line"]), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 7)]))
    story.extend([meta_table, Spacer(1, 5 * mm)])

    issuer = quotation.get("issuer_snapshot") or {"name": "Moneda Technologies", "email": "business@monedatechnologies.com"}
    creator = quotation.get("creator_snapshot") or quotation.get("salesperson_snapshot") or {}
    customer_company = quotation.get("customer_company_snapshot") or quotation.get("company_snapshot") or {}
    customer = quotation.get("customer_snapshot") or customer_company
    issued_by = [
        Paragraph("FROM", ParagraphStyle("YellowLabel", parent=label, textColor=palette["yellow"])),
        Paragraph(safe(issuer.get("name") or "Moneda Technologies"), white_heading),
        Paragraph(lines(
            issuer.get("address"), issuer.get("email"), issuer.get("phone"),
            f"Made by: {creator.get('name')}" if creator.get("name") else None,
            f"Creator email: {creator.get('email')}" if creator.get("email") else None,
            f"Creator phone: {creator.get('phone')}" if creator.get("phone") else None,
        ), ParagraphStyle("WhiteMuted", parent=muted, textColor=colors.HexColor("#bdbdbd"))),
    ]
    prepared_for = [
        Paragraph("TO", label), Paragraph(safe(customer_company.get("name") or customer.get("name")), heading),
        Paragraph(lines(f"Attention: {customer['contact_name']}" if customer.get("contact_name") else None, customer.get("address"), customer.get("email"), customer.get("phone")), muted),
    ]
    parties = Table([[issued_by, prepared_for]], colWidths=[90 * mm, 90 * mm], rowHeights=[38 * mm])
    parties.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), palette["black"]), ("BACKGROUND", (1, 0), (1, 0), palette["soft"]),
        ("BOX", (0, 0), (0, 0), .5, palette["black"]), ("BOX", (1, 0), (1, 0), .5, palette["line"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 11),
    ]))
    story.extend([parties, Spacer(1, 5 * mm)])

    currency = str(quotation.get("currency", "EUR"))

    story.append(Paragraph("PRODUCTS AND CONFIGURATION", ParagraphStyle("Section", parent=heading, fontSize=8, leading=10, spaceAfter=5)))
    totals = quotation.get("totals") or {}
    currency = str(quotation.get("currency", "EUR")).upper()
    has_tax = currency == "INR" and bool(float(totals.get("tax_amount", 0) or 0) or float(totals.get("transport_tax_amount", 0) or 0))
    header_row = [
        Paragraph("PRODUCT AND DESCRIPTION", white), Paragraph("QTY", white_center),
        Paragraph("UNIT PRICE", white_right), Paragraph("TOTAL", white_right),
    ]
    table_data: list[list[Any]] = [header_row]
    for line in quotation.get("lines") or []:
        product = Paragraph(
            f"<b>{safe(line.get('product_name'))}</b>"
            f"<br/><font color='#70706b' size='6'>{safe(line.get('description'))}</font>"
            f"<br/><font size='6'>{safe(_line_configuration(line))}</font>", normal,
        )
        discount = float(line.get("discount_percent", 0))
        net_subtotal = float(line.get("subtotal", line.get("line_total", 0)))
        quantity = float(line.get("quantity", 1)) or 1
        display_unit_price = float(line.get("unit_price", net_subtotal / quantity)) if has_tax else net_subtotal / quantity
        display_line_total = float(line.get("line_total", net_subtotal)) if has_tax else net_subtotal
        discount_label = f"{discount:g}% discount"
        unit_price = f"<b>{currency} {display_unit_price:,.2f}</b>" + (f"<br/><font color='#df3731'><b>{discount_label}</b></font>" if discount else "")
        row = [
            product, Paragraph(f"{float(line.get('quantity', 0)):g}", center), Paragraph(unit_price, right),
            Paragraph(f"<b>{safe(currency)} {display_line_total:,.2f}</b>", right),
        ]
        table_data.append(row)
    column_widths = [106 * mm, 16 * mm, 29 * mm, 33 * mm]
    items = Table(table_data, colWidths=column_widths, repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), palette["black"]), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 1), (-1, -1), .45, palette["line"]),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafaf8")]),
        ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.extend([items, Spacer(1, 5 * mm)])

    total_rows = [("Subtotal", totals.get("subtotal", 0))]
    if totals.get("discount_amount"):
        total_rows.append(("Discount", -float(totals.get("discount_amount", 0))))
    if has_tax:
        inclusive_tax = all(line.get("tax_mode") == "inclusive" for line in quotation.get("lines", []) if line.get("tax_amount"))
        gst_label = f"GST {'included ' if inclusive_tax else ''}({float(next((line.get('tax_rate', 18) for line in quotation.get('lines', []) if line.get('tax_amount')), 18)):.0f}%)"
        total_rows.extend([
            ("Taxable amount", totals.get("taxable_amount", 0)),
            (gst_label, float(totals.get("tax_amount", 0))),
        ])
    if totals.get("transport_cost"):
        total_rows.append(("Transport / freight", totals["transport_cost"]))
    net_grand_total = float(totals.get("grand_total", 0) or 0) if has_tax else sum(float(line.get("subtotal", line.get("line_total", 0))) for line in quotation.get("lines") or []) + float(totals.get("transport_cost", 0) or 0)
    total_rows.append(("Grand total", net_grand_total))
    last = len(total_rows) - 1
    totals_data = []
    for index, (name, value) in enumerate(total_rows):
        name_style = white if index == last else normal
        value_style = white_right if index == last else right
        totals_data.append([Paragraph(safe(name), name_style), Paragraph(f"{safe(currency)} {float(value):,.2f}", value_style)])
    totals_table = Table(totals_data, colWidths=[36 * mm, 35 * mm], hAlign="RIGHT")
    totals_table.setStyle(TableStyle([("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4), ("BACKGROUND", (0, last), (-1, last), palette["black"]), ("TEXTCOLOR", (0, last), (-1, last), colors.white), ("FONTNAME", (0, last), (-1, last), "Helvetica-Bold"), ("TOPPADDING", (0, last), (-1, last), 7), ("BOTTOMPADDING", (0, last), (-1, last), 7)]))
    story.extend([totals_table, Spacer(1, 6 * mm)])

    conditions = quotation.get("commercial_conditions") or {}
    transport = quotation.get("transport") or {}
    condition_rows = [
        [Paragraph("PAYMENT", label), Paragraph("PROFORMA VALIDITY", label)],
        [Paragraph(safe(quotation.get("payment_terms") or conditions.get("payment") or "As agreed"), muted), Paragraph(f"{int(quotation.get('proforma_validity_days') or quotation.get('validity_days') or 30)} Days", muted)],
        [Paragraph("TRANSPORT", label), Paragraph("", label)],
        [Paragraph(safe(transport.get("description") or conditions.get("duties_taxes_bank_charges") or "As agreed"), muted), Paragraph("", muted)],
    ]
    commercial = Table(condition_rows, colWidths=[58 * mm, 58 * mm])
    commercial.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBEFORE", (0, 0), (-1, -1), 1.2, palette["line"]), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    signature = Table([[Paragraph("AUTHORIZATION", label)], [Spacer(1, 15 * mm)], [Paragraph("<b>For Moneda Technologies</b><br/><font color='#70706b' size='6'>Authorized signatory</font>", normal)]], colWidths=[58 * mm])
    signature.setStyle(TableStyle([("LINEABOVE", (0, -1), (0, -1), .7, palette["black"]), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    commercial_block: list[Any] = []
    if quotation.get("customer_notes"):
        customer_note = Table(
            [[Paragraph("CUSTOMER NOTES", label)], [Paragraph(safe_multiline(quotation["customer_notes"]), normal)]],
            colWidths=[180 * mm],
        )
        customer_note.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), palette["soft"]), ("BOX", (0, 0), (-1, -1), .5, palette["line"]), ("PADDING", (0, 0), (-1, -1), 7)]))
        commercial_block.extend([customer_note, Spacer(1, 4 * mm)])
    commercial_block.extend([Paragraph("COMMERCIAL CONDITIONS", ParagraphStyle("TermsHeading", parent=heading, fontSize=8, leading=10, spaceAfter=6)), Table([[commercial, signature]], colWidths=[122 * mm, 58 * mm], style=[("VALIGN", (0, 0), (-1, -1), "TOP")])])
    if quotation.get("notes"):
        note = Table([[Paragraph(f"<b>Notes:</b> {safe(quotation['notes'])}", muted)]], colWidths=[116 * mm])
        note.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), palette["soft"]), ("PADDING", (0, 0), (-1, -1), 7)]))
        commercial_block.append(Spacer(1, 2 * mm))
        commercial_block.append(note)
    story.append(KeepTogether(commercial_block))
    story.append(Spacer(1, 4 * mm))
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()


def render_quotation_pdf(quotation: dict[str, Any]) -> bytes:
    logo_path = _logo_path()
    renderer_preference = str(current_app.config.get("PDF_RENDERER", "auto")).lower()
    if renderer_preference not in {"auto", "weasyprint", "reportlab"}:
        raise RuntimeError("PDF_RENDERER must be auto, weasyprint, or reportlab")
    if renderer_preference == "reportlab":
        content = _render_reportlab_pdf(quotation, logo_path)
        current_app.logger.info("quotation_pdf_generation quotation_id=%s renderer=reportlab format=pdf result=PASS", quotation.get("_id", "preview"))
        return content
    html = render_template(
        "quotation/quotation.html",
        quotation=quotation,
        logo_uri=logo_path.as_uri(),
        display_date=_display_date,
        line_configuration=_line_configuration,
    )
    try:
        from weasyprint import HTML
        content = HTML(string=html, base_url=str(Path(current_app.root_path).parents[1])).write_pdf()
        current_app.logger.info("quotation_pdf_generation quotation_id=%s renderer=weasyprint format=pdf result=PASS", quotation.get("_id", "preview"))
        return content
    except (ImportError, OSError) as exc:
        if renderer_preference == "weasyprint":
            current_app.logger.error("quotation_pdf_generation quotation_id=%s renderer=weasyprint format=pdf result=FAIL error_type=%s", quotation.get("_id", "preview"), type(exc).__name__)
            raise RuntimeError("WeasyPrint is unavailable; install its native Pango/GLib runtime or set PDF_RENDERER=reportlab") from exc
        content = _render_reportlab_pdf(quotation, logo_path)
        current_app.logger.warning("quotation_pdf_generation quotation_id=%s renderer=reportlab format=pdf result=PASS fallback=weasyprint_unavailable error_type=%s", quotation.get("_id", "preview"), type(exc).__name__)
        return content
