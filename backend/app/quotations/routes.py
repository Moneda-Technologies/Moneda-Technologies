from __future__ import annotations

import base64
from copy import deepcopy
import re
from datetime import datetime, timezone
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.middleware.access import (
    can_view_all_quotations, current_user, customer_id_from, customer_record, enforce_active_customer,
    enforce_customer, enforce_active_customer_company, permitted_quotation_query, quotation_is_authorized, selected_customer_id,
    permission_required, permission_required_any,
)
from app.pricing.engine import PricingUnavailable
from app.quotations.pdf import render_quotation_pdf
from app.quotations.integrity import EDITABLE_STATUSES, has_historical_reference, live_working_order, repair_stale_conversions, quotation_status
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("quotations", __name__, url_prefix="/api/quotations")


def _accessible(quotation: dict) -> bool:
    return quotation_is_authorized(quotation, current_user() or {})


def _edit_signature(value: dict) -> tuple:
    return (
        str(value.get("product_id") or ""), repr(sorted((value.get("configuration") or {}).items())),
        int(value.get("requested_quantity", value.get("quantity", 1)) or 1),
        float(value.get("requested_discount_percent", value.get("discount_percent", 0)) or 0),
    )


def _replace_quotation_items(store, quotation: dict, submitted: object, submitted_transport: object = None) -> dict:
    from app.pricing.engine import calculate_quote_totals
    from app.pricing.routes import _calculate

    if not isinstance(submitted, list) or not submitted:
        raise ValueError("A quotation must contain at least one item")
    customer_id = str(quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id") or "")
    existing_lines = quotation.get("lines") or []
    existing = {str(line.get("item_id") or line.get("line_id") or index): line for index, line in enumerate(existing_lines)}
    lines = []
    for index, raw in enumerate(submitted):
        if not isinstance(raw, dict):
            raise ValueError("Each quotation item must be an object")
        item_id = str(raw.get("item_id") or raw.get("line_id") or uuid4().hex)
        previous = existing.get(item_id)
        if previous and _edit_signature(previous) == _edit_signature(raw):
            line = deepcopy(previous)
        else:
            _customer, _product, priced, rate_meta = _calculate(raw, customer_id_override=customer_id, require_active_context=False)
            line = {**priced, "exchange_rate_meta": rate_meta}
        line.update({"item_id": item_id, "line_id": item_id})
        lines.append(line)
    transport = submitted_transport if isinstance(submitted_transport, dict) else (quotation.get("transport") or {})
    transport_cost = float(transport.get("charges") or (quotation.get("totals") or {}).get("transport_cost") or 0)
    totals = calculate_quote_totals(lines, transport_cost)
    now = utcnow()
    version = int(quotation.get("version") or 1)
    store.insert_one("quotation_versions", {
        "quotation_id": quotation.get("_id"), "quotation_number": quotation.get("quotation_number"),
        "version": version, "lines": deepcopy(existing_lines), "totals": deepcopy(quotation.get("totals") or {}),
        "created_at": now, "created_by": (current_user() or {}).get("_id"),
    })
    return store.update_one("quotations", {"_id": quotation.get("_id")}, {
        "lines": lines, "totals": totals, "eur_totals": totals, "version": version + 1,
        "history": [*(quotation.get("history") or []), {"status": quotation.get("status"), "event": "ITEMS_UPDATED", "at": now, "by": (current_user() or {}).get("_id")}],
    }) or {**quotation, "lines": lines, "totals": totals, "version": version + 1}


def _valid_email(value: object) -> str | None:
    """Return a normalized email, treating legacy placeholders as missing."""
    candidate = str(value or "").strip()
    if not candidate or candidate.lower() in {"-", "—", "none", "null"}:
        return None
    try:
        return validate_email(candidate, check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def _stored_quotation_recipient(row: dict) -> str | None:
    values = row.get("to")
    if isinstance(values, (list, tuple)):
        values = values[0] if values else None
    return _valid_email(values or row.get("recipient"))


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
    integrity = repair_stale_conversions(current_app.extensions["store"])
    if integrity["repaired"]:
        current_app.logger.info(
            "quotation_integrity_repair_on_list scanned=%s repaired=%s",
            integrity["scanned"], integrity["repaired"],
        )
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
    role_id = str(user.get("role_id") or "")
    scope_label = "all" if can_view_all_quotations(user) else "team" if role_id in {"manager", "manager_sales_admin"} else "own"
    return success({"items": rows, "pagination": {"page": page, "limit": limit, "total": total}, "scope": scope_label})


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
    store = current_app.extensions["store"]
    integrity = repair_stale_conversions(store)
    if integrity["repaired"]:
        current_app.logger.info("quotation_integrity_repair_on_get quotation_id=%s repaired=%s", quotation_id, integrity["repaired"])
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    return success(row) if _accessible(row) else failure("Quotation access denied", status=403)


@bp.patch("/<quotation_id>")
@permission_required("quotations.edit")
def update_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    repair_stale_conversions(store)
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Customer company access denied", status=403)
    if has_historical_reference(row):
        return failure("Historical Order Confirmations are immutable", status=409, error="QUOTATION_LOCKED")
    if live_working_order(store, row):
        return failure("This quotation is locked while its Working Order is active", status=409, error="QUOTATION_CONVERTED")
    if quotation_status(row) not in EDITABLE_STATUSES:
        return failure("This quotation is not in an editable state", status=409, error="QUOTATION_NOT_EDITABLE")
    payload = request.get_json(silent=True) or {}
    items_changed = "items" in payload
    if items_changed:
        try:
            row = _replace_quotation_items(store, row, payload.get("items"), payload.get("transport"))
        except PermissionError as exc:
            return failure(str(exc), status=403)
        except LookupError as exc:
            return failure(str(exc), status=404)
        except PricingUnavailable as exc:
            return failure(str(exc), status=409)
        except (ValueError, TypeError) as exc:
            return failure(str(exc), status=422)
        payload = {key: value for key, value in payload.items() if key != "items"}
    if {"tax_rate", "tax_mode", "lines", "totals", "exchange_rate"}.intersection(payload):
        return failure("Money and tax fields cannot be changed directly", status=422)
    allowed = {"payment_terms", "transport", "notes", "customer_notes", "terms", "expiry_date", "validity_days", "proforma_validity_days"}
    changes = {key: value for key, value in payload.items() if key in allowed}
    if not changes and not items_changed:
        return failure("No editable quotation fields were supplied", status=422, error="NO_QUOTATION_CHANGES")
    updated = store.update_one("quotations", {"_id": quotation_id}, changes) if changes else row
    audit("quotation.update", "quotation", quotation_id, {"fields": sorted(changes)})
    return success(updated, "Quotation updated")


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
    payload = request.get_json(silent=True) or {}
    customer_recipient = _valid_email((row.get("customer_snapshot") or {}).get("email"))
    requested_recipient = _valid_email(payload.get("to"))
    # A saved customer email is authoritative. A one-off recipient is accepted
    # only when the customer has no email, allowing quotation delivery without
    # changing the customer record or weakening customer access checks.
    recipient = customer_recipient or requested_recipient or _stored_quotation_recipient(row)
    if not recipient:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    subject = str(payload.get("subject") or f"Quotation {row['quotation_number']} - Moneda Technologies")[:200]
    message = str(payload.get("message") or f"<p>Please find quotation <strong>{row['quotation_number']}</strong> attached.</p><p>Total: {row.get('quotation_currency') or row.get('currency') or 'EUR'} {row['totals']['grand_total']:,.2f}</p>")
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
    # Persist the exact recipient snapshot used for this quotation send so a
    # later Order Confirmation conversion can inherit it server-side.
    updated = store.update_one("quotations", {"_id": quotation_id}, {
        "status": "Sent", "email_status": "sent", "history": history,
        "to": [recipient], "cc": routing["cc"], "bcc": routing["bcc"],
    })
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


@bp.delete("/<quotation_id>")
@permission_required_any("quotations.archive", "quotations.delete")
def delete_or_archive_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Quotation access denied", status=403)
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or request.args.get("reason") or "").strip()[:500]
    status = str(row.get("status") or "Draft").strip().lower()
    actor = current_user() or {}

    # Active quotations always take the reversible archive path. The client
    # may send `permanent=true`, but it must never bypass this first step.
    if status != "archived":
        if "quotations.archive" not in set(actor.get("permissions") or []):
            return failure("You do not have permission to archive quotations", status=403, error="QUOTATION_ARCHIVE_FORBIDDEN")
        previous = str(row.get("status") or "Draft")
        store.update_one("quotations", {"_id": quotation_id}, {"status": "archived", "archived_at": utcnow(), "archived_by": actor.get("_id")})
        audit("QUOTATION_ARCHIVED", "quotation", quotation_id, {"reason": reason, "previous_state": previous, "new_state": "archived"})
        return success(message="Quotation archived")

    # Permanent deletion is intentionally narrower than archival: only the
    # canonical Superadmin role may remove an already archived quotation.
    if str(actor.get("role_id") or "") != "superadmin" or "quotations.delete" not in set(actor.get("permissions") or []):
        return failure("Only a Superadmin can permanently delete an archived quotation", status=403, error="QUOTATION_DELETE_SUPERADMIN_ONLY")
    related, _ = store.list("orders", {"$or": [{"quotation_id": quotation_id}, {"source_quotation_id": quotation_id}]}, limit=1)
    if related:
        return failure("This quotation has an associated order and cannot be deleted", status=409, error="QUOTATION_HAS_ORDER")
    if str(payload.get("permanent", request.args.get("permanent", "true"))).lower() in {"1", "true", "yes"}:
        if not reason:
            return failure("A reason is required to permanently delete a quotation", status=422, error="REASON_REQUIRED")
        audit("QUOTATION_DELETED", "quotation", quotation_id, {"reason": reason, "previous_state": row.get("status") or "archived"})
        store.delete_one("quotations", {"_id": quotation_id})
        return success(message="Quotation deleted")
    return failure("An archived quotation can only be permanently deleted", status=409, error="QUOTATION_ARCHIVED_DELETE_REQUIRED")


@bp.post("/<quotation_id>/restore")
@permission_required_any("quotations.restore", "quotations.edit")
def restore_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    row = store.find_one("quotations", {"_id": quotation_id})
    if not row:
        return failure("Quotation not found", status=404)
    if not _accessible(row):
        return failure("Quotation access denied", status=403)
    if str(row.get("status") or "").strip().lower() != "archived":
        return success(row, "Quotation is not archived")
    restored = store.update_one("quotations", {"_id": quotation_id}, {"status": "Draft", "archived_at": None, "archived_by": None}) or row
    audit("QUOTATION_RESTORED", "quotation", quotation_id, {"previous_state": "archived", "new_state": "Draft"})
    return success(restored, "Quotation restored")


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
