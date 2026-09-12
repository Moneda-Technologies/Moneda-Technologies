from __future__ import annotations

from datetime import datetime, timezone
from email_validator import EmailNotValidError, validate_email
from html import escape
from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.finance.service import activate_incentive, money, recompute_incentive_totals
from app.middleware.access import current_user, customer_access_ids_for_user, customer_record, enforce_customer, login_required, permission_required, permission_required_any
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("finance", __name__, url_prefix="/api")


def _store():
    return current_app.extensions["store"]


def _email(value: object) -> str:
    try:
        return validate_email(str(value or "").strip(), check_deliverability=False).normalized
    except EmailNotValidError:
        return ""


def _parse_date(value: object, *, required: bool = True):
    raw = str(value or "").strip()
    if not raw:
        return utcnow() if not required else None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _order(order_id: str):
    return _store().find_one("orders", {"_id": order_id})


def _superadmin() -> bool:
    return str((current_user() or {}).get("role_id") or "") == "superadmin"


def _user_id(user: dict | None = None) -> str:
    return str((user or current_user() or {}).get("_id") or "")


def _order_customer_id(order: dict) -> str:
    return str(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id") or "")


def _order_owner_ids(order: dict) -> set[str]:
    """Return the explicit people attached to an Order Confirmation.

    ``salesperson_id`` is the primary relationship.  The other fields keep
    older Order Confirmations accessible without guessing from names/emails.
    """
    owner_ids = {
        str(order.get(field) or "")
        for field in ("salesperson_id", "prepared_by_user_id", "created_by_user_id", "user_id")
        if order.get(field)
    }
    for snapshot_key in ("salesperson_snapshot",):
        snapshot = order.get(snapshot_key)
        if isinstance(snapshot, dict) and snapshot.get("_id"):
            owner_ids.add(str(snapshot["_id"]))
    return owner_ids


def _customer_access(actor: dict, customer_id: str) -> bool:
    """Check finance scope without treating Admin as automatically global."""
    if not customer_id:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    if str(actor.get("role_id") or "") != "admin":
        return enforce_customer(customer_id)
    customer = customer_record(customer_id) or {}
    actor_id = _user_id(actor)
    return actor_id in {str(value) for value in (customer.get("assigned_user_ids") or []) if value} or str(customer.get("created_by_user_id") or "") == actor_id


def _can_access_order(order: dict, user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    if not actor:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    customer_id = _order_customer_id(order)
    owner = _user_id(actor) in _order_owner_ids(order)
    # The OC relationship is authoritative for its creator/salesperson. This
    # keeps an owner from being blocked by a missing legacy customer assignment
    # while still requiring customer scope for other sales users/managers.
    if owner and str(actor.get("role_id") or "") in {"user", "manager_sales_admin"}:
        return bool(customer_id)
    if not _customer_access(actor, customer_id):
        return False
    return owner or str(actor.get("role_id") or "") in {"admin", "manager_sales_admin"}


def _can_view_payment(payment: dict, user: dict | None = None) -> bool:
    order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if order:
        return _can_access_order(order, user)
    actor = user or current_user() or {}
    return _customer_access(actor, str(payment.get("customer_id") or ""))


def _can_record_payment(order: dict, user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    if not _can_access_order(order, actor):
        return False
    role_id = str(actor.get("role_id") or "")
    if role_id in {"admin", "superadmin", "manager_sales_admin"}:
        return True
    return role_id == "user" and _user_id(actor) in _order_owner_ids(order)


def _can_view_incentive(row: dict, user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    if not actor:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    actor_id = _user_id(actor)
    if str(actor.get("role_id") or "") == "admin":
        return _customer_access(actor, str(row.get("customer_id") or ""))
    if str(row.get("salesperson_id") or "") == actor_id:
        order = _order(str(row.get("order_id") or ""))
        if order and actor_id in _order_owner_ids(order):
            return _can_access_order(order, actor)
        return _customer_access(actor, str(row.get("customer_id") or ""))
    order = _order(str(row.get("order_id") or ""))
    return bool(order and actor_id in _order_owner_ids(order) and _can_access_order(order, actor))


def _payment_recipients(order: dict, payment: dict | None = None) -> list[str]:
    values: list[str] = []
    customer = order.get("customer_snapshot") or order.get("customer_company_snapshot") or {}
    for value in (customer.get("email"), (order.get("salesperson_snapshot") or {}).get("email")):
        email = _email(value)
        if email and email not in values:
            values.append(email)
    return values


def _configured_payment_cc_bcc(order: dict) -> tuple[list[str], list[str]]:
    def normalise(values):
        result: list[str] = []
        for value in values if isinstance(values, list) else [values]:
            email = _email(value)
            if email and email not in result:
                result.append(email)
        return result
    return normalise(order.get("cc")), normalise(order.get("bcc"))


def _payment_list_row(payment: dict) -> dict:
    """Return the existing payment record with safe OC/quote context.

    Banking is an operational view over the payments collection; this helper
    only adds references already present on the related Order Confirmation and
    never creates or duplicates a financial record.
    """
    row = dict(payment)
    # Payment proof is private evidence.  Banking list/detail responses may
    # expose its metadata, but never return the base64/file body.
    attachment = row.get("attachment")
    if isinstance(attachment, dict):
        row["attachment"] = {
            key: attachment.get(key)
            for key in ("name", "type", "size")
            if attachment.get(key) is not None
        }
    order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if not order:
        return row
    row.setdefault("order_number", order.get("order_number") or order.get("oc_number"))
    row.setdefault("quotation_id", order.get("quotation_id"))
    row.setdefault("quotation_number", order.get("quotation_number") or order.get("quote_number"))
    row.setdefault("customer_snapshot", order.get("customer_snapshot") or order.get("customer_company_snapshot"))
    row.setdefault("order_confirmation_id", order.get("_id"))
    return row


def _invoice_amount(order: dict) -> float:
    totals = order.get("totals") if isinstance(order.get("totals"), dict) else {}
    return money(order.get("order_amount", totals.get("grand_total", 0)))


def _confirmed_payment_total(order_id: str) -> float:
    """Return confirmed receipts for an OC without counting rejected entries."""
    rows, _ = _store().list("payments", {"order_id": order_id, "status": "CONFIRMED"}, limit=100_000)
    return money(sum(money(row.get("amount", row.get("payment_amount"))) for row in rows))


def _payment_rollup(order: dict, payments: list[dict], current_id: str) -> dict:
    valid = [row for row in payments if str(row.get("status") or "").upper() != "REJECTED"]
    total_paid = money(sum(money(row.get("amount", row.get("payment_amount"))) for row in valid))
    current_index = next((index for index, row in enumerate(valid) if str(row.get("_id")) == current_id), len(valid))
    previous_paid = money(sum(money(row.get("amount", row.get("payment_amount"))) for row in valid[:current_index]))
    invoice_amount = _invoice_amount(order)
    balance = money(max(invoice_amount - total_paid, 0))
    credit = money(max(total_paid - invoice_amount, 0))
    status = ""
    if credit > 0:
        status = "OVERPAID / CREDIT GENERATED"
    elif balance <= 0 and total_paid > 0:
        status = "PAID"
    elif total_paid > 0:
        status = "PARTIALLY PAID / OUTSTANDING"
    return {
        "invoice_amount": invoice_amount,
        "previous_paid": previous_paid,
        "total_paid": total_paid,
        "balance": balance,
        "remaining_balance": balance,
        "customer_credit": credit,
        "derived_status": status,
    }


def _enrich_payment_rows(rows: list[dict]) -> list[dict]:
    store = _store()
    order_cache: dict[str, dict] = {}
    payment_cache: dict[str, list[dict]] = {}
    enriched: list[dict] = []
    for payment in rows:
        order_id = str(payment.get("order_id") or payment.get("oc_id") or "")
        if order_id not in order_cache:
            order_cache[order_id] = _order(order_id) or {}
        order = order_cache[order_id]
        if order_id not in payment_cache:
            all_payments, _ = store.list("payments", {"order_id": order_id}, limit=100_000)
            payment_cache[order_id] = sorted(all_payments, key=lambda row: str(row.get("created_at") or ""))
        row = _payment_list_row(payment)
        row.update(_payment_rollup(order, payment_cache[order_id], str(payment.get("_id") or "")))
        row["workflow_status"] = str(payment.get("status") or "")
        enriched.append(row)
    return enriched


def _validate_banking_payload(payload: dict) -> dict[str, str]:
    fields = {
        "customer_id": "Customer",
        "order_id": "Invoice / Order Confirmation",
        "amount": "Payment amount",
        "payment_date": "Payment date",
        "payment_mode": "Payment mode",
        "bank_name": "Bank name",
        "bank_account": "Bank account",
        "utr": "UTR / transaction reference",
        "reference_number": "Payment reference",
    }
    errors: dict[str, str] = {}
    for field, label in fields.items():
        value = payload.get(field)
        if field == "amount":
            if money(value) <= 0:
                errors[field] = f"{label} must be greater than zero"
        elif not str(value or "").strip():
            errors[field] = f"{label} is required"
    currency = str(payload.get("currency") or "EUR").upper()
    if currency != "EUR":
        errors["currency"] = "Payments must use the invoice currency (EUR)"
    return errors


@bp.get("/payments")
@login_required
def list_payments():
    store = _store()
    user = current_user() or {}
    order_id = str(request.args.get("order_id") or "").strip()
    global_scope = str(user.get("role_id") or "") == "superadmin"
    has_global_permission = (
        global_scope
        or "payments.view" in user.get("permissions", [])
        or "payments.manage" in user.get("permissions", [])
    )
    if not has_global_permission and not order_id:
        return failure("You do not have permission to view payments", status=403)
    query: dict = {}
    if request.args.get("status"):
        query["status"] = request.args["status"]
    if order_id:
        query["order_id"] = order_id
        order = _order(order_id)
        if not order:
            return failure("Order Confirmation not found", status=404)
        if not global_scope and not _can_access_order(order, user):
            return failure("Payment access denied", status=403)
    if not global_scope:
        rows, _ = store.list("payments", query, limit=100_000)
        rows = [row for row in rows if _can_view_payment(row, user)]
        rows = _enrich_payment_rows(rows)
        return success({"items": rows, "total": len(rows)})
    rows, total = store.list("payments", query, limit=min(int(request.args.get("limit", 100)), 500))
    rows = _enrich_payment_rows(rows)
    return success({"items": rows, "total": total})


@bp.post("/payments")
@login_required
def create_payment():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("oc_id") or "").strip()
    order = _order(order_id)
    if not order:
        return failure("Order Confirmation not found", status=404, error="order_not_found")
    customer_id = _order_customer_id(order)
    user = current_user() or {}
    if payload.get("workflow") == "banking":
        errors = _validate_banking_payload({**payload, "customer_id": payload.get("customer_id"), "order_id": order_id})
        if str(payload.get("customer_id") or "") != customer_id:
            errors["customer_id"] = "Selected customer does not own this Order Confirmation"
        if errors:
            return failure("Please correct the payment details", status=422, error="invalid_payment_fields", details={"fields": errors})
    if not _can_record_payment(order, user):
        return failure("Payment access denied", status=403)
    if order.get("financial_locked") and not _superadmin() and _confirmed_payment_total(order_id) >= _invoice_amount(order):
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    amount = money(payload.get("payment_amount", payload.get("amount")))
    payment_date = _parse_date(payload.get("payment_date"))
    if amount <= 0 or payment_date is None:
        return failure("Payment amount and date are required", status=422, error="invalid_payment")
    attachment = payload.get("attachment")
    if attachment is not None:
        if not isinstance(attachment, dict) or not isinstance(attachment.get("data"), str):
            return failure("Payment proof must be a valid uploaded file", status=422, error="invalid_payment_proof")
        allowed_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
        if payload.get("workflow") == "banking" and str(attachment.get("type") or "").lower() not in allowed_types:
            return failure("Payment proof must be a PDF or image", status=422, error="invalid_payment_proof_type")
        if len(attachment["data"]) > 8_000_000:
            return failure("Payment proof is too large", status=422, error="payment_proof_too_large")
        try:
            attachment_size = max(0, min(int(attachment.get("size") or 0), 5 * 1024 * 1024))
        except (TypeError, ValueError):
            attachment_size = 0
        attachment = {
            "name": str(attachment.get("name") or "proof")[:160],
            "type": str(attachment.get("type") or "application/octet-stream")[:120],
            "size": attachment_size,
            "data": attachment["data"],
        }
    now = utcnow()
    row = _store().insert_one("payments", {
        "order_id": order_id, "oc_id": order_id, "quotation_id": order.get("quotation_id"),
        "customer_id": customer_id, "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or {},
        "amount": amount, "payment_amount": amount, "currency": "EUR", "payment_date": payment_date,
        "bank_name": str(payload.get("bank_name") or "").strip()[:160], "bank_account": str(payload.get("bank_account") or "").strip()[:160],
        "utr": str(payload.get("utr") or payload.get("transaction_reference") or "").strip()[:160],
        "payment_mode": str(payload.get("payment_mode") or "").strip()[:80], "reference_number": str(payload.get("reference_number") or "").strip()[:160],
        "notes": str(payload.get("notes") or "").strip()[:2000], "attachment": attachment,
        "status": "PAYMENT RECORDED", "created_by_user_id": (current_user() or {}).get("_id"), "workflow": payload.get("workflow"),
        "audit": [{"action": "created", "by": (current_user() or {}).get("_id"), "at": now}],
    })
    audit("payment.create", "payment", str(row["_id"]), {"order_id": order_id, "amount": amount})
    return success(row, "Payment recorded", 201)


@bp.patch("/payments/<payment_id>")
@login_required
def update_payment(payment_id: str):
    """Update an unconfirmed payment and return it to the submission queue."""
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    payment_order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if not payment_order or not _can_record_payment(payment_order):
        return failure("Payment access denied", status=403)
    if payment.get("financial_locked") or payment.get("status") == "CONFIRMED":
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    payload = request.get_json(silent=True) or {}
    if payload.get("workflow") == "banking":
        order_customer_id = _order_customer_id(payment_order)
        errors = _validate_banking_payload({**payment, **payload, "customer_id": payload.get("customer_id") or order_customer_id, "order_id": payment_order.get("_id")})
        if payload.get("customer_id") and str(payload.get("customer_id")) != order_customer_id:
            errors["customer_id"] = "Selected customer does not own this Order Confirmation"
        if errors:
            return failure("Please correct the payment details", status=422, error="invalid_payment_fields", details={"fields": errors})
    changes: dict[str, object] = {}
    if "amount" in payload or "payment_amount" in payload:
        amount = money(payload.get("payment_amount", payload.get("amount")))
        if amount <= 0:
            return failure("Payment amount must be greater than zero", status=422, error="invalid_payment")
        changes["amount"] = amount
        changes["payment_amount"] = amount
    if "payment_date" in payload:
        payment_date = _parse_date(payload.get("payment_date"))
        if payment_date is None:
            return failure("Payment date is required", status=422, error="invalid_payment")
        changes["payment_date"] = payment_date
    for field, limit in (("bank_name", 160), ("bank_account", 160), ("utr", 160), ("payment_mode", 80), ("reference_number", 160), ("notes", 2000)):
        if field in payload:
            changes[field] = str(payload.get(field) or "").strip()[:limit]
    if "attachment" in payload:
        attachment = payload.get("attachment")
        if attachment is not None:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("data"), str):
                return failure("Payment proof must be a valid uploaded file", status=422, error="invalid_payment_proof")
            allowed_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
            if payload.get("workflow") == "banking" and str(attachment.get("type") or "").lower() not in allowed_types:
                return failure("Payment proof must be a PDF or image", status=422, error="invalid_payment_proof_type")
            if len(attachment["data"]) > 8_000_000:
                return failure("Payment proof is too large", status=422, error="payment_proof_too_large")
            try:
                attachment_size = max(0, min(int(attachment.get("size") or 0), 5 * 1024 * 1024))
            except (TypeError, ValueError):
                attachment_size = 0
            attachment = {"name": str(attachment.get("name") or "proof")[:160], "type": str(attachment.get("type") or "application/octet-stream")[:120], "size": attachment_size, "data": attachment["data"]}
        changes["attachment"] = attachment
    if not changes:
        return failure("No payment fields were supplied", status=422, error="invalid_payment")
    now = utcnow()
    changes.update({"status": "PAYMENT RECORDED", "submitted_at": None, "submitted_by_user_id": None, "updated_at": now, "audit": [*payment.get("audit", []), {"action": "updated", "by": (current_user() or {}).get("_id"), "at": now, "fields": sorted(changes)}]})
    row = store.update_one("payments", {"_id": payment_id}, changes)
    audit("payment.update", "payment", payment_id, {"fields": sorted(changes)})
    return success(row, "Payment details updated")


@bp.post("/payments/<payment_id>/submit")
@login_required
def submit_payment(payment_id: str):
    payment = _store().find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    payment_order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if not payment_order or not _can_record_payment(payment_order):
        return failure("Payment access denied", status=403)
    if payment.get("financial_locked") and not _superadmin():
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    if payment.get("status") not in {"PAYMENT RECORDED", "REJECTED"}:
        return failure("Payment cannot be submitted in its current state", status=409)
    now = utcnow()
    row = _store().update_one("payments", {"_id": payment_id}, {"status": "AWAITING SUPERADMIN CONFIRMATION", "submitted_at": now, "submitted_by_user_id": (current_user() or {}).get("_id"), "audit": [*payment.get("audit", []), {"action": "submitted", "by": (current_user() or {}).get("_id"), "at": now}]})
    audit("payment.submit", "payment", payment_id, {})
    return success(row, "Payment submitted for bank confirmation")


@bp.post("/payments/<payment_id>/confirm")
@login_required
def confirm_payment(payment_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can confirm bank receipt", status=403, error="superadmin_required")
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    # Confirmation is a separate privileged step.  A merely entered payment
    # must first be submitted into the Superadmin review queue.
    if payment.get("status") not in {"AWAITING SUPERADMIN CONFIRMATION", "AWAITING BANK CONFIRMATION"}:
        return failure("Payment is not awaiting confirmation", status=409)
    confirmed_at = utcnow()
    actor = current_user() or {}
    row = store.update_one("payments", {"_id": payment_id}, {"status": "CONFIRMED", "confirmed_by_user_id": actor.get("_id"), "confirmed_at": confirmed_at, "financial_locked": True, "audit": [*payment.get("audit", []), {"action": "confirmed", "by": actor.get("_id"), "at": confirmed_at}]})
    incentive = activate_incentive(store, str(payment.get("order_id")), confirmed_at)
    if incentive:
        audit("incentive.activated", "incentive", str(incentive.get("_id")), {"order_id": payment.get("order_id"), "payment_id": payment_id})
    order = _order(str(payment.get("order_id"))) or {}
    store.update_one("orders", {"_id": payment.get("order_id")}, {"financial_locked": True, "payment_status": "CONFIRMED"})
    if order.get("quotation_id"):
        store.update_one("quotations", {"_id": order.get("quotation_id")}, {"financial_locked": True, "payment_status": "CONFIRMED"})
    recipients = _payment_recipients(order, payment)
    email_result = None
    if recipients:
        try:
            email_result = current_app.extensions["email_service"].send(
                purpose="order", to=recipients,
                subject=f"Payment confirmation {order.get('order_number', order.get('_id', ''))} - Moneda Technologies",
                html=f"<p>Payment of <strong>EUR {money(payment.get('amount')):.2f}</strong> has been confirmed for Order Confirmation <strong>{escape(str(order.get('order_number') or order.get('_id') or ''))}</strong>.</p><p>Payment date: {escape(str(payment.get('payment_date') or ''))}<br>UTR/reference: {escape(str(payment.get('utr') or payment.get('reference_number') or 'Not provided'))}</p>",
                cc=_configured_payment_cc_bcc(order)[0], bcc=_configured_payment_cc_bcc(order)[1],
                customer_facing=True, request_id=f"payment-confirmed-{payment_id}",
            )
        except Exception:
            current_app.logger.exception("payment confirmation email failed payment_id=%s", payment_id)
    store.insert_one("email_logs", {"payment_id": payment_id, "order_id": payment.get("order_id"), "message_type": "payment_confirmation", "status": "sent" if email_result else "not_sent", "created_at": utcnow()})
    audit("payment.confirm", "payment", payment_id, {"order_id": payment.get("order_id"), "incentive_id": incentive.get("_id") if incentive else None})
    return success({"payment": row, "incentive": incentive}, "Payment confirmed")


@bp.post("/payments/<payment_id>/reject")
@login_required
def reject_payment(payment_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can reject a payment", status=403, error="superadmin_required")
    payment = _store().find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return failure("A rejection reason is required", status=422)
    row = _store().update_one("payments", {"_id": payment_id}, {"status": "REJECTED", "rejected_by_user_id": (current_user() or {}).get("_id"), "rejected_at": utcnow(), "rejection_reason": reason, "audit": [*payment.get("audit", []), {"action": "rejected", "reason": reason, "by": (current_user() or {}).get("_id"), "at": utcnow()}]})
    audit("payment.reject", "payment", payment_id, {"reason": reason})
    return success(row, "Payment sent back for correction")


@bp.get("/incentives")
@login_required
def list_incentives():
    store = _store()
    user = current_user() or {}
    has_permission = "incentives.view" in user.get("permissions", []) or "incentives.manage" in user.get("permissions", [])
    if not has_permission and str(user.get("role_id") or "") not in {"admin", "superadmin", "user", "manager_sales_admin"}:
        return failure("You do not have permission to view incentives", status=403)
    query: dict = {}
    # Superadmin has company-wide visibility. Admins are restricted to their
    # assigned customer scope; sales people and managers remain restricted to
    # their own OC relationships.
    global_scope = str(user.get("role_id") or "") == "superadmin"
    allowed_customer_ids: list[str] | None = None
    if not global_scope:
        allowed_customer_ids = customer_access_ids_for_user(str(user.get("_id") or ""))
        owner_orders, _ = store.list("orders", {"$or": [
            {"salesperson_id": user.get("_id")}, {"prepared_by_user_id": user.get("_id")},
            {"created_by_user_id": user.get("_id")}, {"user_id": user.get("_id")},
        ]}, limit=100_000)
        owner_order_ids = [row.get("_id") for row in owner_orders if row.get("_id")]
        query["$or"] = [
            {"salesperson_id": user.get("_id")},
            {"order_id": {"$in": owner_order_ids or ["__no_owned_orders__"]}},
        ]
        if str(user.get("role_id") or "") == "admin" and allowed_customer_ids:
            query["$or"].append({"customer_id": {"$in": allowed_customer_ids}})
    for key in ("status", "salesperson_id", "customer_id", "order_id"):
        if request.args.get(key):
            if key == "salesperson_id" and not global_scope and request.args[key] != str(user.get("_id") or ""):
                return success({"items": [], "total": 0})
            if key == "customer_id" and allowed_customer_ids is not None and request.args[key] not in allowed_customer_ids:
                return success({"items": [], "total": 0})
            query[key] = request.args[key]
    # Search and payment/date filters are applied to the server-authorized
    # result set below.  Fetching the bounded management list first avoids
    # allowing a client-supplied filter to bypass the scope query above.
    rows, _ = store.list("incentives", query, limit=min(int(request.args.get("limit", 500)), 500))
    search = str(request.args.get("search") or "").strip().casefold()
    payment_filter = str(request.args.get("payment_status") or "").strip().casefold()
    product_filter = str(request.args.get("product_id") or "").strip()
    category_filter = str(request.args.get("category_id") or request.args.get("category") or "").strip().casefold()
    from_date = _parse_date(request.args.get("from_date"), required=False) if request.args.get("from_date") else None
    to_date = _parse_date(request.args.get("to_date"), required=False) if request.args.get("to_date") else None
    if to_date:
        # Date inputs are inclusive from the user's perspective.
        from datetime import timedelta
        to_date = to_date + timedelta(days=1)
    filtered: list[dict] = []
    for index, row in enumerate(rows):
        refreshed = recompute_incentive_totals(store, str(row["_id"]))
        if refreshed:
            rows[index] = refreshed
        row = rows[index]
        if product_filter and not any(str(line.get("product_id") or "") == product_filter for line in (row.get("incentive_lines") or [])):
            continue
        if category_filter and not any(str(line.get("category_id") or line.get("category_name") or "").casefold() == category_filter for line in (row.get("incentive_lines") or [])):
            continue
        if not global_scope and not _can_view_incentive(row, user):
            continue
        order = store.find_one("orders", {"_id": row.get("order_id")}) or {}
        if not row.get("customer_snapshot"):
            row["customer_snapshot"] = order.get("customer_snapshot") or order.get("customer_company_snapshot") or order.get("company_snapshot") or {}
        if not row.get("salesperson_snapshot"):
            row["salesperson_snapshot"] = order.get("salesperson_snapshot") or {}
        row["order_number"] = row.get("order_number") or order.get("order_number")
        payments, _ = store.list("payments", {"order_id": row.get("order_id")}, limit=100)
        payment_status = "Paid" if row.get("payment_confirmation_date") or any(str(payment.get("status") or "").upper() == "CONFIRMED" for payment in payments) else "Pending Payment"
        row["payment_status"] = payment_status
        customer = row.get("customer_snapshot") or {}
        salesperson = row.get("salesperson_snapshot") or {}
        haystack = " ".join(str(value or "") for value in (
            row.get("order_number"), row.get("oc_number"), row.get("order_id"),
            row.get("customer_id"), customer.get("name"), customer.get("company_name"),
            salesperson.get("name"), salesperson.get("email"), row.get("salesperson_id"),
        )).casefold()
        if search and search not in haystack:
            continue
        if payment_filter and payment_filter not in {payment_status.casefold(), "confirmed" if payment_status == "Paid" else "pending", "unpaid" if payment_status != "Paid" else ""}:
            continue
        created_at = row.get("created_at")
        if from_date and (not created_at or created_at < from_date):
            continue
        if to_date and (not created_at or created_at >= to_date):
            continue
        filtered.append(row)
    return success({"items": filtered, "total": len(filtered)})


@bp.get("/incentives/<incentive_id>")
@login_required
def get_incentive(incentive_id: str):
    store = _store()
    row = store.find_one("incentives", {"_id": incentive_id})
    if not row:
        return failure("Incentive not found", status=404)
    user = current_user() or {}
    if not _can_view_incentive(row, user):
        return failure("Incentive access denied", status=403)
    return success(recompute_incentive_totals(store, incentive_id) or row)


@bp.post("/incentives/<incentive_id>/pay")
@login_required
def pay_incentive(incentive_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can mark incentives paid", status=403, error="superadmin_required")
    store = _store()
    incentive = store.find_one("incentives", {"_id": incentive_id})
    if not incentive:
        return failure("Incentive not found", status=404)
    amount = money((request.get_json(silent=True) or {}).get("paid_amount", incentive.get("net_payable_incentive", 0)))
    if amount < 0:
        return failure("Paid amount cannot be negative", status=422)
    row = store.update_one("incentives", {"_id": incentive_id}, {"paid_amount": amount, "paid_at": utcnow(), "paid_by_user_id": (current_user() or {}).get("_id")})
    row = recompute_incentive_totals(store, incentive_id) or row
    audit("incentive.pay", "incentive", incentive_id, {"paid_amount": amount})
    return success(row, "Incentive payment recorded")


@bp.post("/incentives/<incentive_id>/confirm-payment")
@login_required
def confirm_incentive_payment(incentive_id: str):
    """Activate an incentive for an already bank-confirmed payment.

    Customer payment confirmation belongs to ``/payments/<id>/confirm``. This
    compatibility endpoint cannot bypass that workflow.
    """
    if not _superadmin():
        return failure("Only a Superadmin can confirm payment", status=403, error="superadmin_required")
    store = _store()
    incentive = store.find_one("incentives", {"_id": incentive_id})
    if not incentive:
        return failure("Incentive not found", status=404)
    if incentive.get("payment_confirmation_date"):
        return success(recompute_incentive_totals(store, incentive_id) or incentive, "Payment already confirmed")
    payload = request.get_json(silent=True) or {}
    payment_id = str(payload.get("payment_id") or "").strip()
    payment = store.find_one("payments", {"_id": payment_id}) if payment_id else None
    if not payment or str(payment.get("order_id") or "") != str(incentive.get("order_id") or ""):
        return failure("Confirm the related customer payment first", status=409, error="payment_confirmation_required")
    if str(payment.get("status") or "").upper() != "CONFIRMED":
        return failure("Confirm the related customer payment first", status=409, error="payment_confirmation_required")
    confirmed_at = payment.get("confirmed_at") or utcnow()
    activated = activate_incentive(store, str(incentive.get("order_id")), confirmed_at)
    if not activated:
        return failure("Order Confirmation not found", status=404)
    row = store.update_one("incentives", {"_id": incentive_id}, {
        "payment_reference": str(payload.get("payment_reference") or payload.get("reference") or payment.get("utr") or payment.get("reference_number") or "").strip()[:160] or None,
        "payment_confirmed_by_user_id": (current_user() or {}).get("_id"),
        "payment_confirmed_at": confirmed_at,
    }) or activated
    row = recompute_incentive_totals(store, incentive_id) or row
    audit("incentive.activated", "incentive", incentive_id, {"order_id": incentive.get("order_id"), "payment_reference": row.get("payment_reference")})
    audit("incentive.payment_confirmed", "incentive", incentive_id, {"order_id": incentive.get("order_id")})
    return success(row, "Payment confirmed and incentive activated")


@bp.get("/credit-notes")
@permission_required_any("credit_notes.view", "credit_notes.manage")
def list_credit_notes():
    store = _store()
    user = current_user() or {}
    rows, total = store.list("credit_notes", {}, limit=min(int(request.args.get("limit", 100)), 500))
    if str(user.get("role_id") or "") != "superadmin":
        rows = [row for row in rows if (_order(str(row.get("order_id") or row.get("oc_id") or "")) and _can_access_order(_order(str(row.get("order_id") or row.get("oc_id") or "")), user))]
        total = len(rows)
    return success({"items": rows, "total": total})


@bp.post("/credit-notes")
@permission_required_any("credit_notes.create", "credit_notes.manage")
def create_credit_note():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("oc_id") or "").strip()
    order = _order(order_id)
    if not order:
        return failure("Order Confirmation not found", status=404)
    if not _can_access_order(order):
        return failure("Customer access denied", status=403)
    if order.get("financial_locked") and not _superadmin():
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    amount = money(payload.get("amount", payload.get("credit_note_amount")))
    note_date = _parse_date(payload.get("credit_note_date"))
    if note_date is None or (amount <= 0 and not isinstance(payload.get("lines"), list)):
        return failure("Credit Note amount and date are required", status=422)
    incentive = _store().find_one("incentives", {"order_id": order_id})
    if not incentive:
        return failure("No incentive exists for this Order Confirmation", status=409)
    incentive_lines = incentive.get("incentive_lines") or []
    requested_lines = payload.get("lines")
    note_lines: list[dict] = []
    rate = None
    if requested_lines is not None:
        if not isinstance(requested_lines, list) or not requested_lines:
            return failure("Credit Note product lines are required", status=422, error="invalid_credit_note_lines")
        for requested in requested_lines:
            if not isinstance(requested, dict):
                return failure("Each Credit Note line must be an object", status=422, error="invalid_credit_note_lines")
            requested_line_id = str(requested.get("oc_line_id") or "").strip()
            product_id = str(requested.get("product_id") or "").strip()
            source = next((line for line in incentive_lines if requested_line_id and str(line.get("oc_line_id") or "") == requested_line_id), None)
            if source is None and product_id:
                source = next((line for line in incentive_lines if str(line.get("product_id") or "") == product_id), None)
            if not source:
                return failure("Credit Note line must reference a product on the Order Confirmation", status=422, error="credit_note_product_not_found")
            line_amount = money(requested.get("amount", requested.get("credit_note_amount")))
            if line_amount <= 0:
                return failure("Credit Note line amounts must be positive", status=422, error="invalid_credit_note_lines")
            line_rate = float(source.get("incentive_rate_snapshot") or 0)
            note_lines.append({
                "product_id": source.get("product_id"), "oc_line_id": source.get("oc_line_id"),
                "product_name": source.get("product_name"), "category_id": source.get("category_id"), "category_name": source.get("category_name"), "amount": line_amount,
                "incentive_rate_snapshot": line_rate, "incentive_deduction_amount": money(line_amount * line_rate / 100),
            })
    else:
        # Compatibility for older notes that were entered as one OC total.
        if len({float(line.get("incentive_rate_snapshot") or 0) for line in incentive_lines}) > 1:
            return failure("Product-level Credit Note lines are required for a multi-rate Order Confirmation", status=422, error="credit_note_lines_required")
        rate = float(incentive.get("incentive_percentage_snapshot") or 0)
        note_lines = [{"product_id": None, "oc_line_id": None, "product_name": "Order Confirmation", "category_id": None, "category_name": None, "amount": amount, "incentive_rate_snapshot": rate, "incentive_deduction_amount": money(amount * rate / 100)}]
    if requested_lines is not None:
        amount = money(sum(money(line.get("amount")) for line in note_lines))
    if rate is None:
        rates = {float(line.get("incentive_rate_snapshot") or 0) for line in note_lines}
        rate = next(iter(rates)) if len(rates) == 1 else None
    deduction = money(sum(money(line.get("incentive_deduction_amount")) for line in note_lines))
    now = utcnow()
    sequence = _store().next_counter("credit_note")
    row = _store().insert_one("credit_notes", {
        "credit_note_number": f"CN-{sequence:05d}", "order_id": order_id, "oc_id": order_id, "quotation_id": order.get("quotation_id"),
        "customer_id": order.get("customer_id"), "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or {},
        "amount": amount, "credit_note_amount": amount, "currency": "EUR", "credit_note_date": note_date,
        "reason": str(payload.get("reason") or "").strip()[:2000], "incentive_percentage_snapshot": rate,
        "incentive_deduction_amount": deduction, "lines": note_lines, "status": "APPROVED", "created_by_user_id": (current_user() or {}).get("_id"),
        "created_at": now, "audit": [{"action": "created", "by": (current_user() or {}).get("_id"), "at": now}],
    })
    for line in note_lines:
        adjustment = _store().insert_one("incentive_adjustments", {
            "credit_note_id": row["_id"], "incentive_id": incentive["_id"], "order_id": order_id,
            "product_id": line.get("product_id"), "oc_line_id": line.get("oc_line_id"),
            "category_id": line.get("category_id"), "category_name": line.get("category_name"),
            "amount": line.get("amount", 0), "incentive_rate_snapshot": line.get("incentive_rate_snapshot", 0),
            "deduction_amount": line.get("incentive_deduction_amount", 0), "created_at": now,
        })
        audit("incentive.adjustment_created", "incentive_adjustment", str(adjustment.get("_id")), {
            "credit_note_id": row["_id"], "incentive_id": incentive["_id"],
            "product_id": line.get("product_id"), "category_id": line.get("category_id"), "deduction_amount": line.get("incentive_deduction_amount", 0),
        })
    notes, _ = _store().list("credit_notes", {"order_id": order_id, "status": {"$ne": "VOID"}}, limit=10000)
    total_deduction = money(sum(money(item.get("incentive_deduction_amount")) for item in notes))
    _store().update_one("incentives", {"_id": incentive["_id"]}, {"credit_note_deduction": total_deduction})
    recompute_incentive_totals(_store(), str(incentive["_id"]))
    audit("credit_note.create", "credit_note", str(row["_id"]), {"order_id": order_id, "deduction": deduction})
    return success(row, "Credit Note created", 201)


@bp.post("/credit-notes/<credit_note_id>/void")
@login_required
def void_credit_note(credit_note_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can void a Credit Note", status=403, error="superadmin_required")
    store = _store()
    note = store.find_one("credit_notes", {"_id": credit_note_id})
    if not note:
        return failure("Credit Note not found", status=404)
    if note.get("status") == "VOID":
        return success(note, "Credit Note already void")
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return failure("A reason is required", status=422, error="void_reason_required")
    row = store.update_one("credit_notes", {"_id": credit_note_id}, {"status": "VOID", "voided_by_user_id": (current_user() or {}).get("_id"), "voided_at": utcnow(), "void_reason": reason})
    incentive_id = None
    if note.get("order_id"):
        incentive = store.find_one("incentives", {"order_id": note.get("order_id")})
        if incentive:
            incentive_id = incentive.get("_id")
            notes, _ = store.list("credit_notes", {"order_id": note.get("order_id"), "status": {"$ne": "VOID"}}, limit=10_000)
            store.update_one("incentives", {"_id": incentive["_id"]}, {"credit_note_deduction": money(sum(money(item.get("incentive_deduction_amount")) for item in notes))})
            recompute_incentive_totals(store, str(incentive["_id"]))
    audit("credit_note.void", "credit_note", credit_note_id, {"reason": reason, "incentive_id": incentive_id})
    return success(row, "Credit Note voided")
