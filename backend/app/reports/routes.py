from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import datetime

from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.middleware.access import can_view_all_customers, current_user, enforce_customer, permission_required, permitted_customer_query, selected_customer_id
from app.exchange_rates.service import ExchangeRateUnavailable


bp = Blueprint("reports", __name__, url_prefix="/api")

REPORT_DEFINITIONS = {
    "sales-performance": ("Sales performance", "Revenue and order activity from recorded orders."),
    "quotation-analysis": ("Quotation analysis", "Quotation statuses, values and order conversion."),
    "customer-growth": ("Customer growth", "Customer creation activity within your authorized scope."),
    "product-demand": ("Product demand", "Quantities requested across recorded quotation lines."),
    "tax-summary": ("Tax summary", "Tax totals recorded on quotations, grouped without double counting."),
    "currency-exposure": ("Currency exposure", "Recorded quotation values and configured EUR reference rates."),
}


def _accessible_customers(store, user: dict | None, *, include_archived: bool = False) -> list[dict]:
    """Return the report customer scope from authorization, never session state."""
    if include_archived:
        if can_view_all_customers(user):
            query = {}
        else:
            user_id = str((user or {}).get("_id") or "")
            query = {"$or": [{"assigned_user_ids": user_id}, {"created_by_user_id": user_id}]} if user_id else {"_id": "__no_customer_access__"}
    else:
        query = permitted_customer_query(user)
    rows, _ = store.list("customers", query, limit=100_000, sort="name", direction=1)
    return [row for row in rows if row.get("_id") and not row.get("is_issuer")]


def _report_scope(store, user: dict | None, requested_customer_id: str | None) -> tuple[list[dict], dict | None]:
    customers = _accessible_customers(store, user)
    selected = None
    if requested_customer_id:
        selected = next((row for row in customers if str(row.get("_id")) == str(requested_customer_id)), None)
        if selected is None:
            return customers, None
        customers = [selected]
    customer_ids = [str(row["_id"]) for row in customers]
    if not customer_ids:
        return customers, {"_id": "__no_customer_access__"}
    return customers, {"$or": [{"customer_id": {"$in": customer_ids}}, {"customer_company_id": {"$in": customer_ids}}, {"company_id": {"$in": customer_ids}}]}


def _number(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _date(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _user_name(row: dict) -> str:
    snapshot = row.get("salesperson_snapshot") or row.get("creator_snapshot") or {}
    if isinstance(snapshot, dict) and snapshot.get("name"):
        return str(snapshot["name"])
    return str(row.get("salesperson_id") or row.get("prepared_by_user_id") or "Unassigned")


def _detail_scope_payload(customers: list[dict], requested_customer_id: str | None) -> dict:
    return {"customer_count": len(customers), "customer_filter": requested_customer_id or None}


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
    store = current_app.extensions["store"]
    customers, scope = _report_scope(store, current_user(), customer_id)
    if customer_id and not customers:
        return failure("Customer access denied", status=403)
    if customer_id and scope is None:
        return failure("Customer access denied", status=403)
    scope = scope or {"_id": "__no_customer_access__"}
    return success({
        "customers": len(customers),
        "customer_options": [{"_id": str(row["_id"]), "name": row.get("company_name") or row.get("name") or "Customer"} for row in _accessible_customers(store, current_user())],
        "quotations": store.count("quotations", scope),
        "orders": store.count("orders", scope),
        "leads": store.count("leads", scope),
    })


@bp.get("/reports/quotations.csv")
@permission_required("reports.view")
def quotations_csv():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    store = current_app.extensions["store"]
    customers, scope = _report_scope(store, current_user(), customer_id)
    if customer_id and (not customers or scope is None):
        return failure("Customer access denied", status=403)
    rows, _ = store.list("quotations", scope or {"_id": "__no_customer_access__"}, limit=10000)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Quotation", "Customer Company", "Status", "Currency", "Subtotal", "Total", "Created"])
    for row in rows:
        writer.writerow([row.get("quotation_number"), (row.get("customer_company_snapshot") or row.get("company_snapshot") or row.get("customer_snapshot") or {}).get("name"), row.get("status"), row.get("currency"), row.get("totals", {}).get("subtotal"), row.get("totals", {}).get("grand_total"), row.get("created_at")])
    return Response(buffer.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=moneda-quotations.csv"})


@bp.get("/reports/detail/<report_name>")
@permission_required("reports.view")
def report_detail(report_name: str):
    """Return one report's read-only aggregation over authorized business data."""
    definition = REPORT_DEFINITIONS.get(report_name)
    if not definition:
        return failure("Report not found", status=404)
    requested_customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    store = current_app.extensions["store"]
    customers, scope = _report_scope(store, current_user(), requested_customer_id)
    if requested_customer_id and (not customers or scope is None):
        return failure("Customer access denied", status=403)
    scope = scope or {"_id": "__no_customer_access__"}
    metrics: list[dict[str, object]] = []
    rows: list[dict[str, object]] = []
    rates: dict[str, object] | None = None
    empty_message = "No matching records are available for this report."

    if report_name == "sales-performance":
        orders, _ = store.list("orders", scope, limit=100_000, sort="created_at", direction=-1)
        quotes, _ = store.list("quotations", scope, limit=100_000)
        valid_orders = [row for row in orders if str(row.get("status", "")).lower() not in {"cancelled", "canceled"}]
        revenue = sum(_number((row.get("totals") or {}).get("grand_total")) for row in valid_orders)
        currencies = {str(row.get("currency") or row.get("quotation_currency") or "EUR").upper() for row in valid_orders}
        converted = sum(1 for row in quotes if str(row.get("status", "")).lower() in {"converted to order", "accepted"})
        metrics = [{"label": "Revenue", "value": revenue if len(currencies) <= 1 else "Multiple currencies", "format": "money" if len(currencies) <= 1 else None}, {"label": "Orders", "value": len(valid_orders)}, {"label": "Quotation conversion", "value": round(converted / len(quotes) * 100, 1) if quotes else 0, "format": "percent"}]
        by_salesperson: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"orders": 0, "revenue": 0})
        for order in valid_orders:
            person = _user_name(order)
            currency = str(order.get("currency") or order.get("quotation_currency") or "EUR").upper()
            by_salesperson[(person, currency)]["orders"] += 1
            by_salesperson[(person, currency)]["revenue"] += _number((order.get("totals") or {}).get("grand_total"))
        rows = [{"salesperson": name, "currency": currency, "orders": int(value["orders"]), "revenue": value["revenue"]} for (name, currency), value in sorted(by_salesperson.items(), key=lambda item: item[1]["revenue"], reverse=True)]
        empty_message = "No completed order data is available yet."

    elif report_name == "quotation-analysis":
        quotations, _ = store.list("quotations", scope, limit=100_000, sort="created_at", direction=-1)
        status_totals: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"count": 0, "value": 0})
        for quote in quotations:
            status = str(quote.get("status") or "Unknown")
            currency = str(quote.get("currency") or quote.get("quotation_currency") or "EUR").upper()
            status_totals[(status, currency)]["count"] += 1
            status_totals[(status, currency)]["value"] += _number((quote.get("totals") or {}).get("grand_total"))
        total_value = sum(item["value"] for item in status_totals.values())
        converted = sum(item["count"] for (status, _currency), item in status_totals.items() if status.lower() in {"converted to order", "accepted"})
        quote_currencies = {currency for (_status, currency) in status_totals}
        metrics = [{"label": "Quotations", "value": len(quotations)}, {"label": "Quoted value", "value": total_value if len(quote_currencies) <= 1 else "Multiple currencies", "format": "money" if len(quote_currencies) <= 1 else None}, {"label": "Conversion", "value": round(converted / len(quotations) * 100, 1) if quotations else 0, "format": "percent"}]
        rows = [{"status": status, "currency": currency, "count": int(item["count"]), "value": item["value"]} for (status, currency), item in sorted(status_totals.items())]
        empty_message = "No quotations match this scope."

    elif report_name == "customer-growth":
        growth_customers = _accessible_customers(store, current_user(), include_archived=True)
        if requested_customer_id:
            growth_customers = [row for row in growth_customers if str(row.get("_id")) == str(requested_customer_id)]
        grouped: dict[str, dict[str, int]] = defaultdict(lambda: {"new_customers": 0, "active": 0, "archived": 0})
        for customer in growth_customers:
            created = _date(customer.get("created_at"))
            if not created:
                continue
            key = created.strftime("%Y-%m")
            grouped[key]["new_customers"] += 1
            if str(customer.get("status", "")).lower() == "archived" or customer.get("active") is False:
                grouped[key]["archived"] += 1
            else:
                grouped[key]["active"] += 1
        rows = [{"period": period, **values} for period, values in sorted(grouped.items(), reverse=True)]
        metrics = [{"label": "Customers in scope", "value": len(growth_customers)}, {"label": "Active", "value": sum(1 for row in growth_customers if row.get("active") is not False and str(row.get("status", "")).lower() != "archived")}, {"label": "Archived", "value": sum(1 for row in growth_customers if row.get("active") is False or str(row.get("status", "")).lower() == "archived")}]
        empty_message = "No customer creation dates are available for this scope."

    elif report_name == "product-demand":
        quotations, _ = store.list("quotations", scope, limit=100_000)
        products, _ = store.list("products", {}, limit=100_000)
        product_by_id = {str(row.get("_id")): row for row in products}
        aggregate: dict[str, dict[str, object]] = {}
        for quote in quotations:
            for line in quote.get("lines") or []:
                product_id = str(line.get("product_id") or line.get("sku") or line.get("product_name") or "Unknown")
                product = product_by_id.get(product_id, {})
                item = aggregate.setdefault(product_id, {"product": line.get("product_name") or product.get("name") or "Unknown product", "category": product.get("category_id") or line.get("category") or "Uncategorised", "quantity": 0, "quotations": 0})
                item["quantity"] = _number(item["quantity"]) + _number(line.get("requested_quantity") or line.get("quantity"))
                item["quotations"] = int(item["quotations"]) + 1
        rows = sorted(aggregate.values(), key=lambda item: _number(item.get("quantity")), reverse=True)
        metrics = [{"label": "Products", "value": len(rows)}, {"label": "Units requested", "value": sum(_number(row.get("quantity")) for row in rows)}, {"label": "Quotations", "value": len(quotations)}]
        empty_message = "No quotation line items are available for this scope."

    elif report_name == "tax-summary":
        quotations, _ = store.list("quotations", scope, limit=100_000)
        grouped: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"taxable": 0, "tax": 0, "documents": 0})
        for quote in quotations:
            totals = quote.get("totals") or {}
            tax = _number(totals.get("tax_amount", totals.get("gst_amount", 0)))
            taxable = _number(totals.get("taxable_amount", totals.get("taxable_subtotal", totals.get("subtotal", 0)))) if tax or totals.get("taxable_amount") is not None else 0
            if not tax and not taxable and not any(key in totals for key in ("tax_amount", "gst_amount", "taxable_amount", "taxable_subtotal")):
                continue
            rate = next((line.get("tax_rate") or line.get("gst_rate") for line in quote.get("lines") or [] if line.get("tax_rate") or line.get("gst_rate")), None)
            mode = next((line.get("tax_mode") for line in quote.get("lines") or [] if line.get("tax_mode")), None) or "recorded"
            rate_mode = f"{rate:g}% · {mode}" if isinstance(rate, (int, float)) else f"{rate or 'Unspecified'} · {mode}"
            currency = str(quote.get("currency") or quote.get("quotation_currency") or "EUR").upper()
            grouped[(rate_mode, currency)]["taxable"] += taxable
            grouped[(rate_mode, currency)]["tax"] += tax
            grouped[(rate_mode, currency)]["documents"] += 1
        rows = [{"rate_mode": rate_mode, "currency": currency, **value} for (rate_mode, currency), value in sorted(grouped.items())]
        tax_currencies = {str(row.get("currency") or "EUR") for row in rows}
        taxable_total = sum(_number(row.get("taxable")) for row in rows)
        tax_total = sum(_number(row.get("tax")) for row in rows)
        metrics = [{"label": "Taxable amount", "value": taxable_total if len(tax_currencies) <= 1 else "Multiple currencies", "format": "money" if len(tax_currencies) <= 1 else None}, {"label": "Tax recorded", "value": tax_total if len(tax_currencies) <= 1 else "Multiple currencies", "format": "money" if len(tax_currencies) <= 1 else None}, {"label": "Tax groups", "value": len(rows)}]
        empty_message = "No tax amounts are recorded in the matching quotations."

    elif report_name == "currency-exposure":
        quotations, _ = store.list("quotations", scope, limit=100_000)
        grouped: dict[str, float] = defaultdict(float)
        for quote in quotations:
            grouped[str(quote.get("currency") or quote.get("quotation_currency") or "EUR").upper()] += _number((quote.get("totals") or {}).get("grand_total"))
        try:
            rates = current_app.extensions["exchange_rate_service"].get_rates(force=False)
        except ExchangeRateUnavailable:
            rates = None
        rows = [{"currency": currency, "amount": amount} for currency, amount in sorted(grouped.items())]
        metrics = [{"label": "Currencies", "value": len(rows)}, {"label": "Documents", "value": len(quotations)}, {"label": "Rates", "value": "Available" if rates else "Unavailable"}]
        empty_message = "No quotation currency values are available for this scope."

    return success({"report": report_name, "title": definition[0], "description": definition[1], "scope": _detail_scope_payload(customers, requested_customer_id), "customer_options": [{"_id": str(row["_id"]), "name": row.get("company_name") or row.get("name") or "Customer"} for row in _accessible_customers(store, current_user())], "metrics": metrics, "rows": rows, "rates": rates, "empty_message": empty_message})


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
