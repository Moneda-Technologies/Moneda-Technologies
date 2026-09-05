from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import current_user, enforce_active_customer, enforce_customer, permission_required
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("orders", __name__, url_prefix="/api")
STATUSES = {"Pending", "Confirmed", "Processing", "Completed", "Cancelled"}


@bp.get("/orders")
@permission_required("orders.view")
def list_orders():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    query: dict = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    if request.args.get("status"):
        query["status"] = request.args["status"]
    rows, total = current_app.extensions["store"].list("orders", query, page=max(int(request.args.get("page", 1)), 1), limit=min(int(request.args.get("limit", 25)), 100))
    return success({"items": rows, "total": total})


@bp.get("/orders/<order_id>")
@permission_required("orders.view")
def get_order(order_id: str):
    order = current_app.extensions["store"].find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    return success(order)


@bp.post("/quotations/<quotation_id>/convert-to-order")
@permission_required("orders.create")
def convert_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    if not quotation:
        return failure("Quotation not found", status=404)
    quotation_customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    if not enforce_active_customer(quotation_customer_id):
        return failure("Customer access denied", status=403)
    user = current_user() or {}
    idempotency_key = str(request.headers.get("Idempotency-Key") or (request.get_json(silent=True) or {}).get("idempotency_key") or "").strip()[:160]
    if idempotency_key:
        existing_by_key = store.find_one("orders", {"customer_id": quotation_customer_id, "idempotency_key": idempotency_key})
        if existing_by_key:
            return success(existing_by_key, "Order already created")
    existing = store.find_one("orders", {"quotation_id": quotation_id})
    if existing:
        return failure("Quotation is already linked to an order", status=409)
    sequence = store.next_counter("order")
    now = utcnow()
    lead = store.find_one("leads", {"quotation_id": quotation_id, "$or": [{"customer_id": quotation_customer_id}, {"customer_company_id": quotation_customer_id}, {"company_id": quotation_customer_id}]})
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    issuer = {**(settings.get("issuer") or {}), **(quotation.get("issuer_snapshot") or {})}
    issuer["name"] = "Moneda Technologies"
    issuer["email"] = issuer.get("email") or "business@monedatechnologies.com"
    order_document = {
        "order_number": f"MON_ORD{sequence:05d}", "quotation_id": quotation_id,
        "quotation_number": quotation.get("quotation_number"),
        "lead_id": lead.get("_id") if lead else None,
        "customer_id": quotation_customer_id, "customer_company_id": quotation_customer_id, "company_id": quotation_customer_id,
        "issuer_name": "Moneda Technologies", "issuer_snapshot": issuer,
        "customer_company_snapshot": quotation.get("customer_company_snapshot") or quotation.get("company_snapshot"),
        "company_snapshot": quotation.get("company_snapshot"), "customer_snapshot": quotation.get("customer_snapshot"),
        "salesperson_id": quotation["salesperson_id"], "prepared_by_user_id": quotation.get("prepared_by_user_id") or quotation["salesperson_id"], "created_by_user_id": user.get("_id"), "salesperson_snapshot": quotation.get("salesperson_snapshot"),
        "quotation_snapshot": quotation, "products_snapshot": quotation["lines"], "lines": quotation["lines"],
        "master_currency": quotation.get("master_currency", "EUR"), "currency": quotation["currency"],
        "exchange_rate": quotation.get("exchange_rate"), "exchange_rate_meta": quotation.get("exchange_rate_meta"),
        "totals": quotation["totals"], "payment_terms": quotation.get("payment_terms"), "status": "Pending",
        "notes": (request.get_json(silent=True) or {}).get("notes", ""),
        "history": [{"status": "Pending", "at": now, "by": (current_user() or {}).get("_id")}],
    }
    if idempotency_key:
        order_document["idempotency_key"] = idempotency_key
    order = store.insert_one("orders", order_document)
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
    return success(order, "Order created", 201)


@bp.patch("/orders/<order_id>")
@permission_required("orders.update")
def update_order(order_id: str):
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not enforce_customer(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id")):
        return failure("Customer access denied", status=403)
    payload = request.get_json(silent=True) or {}
    if payload.get("status") not in STATUSES:
        return failure("Invalid order status", status=422)
    history = [*order.get("history", []), {"status": payload["status"], "at": utcnow(), "by": (current_user() or {}).get("_id")}]
    updated = store.update_one("orders", {"_id": order_id}, {"status": payload["status"], "notes": payload.get("notes", order.get("notes", "")), "history": history})
    audit("order.update", "order", order_id, {"status": payload["status"]})
    return success(updated, "Order updated")
