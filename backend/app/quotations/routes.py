from __future__ import annotations

import base64
import re
from datetime import datetime, timezone

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.middleware.access import (
    can_view_all_quotations, current_user, customer_id_from, customer_record, enforce_active_customer,
    enforce_customer, enforce_active_customer_company, permitted_quotation_query, selected_customer_id,
    permission_required,
)
from app.pricing.engine import PricingUnavailable
from app.quotations.pdf import render_quotation_pdf
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("quotations", __name__, url_prefix="/api/quotations")


def _accessible(quotation: dict) -> bool:
    user = current_user() or {}
    if can_view_all_quotations(user):
        return True
    owner_ids = {quotation.get("created_by_user_id"), quotation.get("user_id"), quotation.get("prepared_by_user_id"), quotation.get("salesperson_id")}
    if user.get("_id") in owner_ids:
        return True
    # Legacy quotations may not have an owner snapshot; retain the existing
    # customer authorization bridge for those records.
    if not any(owner_ids):
        return enforce_customer(quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id"))
    return False


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
    user = current_user() or {}
    clauses: list[dict] = []
    authorized = permitted_quotation_query(user)
    if authorized:
        clauses.append(authorized)

    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if customer_id:
        customer_id = str(customer_id).strip()
        if not enforce_customer(customer_id):
            return failure("Customer access denied", status=403)
        clauses.append({"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]})

    status = str(request.args.get("status") or "").strip()
    if status:
        clauses.append({"status": status})
    currency = str(request.args.get("currency") or "").strip().upper()
    if currency:
        clauses.append({"currency": currency})

    search = str(request.args.get("search") or "").strip()
    if search:
        clauses.append({"$or": [
            {"quotation_number": {"$regex": re.escape(search)}},
            {"customer_snapshot.company_name": {"$regex": re.escape(search)}},
            {"customer_snapshot.name": {"$regex": re.escape(search)}},
            {"customer_snapshot.email": {"$regex": re.escape(search)}},
            {"customer_snapshot.contact_name": {"$regex": re.escape(search)}},
        ]})

    region = str(request.args.get("region") or "").strip()
    if region:
        clauses.append({"$or": [{"customer_snapshot.continent": region}, {"customer_snapshot.region.continent": region}]})
    country = str(request.args.get("country") or "").strip()
    if country:
        clauses.append({"$or": [
            {"customer_snapshot.country_code": country.upper()},
            {"customer_snapshot.country_name": country},
            {"customer_snapshot.country": country},
            {"customer_snapshot.region.country_code": country.upper()},
            {"customer_snapshot.region.country_name": country},
        ]})

    min_total = request.args.get("min_total")
    max_total = request.args.get("max_total")
    if (min_total or max_total) and not currency:
        return failure("Select a currency before filtering quotation totals", status=422, error="CURRENCY_REQUIRED_FOR_TOTAL_FILTER")
    try:
        if min_total:
            clauses.append({"totals.grand_total": {"$gte": float(min_total)}})
        if max_total:
            clauses.append({"totals.grand_total": {"$lte": float(max_total)}})
    except (TypeError, ValueError):
        return failure("Minimum and maximum totals must be valid numbers", status=422)

    def date_boundary(value: str, end: bool = False) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999) if end else parsed.replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        if request.args.get("from_date"):
            clauses.append({"created_at": {"$gte": date_boundary(request.args["from_date"])}})
        if request.args.get("to_date"):
            clauses.append({"created_at": {"$lte": date_boundary(request.args["to_date"], end=True)}})
    except ValueError:
        return failure("From and to dates must be valid ISO dates", status=422)

    query: dict = {"$and": clauses} if clauses else {}
    try:
        page = max(int(request.args.get("page", 1)), 1)
        limit = min(max(int(request.args.get("limit", 25)), 1), 100)
    except ValueError:
        return failure("Page and limit must be valid numbers", status=422)
    rows, total = current_app.extensions["store"].list("quotations", query, page=page, limit=limit, sort="created_at", direction=-1)
    return success({"items": rows, "pagination": {"page": page, "limit": limit, "total": total}, "scope": "all" if can_view_all_quotations(user) else "own"})


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
    try:
        document["preview_pdf_base64"] = base64.b64encode(render_quotation_pdf(document)).decode("ascii")
    except Exception:
        current_app.logger.exception("quotation preview PDF rendering failed")
        return failure("Quotation preview PDF could not be generated", status=503, error="PDF_GENERATION_FAILED")
    return success(document, "Quotation preview calculated")


@bp.get("/<quotation_id>")
@permission_required("quotations.view")
def get_quotation(quotation_id: str):
    row = current_app.extensions["store"].find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    return success(row) if _accessible(row) else failure("Quotation access denied", status=403)


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
    allowed = {"payment_terms", "transport", "notes", "customer_notes", "terms", "expiry_date", "validity_days", "proforma_validity_days"}
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
        current_app.logger.error("quotation_pdf_generation quotation_id=%s renderer=unavailable format=pdf result=FAIL", quotation_id)
        return failure(str(exc), status=503, error="PDF_GENERATION_FAILED")
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
    current_app.logger.info(
        "quotation_send_email quotation_id=%s provider=zoho_mail_api result=START",
        quotation_id,
    )
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    previous_email = store.find_one("email_logs", {"quotation_id": quotation_id, "purpose": "quotation"})
    send_event = "resend" if previous_email or row.get("status") in {"Sent", "send_failed"} else "initial_send"
    recipient = str(row.get("customer_snapshot", {}).get("email") or "").strip()
    try:
        recipient = validate_email(recipient, check_deliverability=False).normalized
    except EmailNotValidError:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    payload = request.get_json(silent=True) or {}
    subject = str(payload.get("subject") or f"Quotation {row['quotation_number']} - Moneda Technologies")[:200]
    message = str(payload.get("message") or f"<p>Please find quotation <strong>{row['quotation_number']}</strong> attached.</p><p>Total: {row['currency']} {row['totals']['grand_total']:,.2f}</p>")
    user = current_user() or {}
    sent_by = user.get("_id")
    sender_email = str(user.get("email") or "").strip()
    try:
        sender_email = validate_email(sender_email, check_deliverability=False).normalized
    except EmailNotValidError:
        return failure("A valid authenticated user email is required before sending", status=422, error="SENDER_USER_EMAIL_REQUIRED")
    email_service = current_app.extensions["email_service"]
    routing = email_service.recipients.resolved_for_quotation(to=[recipient], sender_user_email=sender_email)
    request_id = f"quotation-{quotation_id}-{send_event}-{email_diagnostic_id()}"

    def mark_failed(*, error_code: str, diagnostic_id: str, stage: str) -> None:
        history_status = "resend_failed" if send_event == "resend" else "send_failed"
        failed_history = [*row.get("history", []), {"status": history_status, "event": send_event, "at": utcnow(), "by": sent_by, "error_code": error_code, "diagnostic_id": diagnostic_id}]
        quotation_status = "Sent" if row.get("status") == "Sent" else "send_failed"
        store.update_one("quotations", {"_id": quotation_id}, {"status": quotation_status, "email_status": history_status, "email_error_code": error_code, "email_diagnostic_id": diagnostic_id, "history": failed_history})
        store.insert_one("email_logs", {
            "quotation_id": quotation_id, "quotation_number": row.get("quotation_number"), "event": send_event,
            "recipient": recipient, "cc": routing["cc"], "bcc": routing["bcc"], "subject": subject,
            "message_type": "quotation", "purpose": "quotation", "from_address": email_service.senders.resolve("quotation"),
            "to_count": 1, "cc_count": len(routing["cc"]), "bcc_count": len(routing["bcc"]), "sent_by": sent_by,
            "status": "failed", "submission_status": "failed", "error_code": error_code, "diagnostic_id": diagnostic_id,
            "stage": stage, "channel": "email", "created_at": utcnow(),
        })

    try:
        try:
            pdf = render_quotation_pdf(row)
        except Exception:
            diagnostic_id = email_diagnostic_id()
            current_app.logger.exception("quotation email failed diagnostic_id=%s stage=pdf_generation", diagnostic_id)
            mark_failed(error_code="PDF_GENERATION_FAILED", diagnostic_id=diagnostic_id, stage="pdf_generation")
            return failure("Quotation PDF could not be generated.", status=503, error="PDF_GENERATION_FAILED", diagnostic_id=diagnostic_id, stage="pdf_generation")
        result = current_app.extensions["email_service"].send_quotation(
            to=[recipient], subject=subject,
            html=f"<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>{message}</div>",
            attachments=[{"filename": f"{row['quotation_number']}.pdf", "content": base64.b64encode(pdf).decode("ascii")}],
            sender_user_email=sender_email,
            request_id=request_id,
        )
    except EmailDeliveryError as exc:
        current_app.logger.info(
            "quotation_send_email quotation_id=%s provider=zoho_mail_api result=FAIL error_code=%s stage=%s diagnostic_id=%s",
            quotation_id, exc.error_code, exc.stage, exc.diagnostic_id,
        )
        mark_failed(error_code=exc.error_code, diagnostic_id=exc.diagnostic_id, stage=exc.stage)
        message = (
            "Email service is not connected. Please contact the administrator."
            if exc.error_code == "OAUTH_NOT_CONNECTED" else "Quotation email could not be delivered."
        )
        if exc.error_code == "QUOTATION_SENDER_ALIAS_UNAVAILABLE":
            message = "Quotation email is not ready because its Zoho sender alias is unavailable."
        status = 429 if exc.error_code == "ZOHO_MAIL_API_RATE_LIMIT" else 422 if exc.error_code == "SENDER_INVALID" else 503
        return failure(message, status=status, error=exc.error_code, diagnostic_id=exc.diagnostic_id, stage=exc.stage)
    except Exception as exc:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("quotation email failed diagnostic_id=%s stage=email_service", diagnostic_id)
        mark_failed(error_code="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
        return failure("Quotation email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    history_event = "Resent" if send_event == "resend" else "Sent"
    history = [*row.get("history", []), {"status": history_event, "event": send_event, "at": utcnow(), "by": user.get("_id")}]
    current_app.logger.info(
        "quotation_send_email quotation_id=%s provider=zoho_mail_api result=PASS",
        quotation_id,
    )
    updated = store.update_one("quotations", {"_id": quotation_id}, {"status": "Sent", "email_status": "sent", "history": history})
    store.insert_one("email_logs", {"quotation_id": quotation_id, "quotation_number": row.get("quotation_number"), "event": send_event, "recipient": recipient, "cc": routing["cc"], "bcc": routing["bcc"], "subject": subject, "message_type": "quotation", "purpose": "quotation", "from_address": email_service.senders.resolve("quotation"), "to_count": 1, "cc_count": len(routing["cc"]), "bcc_count": len(routing["bcc"]), "sent_by": sent_by, "status": "sent", "submission_status": "sent", "provider_id": result.get("id"), "diagnostic_id": result.get("diagnostic_id"), "stage": result.get("stage", "message_submission"), "channel": "email", "created_at": utcnow()})
    store.insert_one("notifications", {
        "user_id": user.get("_id"), "customer_id": row.get("customer_id") or row.get("customer_company_id") or row.get("company_id"), "type": "quotation_sent",
        "title": f"Quotation {row['quotation_number']} {'sent again' if send_event == 'resend' else 'sent'}", "quotation_id": quotation_id, "read": False,
    })
    audit("quotation.send", "quotation", quotation_id, {"number": row["quotation_number"]})
    return success(updated, "Quotation sent again successfully" if send_event == "resend" else "Quotation sent")


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
