from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Any
import re
from xml.sax.saxutils import escape

from flask import current_app


def _display_date(value: Any) -> str:
    if hasattr(value, "strftime"):
        return value.strftime("%d %b %Y")
    text = str(value or "")
    return text[:10] if text else "-"


def _is_mpack_line(line: dict[str, Any]) -> bool:
    configuration = line.get("configuration") or {}
    product_id = str(line.get("product_id") or "").lower().replace("-", "").replace("_", "")
    product_name = str(line.get("product_name") or "").lower()
    return bool(
        line.get("commercial_unit") == "box"
        or product_id == "mtechmpack"
        or "mpack" in product_name
        or line.get("price_per_box_eur") is not None
        or configuration.get("underpacking_type")
        or configuration.get("width_mm") is not None
    )


def _mpack_size(configuration: dict[str, Any]) -> str | None:
    width = configuration.get("width_mm")
    length = configuration.get("length_mm")
    raw_size = configuration.get("size") or configuration.get("machine_size") or configuration.get("size_mm")
    if width is not None and length is not None:
        return f"{width} × {length} mm"
    if raw_size:
        values = re.findall(r"\d+(?:\.\d+)?", str(raw_size))
        if len(values) >= 2:
            return f"{values[0]} × {values[1]} mm"
        if values:
            return f"{values[0]} mm"
    # Legacy MPack snapshots sometimes retained only the second dimension
    # under length_mm. Present it as a size, never as "Length".
    if length is not None:
        return f"{length} mm"
    return None


def _mpack_thickness(configuration: dict[str, Any]) -> str | None:
    thickness_mm = configuration.get("thickness_mm")
    thickness_micron = configuration.get("thickness_micron")
    if thickness_mm is None and thickness_micron is None:
        return None
    if thickness_mm is None:
        thickness_mm = float(thickness_micron) / 1000
    micron = int(round(float(thickness_micron if thickness_micron is not None else float(thickness_mm) * 1000)))
    return f"{float(thickness_mm):.2f} mm ({micron} micron)"


def _line_configuration(line: dict[str, Any]) -> str:
    return " / ".join(_line_configuration_lines(line))


def _line_configuration_lines(line: dict[str, Any]) -> list[str]:
    configuration = line.get("configuration") or {}
    parts: list[str] = []
    if _is_mpack_line(line):
        manufacturer = configuration.get("manufacturer") or configuration.get("machine_manufacturer")
        model = configuration.get("machine_model") or configuration.get("model")
        if manufacturer:
            parts.append(f"Machine: {manufacturer}")
        if model:
            parts.append(f"Model: {model}")
        thickness = _mpack_thickness(configuration)
        if thickness:
            parts.append(f"Thickness: {thickness}")
        size = _mpack_size(configuration)
        if size:
            parts.append(f"Size: {size}")
        return parts or ["Standard product configuration"]
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
    return parts or ["Standard product configuration"]


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
    normal = ParagraphStyle("NormalSmall", parent=base["Normal"], fontName="Helvetica", fontSize=8, leading=10.5, textColor=palette["black"])
    muted = ParagraphStyle("Muted", parent=normal, fontSize=7, leading=9, textColor=palette["muted"])
    label = ParagraphStyle("Label", parent=normal, fontName="Helvetica-Bold", fontSize=6.5, leading=8, textColor=palette["muted"], spaceAfter=2)
    heading = ParagraphStyle("Heading", parent=normal, fontName="Helvetica-Bold", fontSize=10, leading=12)
    right = ParagraphStyle("Right", parent=normal, alignment=TA_RIGHT)
    center = ParagraphStyle("Center", parent=normal, alignment=TA_CENTER)
    quantity_style = ParagraphStyle("Quantity", parent=center, fontSize=7.4, leading=9)
    white = ParagraphStyle("White", parent=normal, textColor=colors.white)
    white_heading = ParagraphStyle("WhiteHeading", parent=heading, textColor=colors.white)
    yellow_label = ParagraphStyle("YellowLabel", parent=label, textColor=palette["yellow"])
    white_muted = ParagraphStyle("WhiteMuted", parent=muted, textColor=colors.HexColor("#bdbdbd"))
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
        canvas.line(17 * mm, 11 * mm, 193 * mm, 11 * mm)
        canvas.setFont("Helvetica-Bold", 6)
        canvas.setFillColor(palette["muted"])
        canvas.drawString(17 * mm, 7 * mm, "MONEDA TECHNOLOGIES")
        canvas.setFont("Helvetica", 6)
        canvas.drawRightString(193 * mm, 7 * mm, f"Page {document.page}")
        canvas.restoreState()

    stream = BytesIO()
    content_width = 176 * mm
    document = SimpleDocTemplate(stream, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm, topMargin=14 * mm, bottomMargin=17 * mm)
    story: list[Any] = []
    logo: Any
    try:
        from svglib.svglib import svg2rlg

        logo = svg2rlg(str(logo_path))
        target_width = 62 * mm
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
    header = Table([[logo, title]], colWidths=[112 * mm, 64 * mm])
    header.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("ALIGN", (1, 0), (1, 0), "RIGHT"), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story.append(header)
    stripe = Table([["", "", ""]], colWidths=[content_width / 3] * 3, rowHeights=[2.2 * mm])
    stripe.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), palette["black"]), ("BACKGROUND", (1, 0), (1, 0), palette["red"]), ("BACKGROUND", (2, 0), (2, 0), palette["yellow"]), ("PADDING", (0, 0), (-1, -1), 0)]))
    story.extend([stripe, Spacer(1, 6 * mm)])

    meta = [
        ("Proforma validity", f"{int(quotation.get('proforma_validity_days') or quotation.get('validity_days') or 30)} Days"),
        ("Payment terms", quotation.get("payment_terms") or "Advance"),
        ("Transport", (quotation.get("transport") or {}).get("label") or "By Consignee"),
    ]
    meta_table = Table(
        [[Paragraph(safe(key.upper()), label) for key, _ in meta], [Paragraph(safe(value), normal) for _, value in meta]],
        colWidths=[content_width / 3] * 3,
    )
    meta_table.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), .5, palette["line"]), ("LINEBELOW", (0, -1), (-1, -1), .5, palette["line"]), ("LINEAFTER", (0, 0), (-2, -1), .5, palette["line"]), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, -1), (-1, -1), 7)]))
    story.extend([meta_table, Spacer(1, 5 * mm)])

    issuer = quotation.get("issuer_snapshot") or {"name": "Moneda Technologies", "email": "business@monedatechnologies.com"}
    creator = quotation.get("creator_snapshot") or quotation.get("salesperson_snapshot") or {}
    customer_company = quotation.get("customer_company_snapshot") or quotation.get("company_snapshot") or {}
    customer = quotation.get("customer_snapshot") or customer_company
    issued_by = [
        Paragraph("FROM", yellow_label),
        Paragraph(safe(issuer.get("name") or "Moneda Technologies"), white_heading),
        Paragraph(lines(
            issuer.get("address"), issuer.get("email"), issuer.get("phone"),
            f"Made by: {creator.get('name')}" if creator.get("name") else None,
            f"Creator email: {creator.get('email')}" if creator.get("email") else None,
            f"Creator phone: {creator.get('phone')}" if creator.get("phone") else None,
        ), ParagraphStyle("WhiteMuted", parent=muted, textColor=colors.HexColor("#bdbdbd"))),
    ]
    prepared_for = [
        Paragraph("TO", yellow_label), Paragraph(safe(customer_company.get("name") or customer.get("name")), white_heading),
        Paragraph(lines(f"Attention: {customer['contact_name']}" if customer.get("contact_name") else None, customer.get("address"), customer.get("email"), customer.get("phone")), white_muted),
    ]
    parties = Table([[issued_by, prepared_for]], colWidths=[86 * mm, 86 * mm], rowHeights=[34 * mm], splitByRow=0)
    parties.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), palette["black"]),
        ("BOX", (0, 0), (-1, 0), .5, palette["black"]),
        # One black panel with a solid Moneda-red divider at the 50% boundary.
        ("LINEBEFORE", (1, 0), (1, 0), 2.0, palette["red"]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12), ("TOPPADDING", (0, 0), (-1, -1), 11),
    ]))
    story.extend([parties, Spacer(1, 5 * mm)])

    story.append(Paragraph("PRODUCTS AND CONFIGURATION", ParagraphStyle("Section", parent=heading, fontSize=8, leading=10, spaceAfter=5)))
    totals = quotation.get("totals") or {}
    currency = "EUR"
    header_row = [
        Paragraph("PRODUCT AND DESCRIPTION", white), Paragraph("QTY", white_center),
        Paragraph("UNIT PRICE", white_right), Paragraph("TOTAL", white_right),
    ]
    table_data: list[list[Any]] = [header_row]
    for line in quotation.get("lines") or []:
        product = Paragraph(
            f"<b>{safe(line.get('product_name'))}</b>"
            f"<br/><font color='#70706b' size='6'>{safe(line.get('description'))}</font>"
            f"<br/>{'<br/>'.join(safe(item) for item in _line_configuration_lines(line))}", normal,
        )
        discount = float(line.get("discount_percent", 0))
        net_subtotal = float(line.get("subtotal", line.get("line_total", 0)))
        quantity = float(line.get("quantity", 1)) or 1
        display_unit_price = float(line.get("unit_price", net_subtotal / quantity))
        display_line_total = float(line.get("line_total", net_subtotal))
        discount_label = f"{discount:g}% discount"
        is_mpack = _is_mpack_line(line)
        raw_unit = str(line.get("commercial_unit") or "pc").lower()
        quantity_value = float(line.get("quantity", 0))
        unit_price_unit = "Box" if is_mpack else ("Pc" if raw_unit == "pc" else raw_unit)
        unit_label = ("Box" if quantity_value == 1 else "Boxes") if is_mpack else (("Pc" if quantity_value == 1 else "Pcs") if raw_unit == "pc" else raw_unit)
        sheet_count = line.get("sheets_per_box")
        quantity_label = f"{quantity_value:g} {unit_label}"
        if is_mpack and sheet_count:
            quantity_label += f"\n({int(sheet_count):,} {'sheets/box' if quantity_value != 1 else 'sheets'})"
        unit_price = f"<b>{currency} {display_unit_price:,.2f} / {unit_price_unit}</b>" + (f"<br/><font color='#df3731'><b>{discount_label}</b></font>" if discount else "")
        row = [
            product, Paragraph(safe(quantity_label).replace("\n", "<br/>"), quantity_style), Paragraph(unit_price, right),
            Paragraph(f"<b>{safe(currency)} {display_line_total:,.2f}</b>", right),
        ]
        table_data.append(row)
    column_widths = [92 * mm, 26 * mm, 29 * mm, 29 * mm]
    items = Table(table_data, colWidths=column_widths, repeatRows=1, splitByRow=0)
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
    if totals.get("transport_cost"):
        total_rows.append(("Transport / freight", totals["transport_cost"]))
    net_grand_total = float(totals.get("grand_total", 0) or 0)
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
            colWidths=[content_width],
        )
        customer_note.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), palette["soft"]), ("BOX", (0, 0), (-1, -1), .5, palette["line"]), ("PADDING", (0, 0), (-1, -1), 7)]))
        commercial_block.extend([customer_note, Spacer(1, 4 * mm)])
    commercial_block.extend([Paragraph("COMMERCIAL CONDITIONS", ParagraphStyle("TermsHeading", parent=heading, fontSize=8, leading=10, spaceAfter=6)), Table([[commercial, signature]], colWidths=[118 * mm, 58 * mm], style=[("VALIGN", (0, 0), (-1, -1), "TOP")])])
    if quotation.get("notes"):
        note = Table([[Paragraph(f"<b>Notes:</b> {safe(quotation['notes'])}", muted)]], colWidths=[content_width])
        note.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), palette["soft"]), ("PADDING", (0, 0), (-1, -1), 7)]))
        commercial_block.append(Spacer(1, 2 * mm))
        commercial_block.append(note)
    story.append(KeepTogether(commercial_block))
    story.append(Spacer(1, 4 * mm))
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return stream.getvalue()


def render_quotation_pdf(quotation: dict[str, Any]) -> bytes:
    logo_path = _logo_path()
    content = _render_reportlab_pdf(quotation, logo_path)
    current_app.logger.info("quotation_pdf_generation quotation_id=%s renderer=reportlab format=pdf result=PASS", quotation.get("_id", "preview"))
    return content
