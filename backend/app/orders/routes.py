from __future__ import annotations

import base64
from datetime import timedelta
from html import escape
import re
from threading import Lock

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.customers.codes import available_customer_code
from app.finance.service import IncentiveConfigurationError, create_incentive_for_order, validate_incentive_configuration
from app.middleware.access import can_view_all_customers, current_user, enforce_active_customer, enforce_customer, login_required, permission_required, permitted_customer_query
from app.repositories.store import utcnow
from app.services.audit import audit
from app.quotations.pdf import render_order_confirmation_pdf


bp = Blueprint("orders", __name__, url_prefix="/api")
STATUSES = {"Pending", "Confirmed", "Processing", "Completed", "Cancelled"}
_CONVERSION_LOCK = Lock()
PAYMENT_TERMS = ("Advance", "POD", "30 Days from receipt", "60 Days", "Custom")


def _normalise_recipients(values) -> list[str]:
    """Return valid, lower-cased, de-duplicated addresses from mixed input."""
    if values is None:
        return []
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    result: list[str] = []
    for raw in raw_values:
        if isinstance(raw, (list, tuple, set)):
            for nested in _normalise_recipients(raw):
                if nested not in result:
                    result.append(nested)
            continue
        for value in str(raw or "").replace(";", ",").split(","):
            value = value.strip()
            if not value:
                continue
            try:
                address = validate_email(value, check_deliverability=False).normalized
            except EmailNotValidError:
                continue
            if address not in result:
                result.append(address)
    return result


def _quotation_recipients(store, quotation: dict) -> tuple[list[str], list[str], list[str]]:
    """Load recipient snapshots from the quotation and its send record.

    Older quotations do not have recipient fields; their latest quotation
    email log is the authoritative snapshot for CC/BCC in that case.
    """
    quotation_id = quotation.get("_id")
    customer_email = (quotation.get("customer_snapshot") or {}).get("email")
    to = _normalise_recipients([quotation.get("to"), quotation.get("recipient"), customer_email])
    cc = _normalise_recipients(quotation.get("cc"))
    bcc = _normalise_recipients(quotation.get("bcc"))
    if quotation_id:
        logs, _ = store.list("email_logs", {"quotation_id": quotation_id, "purpose": "quotation"}, limit=100, sort="created_at", direction=-1)
        if logs:
            latest = logs[0]
            to = _normalise_recipients([*to, latest.get("recipient"), latest.get("to")])
            cc = _normalise_recipients([*cc, latest.get("cc")])
            bcc = _normalise_recipients([*bcc, latest.get("bcc")])
    # An address must have one delivery role only; To takes precedence.
    to_set = set(to)
    cc = [value for value in cc if value not in to_set]
    cc_set = set(cc)
    bcc = [value for value in bcc if value not in to_set and value not in cc_set]
    return to, cc, bcc


def _additional_recipients(payload: dict) -> list[str]:
    values = payload.get("additional_recipients", [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("Additional recipients must be an array")
    if len(values) > 5:
        raise ValueError("A maximum of 5 additional recipients is allowed")
    result: list[str] = []
    for value in values:
        address = _normalise_recipients(value)
        if not address:
            raise ValueError("Every additional recipient must be a valid email address")
        result.extend(address)
    return list(dict.fromkeys(result))[:5]


def _validated_payment_terms(value, fallback: str | None) -> str:
    terms = str(value or fallback or "Advance").strip()
    if terms in PAYMENT_TERMS:
        return terms
    if re.fullmatch(r"Custom:\s*\d+\s+Days", terms):
        return terms
    raise ValueError("Invalid payment terms")


def _order_recipient(order: dict) -> str:
    for snapshot_name in ("customer_snapshot", "customer_company_snapshot", "company_snapshot"):
        value = (order.get(snapshot_name) or {}).get("email")
        if value:
            try:
                return validate_email(str(value).strip(), check_deliverability=False).normalized
            except EmailNotValidError:
                continue
    return ""


def _order_recipients(order: dict) -> list[str]:
    values: list[str] = _normalise_recipients([
        order.get("to"), order.get("additional_recipients"), order.get("cc"), order.get("bcc"),
    ])
    for snapshot_name in ("customer_snapshot", "customer_company_snapshot", "company_snapshot"):
        value = (order.get(snapshot_name) or {}).get("email")
        if value:
            values.extend(_normalise_recipients(value))
    sales_email = (order.get("salesperson_snapshot") or {}).get("email")
    if sales_email:
        values.extend(_normalise_recipients(sales_email))
    return list(dict.fromkeys(values))


def _order_to_recipients(order: dict) -> list[str]:
    explicit = _normalise_recipients(order.get("to"))
    if not explicit:
        explicit = _normalise_recipients([
            (order.get(snapshot_name) or {}).get("email")
            for snapshot_name in ("customer_snapshot", "customer_company_snapshot", "company_snapshot")
        ])
    explicit.extend(_normalise_recipients(order.get("additional_recipients")))
    # The assigned Sales Person / Manager is always a direct recipient of the
    # OC, alongside the customer.  CC/BCC remain independently configurable.
    explicit.extend(_normalise_recipients((order.get("salesperson_snapshot") or {}).get("email")))
    return list(dict.fromkeys(explicit))


def _can_convert_quotation(user: dict, quotation: dict) -> bool:
    """Authorize conversion without broadening a user's order permissions.

    Users with the existing orders.create permission keep the normal managed
    conversion path.  A user without that global permission may convert only
    a quotation they own, and only when the customer is in their server-side
    customer scope.
    """
    customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    if not enforce_customer(customer_id):
        return False
    if "orders.create" in user.get("permissions", []):
        return True
    user_id = str(user.get("_id") or "")
    owner_ids = {
        str(quotation.get(field) or "")
        for field in ("created_by_user_id", "user_id", "prepared_by_user_id", "salesperson_id")
        if quotation.get(field)
    }
    return bool(user_id and user_id in owner_ids)


def _customer_oc_code(store, customer_id: str) -> str:
    customer = store.find_one("customers", {"_id": customer_id}) or store.find_one("companies", {"_id": customer_id}) or {}
    code = str(customer.get("customer_code") or "").strip().upper()
    if not code:
        code = available_customer_code(store, str(customer.get("name") or customer.get("company_name") or "Customer"), customer_id)
        if store.find_one("customers", {"_id": customer_id}):
            store.update_one("customers", {"_id": customer_id}, {"customer_code": code})
    return re.sub(r"[^A-Z0-9-]", "-", code).strip("-") or "CUSTOMER"


def _oc_sequence_state(store, code: str) -> tuple[str, int]:
    pattern = rf"^MT-OC-{re.escape(code)}-(\d+)$"
    rows, _ = store.list("orders", {"order_number": {"$regex": pattern}}, limit=100_000)
    highest_existing = max((int(match.group(1)) for row in rows if (match := re.match(pattern, str(row.get("order_number") or "")))), default=0)
    counter_name = f"order_oc:{code}"
    counter = store.find_one("quotation_counters", {"_id": counter_name}) or {}
    return counter_name, max(highest_existing, int(counter.get("sequence") or 0))


def _next_oc_number(store, customer_id: str) -> str:
    code = _customer_oc_code(store, customer_id)
    counter_name, current = _oc_sequence_state(store, code)
    store.ensure_counter_at_least(counter_name, current)
    return f"MT-OC-{code}-{store.next_counter(counter_name):03d}"


def _preview_oc_number(store, customer_id: str) -> str:
    code = _customer_oc_code(store, customer_id)
    _, current = _oc_sequence_state(store, code)
    return f"MT-OC-{code}-{current + 1:03d}"


def _order_email_failure(exc: EmailDeliveryError):
    message = (
        "Email service is not connected. Please contact the administrator."
        if exc.error_code == "OAUTH_NOT_CONNECTED" else "Order email could not be delivered."
    )
    if exc.error_code == "ORDER_SENDER_ALIAS_UNAVAILABLE":
        message = "Order email is not ready because its Zoho sender alias is unavailable."
    status = 429 if exc.error_code == "ZOHO_MAIL_API_RATE_LIMIT" else 422 if exc.error_code == "SENDER_INVALID" else 503
    return failure(
        message, status=status, error=exc.error_code,
        diagnostic_id=exc.diagnostic_id, stage=exc.stage,
    )


def _order_pdf_id(order_id: str) -> str:
    return f"order-confirmation-pdf-{order_id}"


def _ensure_order_pdf(order: dict) -> tuple[bytes, dict]:
    """Generate and persist the canonical OC PDF, reusing an existing copy."""
    store = current_app.extensions["store"]
    order_id = str(order.get("_id") or "")
    document_id = str(order.get("oc_pdf_document_id") or _order_pdf_id(order_id))
    existing = store.find_one("order_documents", {"_id": document_id})
    if existing and existing.get("content_base64"):
        try:
            content = base64.b64decode(str(existing["content_base64"]))
            if order.get("oc_pdf_status") != "Generated" or order.get("oc_pdf_document_id") != document_id:
                order = store.update_one("orders", {"_id": order_id}, {
                    "oc_pdf_document_id": document_id, "oc_pdf_filename": existing.get("filename") or f"{order.get('order_number') or order_id}.pdf",
                    "oc_pdf_status": "Generated", "document_status": "Generated",
                }) or order
            return content, order
        except (ValueError, TypeError):
            pass
    content = render_order_confirmation_pdf(order)
    now = utcnow()
    filename = f"{order.get('order_number') or order_id}.pdf"
    document = {
        "_id": document_id, "order_id": order_id, "document_type": "order_confirmation",
        "filename": filename, "content_type": "application/pdf",
        "content_base64": base64.b64encode(content).decode("ascii"), "created_at": now,
        "updated_at": now,
    }
    if existing:
        document = store.update_one("order_documents", {"_id": document_id}, document) or document
    else:
        document = store.insert_one("order_documents", document)
    updated = store.update_one("orders", {"_id": order_id}, {
        "oc_pdf_document_id": document_id, "oc_pdf_filename": filename,
        "oc_pdf_status": "Generated", "document_status": "Generated", "oc_pdf_generated_at": now,
    }) or order
    audit("order.pdf_generated", "order", order_id, {"document_id": document_id})
    return content, updated


def _send_order_email(order: dict, message_type: str, attachment: bytes | None = None) -> dict:
    recipients = _order_to_recipients(order)
    if not recipients:
        raise ValueError("CUSTOMER_EMAIL_REQUIRED")
    cc = _normalise_recipients(order.get("cc"))
    bcc = _normalise_recipients(order.get("bcc"))
    number = escape(str(order.get("order_number") or order.get("_id") or "order"))
    status = escape(str(order.get("status") or "Pending"))
    if message_type == "order_confirmation":
        subject = f"Order confirmation {order.get('order_number')} - Moneda Technologies"
        html = (
            "<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
            f"<p>Your Order Confirmation <strong>{number}</strong> has been received.</p>"
            f"<p>Current status: <strong>{status}</strong>.</p></div>"
        )
        method = current_app.extensions["email_service"].send_order_confirmation
    else:
        subject = f"Order status {order.get('order_number')} - {order.get('status')}"
        html = (
            "<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
            f"<p>Order <strong>{number}</strong> is now <strong>{status}</strong>.</p></div>"
        )
        method = current_app.extensions["email_service"].send_order_status
    result = method(
        to=recipients, subject=subject, html=html,
        attachments=([{"filename": str(order.get("oc_pdf_filename") or f"{order.get('order_number') or 'order-confirmation'}.pdf"), "content": base64.b64encode(attachment).decode("ascii")}]
                     if attachment is not None and message_type == "order_confirmation" else None),
        cc=cc, bcc=bcc,
        request_id=f"{message_type}-{order.get('_id')}",
    )
    current_app.extensions["store"].insert_one("email_logs", {
        "order_id": order.get("_id"), "recipient": recipients, "subject": subject,
        "message_type": message_type, "sent_by": (current_user() or {}).get("_id"),
        "status": "sent", "provider_id": result.get("id"), "cc": result.get("cc", []), "bcc": result.get("bcc", []),
        "diagnostic_id": result.get("diagnostic_id"), "stage": result.get("stage", "message_submission"),
        "attachments": [str(order.get("oc_pdf_filename"))] if attachment is not None else [],
        "channel": "email", "created_at": utcnow(),
    })
    return result


def _log_order_email_failure(order: dict, message_type: str, exc: EmailDeliveryError) -> None:
    current_app.extensions["store"].insert_one("email_logs", {
        "order_id": order.get("_id"), "recipient": _order_recipient(order),
        "message_type": message_type, "sent_by": (current_user() or {}).get("_id"),
        "status": "failed", "error_code": exc.error_code,
        "diagnostic_id": exc.diagnostic_id, "stage": exc.stage,
        "channel": "email", "created_at": utcnow(),
    })


def _mark_order_email_failed(order: dict, exc: Exception) -> None:
    error_code = str(getattr(exc, "error_code", "MESSAGE_SUBMISSION_FAILED"))
    details = {
        "email_status": "Failed", "email_error": error_code,
        "email_failed_at": utcnow(),
    }
    current_app.extensions["store"].update_one("orders", {"_id": order.get("_id")}, details)
    if isinstance(exc, EmailDeliveryError):
        _log_order_email_failure(order, "order_confirmation", exc)
        audit("order.email_failed", "order", str(order.get("_id")), {"error_code": exc.error_code, "diagnostic_id": exc.diagnostic_id})
    else:
        current_app.extensions["store"].insert_one("email_logs", {
            "order_id": order.get("_id"), "recipient": _order_recipient(order),
            "message_type": "order_confirmation", "status": "failed",
            "error_code": error_code, "channel": "email", "created_at": utcnow(),
        })
        audit("order.email_failed", "order", str(order.get("_id")), {"error_code": "MESSAGE_SUBMISSION_FAILED"})


@bp.get("/orders")
@permission_required("orders.view")
def list_orders():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    store = current_app.extensions["store"]
    if customer_id:
        if not enforce_active_customer(customer_id):
            return failure("Customer access denied", status=403)
        query: dict = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    else:
        user = current_user() or {}
        if can_view_all_customers(user):
            query = {}
        else:
            customers, _ = store.list("customers", permitted_customer_query(user), limit=100_000)
            customer_ids = [str(row["_id"]) for row in customers if row.get("_id") and not row.get("is_issuer")]
            query = ({"$or": [{"customer_id": {"$in": customer_ids}}, {"customer_company_id": {"$in": customer_ids}}, {"company_id": {"$in": customer_ids}}]} if customer_ids else {"_id": "__no_customer_access__"})
    if request.args.get("status"):
        query["status"] = request.args["status"]
    rows, total = store.list("orders", query, page=max(int(request.args.get("page", 1)), 1), limit=min(int(request.args.get("limit", 25)), 100))
    # Add lightweight payment/incentive state for the operational OC table.
    # Payment proofs are intentionally omitted from the list payload.
    enriched = []
    for row in rows:
        payments, _ = store.list("payments", {"order_id": row.get("_id")}, limit=100)
        latest_payment = payments[-1] if payments else None
        if latest_payment:
            payment_snapshot = {key: value for key, value in latest_payment.items() if key != "attachment"}
            row["payment_snapshot"] = payment_snapshot
            row["payment_status"] = latest_payment.get("status")
        else:
            row["payment_status"] = "PENDING PAYMENT"
        incentive = store.find_one("incentives", {"order_id": row.get("_id")})
        if incentive:
            row["incentive_id"] = incentive.get("_id")
            row["incentive_status"] = incentive.get("status", "PENDING PAYMENT")
            row["incentive_amount"] = incentive.get("gross_incentive_amount", row.get("incentive_amount", 0))
        enriched.append(row)
    return success({"items": enriched, "total": total})


@bp.get("/orders/<order_id>")
@permission_required("orders.view")
def get_order(order_id: str):
    order = current_app.extensions["store"].find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    return success(order)


@bp.post("/orders/<order_id>/send-confirmation")
@permission_required("orders.update")
def send_order_confirmation(order_id: str):
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    try:
        pdf, order = _ensure_order_pdf(order)
        result = _send_order_email(order, "order_confirmation", pdf)
        store.update_one("orders", {"_id": order_id}, {
            "email_status": "Sent", "email_last_sent_at": utcnow(),
            "email_diagnostic_id": result.get("diagnostic_id"),
        })
    except ValueError as exc:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    except EmailDeliveryError as exc:
        _mark_order_email_failed(order, exc)
        return _order_email_failure(exc)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order confirmation email failed diagnostic_id=%s stage=email_service", diagnostic_id)
        _mark_order_email_failed(order, RuntimeError("MESSAGE_SUBMISSION_FAILED"))
        return failure("Order email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    audit("order.email_confirmation", "order", order_id, {"diagnostic_id": result.get("diagnostic_id")})
    return success({"sent": True, "diagnostic_id": result.get("diagnostic_id")}, "Order confirmation sent")


@bp.get("/orders/<order_id>/pdf")
@permission_required("orders.view")
def order_confirmation_pdf(order_id: str):
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    try:
        content, order = _ensure_order_pdf(order)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order confirmation PDF failed diagnostic_id=%s order_id=%s", diagnostic_id, order_id)
        return failure("Order Confirmation PDF could not be generated.", status=503, error="OC_PDF_GENERATION_FAILED", diagnostic_id=diagnostic_id)
    disposition = "inline" if request.args.get("preview") == "true" else "attachment"
    filename = str(order.get("oc_pdf_filename") or f"{order.get('order_number') or order_id}.pdf")
    return Response(content, mimetype="application/pdf", headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@bp.post("/orders/<order_id>/resend-confirmation")
@permission_required("orders.update")
def resend_order_confirmation(order_id: str):
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    try:
        pdf, order = _ensure_order_pdf(order)
        result = _send_order_email(order, "order_confirmation", pdf)
        store.update_one("orders", {"_id": order_id}, {
            "email_status": "Sent", "email_last_sent_at": utcnow(),
            "email_diagnostic_id": result.get("diagnostic_id"),
        })
    except ValueError:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    except EmailDeliveryError as exc:
        _mark_order_email_failed(order, exc)
        return _order_email_failure(exc)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order confirmation resend failed diagnostic_id=%s order_id=%s", diagnostic_id, order_id)
        _mark_order_email_failed(order, RuntimeError("MESSAGE_SUBMISSION_FAILED"))
        return failure("Order email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    audit("order.email_resent", "order", order_id, {"diagnostic_id": result.get("diagnostic_id")})
    return success({"sent": True, "diagnostic_id": result.get("diagnostic_id")}, "Order confirmation resent")


@bp.post("/orders/<order_id>/send-status")
@permission_required("orders.update")
def send_order_status(order_id: str):
    order = current_app.extensions["store"].find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    try:
        result = _send_order_email(order, "order_status")
    except ValueError as exc:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    except EmailDeliveryError as exc:
        _log_order_email_failure(order, "order_status", exc)
        return _order_email_failure(exc)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order status email failed diagnostic_id=%s stage=email_service", diagnostic_id)
        return failure("Order email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    audit("order.email_status", "order", order_id, {"diagnostic_id": result.get("diagnostic_id")})
    return success({"sent": True, "diagnostic_id": result.get("diagnostic_id")}, "Order status email sent")


@bp.get("/quotations/<quotation_id>/order-configuration")
@login_required
def order_configuration(quotation_id: str):
    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    if not quotation:
        return failure("Quotation not found", status=404)
    if not _can_convert_quotation(current_user() or {}, quotation):
        return failure("Customer access denied", status=403)
    user = store.find_one("users", {"_id": quotation.get("salesperson_id") or quotation.get("created_by_user_id") or quotation.get("user_id")}) or {}
    return success({
        "quote": quotation,
        "defaults": {"oc_number": _preview_oc_number(store, str(quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id"))), "payment_terms": quotation.get("payment_terms"), "order_amount": (quotation.get("totals") or {}).get("grand_total"), "salesperson": {"_id": user.get("_id"), "name": user.get("name"), "email": user.get("email")}},
    })


@bp.post("/quotations/<quotation_id>/convert-to-order")
@login_required
def convert_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    if not quotation:
        return failure("Quotation not found", status=404)
    quotation_customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    user = current_user() or {}
    if not _can_convert_quotation(user, quotation):
        return failure("You do not have permission to convert this quotation", status=403)
    payload = request.get_json(silent=True) or {}
    idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()[:160]
    if idempotency_key:
        existing_by_key = store.find_one("orders", {"customer_id": quotation_customer_id, "idempotency_key": idempotency_key})
        if existing_by_key:
            return success(existing_by_key, "Order Confirmation already created")
    existing = store.find_one("orders", {"quotation_id": quotation_id})
    if existing:
        return failure("Quotation is already linked to an order", status=409)
    try:
        additional_recipients = _additional_recipients(payload)
        payment_terms = _validated_payment_terms(payload.get("payment_terms"), quotation.get("payment_terms"))
    except ValueError as exc:
        return failure(str(exc), status=422, error="INVALID_ORDER_CONFIGURATION")
    quotation_to, quotation_cc, quotation_bcc = _quotation_recipients(store, quotation)
    quotation_to = list(dict.fromkeys([*quotation_to, *additional_recipients]))
    now = utcnow()
    salesperson_id = quotation.get("salesperson_id") or quotation.get("created_by_user_id") or quotation.get("user_id") or user.get("_id")
    salesperson = store.find_one("users", {"_id": salesperson_id}) or {}
    salesperson_snapshot = quotation.get("salesperson_snapshot") or {"name": salesperson.get("name"), "email": salesperson.get("email"), "_id": salesperson_id}
    lead = store.find_one("leads", {"quotation_id": quotation_id, "$or": [{"customer_id": quotation_customer_id}, {"customer_company_id": quotation_customer_id}, {"company_id": quotation_customer_id}]})
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    issuer = {**(settings.get("issuer") or {}), **(quotation.get("issuer_snapshot") or {})}
    issuer["name"] = "Moneda Technologies"
    issuer["email"] = issuer.get("email") or "business@monedatechnologies.com"
    order_document = {
        "order_number": "", "quotation_id": quotation_id,
        "quotation_number": quotation.get("quotation_number"),
        "lead_id": lead.get("_id") if lead else None,
        "customer_id": quotation_customer_id, "customer_company_id": quotation_customer_id, "company_id": quotation_customer_id,
        "issuer_name": "Moneda Technologies", "issuer_snapshot": issuer,
        "customer_company_snapshot": quotation.get("customer_company_snapshot") or quotation.get("company_snapshot"),
        "company_snapshot": quotation.get("company_snapshot"), "customer_snapshot": quotation.get("customer_snapshot"),
        "salesperson_id": salesperson_id, "prepared_by_user_id": quotation.get("prepared_by_user_id") or salesperson_id, "created_by_user_id": user.get("_id"), "salesperson_snapshot": salesperson_snapshot,
        "quotation_snapshot": quotation, "products_snapshot": quotation["lines"], "lines": quotation["lines"],
        "master_currency": quotation.get("master_currency", "EUR"), "currency": quotation["currency"],
        "exchange_rate": quotation.get("exchange_rate"), "exchange_rate_meta": quotation.get("exchange_rate_meta"),
        # The quotation's server-calculated grand total is authoritative for
        # the Order Confirmation and incentive base.  Never trust a client
        # supplied amount from the conversion form.
        "totals": quotation["totals"], "order_amount": float((quotation.get("totals") or {}).get("grand_total") or 0),
        "original_quote_payment_terms": quotation.get("payment_terms"), "payment_terms": payment_terms,
        "oc_date": payload.get("oc_date") or now.date().isoformat(), "document_type": "order_confirmation", "status": "Pending",
        "created_at": now, "document_status": "Pending", "email_status": "Pending",
        "notes": quotation.get("notes", ""), "to": quotation_to, "cc": quotation_cc, "bcc": quotation_bcc,
        "additional_recipients": additional_recipients,
        "history": [{"status": "Pending", "at": now, "by": (current_user() or {}).get("_id")}],
    }
    if idempotency_key:
        order_document["idempotency_key"] = idempotency_key
    try:
        # Validate the category-wise incentive matrix before allocating an OC
        # number or inserting any financial record.
        validate_incentive_configuration(store, order_document, salesperson)
    except IncentiveConfigurationError as exc:
        return failure(str(exc), status=422, error="INCENTIVE_CONFIGURATION_REQUIRED", category_id=exc.category_id)
    # Keep the existing preflight check for a friendly 409, then repeat it
    # while holding a process-local lock so two rapid conversion requests in
    # this worker cannot both create an Order Confirmation.
    with _CONVERSION_LOCK:
        existing = store.find_one("orders", {"quotation_id": quotation_id})
        if existing:
            return failure("Quotation is already linked to an order", status=409)
        try:
            # Allocate the customer-scoped OC sequence only after the
            # idempotency check is held under the conversion lock.
            order_document["order_number"] = _next_oc_number(store, str(quotation_customer_id))
            order = store.insert_one("orders", order_document)
        except Exception as exc:
            # Mongo's unique/index errors (or a concurrent worker) should be
            # reported as an idempotent conversion conflict, never retried as
            # a second order creation.
            if exc.__class__.__name__ == "DuplicateKeyError":
                existing = store.find_one("orders", {"quotation_id": quotation_id})
                if existing:
                    return failure("Quotation is already linked to an order", status=409)
            raise
    try:
        order_pdf, order = _ensure_order_pdf(order)
    except Exception:
        current_app.logger.exception("order confirmation PDF generation failed order_id=%s", order.get("_id"))
        store.update_one("orders", {"_id": order.get("_id")}, {"document_status": "Failed"})
        order_pdf = None
    try:
        incentive = create_incentive_for_order(store, order, salesperson)
    except IncentiveConfigurationError as exc:
        # The product matrix may have changed between preflight and insert.
        # Remove only this brand-new, unreferenced order/document so no
        # partially-created financial record remains.
        store.delete_one("order_documents", {"order_id": order.get("_id")})
        store.delete_one("orders", {"_id": order.get("_id")})
        return failure(str(exc), status=422, error="INCENTIVE_CONFIGURATION_REQUIRED", category_id=exc.category_id)
    except Exception:
        current_app.logger.exception("incentive creation failed order_id=%s", order.get("_id"))
        store.delete_one("order_documents", {"order_id": order.get("_id")})
        store.delete_one("orders", {"_id": order.get("_id")})
        return failure("Order Confirmation could not be created because its incentive record failed.", status=503, error="INCENTIVE_CREATION_FAILED")
    audit("incentive.created", "incentive", str(incentive.get("_id")), {
        "order_id": order.get("_id"), "rate": incentive.get("incentive_percentage_snapshot", 0),
        "line_count": len(incentive.get("incentive_lines") or []),
        "rates": [line.get("incentive_rate_snapshot") for line in incentive.get("incentive_lines") or []],
    })
    order = store.update_one("orders", {"_id": order.get("_id")}, {
        "incentive_id": incentive.get("_id"),
        "incentive_status": incentive.get("status", "PENDING PAYMENT"),
        "incentive_amount": incentive.get("gross_incentive_amount", 0),
    }) or order
    quotation_history = [*quotation.get("history", []), {"status": "Converted to Order", "at": now, "by": user.get("_id")}]
    store.update_one("quotations", {"_id": quotation_id}, {"status": "Converted to Order", "history": quotation_history})
    open_reminders, _ = store.list("reminders", {"quotation_id": quotation_id, "$or": [{"customer_id": quotation_customer_id}, {"customer_company_id": quotation_customer_id}, {"company_id": quotation_customer_id}], "status": {"$in": ["Pending", "Due", "Overdue", "open"]}}, limit=100)
    for reminder in open_reminders:
        store.update_one("reminders", {"_id": reminder["_id"]}, {"status": "Cancelled", "cancelled_at": now, "cancelled_reason": "Quotation converted to order"})
    for interval in settings.get("post_order_follow_up_days", [15, 25]):
        store.insert_one("reminders", {"customer_id": quotation_customer_id, "customer_company_id": quotation_customer_id, "company_id": quotation_customer_id, "order_id": order["_id"], "lead_id": lead.get("_id") if lead else None, "assigned_to": user.get("_id"), "due_date": (now + timedelta(days=int(interval))).date().isoformat(), "status": "Pending", "priority": "normal", "notes": f"Post-order follow-up ({interval} days)", "frequency": "none", "repeat_frequency": "none", "repeat_enabled": False})
    if lead:
        store.update_one("leads", {"_id": lead["_id"]}, {"status": "Order Received", "order_id": order["_id"], "follow_up_date": None})
    store.insert_one("notifications", {
        "user_id": (current_user() or {}).get("_id"), "customer_id": quotation_customer_id,
        "type": "order_created", "title": f"Order {order['order_number']} created",
        "order_id": order["_id"], "read": False,
    })
    audit("order.create", "order", str(order["_id"]), {"quotation_id": quotation_id})
    if order_pdf is not None:
        try:
            email_result = _send_order_email(order, "order_confirmation", order_pdf)
            order = store.update_one("orders", {"_id": order.get("_id")}, {
                "email_status": "Sent", "email_last_sent_at": utcnow(),
                "email_diagnostic_id": email_result.get("diagnostic_id"),
            }) or {**order, "email_status": "Sent"}
            audit("order.email_sent", "order", str(order.get("_id")), {"diagnostic_id": email_result.get("diagnostic_id")})
        except Exception as exc:
            current_app.logger.exception("order confirmation email after conversion failed order_id=%s", order.get("_id"))
            _mark_order_email_failed(order, exc)
            order = {**order, "email_status": "Failed", "email_error": str(getattr(exc, "error_code", "MESSAGE_SUBMISSION_FAILED"))}
    order["incentive_id"] = incentive.get("_id")
    return success(order, "Order Confirmation created", 201)


@bp.patch("/orders/<order_id>")
@permission_required("orders.update")
def update_order(order_id: str):
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    if order.get("financial_locked") and (current_user() or {}).get("role_id") != "superadmin":
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    payload = request.get_json(silent=True) or {}
    if payload.get("status") not in STATUSES:
        return failure("Invalid order status", status=422)
    history = [*order.get("history", []), {"status": payload["status"], "at": utcnow(), "by": (current_user() or {}).get("_id")}]
    updated = store.update_one("orders", {"_id": order_id}, {"status": payload["status"], "notes": payload.get("notes", order.get("notes", "")), "history": history})
    audit("order.update", "order", order_id, {"status": payload["status"]})
    return success(updated, "Order updated")
