from __future__ import annotations

import re

from flask import Blueprint, current_app, request, session

from app.api.responses import failure, success
from app.customers.codes import available_customer_code, customer_code
from app.middleware.access import customer_record, enforce_customer, permission_required
from app.services.audit import audit


bp = Blueprint("companies", __name__, url_prefix="/api/companies")


def _customer_view(row: dict) -> dict:
    """Return one canonical customer record with compatibility display fields."""
    customer = {**row}
    customer.setdefault("customer_id", customer.get("_id"))
    customer.setdefault("company_name", customer.get("name"))
    customer.setdefault("customer_code", customer_code(customer.get("name", "Customer")))
    customer.setdefault("default_currency", customer.get("preferred_currency", "EUR"))
    customer.setdefault("default_tax_rate", 0)
    customer.setdefault("default_tax_mode", "no_tax")
    customer.setdefault("tax_enabled", False)
    customer.setdefault("active", customer.get("status", "active") != "archived")
    return customer


def _list_customers(term: str = "") -> tuple[list[dict], int]:
    store = current_app.extensions["store"]
    # Import lazily to keep this compatibility blueprint independent of auth
    # module initialization order.
    from app.middleware.access import current_user
    user = current_user() or {}
    query: dict = {"active": {"$ne": False}, "status": {"$ne": "archived"}}
    if user.get("role_id") != "superadmin":
        permitted = user.get("customer_ids") or user.get("customer_company_ids") or user.get("company_ids", [])
        query["_id"] = {"$in": permitted}
    if term:
        query["$or"] = [{field: {"$regex": re.escape(term)}} for field in ("name", "company_name", "contact_name", "email", "phone")]
    rows, total = store.list("customers", query, limit=500, sort="name", direction=1)
    return [_customer_view(row) for row in rows], total


@bp.get("")
@bp.get("/customer-companies")
@permission_required("companies.view")
def list_customer_companies():
    rows, total = _list_customers()
    return success({"items": rows, "total": total})


@bp.get("/search")
@bp.get("/customer-companies/search")
@permission_required("companies.view")
def search_customer_companies():
    rows, total = _list_customers(request.args.get("q", "").strip()[:100])
    return success({"items": rows, "total": total})


@bp.post("/select")
@bp.post("/select-customer")
@permission_required("companies.view")
def select_customer():
    payload = request.get_json(silent=True) or {}
    customer_id = str(payload.get("customer_id") or payload.get("customer_company_id") or payload.get("company_id") or "").strip()
    if not customer_id or not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    # Only active_customer_id is canonical. Legacy keys are mirrored for old clients.
    session["active_customer_id"] = customer_id
    session["selected_customer_company_id"] = customer_id
    session["active_company_id"] = customer_id
    audit("customer.select", "customer", customer_id)
    return success({"customer_id": customer_id, "customer_company_id": customer_id, "company_id": customer_id}, "Customer selected")


@bp.post("/clear-customer")
@permission_required("companies.view")
def clear_customer():
    """Clear the active customer before showing the selection workspace."""
    for key in ("active_customer_id", "selected_customer_company_id", "active_company_id"):
        session.pop(key, None)
    audit("customer.clear", "customer", None)
    return success({"customer_id": None, "customer_company_id": None, "company_id": None}, "Customer selection cleared")


@bp.post("")
@permission_required("companies.create")
def create_customer_compat():
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or payload.get("company_name") or "").strip()
    if not name:
        return failure("Customer company name is required", status=422)
    customer = {key: value for key, value in payload.items() if key not in {"_id", "company_id", "customer_company_id"}}
    customer["name"] = name
    customer["company_name"] = name
    customer.setdefault("customer_id", customer.get("_id"))
    customer.setdefault("preferred_currency", "EUR")
    customer.setdefault("default_currency", customer["preferred_currency"])
    customer.setdefault("default_tax_rate", 0)
    customer.setdefault("default_tax_mode", "no_tax")
    customer.setdefault("tax_enabled", False)
    customer.setdefault("status", "active")
    customer.setdefault("active", True)
    store = current_app.extensions["store"]
    customer["customer_code"] = available_customer_code(store, name)
    row = store.insert_one("customers", customer)
    if row.get("customer_id") != row.get("_id"):
        row = current_app.extensions["store"].update_one("customers", {"_id": row["_id"]}, {"customer_id": row["_id"]}) or row
    audit("customer.create", "customer", str(row["_id"]))
    return success(_customer_view(row), "Customer created", 201)


@bp.patch("/<company_id>")
@permission_required("companies.update")
def update_customer_compat(company_id: str):
    if not enforce_customer(company_id):
        return failure("Customer access denied", status=403)
    store = current_app.extensions["store"]
    collection = "customers" if store.find_one("customers", {"_id": company_id}) else "companies"
    allowed = {"name", "company_name", "legal_name", "logo", "address", "country", "state", "city", "postal_code", "phone", "email", "tax_number", "default_currency", "preferred_currency", "region", "timezone", "tax_jurisdiction", "tax_enabled", "default_tax_rate", "default_tax_mode", "transport_taxable", "quotation_settings", "communication_settings", "active", "status", "contact_name", "notes"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    if "name" in changes and "company_name" not in changes:
        changes["company_name"] = changes["name"]
    row = store.update_one(collection, {"_id": company_id}, changes)
    if not row:
        return failure("Customer not found", status=404)
    audit("customer.update", "customer", company_id, {"fields": sorted(changes)})
    return success(_customer_view(row), "Customer updated")
