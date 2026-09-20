from __future__ import annotations

import base64
from html import escape
from pathlib import Path
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, current_app, request, send_file

from app.api.responses import failure, success
from app.config import PROJECT_ROOT
from app.middleware.access import current_user, enforce_customer, permission_required
from app.pricing.price_lists import list_price_lists, price_list_by_id
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("price_lists", __name__, url_prefix="/api/price-lists")


def _emails(value: object) -> list[str]:
    raw_values = value if isinstance(value, list) else str(value or "").split(",")
    cleaned: list[str] = []
    for raw in raw_values:
        address = str(raw or "").strip()
        if not address:
            continue
        try:
            normalized = validate_email(address, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            raise ValueError(f"Invalid email address: {address}") from exc
        if normalized.casefold() not in {item.casefold() for item in cleaned}:
            cleaned.append(normalized)
    return cleaned


def _merge_recipients(to: list[str], cc: list[str], bcc: list[str], configured: dict[str, list[str]]) -> tuple[list[str], list[str], list[str]]:
    """Apply centrally configured CC/BCC without allowing duplicates or BCC leaks."""
    to_keys = {item.casefold() for item in to}
    merged_cc = list(dict.fromkeys([*configured.get("cc", []), *cc]))
    merged_cc = [item for item in merged_cc if item.casefold() not in to_keys]
    cc_keys = {item.casefold() for item in merged_cc}
    merged_bcc = list(dict.fromkeys([*configured.get("bcc", []), *bcc]))
    merged_bcc = [item for item in merged_bcc if item.casefold() not in to_keys and item.casefold() not in cc_keys]
    return to, merged_cc, merged_bcc


def _official_pdf_path(definition: dict) -> Path | None:
    relative_path = str(definition.get("pdf_path") or "").strip()
    if not relative_path:
        return None
    root = PROJECT_ROOT.resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path if path.is_file() and path.suffix.lower() == ".pdf" else None


def _document_metadata(definition: dict) -> dict:
    """Expose page orientation without changing the official PDF asset."""
    metadata = dict(definition.get("metadata") or {})
    path = _official_pdf_path(definition)
    if path is None:
        return metadata
    try:
        from pypdf import PdfReader

        sizes: list[dict[str, float | str]] = []
        orientations: set[str] = set()
        for page in PdfReader(str(path)).pages:
            width = float(page.mediabox.width)
            height = float(page.mediabox.height)
            orientation = "portrait" if height >= width else "landscape"
            orientations.add(orientation)
            sizes.append({"width": width, "height": height, "orientation": orientation})
        if sizes:
            metadata["page_orientation"] = next(iter(orientations)) if len(orientations) == 1 else "mixed"
            metadata["page_sizes"] = sizes
    except Exception:
        current_app.logger.exception("price-list PDF metadata inspection failed price_list_id=%s", definition.get("_id"))
    return metadata


@bp.get("")
@permission_required("price_list.view")
def get_price_lists():
    rows = list_price_lists(current_app.extensions["store"])
    rows = [{**row, "metadata": _document_metadata(row)} for row in rows]
    category = str(request.args.get("category") or "").strip()
    search = str(request.args.get("search") or "").strip().casefold()
    if category:
        rows = [row for row in rows if str(row.get("category")) == category]
    if search:
        rows = [row for row in rows if search in f"{row.get('display_name', '')} {row.get('description', '')}".casefold()]
    return success({"items": rows, "total": len(rows)})


@bp.get("/<price_list_id>/document")
@permission_required("price_list.view")
def price_list_document(price_list_id: str):
    """Serve the official server-side PDF without exposing filesystem paths."""
    definition = price_list_by_id(current_app.extensions["store"], price_list_id)
    if not definition or not definition.get("active", True):
        return failure("Price list not found", status=404)
    path = _official_pdf_path(definition)
    if path is None:
        return failure("Official PDF is not configured for this price list", status=404, error="PRICE_LIST_PDF_MISSING")
    filename = str(definition.get("pdf_filename") or path.name).replace("\"", "")[:180]
    return send_file(path, mimetype="application/pdf", as_attachment=request.args.get("download") == "1", download_name=filename)


@bp.patch("/<price_list_id>")
@permission_required("price_list.manage")
def update_price_list(price_list_id: str):
    store = current_app.extensions["store"]
    existing = price_list_by_id(store, price_list_id)
    if not existing:
        return failure("Price list not found", status=404)
    raw = request.get_json(silent=True) or {}
    allowed = {"display_name", "description", "category", "classification", "document_url", "active", "sort_order", "metadata"}
    changes = {key: value for key, value in raw.items() if key in allowed}
    if "document_url" in changes and not str(changes["document_url"]).startswith("https://workdrive.zohoexternal.in/"):
        return failure("Price list documents must use an approved Zoho WorkDrive URL", status=422)
    row = store.upsert_one("price_list_definitions", {"_id": price_list_id}, {**existing, **changes})
    audit("price_list.update", "price_list", price_list_id, {"fields": sorted(changes)})
    return success(row, "Price list updated")


@bp.post("/<price_list_id>/send")
@permission_required("price_list.send")
def send_price_list(price_list_id: str):
    store = current_app.extensions["store"]
    definition = price_list_by_id(store, price_list_id)
    if not definition or not definition.get("active", True):
        return failure("Price list not found", status=404)
    raw = request.get_json(silent=True) or {}
    customer_id = str(raw.get("customer_id") or "").strip()
    if not customer_id or not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    customer = store.find_one("customers", {"_id": customer_id})
    if not customer:
        return failure("Customer not found", status=404)
    pdf_path = _official_pdf_path(definition)
    # Never send a link while silently omitting the requested official file.
    # The embed URL remains useful for preview, but delivery requires the
    # exact server-side PDF asset.
    if pdf_path is None:
        return failure("The official PDF is not configured for this price list", status=409, error="PRICE_LIST_PDF_MISSING")
    try:
        to = _emails(raw.get("to"))
        cc = _emails(raw.get("cc"))
        bcc = _emails(raw.get("bcc"))
    except ValueError as exc:
        return failure(str(exc), status=422)
    if not to:
        return failure("At least one recipient is required", status=422)
    email_service = current_app.extensions["email_service"]
    to, cc, bcc = _merge_recipients(to, cc, bcc, email_service.recipients.resolved_for_price_list(price_list_id))
    subject = str(raw.get("subject") or f"{definition.get('display_name')} — Moneda Technologies").strip()[:300]
    message = str(raw.get("message") or "Please use the secure link below to view the current Moneda price list.").strip()[:5000]
    url = str(definition.get("document_url") or "")
    sender_user = current_user() or {}
    sender_name = str(sender_user.get("name") or "Moneda Technologies").strip()
    sender_email = str(sender_user.get("email") or "").strip()
    try:
        reply_to = validate_email(sender_email, check_deliverability=False).normalized if sender_email else None
    except EmailNotValidError:
        reply_to = None
    signature_markup = ""
    from app.account.signature import read_signature
    signature = read_signature(sender_user, current_app.config["UPLOAD_DIRECTORY"])
    if signature:
        signature_info, signature_bytes = signature
        encoded = base64.b64encode(signature_bytes).decode("ascii")
        signature_markup = (
            f"<p style='margin-top:20px'><img alt='Email signature' "
            f"style='max-width:320px;max-height:160px;object-fit:contain' "
            f"src='data:{escape(signature_info['mime_type'], quote=True)};base64,{encoded}'></p>"
        )
    diagnostic_id = f"price-list-{uuid4()}"
    html = (
        "<div style='font-family:Arial,sans-serif;color:#171717;line-height:1.55'>"
        f"<p>{escape(message)}</p><p><a href='{escape(url, quote=True)}'>"
        f"View {escape(str(definition.get('display_name') or 'price list'))}</a></p>"
        f"<p>Regards,<br>{escape(sender_name)}"
        f"{f' &lt;{escape(sender_email)}&gt;' if sender_email else ''}</p>"
        f"{signature_markup}</div>"
    )
    attachments: list[dict[str, str]] = [{
        "filename": str(definition.get("pdf_filename") or pdf_path.name),
        "content": base64.b64encode(pdf_path.read_bytes()).decode("ascii"),
    }]
    try:
        delivery = email_service.send(
            purpose="general", to=to, cc=cc, bcc=bcc, subject=subject, html=html,
            attachments=attachments,
            request_id=diagnostic_id, customer_facing=True, reply_to=reply_to,
        )
        result = str(delivery.get("status") or "sent")
    except Exception as exc:
        current_app.logger.exception("[%s] price_list_delivery result=FAIL", diagnostic_id)
        store.insert_one("price_list_email_logs", {
            "price_list_id": price_list_id, "customer_id": customer_id, "sent_by": (current_user() or {}).get("_id"),
            "to": to, "cc": cc, "bcc": bcc, "reply_to": reply_to, "from": sender_email,
            "attachment_names": [item["filename"] for item in attachments], "attachment_count": len(attachments), "result": "failed",
            "diagnostic_id": diagnostic_id, "created_at": utcnow(),
        })
        return failure("Price list email could not be sent", status=502, error="PRICE_LIST_EMAIL_FAILED", diagnostic_id=diagnostic_id)
    store.insert_one("price_list_email_logs", {
        "price_list_id": price_list_id, "customer_id": customer_id, "sent_by": (current_user() or {}).get("_id"),
        "to": to, "cc": cc, "bcc": bcc, "reply_to": reply_to, "from": sender_email,
        "attachment_names": [item["filename"] for item in attachments], "to_count": len(to), "cc_count": len(cc), "bcc_count": len(bcc), "attachment_count": len(attachments), "result": result,
        "provider_id": delivery.get("id"), "diagnostic_id": diagnostic_id, "created_at": utcnow(),
    })
    audit("price_list.send", "price_list", price_list_id, {
        "customer_id": customer_id, "to": to, "cc": cc, "bcc": bcc, "reply_to": reply_to,
        "from": sender_email, "attachment_names": [item["filename"] for item in attachments],
        "to_count": len(to), "cc_count": len(cc), "bcc_count": len(bcc), "attachment_count": len(attachments),
        "result": result, "provider_id": delivery.get("id"), "diagnostic_id": diagnostic_id,
    })
    return success({"sent": True, "status": result, "diagnostic_id": diagnostic_id,
                    "attachments": [item["filename"] for item in attachments],
                    "cc": cc, "bcc": bcc}, "Price list email sent")
