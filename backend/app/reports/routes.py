from __future__ import annotations

import csv
import io
import re

from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.middleware.access import can_view_all_customers, current_user, enforce_customer, permission_required, permitted_customer_query, selected_customer_id


bp = Blueprint("reports", __name__, url_prefix="/api")


@bp.get("/dashboard")
@permission_required("dashboard.view")
def dashboard():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id") or selected_customer_id()
    store = current_app.extensions["store"]
    if customer_id:
        if not enforce_customer(customer_id):
            return failure("Customer access denied", status=403)
        scope = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
        customer_scope = {"_id": customer_id}
    else:
        user = current_user() or {}
        if can_view_all_customers(user):
            customer_scope = {"active": {"$ne": False}, "status": {"$ne": "archived"}}
        else:
            customer_scope = permitted_customer_query(user)
        customer_rows, _ = store.list("customers", customer_scope, limit=5000)
        ids = [row["_id"] for row in customer_rows if row.get("_id") and not row.get("is_issuer")]
        scope = {"$or": [{"customer_id": {"$in": ids}}, {"customer_company_id": {"$in": ids}}, {"company_id": {"$in": ids}}]} if ids else {"_id": "__no_customer_access__"}
    quotes, _ = store.list("quotations", scope, limit=10000)
    orders, _ = store.list("orders", scope, limit=10000)
    leads, _ = store.list("leads", scope, limit=10000)
    reminders, _ = store.list("reminders", {"$and": [scope, {"status": {"$in": ["Pending", "Due", "Overdue", "open"]}}]}, limit=10000)
    accepted = [row for row in quotes if row.get("status") in {"Accepted", "Converted to Order"}]
    revenue = sum(float(row.get("totals", {}).get("grand_total", 0)) for row in orders if row.get("status") != "Cancelled")
    status_counts: dict[str, int] = {}
    for row in quotes:
        status_counts[row.get("status", "Unknown")] = status_counts.get(row.get("status", "Unknown"), 0) + 1
    return success({
        "metrics": {"quotations": len(quotes), "open_quotations": len([q for q in quotes if q.get("status") in {"Draft", "Sent", "Viewed"}]),
                    "accepted_quotations": len(accepted), "orders": len(orders), "revenue": revenue,
                    "conversion_rate": round(len(accepted) / len(quotes) * 100, 1) if quotes else 0,
                    "leads": len(leads), "follow_ups_due": len(reminders)},
        "quotation_status": status_counts,
        "recent_quotations": quotes[:6], "recent_customers": [row for row in store.list("customers", customer_scope, limit=12, sort="created_at", direction=-1)[0] if not row.get("is_issuer")][:6],
    })


@bp.get("/reports/summary")
@permission_required("reports.view")
def report_summary():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    store = current_app.extensions["store"]
    return success({
        "customers": store.count("customers", {"_id": customer_id}),
        "quotations": store.count("quotations", {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}),
        "orders": store.count("orders", {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}),
        "leads": store.count("leads", {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}),
    })


@bp.get("/reports/quotations.csv")
@permission_required("reports.view")
def quotations_csv():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    rows, _ = current_app.extensions["store"].list("quotations", {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}, limit=10000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Quotation", "Customer Company", "Status", "Currency", "Subtotal", "Total", "Created"])
    for row in rows:
        writer.writerow([row.get("quotation_number"), (row.get("customer_company_snapshot") or row.get("company_snapshot") or row.get("customer_snapshot") or {}).get("name"), row.get("status"), row.get("currency"), row.get("totals", {}).get("subtotal"), row.get("totals", {}).get("grand_total"), row.get("created_at")])
    return Response(buffer.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=moneda-quotations.csv"})


@bp.get("/search")
@permission_required("dashboard.view")
def global_search():
    term = request.args.get("q", "").strip()
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if len(term) < 2:
        return success([])
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    store = current_app.extensions["store"]
    results = []
    for collection, field, label in (("products", "name", "Product"), ("customers", "name", "Customer"), ("quotations", "quotation_number", "Quotation"), ("orders", "order_number", "Order"), ("leads", "notes", "Lead")):
        query: dict = {field: {"$regex": re.escape(term[:100])}}
        if collection != "products":
            query["$or"] = [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]
        rows, _ = store.list(collection, query, limit=5)
        for row in rows:
            results.append({"type": label, "id": row["_id"], "title": row.get(field) or row.get("name") or label})
    return success(results[:20])
