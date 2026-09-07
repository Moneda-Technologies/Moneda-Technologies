from __future__ import annotations

import re

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import customer_record, current_user, enforce_customer, permission_required, permitted_customer_query
from app.services.audit import audit
from app.customers.codes import available_customer_code, customer_code
from app.customers.metadata import customer_gst_applicable, normalize_customer_profile, validation_message


bp = Blueprint("customers", __name__, url_prefix="/api/customers")

FIELDS = {
    "name", "company_name", "contact_name", "email", "phone", "alternate_phone", "legal_name",
    "address", "billing_address", "shipping_address", "country", "state", "city", "postal_code",
    "tax_number", "gst_vat_number", "payment_terms", "preferred_currency", "default_currency",
    "default_tax_rate", "default_tax_mode", "tax_enabled", "assigned_salesperson", "credit_limit",
    "notes", "status", "active", "continent", "country_code", "country_name", "region",
    "tax_profile", "custom_payment_days", "payment_terms_display",
}


def _view(row: dict) -> dict:
    customer = {**row}
    customer.setdefault("customer_id", customer.get("_id"))
    customer.setdefault("company_name", customer.get("name"))
    customer.setdefault("customer_code", customer_code(customer.get("name", "Customer")))
    preferred_currency = customer.get("preferred_currency") or customer.get("default_currency") or "EUR"
    customer["preferred_currency"] = preferred_currency
    customer["default_currency"] = preferred_currency
    customer.setdefault("default_tax_rate", 0)
    customer.setdefault("default_tax_mode", "no_tax")
    customer.setdefault("tax_enabled", False)
    customer["gst_applicable"] = customer_gst_applicable(customer)
    customer.setdefault("active", customer.get("status", "active") != "archived")
    return customer


def _permitted_query() -> dict:
    return permitted_customer_query()


@bp.get("")
@permission_required("customers.view")
def list_customers():
    store = current_app.extensions["store"]
    requested = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if requested:
        if not enforce_customer(requested):
            return failure("Customer access denied", status=403)
        row = customer_record(requested)
        if row and row.get("is_issuer"):
            row = None
        rows = [_view(row)] if row else []
    else:
        query = _permitted_query()
        term = request.args.get("search", "").strip()[:100]
        if term:
            query = {"$and": [query, {"$or": [{field: {"$regex": re.escape(term)}} for field in ("name", "company_name", "contact_name", "email", "phone")]}]}
        status = request.args.get("status")
        if status in {"active", "inactive", "archived"}:
            query["status"] = status
        page = max(int(request.args.get("page", 1)), 1)
        limit = min(max(int(request.args.get("limit", 25)), 1), 100)
        found, total = store.list("customers", query, page=page, limit=limit, sort="name", direction=1)
        rows = [_view(row) for row in found if not row.get("is_issuer")]
        return success({"items": rows, "pagination": {"page": page, "limit": limit, "total": total}})
    return success({"items": rows, "pagination": {"page": 1, "limit": len(rows), "total": len(rows)}})


@bp.post("")
@permission_required("customers.create")
def create_customer():
    raw = request.get_json(silent=True) or {}
    payload = {key: value for key, value in raw.items() if key in FIELDS}
    name = str(payload.get("name") or payload.get("company_name") or "").strip()
    if not name:
        return failure("Customer company name is required", status=422)
    payload["name"] = name
    payload["company_name"] = name
    normalized, error = normalize_customer_profile(payload, require_complete=True)
    if error:
        return failure(validation_message(error), error=error, status=422)
    payload = normalized or payload
    store = current_app.extensions["store"]
    payload["customer_code"] = available_customer_code(store, name)
    payload.setdefault("status", "active")
    payload.setdefault("active", payload["status"] != "archived")
    if payload["status"] not in {"active", "inactive", "archived"}:
        return failure("Invalid customer status", status=422)
    if payload.get("preferred_currency") and payload["preferred_currency"] not in {"EUR", "USD", "INR"}:
        return failure("Unsupported customer currency", status=422)
    payload.setdefault("preferred_currency", payload.get("default_currency", "EUR"))
    payload.setdefault("default_currency", payload["preferred_currency"])
    payload.setdefault("default_tax_rate", 0)
    payload.setdefault("default_tax_mode", "no_tax")
    payload.setdefault("tax_enabled", False)
    payload.setdefault("assigned_salesperson", (current_user() or {}).get("_id"))
    creator_id = (current_user() or {}).get("_id")
    payload["created_by_user_id"] = creator_id
    payload["assigned_user_ids"] = [creator_id] if creator_id else []
    row = store.insert_one("customers", payload)
    if row.get("customer_id") != row.get("_id"):
        row = current_app.extensions["store"].update_one("customers", {"_id": row["_id"]}, {"customer_id": row["_id"]}) or row
    audit("customer.create", "customer", str(row["_id"]))
    return success(_view(row), "Customer created", 201)


@bp.get("/<customer_id>")
@permission_required("customers.view")
def get_customer(customer_id: str):
    row = customer_record(customer_id)
    if not row:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    store = current_app.extensions["store"]
    related = {}
    for collection in ("quotations", "orders", "leads"):
        related[collection], _ = store.list(collection, {"customer_id": customer_id}, limit=10)
    return success({**_view(row), "related": related})


@bp.patch("/<customer_id>")
@permission_required("customers.update")
def update_customer(customer_id: str):
    store = current_app.extensions["store"]
    existing = customer_record(customer_id)
    if not existing:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in FIELDS}
    if "company_name" in changes and "name" not in changes:
        changes["name"] = str(changes["company_name"]).strip()
    if "name" in changes:
        changes["company_name"] = str(changes["name"]).strip()
        # Customer codes are stable identifiers and are not renamed with the display name.
    normalized, error = normalize_customer_profile(changes, existing)
    if error:
        return failure(validation_message(error), error=error, status=422)
    changes = normalized or changes
    if changes.get("status") and changes["status"] not in {"active", "inactive", "archived"}:
        return failure("Invalid customer status", status=422)
    if changes.get("preferred_currency") and changes["preferred_currency"] not in {"EUR", "USD", "INR"}:
        return failure("Unsupported customer currency", status=422)
    if "status" in changes and "active" not in changes:
        changes["active"] = changes["status"] != "archived"
    row = store.update_one("customers", {"_id": customer_id}, changes)
    if not row:
        return failure("Customer not found", status=404)
    audit("customer.update", "customer", customer_id, {"fields": sorted(changes)})
    return success(_view(row), "Customer updated")


@bp.delete("/<customer_id>")
@permission_required("customers.delete")
def archive_customer(customer_id: str):
    store = current_app.extensions["store"]
    if not customer_record(customer_id):
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    store.update_one("customers", {"_id": customer_id}, {"status": "archived", "active": False})
    audit("customer.archive", "customer", customer_id)
    return success(message="Customer archived")
