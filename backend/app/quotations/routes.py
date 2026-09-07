from __future__ import annotations

import base64

from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.middleware.access import (
    current_user, customer_id_from, customer_record, enforce_active_customer,
    enforce_customer, enforce_active_customer_company, selected_customer_id,
    permission_required,
)
from app.pricing.engine import PricingUnavailable
from app.quotations.pdf import render_quotation_pdf
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("quotations", __name__, url_prefix="/api/quotations")


def _accessible(quotation: dict) -> bool:
    return enforce_customer(quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id"))


def _scope_id(payload: dict) -> str | None:
    """Resolve the active customer while accepting legacy company payloads."""
    canonical = customer_id_from(payload)
    legacy = payload.get("customer_company_id") or payload.get("company_id")
    selected = selected_customer_id()
    # Legacy callers used company_id for the workspace and customer_id for a
    # contact.  If that scope is the active customer, preserve it; canonical
    # callers send customer_id alone (or the same value in both fields).
    if legacy and selected == str(legacy):
        return str(legacy)
    return str(canonical) if canonical else (str(legacy) if legacy else None)


@bp.get("")
@permission_required("quotations.view")
def list_quotations():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id") or selected_customer_id()
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    query: dict = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    if request.args.get("status"):
        query["status"] = request.args["status"]
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(max(int(request.args.get("limit", 25)), 1), 100)
    rows, total = current_app.extensions["store"].list("quotations", query, page=page, limit=limit)
    return success({"items": rows, "pagination": {"page": page, "limit": limit, "total": total}})


@bp.post("")
@permission_required("quotations.create")
def create_quotation():
    payload = request.get_json(silent=True) or {}
    customer_id = _scope_id(payload)
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    user = current_user() or {}
    idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()[:160]
    if idempotency_key:
        existing = current_app.extensions["store"].find_one("quotations", {"customer_id": customer_id, "user_id": user.get("_id"), "idempotency_key": idempotency_key})
        if existing:
            return success(existing, "Quotation already created")
        payload["idempotency_key"] = idempotency_key
    customer_company = customer_record(customer_id)
    try:
        row = current_app.extensions["quotation_service"].create(payload=payload, user=current_user() or {}, customer_company=customer_company)
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except LookupError as exc:
        return failure(str(exc), status=404)
    except (ValueError, TypeError) as exc:
        return failure(str(exc), status=422)
    audit("quotation.create", "quotation", str(row["_id"]), {"number": row["quotation_number"]})
    store = current_app.extensions["store"]
    if payload.get("create_lead"):
        lead = store.insert_one("leads", {
            "customer_id": customer_id, "customer_company_id": customer_id, "company_id": customer_id, "quotation_id": row["_id"],
            "assigned_to": user.get("_id"), "estimated_value": row["totals"]["grand_total"],
            "currency": row["currency"], "source": "quotation", "status": "Lead",
            "notes": str(payload.get("lead_notes", ""))[:2000], "follow_up_date": payload.get("follow_up_date"),
            "activity": [{"type": "quotation_created", "quotation_id": row["_id"], "at": utcnow()}],
        })
        audit("lead.create", "lead", str(lead["_id"]), {"quotation_id": row["_id"]})
        if payload.get("follow_up_date"):
            store.insert_one("reminders", {
                "customer_id": customer_id, "customer_company_id": customer_id, "company_id": customer_id, "lead_id": lead["_id"],
                "quotation_id": row["_id"], "assigned_to": user.get("_id"),
                "due_date": payload["follow_up_date"], "status": "Pending", "priority": "normal",
                "frequency": payload.get("reminder_frequency", "none"), "repeat_frequency": payload.get("reminder_frequency", "none"),
                "repeat_enabled": payload.get("reminder_frequency", "none") != "none", "notes": "Quotation follow-up",
            })
    cart_rows, _ = store.list("cart_items", {"user_id": user.get("_id"), "$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}, limit=250)
    for cart_row in cart_rows:
        store.delete_one("cart_items", {"_id": cart_row["_id"]})
    return success(row, "Quotation created", 201)


@bp.post("/preview")
@permission_required("quotations.create")
def preview_quotation():
    payload = request.get_json(silent=True) or {}
    customer_id = _scope_id(payload)
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    store = current_app.extensions["store"]
    customer_company = customer_record(customer_id)
    try:
        document = current_app.extensions["quotation_service"].preview(
            payload=payload, user=current_user() or {}, customer_company=customer_company,
        )
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except LookupError as exc:
        return failure(str(exc), status=404)
    except (ValueError, TypeError) as exc:
        return failure(str(exc), status=422)
    return success(document, "Quotation preview calculated")


@bp.get("/<quotation_id>")
@permission_required("quotations.view")
def get_quotation(quotation_id: str):
    row = current_app.extensions["store"].find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    return success(row) if _accessible(row) else failure("Customer company access denied", status=403)


@bp.patch("/<quotation_id>")
@permission_required("quotations.edit")
def update_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    if row.get("status") != "Draft":
        return failure("Sent quotations are immutable; create a revision instead", status=409)
    payload = request.get_json(silent=True) or {}
    if {"tax_rate", "tax_mode", "lines", "totals", "exchange_rate"}.intersection(payload):
        return failure("Money and tax fields cannot be changed directly", status=422)
    allowed = {"payment_terms", "transport", "notes", "terms", "expiry_date", "validity_days", "proforma_validity_days"}
    changes = {key: value for key, value in payload.items() if key in allowed}
    updated = store.update_one("quotations", {"_id": quotation_id}, changes)
    audit("quotation.update", "quotation", quotation_id, {"fields": sorted(changes)})
    return success(updated, "Draft updated")


@bp.get("/<quotation_id>/pdf")
@permission_required("quotations.download")
def quotation_pdf(quotation_id: str):
    row = current_app.extensions["store"].find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    try:
        content = render_quotation_pdf(row)
    except RuntimeError as exc:
        return failure(str(exc), status=503)
    disposition = "inline" if request.args.get("preview") == "true" else "attachment"
    current_app.extensions["store"].insert_one("communication_logs", {
        "quotation_id": quotation_id, "customer_id": row.get("customer_id") or row.get("customer_company_id") or row.get("company_id"), "channel": "pdf",
        "action": "preview" if disposition == "inline" else "download", "status": "completed",
        "recipient": None, "user_id": (current_user() or {}).get("_id"),
    })
    return Response(content, mimetype="application/pdf", headers={"Content-Disposition": f'{disposition}; filename="{row["quotation_number"]}.pdf"'})


@bp.post("/<quotation_id>/send")
@permission_required("quotations.send")
def send_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    if row.get("status") == "Sent":
        return failure("Quotation has already been sent; retry is available after a failed delivery", status=409)
    recipient = row.get("customer_snapshot", {}).get("email")
    if not recipient:
        return failure("Customer email is required before sending", status=422)
    payload = request.get_json(silent=True) or {}
    cc = [item.strip() for item in str(payload.get("cc", "")).split(",") if item.strip()]
    subject = str(payload.get("subject") or f"Quotation {row['quotation_number']} - Moneda Technologies")[:200]
    message = str(payload.get("message") or f"<p>Please find quotation <strong>{row['quotation_number']}</strong> attached.</p><p>Total: {row['currency']} {row['totals']['grand_total']:,.2f}</p>")
    sent_by = (current_user() or {}).get("_id")
    request_id = f"quotation-{quotation_id}"
    try:
        pdf = render_quotation_pdf(row)
        result = current_app.extensions["email_service"].send_quotation(
            to=[recipient], subject=subject,
            cc=cc, html=f"<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>{message}</div>",
            attachments=[{"filename": f"{row['quotation_number']}.pdf", "content": base64.b64encode(pdf).decode("ascii")}],
            request_id=request_id,
        )
    except EmailDeliveryError as exc:
        store.insert_one("email_logs", {
            "quotation_id": quotation_id, "recipient": recipient, "cc": cc, "subject": subject,
            "message_type": "quotation", "sent_by": sent_by, "status": "failed",
            "error_code": exc.error_code, "diagnostic_id": exc.diagnostic_id, "stage": exc.stage,
            "channel": "email", "created_at": utcnow(),
        })
        message = (
            "Email service is not connected. Please contact the administrator."
            if exc.error_code == "OAUTH_NOT_CONNECTED" else "Quotation email could not be delivered."
        )
        status = 429 if exc.error_code == "ZOHO_MAIL_API_RATE_LIMIT" else 422 if exc.error_code == "SENDER_INVALID" else 503
        return failure(message, status=status, error=exc.error_code, diagnostic_id=exc.diagnostic_id, stage=exc.stage)
    except Exception as exc:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("quotation email failed diagnostic_id=%s stage=email_service", diagnostic_id)
        store.insert_one("email_logs", {"quotation_id": quotation_id, "recipient": recipient, "cc": cc, "subject": subject, "message_type": "quotation", "sent_by": sent_by, "status": "failed", "error_code": "MESSAGE_SUBMISSION_FAILED", "diagnostic_id": diagnostic_id, "stage": "email_service", "channel": "email", "created_at": utcnow()})
        return failure("Quotation email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    user = current_user() or {}
    history = [*row.get("history", []), {"status": "Sent", "at": utcnow(), "by": user.get("_id")}]
    updated = store.update_one("quotations", {"_id": quotation_id}, {"status": "Sent", "history": history})
    store.insert_one("email_logs", {"quotation_id": quotation_id, "recipient": recipient, "cc": cc, "subject": subject, "message_type": "quotation", "sent_by": sent_by, "status": "sent", "provider_id": result.get("id"), "diagnostic_id": result.get("diagnostic_id"), "stage": result.get("stage", "message_submission"), "channel": "email", "created_at": utcnow()})
    store.insert_one("notifications", {
        "user_id": user.get("_id"), "customer_id": row.get("customer_id") or row.get("customer_company_id") or row.get("company_id"), "type": "quotation_sent",
        "title": f"Quotation {row['quotation_number']} sent", "quotation_id": quotation_id, "read": False,
    })
    audit("quotation.send", "quotation", quotation_id, {"number": row["quotation_number"]})
    return success(updated, "Quotation sent")


@bp.get("/<quotation_id>/communications")
@permission_required("quotations.view")
def quotation_communications(quotation_id: str):
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    logs = []
    for collection in ("communication_logs", "email_logs", "whatsapp_logs"):
        rows, _ = store.list(collection, {"quotation_id": quotation_id}, limit=100, sort="created_at", direction=-1)
        logs.extend(rows)
    logs.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
    return success({"items": logs, "total": len(logs)})


@bp.post("/<quotation_id>/whatsapp")
@permission_required("quotations.send")
def send_quotation_whatsapp(quotation_id: str):
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    phone = str(row.get("customer_snapshot", {}).get("phone", "")).strip()
    if not phone:
        return failure("Customer phone is required before sharing", status=422)
    pdf_url = f"{current_app.config['APP_BASE_URL']}/api/v1/quotations/{quotation_id}/pdf"
    result = current_app.extensions["whatsapp_provider"].send_quotation(
        phone=phone, quotation_number=row["quotation_number"], pdf_url=pdf_url,
    )
    log = store.insert_one("whatsapp_logs", {
        "quotation_id": quotation_id, "recipient": phone, "status": result.get("status", "unknown"),
        "provider_id": result.get("id"), "retry_state": "not_required" if result.get("status") == "mocked" else "pending",
    })
    audit("quotation.whatsapp", "quotation", quotation_id, {"delivery_state": result.get("status")})
    message = "WhatsApp share recorded in mock mode; no real message was delivered" if result.get("status") == "mocked" else "WhatsApp share queued"
    return success({"delivery": result, "log_id": log["_id"]}, message, 202)
