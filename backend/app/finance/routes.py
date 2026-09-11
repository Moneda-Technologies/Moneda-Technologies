from __future__ import annotations

from datetime import datetime, timezone
from email_validator import EmailNotValidError, validate_email
from html import escape
from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.finance.service import activate_incentive, money, recompute_incentive_totals
from app.middleware.access import can_view_all_customers, current_user, customer_access_ids_for_user, enforce_customer, permission_required, permission_required_any
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


def _payment_recipients(order: dict, payment: dict | None = None) -> list[str]:
    values: list[str] = []
    customer = order.get("customer_snapshot") or order.get("customer_company_snapshot") or {}
    for value in (customer.get("email"), (order.get("salesperson_snapshot") or {}).get("email")):
        email = _email(value)
        if email and email not in values:
            values.append(email)
    return values


@bp.get("/payments")
@permission_required_any("payments.view", "payments.manage")
def list_payments():
    store = _store()
    user = current_user() or {}
    query: dict = {}
    if request.args.get("status"):
        query["status"] = request.args["status"]
    if request.args.get("order_id"):
        query["order_id"] = request.args["order_id"]
    if not can_view_all_customers(user):
        rows, _ = store.list("payments", query, limit=100_000)
        allowed = {str(row.get("customer_id")) for row in store.list("customers", {"assigned_user_ids": user.get("_id"), "active": {"$ne": False}}, limit=100_000)[0]}
        rows = [row for row in rows if str(row.get("customer_id")) in allowed or row.get("created_by_user_id") == user.get("_id")]
        return success({"items": rows, "total": len(rows)})
    rows, total = store.list("payments", query, limit=min(int(request.args.get("limit", 100)), 500))
    return success({"items": rows, "total": total})


@bp.post("/payments")
@permission_required_any("payments.create", "payments.manage")
def create_payment():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("oc_id") or "").strip()
    order = _order(order_id)
    if not order:
        return failure("Order Confirmation not found", status=404, error="order_not_found")
    customer_id = str(order.get("customer_id") or "")
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    amount = money(payload.get("payment_amount", payload.get("amount")))
    payment_date = _parse_date(payload.get("payment_date"))
    if amount <= 0 or payment_date is None:
        return failure("Payment amount and date are required", status=422, error="invalid_payment")
    now = utcnow()
    row = _store().insert_one("payments", {
        "order_id": order_id, "oc_id": order_id, "quotation_id": order.get("quotation_id"),
        "customer_id": customer_id, "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or {},
        "amount": amount, "payment_amount": amount, "currency": "EUR", "payment_date": payment_date,
        "bank_name": str(payload.get("bank_name") or "").strip()[:160], "bank_account": str(payload.get("bank_account") or "").strip()[:160],
        "utr": str(payload.get("utr") or payload.get("transaction_reference") or "").strip()[:160],
        "payment_mode": str(payload.get("payment_mode") or "").strip()[:80], "reference_number": str(payload.get("reference_number") or "").strip()[:160],
        "notes": str(payload.get("notes") or "").strip()[:2000], "attachment": payload.get("attachment"),
        "status": "PAYMENT RECORDED", "created_by_user_id": (current_user() or {}).get("_id"),
        "audit": [{"action": "created", "by": (current_user() or {}).get("_id"), "at": now}],
    })
    audit("payment.create", "payment", str(row["_id"]), {"order_id": order_id, "amount": amount})
    return success(row, "Payment recorded", 201)


@bp.post("/payments/<payment_id>/submit")
@permission_required_any("payments.create", "payments.manage")
def submit_payment(payment_id: str):
    payment = _store().find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    if payment.get("status") not in {"PAYMENT RECORDED", "REJECTED"}:
        return failure("Payment cannot be submitted in its current state", status=409)
    now = utcnow()
    row = _store().update_one("payments", {"_id": payment_id}, {"status": "AWAITING BANK CONFIRMATION", "submitted_at": now, "submitted_by_user_id": (current_user() or {}).get("_id"), "audit": [*payment.get("audit", []), {"action": "submitted", "by": (current_user() or {}).get("_id"), "at": now}]})
    audit("payment.submit", "payment", payment_id, {})
    return success(row, "Payment submitted for bank confirmation")


@bp.post("/payments/<payment_id>/confirm")
@permission_required("payments.confirm")
def confirm_payment(payment_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can confirm bank receipt", status=403, error="superadmin_required")
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    if payment.get("status") not in {"AWAITING BANK CONFIRMATION", "PAYMENT RECORDED"}:
        return failure("Payment is not awaiting confirmation", status=409)
    confirmed_at = utcnow()
    actor = current_user() or {}
    row = store.update_one("payments", {"_id": payment_id}, {"status": "CONFIRMED", "confirmed_by_user_id": actor.get("_id"), "confirmed_at": confirmed_at, "financial_locked": True, "audit": [*payment.get("audit", []), {"action": "confirmed", "by": actor.get("_id"), "at": confirmed_at}]})
    incentive = activate_incentive(store, str(payment.get("order_id")), confirmed_at)
    order = _order(str(payment.get("order_id"))) or {}
    store.update_one("orders", {"_id": payment.get("order_id")}, {"financial_locked": True, "payment_status": "CONFIRMED"})
    recipients = _payment_recipients(order, payment)
    email_result = None
    if recipients:
        try:
            email_result = current_app.extensions["email_service"].send(
                purpose="order", to=recipients,
                subject=f"Payment confirmation {order.get('order_number', order.get('_id', ''))} - Moneda Technologies",
                html=f"<p>Payment of <strong>EUR {money(payment.get('amount')):.2f}</strong> has been confirmed for Order Confirmation <strong>{escape(str(order.get('order_number') or order.get('_id') or ''))}</strong>.</p><p>Payment date: {escape(str(payment.get('payment_date') or ''))}<br>UTR/reference: {escape(str(payment.get('utr') or payment.get('reference_number') or 'Not provided'))}</p>",
                customer_facing=True, request_id=f"payment-confirmed-{payment_id}",
            )
        except Exception:
            current_app.logger.exception("payment confirmation email failed payment_id=%s", payment_id)
    store.insert_one("email_logs", {"payment_id": payment_id, "order_id": payment.get("order_id"), "message_type": "payment_confirmation", "status": "sent" if email_result else "not_sent", "created_at": utcnow()})
    audit("payment.confirm", "payment", payment_id, {"order_id": payment.get("order_id"), "incentive_id": incentive.get("_id") if incentive else None})
    return success({"payment": row, "incentive": incentive}, "Payment confirmed")


@bp.post("/payments/<payment_id>/reject")
@permission_required("payments.confirm")
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
@permission_required_any("incentives.view", "incentives.manage")
def list_incentives():
    store = _store()
    user = current_user() or {}
    query: dict = {}
    global_scope = can_view_all_customers(user) or user.get("role_id") == "manager_sales_admin"
    allowed_customer_ids: list[str] | None = None
    if not global_scope:
        query["salesperson_id"] = user.get("_id")
        allowed_customer_ids = customer_access_ids_for_user(str(user.get("_id") or ""))
        query["customer_id"] = {"$in": allowed_customer_ids}
    for key in ("status", "salesperson_id", "customer_id", "order_id"):
        if request.args.get(key):
            if key == "customer_id" and allowed_customer_ids is not None and request.args[key] not in allowed_customer_ids:
                return success({"items": [], "total": 0})
            query[key] = request.args[key]
    # Search and payment/date filters are applied to the server-authorized
    # result set below.  Fetching the bounded management list first avoids
    # allowing a client-supplied filter to bypass the scope query above.
    rows, _ = store.list("incentives", query, limit=min(int(request.args.get("limit", 500)), 500))
    search = str(request.args.get("search") or "").strip().casefold()
    payment_filter = str(request.args.get("payment_status") or "").strip().casefold()
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
        payments, _ = store.list("payments", {"order_id": row.get("order_id")}, limit=100)
        payment_status = "Confirmed" if any(str(payment.get("status") or "").upper() == "CONFIRMED" for payment in payments) else "Pending"
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
        if payment_filter and payment_filter not in {payment_status.casefold(), "paid" if payment_status == "Confirmed" else "unpaid"}:
            continue
        created_at = row.get("created_at")
        if from_date and (not created_at or created_at < from_date):
            continue
        if to_date and (not created_at or created_at >= to_date):
            continue
        filtered.append(row)
    return success({"items": filtered, "total": len(filtered)})


@bp.get("/incentives/<incentive_id>")
@permission_required_any("incentives.view", "incentives.manage")
def get_incentive(incentive_id: str):
    store = _store()
    row = store.find_one("incentives", {"_id": incentive_id})
    if not row:
        return failure("Incentive not found", status=404)
    user = current_user() or {}
    global_scope = can_view_all_customers(user) or user.get("role_id") == "manager_sales_admin"
    if not global_scope:
        if str(row.get("salesperson_id") or "") != str(user.get("_id") or "") or not enforce_customer(str(row.get("customer_id") or "")):
            return failure("Incentive not found", status=404)
    return success(recompute_incentive_totals(store, incentive_id) or row)


@bp.post("/incentives/<incentive_id>/pay")
@permission_required("incentives.manage")
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


@bp.get("/credit-notes")
@permission_required_any("credit_notes.view", "credit_notes.manage")
def list_credit_notes():
    rows, total = _store().list("credit_notes", {}, limit=min(int(request.args.get("limit", 100)), 500))
    return success({"items": rows, "total": total})


@bp.post("/credit-notes")
@permission_required_any("credit_notes.create", "credit_notes.manage")
def create_credit_note():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("oc_id") or "").strip()
    order = _order(order_id)
    if not order:
        return failure("Order Confirmation not found", status=404)
    if not enforce_customer(str(order.get("customer_id") or "")):
        return failure("Customer access denied", status=403)
    if order.get("financial_locked") and not _superadmin():
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    amount = money(payload.get("amount", payload.get("credit_note_amount")))
    note_date = _parse_date(payload.get("credit_note_date"))
    if amount <= 0 or note_date is None:
        return failure("Credit Note amount and date are required", status=422)
    incentive = _store().find_one("incentives", {"order_id": order_id})
    if not incentive:
        return failure("No incentive exists for this Order Confirmation", status=409)
    rate = float(incentive.get("incentive_percentage_snapshot") or 0)
    deduction = money(amount * rate / 100)
    now = utcnow()
    sequence = _store().next_counter("credit_note")
    row = _store().insert_one("credit_notes", {
        "credit_note_number": f"CN-{sequence:05d}", "order_id": order_id, "oc_id": order_id, "quotation_id": order.get("quotation_id"),
        "customer_id": order.get("customer_id"), "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or {},
        "amount": amount, "credit_note_amount": amount, "currency": "EUR", "credit_note_date": note_date,
        "reason": str(payload.get("reason") or "").strip()[:2000], "incentive_percentage_snapshot": rate,
        "incentive_deduction_amount": deduction, "status": "APPROVED", "created_by_user_id": (current_user() or {}).get("_id"),
        "created_at": now, "audit": [{"action": "created", "by": (current_user() or {}).get("_id"), "at": now}],
    })
    notes, _ = _store().list("credit_notes", {"order_id": order_id, "status": {"$ne": "VOID"}}, limit=10000)
    total_deduction = money(sum(money(item.get("incentive_deduction_amount")) for item in notes))
    _store().update_one("incentives", {"_id": incentive["_id"]}, {"credit_note_deduction": total_deduction})
    recompute_incentive_totals(_store(), str(incentive["_id"]))
    audit("credit_note.create", "credit_note", str(row["_id"]), {"order_id": order_id, "deduction": deduction})
    return success(row, "Credit Note created", 201)
