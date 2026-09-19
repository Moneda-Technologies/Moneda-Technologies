from __future__ import annotations

import re
from uuid import uuid4

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import can_view_all_customers, customer_record, current_user, enforce_customer, permission_required, permission_required_any, permitted_customer_query
from app.services.audit import audit
from app.customers.codes import available_customer_code, customer_code
from app.customers.metadata import normalize_customer_profile, validation_message
from app.customers.addresses import customer_address_view, customer_shipping_addresses, normalize_address
from app.services.business_logic import normalize_client_type, customer_account_type, validate_customer_incentive_config


bp = Blueprint("customers", __name__, url_prefix="/api/customers")

FIELDS = {
    "name", "company_name", "contact_name", "email", "phone", "alternate_phone", "legal_name",
    "address", "billing_address", "shipping_address", "country", "state", "city", "postal_code",
    "tax_number", "gst_vat_number", "payment_terms", "preferred_currency", "default_currency",
    "default_tax_rate", "default_tax_mode", "tax_enabled", "assigned_salesperson", "credit_limit",
    "notes", "status", "active", "continent", "country_code", "country_name", "region",
    "tax_profile", "custom_payment_days", "payment_terms_display",
    "client_type", "account_type",
    "billing_address_record", "shipping_addresses", "default_price_list_id", "category_price_list_ids",
    "have_to_give_incentive", "incentive_bearer_name", "incentive_designation", "customer_incentive_percentage",
    "incentive_visible_to_managers", "incentive_visible_to_salespersons",
}

CUSTOMER_INCENTIVE_FIELDS = {
    "have_to_give_incentive", "incentive_bearer_name", "incentive_designation", "customer_incentive_percentage",
    "incentive_visible_to_managers", "incentive_visible_to_salespersons",
}


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _customer_incentive_visible(row: dict, actor: dict | None = None) -> bool:
    """Apply the global visibility guard and the customer's explicit opt-in."""
    actor = actor or current_user() or {}
    role = str(actor.get("role_id") or "")
    if role == "superadmin":
        return True
    settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    if role in {"manager", "manager_sales_admin"}:
        global_enabled = bool(settings.get("show_customer_incentives_to_manager", False))
        field = "incentive_visible_to_managers"
    elif role == "user":
        global_enabled = bool(settings.get("show_customer_incentives_to_salesperson", False))
        field = "incentive_visible_to_salespersons"
    else:
        return False
    # Legacy customer records predate per-customer visibility. Preserve the
    # existing global setting for those records until an admin edits them.
    customer_enabled = global_enabled if field not in row else _as_bool(row.get(field))
    return global_enabled and customer_enabled


def _view(row: dict, *, include_sensitive: bool | None = None) -> dict:
    customer = {**row}
    customer.setdefault("customer_id", customer.get("_id"))
    customer.setdefault("company_name", customer.get("name"))
    customer.setdefault("customer_code", customer_code(customer.get("name", "Customer")))
    preferred_currency = customer.get("preferred_currency") or customer.get("default_currency") or "EUR"
    customer["preferred_currency"] = preferred_currency
    customer["default_currency"] = preferred_currency
    customer.setdefault("active", customer.get("status", "active") != "archived")
    customer["client_type"] = normalize_client_type(customer.get("client_type"))
    customer["account_type"] = customer_account_type(customer)
    customer.update(customer_address_view(customer))
    if include_sensitive is None:
        include_sensitive = _customer_incentive_visible(customer)
    if not include_sensitive:
        for field in CUSTOMER_INCENTIVE_FIELDS:
            customer.pop(field, None)
    return customer


def _customer_incentive_changes(raw: dict, existing: dict | None = None) -> tuple[dict, str | None]:
    """Return validated customer incentive fields, preserving omitted edits."""
    existing = existing or {}
    enabled = raw.get("have_to_give_incentive", existing.get("have_to_give_incentive", False))
    bearer = raw.get("incentive_bearer_name", existing.get("incentive_bearer_name"))
    designation = raw.get("incentive_designation", existing.get("incentive_designation"))
    percentage = raw.get("customer_incentive_percentage", existing.get("customer_incentive_percentage"))
    value, error = validate_customer_incentive_config(enabled, bearer, designation, percentage)
    if value is not None:
        if value.get("have_to_give_incentive"):
            value["incentive_visible_to_managers"] = _as_bool(raw.get("incentive_visible_to_managers"), _as_bool(existing.get("incentive_visible_to_managers")))
            value["incentive_visible_to_salespersons"] = _as_bool(raw.get("incentive_visible_to_salespersons"), _as_bool(existing.get("incentive_visible_to_salespersons")))
        else:
            value["incentive_visible_to_managers"] = False
            value["incentive_visible_to_salespersons"] = False
    return value or {}, error


def _permitted_query() -> dict:
    return permitted_customer_query()


def _access_view(row: dict) -> dict | None:
    user = current_user() or {}
    if not can_view_all_customers(user) or "users.view" not in user.get("permissions", []):
        return None
    store = current_app.extensions["store"]
    user_ids = list(dict.fromkeys(str(value) for value in (row.get("assigned_user_ids") or []) if value))
    creator_id = row.get("created_by_user_id") or row.get("owner_user_id")
    if not creator_id and isinstance(row.get("created_by"), str):
        creator_id = row.get("created_by")
    if isinstance(row.get("created_by"), dict):
        creator_id = creator_id or row["created_by"].get("_id") or row["created_by"].get("user_id")
    creator = store.find_one("users", {"_id": creator_id}) if creator_id else None
    assigned = []
    managers: dict[str, dict] = {}
    for user_id in user_ids:
        member = store.find_one("users", {"_id": user_id})
        if member:
            assigned.append({"name": member.get("name") or member.get("email") or "User", "email": member.get("email")})
            manager_id = member.get("manager_id")
            manager = store.find_one("users", {"_id": manager_id}) if manager_id else None
            if manager:
                managers[str(manager.get("_id"))] = {"name": manager.get("name") or manager.get("email") or "Manager", "email": manager.get("email")}
    return {
        "created_by": {"name": creator.get("name") or creator.get("email") or "User", "email": creator.get("email")} if creator else None,
        "assigned_users": assigned,
        "assigned_managers": list(managers.values()),
    }


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
        status = request.args.get("status")
        client_type_filter = str(request.args.get("client_type") or "").strip().upper()
        account_type_filter = str(request.args.get("account_type") or "").strip().upper()
        if not status:
            # The directory's All view intentionally includes archived records;
            # authorization is still enforced by the assignment/global query.
            query.pop("active", None)
            query.pop("status", None)
        if status in {"active", "inactive", "archived"}:
            query["status"] = status
            if status == "archived":
                query.pop("active", None)
        if client_type_filter in {"WHOLESALER", "DEALER", "CUSTOMER"}:
            query["client_type"] = client_type_filter
        if account_type_filter in {"DISTRIBUTOR", "DEALER"}:
            query["account_type"] = account_type_filter
        term = request.args.get("search", "").strip()[:100]
        if term:
            query = {"$and": [query, {"$or": [{field: {"$regex": re.escape(term)}} for field in ("name", "company_name", "contact_name", "email", "phone")]}]}
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
    actor = current_user() or {}
    if CUSTOMER_INCENTIVE_FIELDS.intersection(raw) and str(actor.get("role_id") or "") != "superadmin":
        return failure("Only a Superadmin can configure customer incentives", status=403, error="customer_incentive_configuration_forbidden")
    payload = {key: value for key, value in raw.items() if key in FIELDS}
    if isinstance(payload.get("billing_address_record"), dict):
        payload["billing_address_record"] = normalize_address(payload["billing_address_record"], address_id="billing", active=True, is_default=True)
    if isinstance(payload.get("shipping_addresses"), list):
        payload["shipping_addresses"] = [normalize_address(item) for item in payload["shipping_addresses"] if isinstance(item, dict)]
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
    payload.setdefault("assigned_salesperson", (current_user() or {}).get("_id"))
    if str(actor.get("role_id") or "") == "superadmin":
        incentive_changes, incentive_error = _customer_incentive_changes(raw)
        if incentive_error:
            return failure(incentive_error, error="invalid_customer_incentive_configuration", status=422)
        payload.update(incentive_changes)
    else:
        payload.update({"have_to_give_incentive": False, "incentive_bearer_name": None, "incentive_designation": None, "customer_incentive_percentage": None, "incentive_visible_to_managers": False, "incentive_visible_to_salespersons": False})
    creator_id = (current_user() or {}).get("_id")
    payload["created_by_user_id"] = creator_id
    payload["assigned_user_ids"] = list(dict.fromkeys([creator_id])) if creator_id else []
    row = store.insert_one("customers", payload)
    if row.get("customer_id") != row.get("_id"):
        row = current_app.extensions["store"].update_one("customers", {"_id": row["_id"]}, {"customer_id": row["_id"]}) or row
    audit("customer.create", "customer", str(row["_id"]))
    return success(_view(row), "Customer created", 201)


@bp.get("/<customer_id>")
@permission_required("customers.view")
def get_customer(customer_id: str):
    store = current_app.extensions["store"]
    row = customer_record(customer_id) or store.find_one("customers", {"_id": customer_id})
    if not row:
        return failure("Customer not found", status=404)
    user = current_user() or {}
    archived_visible = row.get("status") == "archived" and can_view_all_customers(user)
    if not archived_visible and not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    relationship_query = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    related = {}
    for collection in ("quotations", "orders", "leads", "opportunities"):
        related[collection], _ = store.list(collection, relationship_query, limit=10)
    payload = {**_view(row), "related": related}
    access = _access_view(row)
    if access is not None:
        payload["access"] = access
    return success(payload)


@bp.patch("/<customer_id>")
@permission_required("customers.update")
def update_customer(customer_id: str):
    store = current_app.extensions["store"]
    existing = customer_record(customer_id)
    if not existing:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    raw = request.get_json(silent=True) or {}
    actor = current_user() or {}
    if CUSTOMER_INCENTIVE_FIELDS.intersection(raw) and str(actor.get("role_id") or "") != "superadmin":
        return failure("Only a Superadmin can configure customer incentives", status=403, error="customer_incentive_configuration_forbidden")
    changes = {key: value for key, value in raw.items() if key in FIELDS}
    if isinstance(changes.get("billing_address_record"), dict):
        changes["billing_address_record"] = normalize_address(changes["billing_address_record"], address_id="billing", active=True, is_default=True)
    if isinstance(changes.get("shipping_addresses"), list):
        changes["shipping_addresses"] = [normalize_address(item) for item in changes["shipping_addresses"] if isinstance(item, dict)]
    if "company_name" in changes and "name" not in changes:
        changes["name"] = str(changes["company_name"]).strip()
    if "name" in changes:
        changes["company_name"] = str(changes["name"]).strip()
        # Customer codes are stable identifiers and are not renamed with the display name.
    normalized, error = normalize_customer_profile(changes, existing)
    if error:
        return failure(validation_message(error), error=error, status=422)
    changes = normalized or changes
    if CUSTOMER_INCENTIVE_FIELDS.intersection(raw):
        incentive_changes, incentive_error = _customer_incentive_changes(raw, existing)
        if incentive_error:
            return failure(incentive_error, error="invalid_customer_incentive_configuration", status=422)
        changes.update(incentive_changes)
    if changes.get("status") and changes["status"] not in {"active", "inactive", "archived"}:
        return failure("Invalid customer status", status=422)
    if changes.get("preferred_currency") and changes["preferred_currency"] not in {"EUR", "USD", "INR"}:
        return failure("Unsupported customer currency", status=422)
    if "status" in changes and "active" not in changes:
        changes["active"] = changes["status"] != "archived"
    row = store.update_one("customers", {"_id": customer_id}, changes)
    if not row:
        return failure("Customer not found", status=404)
    audit_details = {"fields": sorted(changes)}
    if CUSTOMER_INCENTIVE_FIELDS.intersection(changes):
        audit_details["customer_incentive"] = {
            "previous_enabled": bool(existing.get("have_to_give_incentive")),
            "new_enabled": bool(changes.get("have_to_give_incentive", existing.get("have_to_give_incentive"))),
            "previous_bearer": existing.get("incentive_bearer_name"),
            "new_bearer": changes.get("incentive_bearer_name", existing.get("incentive_bearer_name")),
            "previous_designation": existing.get("incentive_designation"),
            "new_designation": changes.get("incentive_designation", existing.get("incentive_designation")),
            "previous_percentage": existing.get("customer_incentive_percentage"),
            "new_percentage": changes.get("customer_incentive_percentage", existing.get("customer_incentive_percentage")),
            "previous_visible_to_managers": _as_bool(existing.get("incentive_visible_to_managers")),
            "new_visible_to_managers": _as_bool(changes.get("incentive_visible_to_managers"), _as_bool(existing.get("incentive_visible_to_managers"))),
            "previous_visible_to_salespersons": _as_bool(existing.get("incentive_visible_to_salespersons")),
            "new_visible_to_salespersons": _as_bool(changes.get("incentive_visible_to_salespersons"), _as_bool(existing.get("incentive_visible_to_salespersons"))),
            "changed_by": actor.get("_id"),
        }
    if "client_type" in changes and changes.get("client_type") != existing.get("client_type"):
        audit_details.update({"old_client_type": normalize_client_type(existing.get("client_type")), "new_client_type": changes.get("client_type")})
    audit("customer.update", "customer", customer_id, audit_details)
    return success(_view(row), "Customer updated")


@bp.get("/<customer_id>/shipping-addresses")
@permission_required("customer.shipping_address.view")
def list_shipping_addresses(customer_id: str):
    customer = customer_record(customer_id)
    if not customer:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    rows = customer_shipping_addresses(customer)
    return success({"items": rows, "total": len(rows)})


@bp.post("/<customer_id>/shipping-addresses")
@permission_required("customer.shipping_address.create")
def create_shipping_address(customer_id: str):
    store = current_app.extensions["store"]
    customer = customer_record(customer_id)
    if not customer:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    raw = request.get_json(silent=True) or {}
    if not str(raw.get("address_line_1") or "").strip():
        return failure("Shipping address line 1 is required", status=422)
    rows = customer_shipping_addresses(customer)
    row = normalize_address(raw, address_id=str(uuid4()), is_default=not any(item.get("active") for item in rows))
    if row["is_default"]:
        for item in rows:
            item["is_default"] = False
    rows.append(row)
    store.update_one("customers", {"_id": customer_id}, {"shipping_addresses": rows})
    audit("customer.shipping_address.create", "customer", customer_id, {"shipping_address_id": row["id"]})
    return success(row, "Shipping address created", 201)


@bp.patch("/<customer_id>/shipping-addresses/<address_id>")
@permission_required("customer.shipping_address.update")
def update_shipping_address(customer_id: str, address_id: str):
    store = current_app.extensions["store"]
    customer = customer_record(customer_id)
    if not customer:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    rows = customer_shipping_addresses(customer)
    index = next((idx for idx, item in enumerate(rows) if str(item.get("id")) == address_id), None)
    if index is None:
        return failure("Shipping address not found", status=404)
    raw = request.get_json(silent=True) or {}
    merged = {**rows[index], **raw, "id": address_id}
    if not str(merged.get("address_line_1") or "").strip():
        return failure("Shipping address line 1 is required", status=422)
    rows[index] = normalize_address(merged, address_id=address_id)
    if rows[index]["is_default"]:
        for idx, item in enumerate(rows):
            if idx != index:
                item["is_default"] = False
    store.update_one("customers", {"_id": customer_id}, {"shipping_addresses": rows})
    audit("customer.shipping_address.update", "customer", customer_id, {"shipping_address_id": address_id})
    return success(rows[index], "Shipping address updated")


@bp.delete("/<customer_id>/shipping-addresses/<address_id>")
@permission_required("customer.shipping_address.deactivate")
def deactivate_shipping_address(customer_id: str, address_id: str):
    store = current_app.extensions["store"]
    customer = customer_record(customer_id)
    if not customer:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    rows = customer_shipping_addresses(customer)
    row = next((item for item in rows if str(item.get("id")) == address_id), None)
    if not row:
        return failure("Shipping address not found", status=404)
    row["active"] = False
    row["is_default"] = False
    next_active = next((item for item in rows if item.get("active")), None)
    if next_active and not any(item.get("active") and item.get("is_default") for item in rows):
        next_active["is_default"] = True
    store.update_one("customers", {"_id": customer_id}, {"shipping_addresses": rows})
    audit("customer.shipping_address.deactivate", "customer", customer_id, {"shipping_address_id": address_id})
    return success(row, "Shipping address deactivated")


@bp.patch("/<customer_id>/pricing")
@permission_required("customer.pricing.update")
def update_customer_pricing(customer_id: str):
    store = current_app.extensions["store"]
    customer = customer_record(customer_id)
    if not customer:
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    raw = request.get_json(silent=True) or {}
    changes = {
        "default_price_list_id": str(raw.get("default_price_list_id") or "").strip() or None,
        "category_price_list_ids": raw.get("category_price_list_ids") if isinstance(raw.get("category_price_list_ids"), dict) else {},
    }
    row = store.update_one("customers", {"_id": customer_id}, changes)
    audit("customer.pricing.update", "customer", customer_id, {"fields": sorted(changes)})
    return success(_view(row or customer), "Customer pricing updated")


@bp.delete("/<customer_id>")
@permission_required_any("customers.archive", "customers.delete")
def archive_customer(customer_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("customers", {"_id": customer_id})
    if not existing or existing.get("is_issuer"):
        return failure("Customer not found", status=404)
    if not enforce_customer(customer_id) and not (existing.get("status") == "archived" and can_view_all_customers(current_user())):
        return failure("Customer access denied", status=403)
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or request.args.get("reason") or "").strip()[:500]
    permanent = str(payload.get("permanent", request.args.get("permanent", "false"))).lower() in {"1", "true", "yes"}
    relationship_query = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    dependent_counts: dict[str, int] = {}
    for collection in ("quotations", "orders", "leads", "opportunities", "cart_items"):
        rows, _ = store.list(collection, relationship_query, limit=1)
        if rows:
            dependent_counts[collection] = len(store.list(collection, relationship_query, limit=100000)[0])
    if permanent and dependent_counts:
        return failure("This customer cannot be permanently deleted because business records reference it. Archive the customer instead.", status=409, error="CUSTOMER_HAS_HISTORY", dependencies=dependent_counts)
    if permanent:
        if not reason:
            return failure("A reason is required to permanently delete a customer", status=422, error="REASON_REQUIRED")
        audit("CUSTOMER_DELETED", "customer", customer_id, {"reason": reason, "previous_state": existing.get("status", "active"), "dependencies": dependent_counts})
        store.delete_one("customers", {"_id": customer_id})
        return success(message="Customer deleted")
    previous = existing.get("status", "active")
    store.update_one("customers", {"_id": customer_id}, {"status": "archived", "active": False})
    audit("CUSTOMER_ARCHIVED", "customer", customer_id, {"reason": reason, "previous_state": previous, "new_state": "archived"})
    return success(message="Customer archived")


@bp.post("/<customer_id>/restore")
@permission_required_any("customers.restore", "customers.update")
def restore_customer(customer_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("customers", {"_id": customer_id})
    if not existing or existing.get("is_issuer"):
        return failure("Customer not found", status=404)
    if not can_view_all_customers(current_user()):
        return failure("Customer access denied", status=403)
    if existing.get("status") != "archived" and existing.get("active", True) is not False:
        return success(_view(existing), "Customer is already active")
    row = store.update_one("customers", {"_id": customer_id}, {"status": "active", "active": True})
    audit("CUSTOMER_RESTORED", "customer", customer_id, {"previous_state": "archived", "new_state": "active"})
    return success(_view(row or existing), "Customer restored")
