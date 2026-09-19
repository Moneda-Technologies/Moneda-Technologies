from __future__ import annotations

from html import escape
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, current_app, request

from app.api.responses import failure, success
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


@bp.get("")
@permission_required("price_list.view")
def get_price_lists():
    rows = list_price_lists(current_app.extensions["store"])
    category = str(request.args.get("category") or "").strip()
    search = str(request.args.get("search") or "").strip().casefold()
    if category:
        rows = [row for row in rows if str(row.get("category")) == category]
    if search:
        rows = [row for row in rows if search in f"{row.get('display_name', '')} {row.get('description', '')}".casefold()]
    return success({"items": rows, "total": len(rows)})


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
    try:
        to = _emails(raw.get("to"))
        cc = _emails(raw.get("cc"))
        bcc = _emails(raw.get("bcc"))
    except ValueError as exc:
        return failure(str(exc), status=422)
    if not to:
        return failure("At least one recipient is required", status=422)
    to_keys = {item.casefold() for item in to}
    cc = [item for item in cc if item.casefold() not in to_keys]
    used = to_keys | {item.casefold() for item in cc}
    bcc = [item for item in bcc if item.casefold() not in used]
    subject = str(raw.get("subject") or f"{definition.get('display_name')} — Moneda Technologies").strip()[:300]
    message = str(raw.get("message") or "Please use the secure link below to view the current Moneda price list.").strip()[:5000]
    url = str(definition.get("document_url") or "")
    diagnostic_id = f"price-list-{uuid4()}"
    html = (
        "<div style='font-family:Arial,sans-serif;color:#171717;line-height:1.55'>"
        f"<p>{escape(message)}</p><p><a href='{escape(url, quote=True)}'>"
        f"View {escape(str(definition.get('display_name') or 'price list'))}</a></p>"
        "<p>Regards,<br>Moneda Technologies</p></div>"
    )
    try:
        delivery = current_app.extensions["email_service"].send(
            purpose="general", to=to, cc=cc, bcc=bcc, subject=subject, html=html,
            request_id=diagnostic_id, customer_facing=True,
        )
        result = str(delivery.get("status") or "sent")
    except Exception as exc:
        current_app.logger.exception("[%s] price_list_delivery result=FAIL", diagnostic_id)
        store.insert_one("price_list_email_logs", {
            "price_list_id": price_list_id, "customer_id": customer_id, "sent_by": (current_user() or {}).get("_id"),
            "to_count": len(to), "cc_count": len(cc), "bcc_count": len(bcc), "result": "failed",
            "diagnostic_id": diagnostic_id, "created_at": utcnow(),
        })
        return failure("Price list email could not be sent", status=502, error="PRICE_LIST_EMAIL_FAILED", diagnostic_id=diagnostic_id)
    store.insert_one("price_list_email_logs", {
        "price_list_id": price_list_id, "customer_id": customer_id, "sent_by": (current_user() or {}).get("_id"),
        "to_count": len(to), "cc_count": len(cc), "bcc_count": len(bcc), "result": result,
        "diagnostic_id": diagnostic_id, "created_at": utcnow(),
    })
    audit("price_list.send", "price_list", price_list_id, {
        "customer_id": customer_id, "to_count": len(to), "cc_count": len(cc), "bcc_count": len(bcc),
        "result": result, "diagnostic_id": diagnostic_id,
    })
    return success({"sent": True, "status": result, "diagnostic_id": diagnostic_id}, "Price list email sent")

