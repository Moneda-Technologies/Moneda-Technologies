from __future__ import annotations

from decimal import Decimal, InvalidOperation
import csv
import io
import json
import re
import secrets
from typing import Any

from flask import Blueprint, current_app, request, session
from werkzeug.security import generate_password_hash

from app.api.responses import failure, success
from app.auth.policy import (
    SIGNUP_EMAIL_DOMAIN_MESSAGE,
    SIGNUP_PASSWORD_POLICY_MESSAGE,
    is_allowed_signup_email,
    is_valid_signup_password,
    normalize_signup_email,
)
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.middleware.access import can_view_all_customers, customer_access_ids_for_user, current_user, customer_record, permission_required
from app.repositories.store import utcnow
from app.services.audit import audit
from app.catalog.service import is_legacy_product
from app.finance.service import INCENTIVE_CATEGORIES, INCENTIVE_ELIGIBLE_ROLES, INCENTIVE_PERCENTAGES
from app.services.business_logic import CLIENT_TYPES, MANAGER_ROLE_IDS, resolve_incentive_rate, valid_manager
from app.devices.service import APPROVED, DENIED, PENDING, REVOKED, safe_device, _append_history, _history_entry, notify_reinstatement, notify_device_decision, notify_device_revocation, _notify_superadmins, _create_login_approval


bp = Blueprint("admin", __name__, url_prefix="/api")

PRICING_TYPES = {
    "fixed", "quantity", "per_piece", "per_bar", "per_sqm", "per_meter", "per_litre",
    "per_kg", "per_pack", "per_packet", "per_roll", "formula", "on_request",
}


def _can_configure_incentive(user: dict[str, Any] | None = None) -> bool:
    """Category incentive configuration is a Superadmin-only control."""
    return str((user or current_user() or {}).get("role_id") or "") == "superadmin"


def _normalise_incentive_rates(value: Any) -> dict[str, float] | None:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Incentive configuration must be a category-to-percentage object")
    rates: dict[str, float] = {}
    for category_id, raw_rate in value.items():
        key = str(category_id or "").strip()
        if not key:
            raise ValueError("Every incentive configuration entry needs a category")
        if key not in INCENTIVE_CATEGORIES:
            raise ValueError("Incentive configuration may only include Blanket, Underpacking and Chemical")
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            raise ValueError("Incentive percentage must be 0% to 6% in 0.5% steps") from None
        if rate not in INCENTIVE_PERCENTAGES:
            raise ValueError("Incentive percentage must be 0% to 6% in 0.5% steps")
        rates[key] = rate
    return rates


def _normalise_manager_id(store, manager_id: Any, *, target_role_id: str, target_user_id: str | None = None) -> str | None:
    """Validate the canonical one-manager relationship; never infer one."""
    if target_role_id != "user":
        if manager_id not in (None, "", False):
            raise ValueError("Only User accounts can be assigned to a manager")
        return None
    if manager_id in (None, "", False):
        return None
    candidate = str(manager_id).strip()
    if target_user_id and candidate == str(target_user_id):
        raise ValueError("A user cannot manage themselves")
    if not valid_manager(store, candidate):
        raise ValueError("Select an active Manager / Sales Admin")
    return candidate


@bp.post("/products")
@permission_required("products.create")
def create_product():
    payload = request.get_json(silent=True) or {}
    required = ("name", "category_id", "pricing_type", "unit")
    if any(not payload.get(field) for field in required):
        return failure("Name, category, pricing type and unit are required", status=422)
    if payload["pricing_type"] not in PRICING_TYPES:
        return failure("Unsupported pricing type", status=422)
    store = current_app.extensions["store"]
    if not store.find_one("categories", {"_id": payload["category_id"], "active": True}):
        return failure("Category not found", status=422)
    base_id = re.sub(r"[^a-z0-9]+", "-", str(payload["name"]).lower()).strip("-")
    product_id = base_id
    suffix = 2
    while store.find_one("products", {"_id": product_id}):
        product_id, suffix = f"{base_id}-{suffix}", suffix + 1
    row = store.insert_one("products", {
        "_id": product_id, "article_no": str(payload.get("article_no", "")).strip(),
        "sku": str(payload.get("sku") or product_id.upper().replace("-", "_")),
        "name": str(payload["name"]).strip(), "category_id": payload["category_id"],
        "description": str(payload.get("description", "")),
        "pricing": {"pricing_type": payload["pricing_type"], "price": None, "master_currency": "EUR", "unit": payload["unit"]},
        "tax": {"mode": None, "rate": None, "override_enabled": False},
        "discount_rules": {"enabled": True, "step": 0.5, "default_max_percent": 5, "privileged_max_percent": 10},
        "configuration": payload.get("configuration", {}), "pricing_status": "pending", "active": True,
    })
    audit("product.create", "product", product_id)
    return success(row, "Product created", 201)


@bp.post("/categories")
@permission_required("products.create")
def create_category():
    payload = request.get_json(silent=True) or {}
    category_id = re.sub(r"[^a-z0-9]+", "-", str(payload.get("_id") or payload.get("name", "")).lower()).strip("-")
    if not category_id or not str(payload.get("name", "")).strip():
        return failure("Category name is required", status=422)
    store = current_app.extensions["store"]
    if store.find_one("categories", {"_id": category_id}):
        return failure("Category already exists", status=409)
    row = store.insert_one("categories", {"_id": category_id, "name": str(payload["name"]).strip(), "description": str(payload.get("description", "")), "icon": str(payload.get("icon", "package")), "calculator_enabled": bool(payload.get("calculator_enabled", False)), "active": bool(payload.get("active", True)), "sort_order": int(payload.get("sort_order", 99))})
    audit("category.create", "category", category_id)
    return success(row, "Category created", 201)


@bp.patch("/categories/<category_id>")
@permission_required("products.update")
def update_category(category_id: str):
    store = current_app.extensions["store"]
    if not store.find_one("categories", {"_id": category_id}):
        return failure("Category not found", status=404)
    allowed = {"name", "description", "icon", "calculator_enabled", "active", "sort_order"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    row = store.update_one("categories", {"_id": category_id}, changes)
    audit("category.update", "category", category_id, {"fields": sorted(changes)})
    return success(row, "Category updated")


@bp.patch("/products/<product_id>/pricing")
@permission_required("pricing.edit")
def update_pricing(product_id: str):
    store = current_app.extensions["store"]
    product = store.find_one("products", {"_id": product_id})
    if not product:
        return failure("Product not found", status=404)
    payload = request.get_json(silent=True) or {}
    pricing = {**product.get("pricing", {})}
    try:
        supplied_price = payload.get("price", payload.get("base_price"))
        if "price" in payload or "base_price" in payload:
            if supplied_price is None:
                pricing["price"] = None
            else:
                value = Decimal(str(supplied_price))
                if value < 0:
                    raise ValueError("Price cannot be negative")
                pricing["price"] = float(value.quantize(Decimal("0.01")))
            pricing.pop("base_price", None)
        if "variant_prices" in payload:
            supplied_variants = payload.get("variant_prices")
            if not isinstance(supplied_variants, dict):
                raise ValueError("Variant prices must be an object keyed by thickness")
            valid_thicknesses = [Decimal(str(value)) for value in product.get("configuration", {}).get("thicknesses_mm", [])]
            cleaned_variants: dict[str, float | None] = {}
            for raw_key, raw_value in supplied_variants.items():
                thickness = Decimal(str(raw_key))
                if valid_thicknesses and thickness not in valid_thicknesses:
                    raise ValueError("Variant price thickness is not available for this product")
                if raw_value is None:
                    cleaned_variants[str(thickness)] = None
                    continue
                value = Decimal(str(raw_value))
                if value < 0:
                    raise ValueError("Variant price cannot be negative")
                cleaned_variants[str(thickness)] = float(value.quantize(Decimal("0.01")))
            pricing["variant_prices"] = cleaned_variants
        pricing["master_currency"] = "EUR"
        if payload.get("pricing_type"):
            if payload["pricing_type"] not in PRICING_TYPES:
                raise ValueError("Unsupported pricing type")
            pricing["pricing_type"] = payload["pricing_type"]
            pricing.pop("type", None)
        if payload.get("unit"):
            pricing["unit"] = payload["unit"]
    except (InvalidOperation, ValueError) as exc:
        return failure(str(exc), status=422)
    user = current_user() or {}
    history = store.insert_one("price_history", {
        "product_id": product_id,
        "old_price": product.get("pricing", {}).get("price", product.get("pricing", {}).get("base_price")),
        "new_price": pricing.get("price"), "currency": "EUR", "unit": pricing.get("unit"),
        "old_pricing": product.get("pricing"), "new_pricing": pricing,
        "changed_by": user.get("_id"),
        "pricing_type": pricing.get("pricing_type"), "source": "admin",
        "reason": str(payload.get("reason", ""))[:500],
    })
    missing_status = payload.get("pricing_status")
    if missing_status not in {"pending", "on_request"}:
        missing_status = "on_request" if product.get("pricing_status") == "on_request" else "pending"
    valid_thicknesses = [Decimal(str(value)) for value in product.get("configuration", {}).get("thicknesses_mm", [])]
    variant_prices = pricing.get("variant_prices") or {}
    variant_complete = bool(valid_thicknesses) and all(
        any(Decimal(str(key)) == thickness and value is not None for key, value in variant_prices.items())
        for thickness in valid_thicknesses
    )
    updated = store.update_one("products", {"_id": product_id}, {
        "pricing": pricing,
        "pricing_status": "configured" if pricing.get("price") is not None or variant_complete else missing_status,
    })
    audit("pricing.update", "product", product_id, {"history_id": history["_id"]})
    return success(updated, "EUR pricing updated")


@bp.get("/products/<product_id>/price-history")
@permission_required("pricing.view")
def price_history(product_id: str):
    rows, total = current_app.extensions["store"].list("price_history", {"product_id": product_id}, limit=100)
    return success({"items": rows, "total": total})


PRICE_STATUSES = {"configured", "on_request", "pending", "inactive"}
PRICE_MAPS = {"variant_prices", "dimension_prices", "package_prices"}


def _master_price(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Price must be a finite number")
    try:
        price = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Price must be a finite number") from exc
    if not price.is_finite():
        raise ValueError("Price must be a finite number")
    if price < 0:
        raise ValueError("Price cannot be negative")
    return float(price.quantize(Decimal("0.01")))


def _find_pricing_resource(resource_id: str) -> tuple[str, str, dict[str, Any]] | None:
    store = current_app.extensions["store"]
    product = store.find_one("products", {"_id": resource_id})
    if product:
        return "product", "products", product
    bar = store.find_one("blanket_bars", {"_id": resource_id})
    if bar:
        return "bar", "blanket_bars", bar
    return None


def _resource_payload(entity_type: str, row: dict[str, Any]) -> dict[str, Any]:
    pricing = row.get("pricing", {})
    configuration = row.get("configuration", {})
    category_id = "bars" if entity_type == "bar" else row.get("category_id")
    family_names = {
        "blankets": "Printing Blankets", "mpacks": "Underpacking", "chemicals": "Chemicals", "bars": "Blanket Bars",
    }
    category_name = "Blanket component"
    if entity_type == "product":
        if category_id == "blankets":
            category_ids = configuration.get("category_ids", [])
            category_rows = [current_app.extensions["store"].find_one("blanket_categories", {"_id": value}) for value in category_ids]
            category_name = ", ".join(item["name"] for item in category_rows if item) or "Blankets"
        elif category_id == "chemicals":
            category_name = configuration.get("sub_category", "Chemicals")
        else:
            category_name = "Underpacking"
    price_maps = {name: pricing.get(name, {}) for name in PRICE_MAPS if pricing.get(name)}
    return {
        "id": row["_id"], "entity_type": entity_type, "name": row.get("name", ""),
        "article_no": row.get("article_no", ""), "sku": row.get("sku", ""),
        "family_id": category_id, "family_name": family_names.get(category_id, str(category_id)),
        "category_name": category_name, "active": bool(row.get("active", True)),
        "pricing_status": row.get("pricing_status", "pending"),
        "pricing": {
            "master_currency": "EUR", "pricing_type": pricing.get("pricing_type"),
            "unit": pricing.get("unit"), "price_eur": pricing.get("price"), **price_maps,
        },
        "price_updated_at": row.get("price_updated_at") or row.get("updated_at"),
        "price_updated_by": row.get("price_updated_by_name") or row.get("price_updated_by"),
    }


@bp.get("/admin/pricing/products")
@permission_required("pricing.history")
def list_pricing_products():
    store = current_app.extensions["store"]
    products, _ = store.list("products", limit=100_000, sort="name", direction=1)
    # Keep historical rows in Mongo for quotation snapshots, but never expose
    # obsolete combined/retired seed products as catalogue resources.
    products = [row for row in products if not is_legacy_product(row) and not (
        row.get("category_id") == "mpacks" and row.get("_id") != "mtech-mpack"
    )]
    bars, _ = store.list("blanket_bars", limit=10_000, sort="article_no", direction=1)
    rows = [_resource_payload("product", row) for row in products] + [_resource_payload("bar", row) for row in bars]
    family = str(request.args.get("family", "all")).strip().lower()
    status = str(request.args.get("status", "all")).strip().lower()
    active = str(request.args.get("active", "all")).strip().lower()
    search = str(request.args.get("search", "")).strip().lower()[:100]
    if family and family != "all":
        rows = [row for row in rows if row["family_id"] == family]
    if status and status != "all":
        rows = [row for row in rows if row["pricing_status"] == status]
    if active in {"true", "false"}:
        rows = [row for row in rows if row["active"] is (active == "true")]
    if search:
        rows = [row for row in rows if search in " ".join((
            row["name"], row["article_no"], row["sku"], row["family_name"], row["category_name"],
        )).lower()]
    rows.sort(key=lambda row: (row["family_name"], row["name"], row["article_no"]))
    return success({"items": rows, "total": len(rows)})


@bp.get("/admin/pricing/products/<resource_id>")
@permission_required("pricing.history")
def pricing_product_detail(resource_id: str):
    found = _find_pricing_resource(resource_id)
    if not found:
        return failure("Pricing resource not found", status=404)
    entity_type, _, row = found
    payload = _resource_payload(entity_type, row)
    history, total = current_app.extensions["store"].list(
        "price_history", {"resource_id": resource_id}, limit=100, sort="changed_at", direction=-1,
    )
    payload["history"] = history
    payload["history_total"] = total
    return success(payload)


@bp.patch("/admin/pricing/products/<resource_id>")
@permission_required("pricing.edit")
def edit_master_price(resource_id: str):
    found = _find_pricing_resource(resource_id)
    if not found:
        return failure("Pricing resource not found", status=404)
    entity_type, collection, row = found
    payload = request.get_json(silent=True) or {}
    currency = str(payload.get("currency", "EUR")).upper()
    if currency != "EUR":
        return failure("Only the EUR master price can be edited", status=422)
    if "price_eur" not in payload:
        return failure("price_eur is required (use null to mark a price as unavailable)", status=422)
    status = str(payload.get("pricing_status", row.get("pricing_status", "pending"))).lower()
    if status not in PRICE_STATUSES:
        return failure("Unsupported pricing status", status=422)
    pricing = {**row.get("pricing", {})}
    price_map = str(payload.get("price_map", "")).strip()
    price_key = str(payload.get("price_key", "")).strip()
    try:
        new_price = _master_price(payload.get("price_eur"))
        if price_map or price_key:
            if price_map not in PRICE_MAPS or not price_key:
                raise ValueError("A valid price_map and price_key are required")
            existing_map = pricing.get(price_map)
            if not isinstance(existing_map, dict) or price_key not in existing_map:
                raise ValueError("The selected price variant does not exist")
            old_price = existing_map.get(price_key)
            pricing[price_map] = {**existing_map, price_key: new_price}
        else:
            old_price = pricing.get("price")
            pricing["price"] = new_price
        available_prices = [pricing.get("price")]
        for map_name in PRICE_MAPS:
            available_prices.extend((pricing.get(map_name) or {}).values())
        if status == "configured" and not any(value is not None for value in available_prices):
            raise ValueError("A configured product requires at least one EUR price")
    except ValueError as exc:
        return failure(str(exc), status=422)
    user = current_user() or {}
    changed_at = utcnow()
    history = current_app.extensions["store"].insert_one("price_history", {
        "resource_id": resource_id, "product_id": resource_id if entity_type == "product" else None,
        "entity_type": entity_type, "article_no": row.get("article_no"),
        "price_map": price_map or None, "price_key": price_key or None,
        "old_price_eur": old_price, "new_price_eur": new_price, "currency": "EUR",
        "unit": pricing.get("unit"), "pricing_status": status,
        "changed_by": user.get("_id"), "changed_by_name": user.get("name") or user.get("email"),
        "changed_at": changed_at, "effective_from": changed_at,
        "reason": str(payload.get("reason", "")).strip()[:500], "source": "admin",
    })
    updated = current_app.extensions["store"].update_one(collection, {"_id": resource_id}, {
        "pricing": pricing, "pricing_status": status, "active": status != "inactive",
        "price_updated_at": changed_at, "price_updated_by": user.get("_id"),
        "price_updated_by_name": user.get("name") or user.get("email"),
    })
    audit("pricing.update", entity_type, resource_id, {"history_id": history["_id"], "price_map": price_map or None, "price_key": price_key or None})
    return success(_resource_payload(entity_type, updated or row), "EUR master price updated")


@bp.get("/admin/pricing/history/<resource_id>")
@permission_required("pricing.history")
def master_price_history(resource_id: str):
    store = current_app.extensions["store"]
    if not _find_pricing_resource(resource_id):
        return failure("Pricing resource not found", status=404)
    rows, total = store.list("price_history", {
        "$or": [{"resource_id": resource_id}, {"product_id": resource_id}],
    }, limit=250, sort="changed_at", direction=-1)
    return success({"items": rows, "total": total})


@bp.patch("/products/<product_id>")
@permission_required("products.update")
def update_product(product_id: str):
    allowed = {"name", "description", "active", "configuration", "discount_rules"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    row = current_app.extensions["store"].update_one("products", {"_id": product_id}, changes)
    if not row:
        return failure("Product not found", status=404)
    audit("product.update", "product", product_id, {"fields": sorted(changes)})
    return success(row, "Product updated")


@bp.get("/admin/users")
@permission_required("users.view")
def list_users():
    store = current_app.extensions["store"]
    actor = current_user() or {}
    rows, total = store.list("users", limit=100, sort="name", direction=1)
    incentive_categories = []
    if str(actor.get("role_id") or "") == "superadmin":
        incentive_categories = [{"_id": key, "name": name} for key, name in INCENTIVE_CATEGORIES.items()]
    roles = {row["_id"]: row for row in store.list("roles", limit=100)[0]}
    manager_rows = [
        {"_id": row.get("_id"), "name": row.get("name"), "email": row.get("email"), "role_id": row.get("role_id")}
        for row in rows if str(row.get("role_id") or "") in MANAGER_ROLE_IDS and row.get("active", True) is not False
    ]
    customer_rows, _ = store.list("customers", {"active": {"$ne": False}, "status": {"$ne": "archived"}}, limit=100_000)
    assigned_by_user: dict[str, list[str]] = {}
    for customer in customer_rows:
        if customer.get("is_issuer") or not customer.get("_id"):
            continue
        customer_id = str(customer["_id"])
        user_ids = {str(value) for value in (customer.get("assigned_user_ids") or []) if value}
        if customer.get("created_by_user_id"):
            user_ids.add(str(customer["created_by_user_id"]))
        for user_id in user_ids:
            assigned_by_user.setdefault(user_id, []).append(customer_id)
    for row in rows:
        row.pop("password_hash", None)
        if str(actor.get("role_id") or "") != "superadmin":
            # Incentive configuration is a Superadmin-only concern; do not
            # expose the value through the user-management API to other roles.
            row.pop("incentive_percentage", None)
            row.pop("incentive_rates", None)
            row.pop("incentive_categories", None)
        role = roles.get(row.get("role_id"), {})
        manager = store.find_one("users", {"_id": row.get("manager_id")}) if row.get("manager_id") else None
        row["manager_id"] = manager.get("_id") if manager else None
        row["manager"] = {"_id": manager.get("_id"), "name": manager.get("name"), "email": manager.get("email")} if manager else None
        row["managed_user_count"] = store.count("users", {"manager_id": row.get("_id"), "active": {"$ne": False}}) if str(row.get("role_id") or "") in MANAGER_ROLE_IDS else 0
        global_access = can_view_all_customers({**row, "permissions": role.get("permissions", [])})
        assigned_ids = list(dict.fromkeys(assigned_by_user.get(str(row.get("_id")), [])))
        row["customer_access_global"] = global_access
        row["assigned_customer_ids"] = [] if global_access else assigned_ids
        row["customer_access_count"] = None if global_access else len(assigned_ids)
        device_rows, _ = store.list("devices", {"user_id": str(row.get("_id"))}, limit=500)
        row["device_counts"] = {
            "total": len(device_rows),
            "approved": sum(1 for device in device_rows if device.get("device_status") == APPROVED),
            "pending": sum(1 for device in device_rows if device.get("device_status") == PENDING),
            "denied": sum(1 for device in device_rows if device.get("device_status") == DENIED),
            "revoked": sum(1 for device in device_rows if device.get("device_status") == REVOKED),
        }
        if str(actor.get("role_id") or "") == "superadmin":
            configured = row.get("incentive_rates") if isinstance(row.get("incentive_rates"), dict) else {}
            if not configured:
                config_rows, _ = store.list("incentive_configurations", {"user_id": row.get("_id")}, limit=10_000)
                configured = {str(config.get("category_id")): config.get("incentive_percentage") for config in config_rows if config.get("category_id")}
            row["incentive_rates"] = configured
            row["incentive_categories"] = incentive_categories
    return success({"items": rows, "total": total, "manager_options": manager_rows})


@bp.get("/admin/users/<user_id>/devices")
@permission_required("users.view")
def list_user_devices(user_id: str):
    store = current_app.extensions["store"]
    actor = current_user() or {}
    if str(actor.get("role_id") or "") not in {"admin", "superadmin"}:
        return failure("Only administrators can inspect trusted devices", status=403)
    if not store.find_one("users", {"_id": user_id}):
        return failure("User not found", status=404)
    audit("DEVICE_DETAILS_VIEWED", "user", user_id, {
        "actor_user_id": actor.get("_id"), "device_count": store.count("devices", {"user_id": user_id}),
    })
    rows, total = store.list("devices", {"user_id": user_id}, limit=500, sort="registered_at", direction=-1)
    for row in rows:
        if not row.get("device_ref"):
            store.update_one("devices", {"_id": row["_id"]}, {"device_ref": f"DVC-{secrets.token_hex(4).upper()}"})
            row["device_ref"] = store.find_one("devices", {"_id": row["_id"]}).get("device_ref")
    current_device_id = session.get("device_id") if str(actor.get("_id")) == str(user_id) else None
    return success({"items": [safe_device(row, current_session=str(row.get("_id")) == str(current_device_id), include_public_ip=True) for row in rows], "total": total})


@bp.get("/admin/users/<user_id>/relationships")
@permission_required("users.view")
def user_relationships(user_id: str):
    """Expose the same canonical manager/team/customer relationship used by access checks."""
    store = current_app.extensions["store"]
    actor = current_user() or {}
    target = store.find_one("users", {"_id": user_id})
    if not target:
        return failure("User not found", status=404)
    if str(actor.get("role_id") or "") not in {"admin", "superadmin"} and str(actor.get("_id")) != str(user_id):
        return failure("You do not have permission to inspect this hierarchy", status=403)
    manager = store.find_one("users", {"_id": target.get("manager_id")}) if target.get("manager_id") else None
    team, _ = store.list("users", {"manager_id": user_id, "active": {"$ne": False}}, limit=100_000, sort="name", direction=1)
    customer_ids = customer_access_ids_for_user(user_id, include_created=False)
    return success({
        "user_id": user_id,
        "manager": {"_id": manager.get("_id"), "name": manager.get("name"), "email": manager.get("email")} if manager else None,
        "team": [{"_id": row.get("_id"), "name": row.get("name"), "email": row.get("email"), "role_id": row.get("role_id")} for row in team],
        "assigned_customer_ids": customer_ids,
    })


def _change_device_status(user_id: str, device_id: str, target: str, *, reason: str = ""):
    store = current_app.extensions["store"]
    actor = current_user() or {}
    if str(actor.get("role_id")) != "superadmin":
        return failure("Only Superadmins can manage trusted devices", status=403)
    if str(actor.get("_id")) == str(user_id) and str(actor.get("role_id")) != "superadmin":
        return failure("You cannot approve or revoke your own device", status=403)
    device = store.find_one("devices", {"device_ref": device_id, "user_id": user_id}) or store.find_one("devices", {"_id": device_id, "user_id": user_id})
    if not device:
        return failure("Device not found", status=404)
    reason = str(reason or "").strip()
    if target in {DENIED, REVOKED, "reinstated"} and not reason:
        return failure("A reason is required", status=422, error="decision_reason_required")
    previous_status = str(device.get("device_status") or PENDING)
    now = utcnow()
    expected_statuses = {APPROVED: {PENDING}, DENIED: {PENDING}, "reinstated": {DENIED, REVOKED}, REVOKED: {APPROVED}}.get(target)
    if expected_statuses and previous_status not in expected_statuses:
        return failure("This device is not in a state that supports that action", status=409, error="invalid_device_transition")
    resulting_status = PENDING if target == "reinstated" else target
    event = "DEVICE_REINSTATED" if target == "reinstated" else "DEVICE_APPROVED" if target == APPROVED else "DEVICE_DENIED" if target == DENIED else "DEVICE_REVOKED"
    changes: dict[str, Any] = {"device_status": resulting_status, "device_history": _append_history(device, _history_entry(
        event, device, previous_status=previous_status, new_status=resulting_status,
        actor_user_id=str(actor.get("_id") or ""), reason=reason or None, timestamp=now,
    ))}
    if target == APPROVED:
        changes.update({"approved_at": now, "approved_by": actor.get("_id"), "approved_by_name": actor.get("name") or actor.get("username"), "revoked_at": None, "revoked_by": None})
        action = "device_approved"
    elif target == DENIED:
        changes.update({"denied_at": now, "denied_by": actor.get("_id"), "denied_by_user_id": actor.get("_id"), "denied_by_name": actor.get("name") or actor.get("username"), "denial_reason": reason, "previous_status": previous_status, "new_status": DENIED})
        action = "device_rejected"
    elif target == "reinstated":
        changes.update({"reinstated_at": now, "reinstated_by": actor.get("_id"), "reinstated_by_user_id": actor.get("_id"), "reinstated_by_name": actor.get("name") or actor.get("username"), "reinstatement_reason": reason, "previous_status": previous_status, "new_status": PENDING})
        action = "device_reinstated"
    elif target == REVOKED:
        changes.update({"revoked_at": now, "revoked_by": actor.get("_id"), "revoked_by_user_id": actor.get("_id"), "revoked_by_name": actor.get("name") or actor.get("username"), "revoke_reason": reason, "previous_status": previous_status, "new_status": REVOKED})
        action = "device_revoked"
    else:
        return failure("Invalid device action", status=422)
    # Match the current status so simultaneous administrator decisions cannot
    # overwrite one another. This is the same first-decision-wins rule used by
    # email approval links.
    updated = store.update_one("devices", {"_id": device["_id"], "user_id": user_id, "device_status": {"$in": list(expected_statuses or {previous_status})}}, changes)
    if not updated:
        return failure("This device status has already changed.", status=409, error="decision_already_completed")
    audit(event, "device", str(device["_id"]), {"user_id": user_id, "actor_user_id": actor.get("_id"), "reason": reason or None, "previous_status": previous_status, "new_status": resulting_status})
    if target in {DENIED, REVOKED}:
        audit("DEVICE_SESSION_TERMINATED", "device", str(device["_id"]), {"user_id": user_id, "actor_user_id": actor.get("_id"), "reason": reason or None})
    user = store.find_one("users", {"_id": user_id}) or {}
    if target == "reinstated":
        # A reinstated device is pending again and gets a fresh one-time
        # approval request; it is never silently trusted.
        attempt, _ = _create_login_approval(user, updated, now=now)
        updated = {**updated, **store.find_one("devices", {"_id": updated["_id"]})}
        updated["login_approval"] = attempt
        _notify_superadmins(user, updated, reinstated=True, attempt=attempt)
        notify_reinstatement(user, updated, actor_name=str(actor.get("name") or actor.get("username") or "Superadmin"), reason=reason)
    elif target in {APPROVED, DENIED}:
        notify_device_decision(user, updated, approved=target == APPROVED, reason=reason)
    elif target == REVOKED:
        notify_device_revocation(user, updated)
    return success(safe_device(updated), "Device reinstated; approval required" if target == "reinstated" else "Device updated")


@bp.post("/admin/users/<user_id>/devices/<device_id>/approve")
@permission_required("users.update")
def approve_device(user_id: str, device_id: str):
    return _change_device_status(user_id, device_id, APPROVED)


@bp.post("/admin/users/<user_id>/devices/<device_id>/reject")
@permission_required("users.update")
def reject_device(user_id: str, device_id: str):
    payload = request.get_json(silent=True) or {}
    return _change_device_status(user_id, device_id, DENIED, reason=str(payload.get("reason") or ""))


@bp.post("/admin/users/<user_id>/devices/<device_id>/reinstate")
@permission_required("users.update")
def reinstate_device(user_id: str, device_id: str):
    payload = request.get_json(silent=True) or {}
    return _change_device_status(user_id, device_id, "reinstated", reason=str(payload.get("reason") or ""))


@bp.post("/admin/users/<user_id>/devices/<device_id>/revoke")
@permission_required("users.update")
def revoke_device(user_id: str, device_id: str):
    payload = request.get_json(silent=True) or {}
    return _change_device_status(user_id, device_id, REVOKED, reason=str(payload.get("reason") or ""))


@bp.post("/admin/users/<user_id>/devices/revoke-all")
@permission_required("users.update")
def revoke_all_devices(user_id: str):
    store = current_app.extensions["store"]
    actor = current_user() or {}
    if str(actor.get("_id")) == str(user_id) and str(actor.get("role_id")) != "superadmin":
        return failure("You cannot revoke your own devices", status=403)
    rows, _ = store.list("devices", {"user_id": user_id, "device_status": {"$ne": REVOKED}}, limit=500)
    now = utcnow()
    user = store.find_one("users", {"_id": user_id}) or {}
    for row in rows:
        changes = {"device_status": REVOKED, "revoked_at": now, "revoked_by": actor.get("_id"), "revoked_by_name": actor.get("name") or actor.get("username"), "device_history": _append_history(row, _history_entry("DEVICE_REVOKED", row, previous_status=str(row.get("device_status") or ""), new_status=REVOKED, actor_user_id=str(actor.get("_id") or ""), timestamp=now))}
        updated = store.update_one("devices", {"_id": row["_id"], "user_id": user_id, "device_status": row.get("device_status")}, changes)
        if updated:
            audit("DEVICE_REVOKED", "device", str(row["_id"]), {"user_id": user_id, "actor_user_id": actor.get("_id"), "bulk": True, "previous_status": row.get("device_status"), "new_status": REVOKED})
            audit("DEVICE_SESSION_TERMINATED", "device", str(row["_id"]), {"user_id": user_id, "actor_user_id": actor.get("_id"), "bulk": True})
            notify_device_revocation(user, updated)
    return success({"revoked": len(rows)}, "Devices revoked")


@bp.delete("/admin/users/<user_id>/devices/<device_id>")
@permission_required("users.update")
def delete_denied_device(user_id: str, device_id: str):
    actor = current_user() or {}
    if str(actor.get("role_id")) != "superadmin":
        return failure("Only Superadmins can delete trusted devices", status=403)
    store = current_app.extensions["store"]
    user = store.find_one("users", {"_id": user_id})
    if not user:
        return failure("User not found", status=404)
    device = store.find_one("devices", {"device_ref": device_id, "user_id": user_id}) or store.find_one("devices", {"_id": device_id, "user_id": user_id})
    if not device:
        return failure("Device not found", status=404)
    if str(device.get("device_status") or "").lower() != DENIED:
        return failure("Only denied devices can be deleted", status=409, error="invalid_device_state")
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return failure("A reason is required", status=422, error="decision_reason_required")
    audit("DEVICE_DELETED", "device", str(device.get("_id")), {
        "target_user_id": user_id, "device_id": device.get("device_ref") or device.get("_id"),
        "browser": device.get("browser"), "operating_system": device.get("operating_system"),
        "device_type": device.get("device_type"), "previous_status": DENIED,
        "actor_user_id": actor.get("_id"), "reason": reason,
    })
    if not store.delete_one("devices", {"_id": device.get("_id"), "user_id": user_id, "device_status": DENIED}):
        return failure("Device could not be deleted", status=409, error="device_delete_conflict")
    return success({"deleted": True, "device_id": device.get("device_ref") or device.get("_id")}, "Denied device deleted")


@bp.post("/admin/users")
@permission_required("users.create")
def create_user():
    payload = request.get_json(silent=True) or {}
    actor = current_user() or {}
    if actor.get("role_id") != "superadmin" or actor.get("active") is False:
        return failure(
            "Only a Superadmin can invite users.", status=403,
            error="superadmin_required",
        )

    name = str(payload.get("name", "")).strip()
    username = str(payload.get("username", "")).strip()
    username_normalized = username.casefold()
    raw_email = str(payload.get("email", "")).strip()
    email = normalize_signup_email(raw_email)
    password = str(payload.get("password", ""))
    confirm_password = str(payload.get("confirm_password", ""))
    role_id = str(payload.get("role_id", "")).strip()
    if "incentive_percentage" in payload or "incentive_rates" in payload:
        return failure("Incentives are configured by category after the user is created.", status=422, error="category_incentive_configuration_required")
    store = current_app.extensions["store"]

    if len(name) < 2:
        return failure("Name must contain at least 2 characters.", status=422, error="invalid_name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,79}", username):
        return failure(
            "Username must contain 3 to 80 characters and may use only letters, numbers, periods, underscores, and hyphens.",
            status=422, error="invalid_username",
        )
    if not email:
        return failure("Enter a valid email address.", status=422, error="invalid_email")
    if not is_allowed_signup_email(email, current_app.config.get("ALLOWED_SIGNUP_EMAIL_DOMAINS")):
        return failure(SIGNUP_EMAIL_DOMAIN_MESSAGE, status=422, error="signup_email_domain_not_allowed")
    if password != confirm_password:
        return failure("Passwords do not match.", status=422, error="password_mismatch")
    if not is_valid_signup_password(password):
        return failure(SIGNUP_PASSWORD_POLICY_MESSAGE, status=422, error="password_policy")
    role = store.find_one("roles", {"_id": role_id})
    if not role:
        return failure("Select a valid role.", status=422, error="invalid_role")
    if store.find_one("users", {"username_normalized": username_normalized}) or store.find_one(
        "users", {"username": {"$regex": f"^{re.escape(username)}$", "$options": "i"}},
    ):
        return failure("Username is already in use.", status=409, error="username_in_use")
    if store.find_one("users", {"email": email}) or store.find_one(
        "users", {"email": {"$regex": f"^{re.escape(email)}$", "$options": "i"}},
    ):
        return failure("An account already exists for this email address.", status=409, error="email_in_use")

    try:
        manager_id = _normalise_manager_id(store, payload.get("manager_id"), target_role_id=role_id)
    except ValueError as exc:
        return failure(str(exc), status=422, error="invalid_manager_assignment")

    requested_ids = payload.get("customer_ids") or payload.get("customer_company_ids") or payload.get("company_ids", [])
    if not isinstance(requested_ids, list):
        return failure("Customer assignments must be a list", status=422)
    customer_ids = []
    for value in requested_ids:
        customer_id = str(value).strip()
        customer = customer_record(customer_id)
        if not customer or customer.get("is_issuer"):
            return failure("Customer assignment is invalid", status=422, error="invalid_customer_assignment")
        if customer_id not in customer_ids:
            customer_ids.append(customer_id)
    device_access_mode = str(payload.get("device_access_mode") or current_app.config.get("DEVICE_ACCESS_MODE", "approved_devices_only"))
    if device_access_mode not in {"any_authorized_device", "approved_devices_only"}:
        return failure("Invalid device access policy", status=422)

    document = {
        "email": email,
        "email_verified": True,
        "name": name,
        "username": username,
        "username_normalized": username_normalized,
        "password_hash": generate_password_hash(password),
        "phone": "",
        "role_id": role_id,
        "manager_id": manager_id,
        "customer_ids": customer_ids,
        "customer_company_ids": customer_ids,
        "company_ids": customer_ids,
        "active": True,
        "device_access_mode": device_access_mode,
        "invitation_email_status": "pending",
        "incentive_percentage": None,
        "incentive_rates": {},
    }
    try:
        row = store.insert_one("users", document)
    except Exception as exc:
        if exc.__class__.__name__ == "DuplicateKeyError":
            return failure(
                "Username or email is already in use.", status=409,
                error="account_exists",
            )
        current_app.logger.exception("user_invite stage=user_creation result=FAIL")
        return failure("User account could not be created.", status=500, error="user_creation_failed")

    for customer_id in customer_ids:
        customer = customer_record(customer_id)
        assigned = list(dict.fromkeys(str(value) for value in (customer or {}).get("assigned_user_ids", []) if value))
        if row["_id"] not in assigned:
            assigned.append(row["_id"])
            store.update_one("customers", {"_id": customer_id}, {"assigned_user_ids": assigned})
            audit("customer_assigned_to_user", "customer", customer_id, {"actor_user_id": actor.get("_id"), "target_user_id": row["_id"]})

    diagnostic_id = email_diagnostic_id()
    email_sent = False
    email_error_code = None
    try:
        current_app.extensions["email_service"].send_user_invitation(
            to=[email],
            name=name,
            username=username,
            initial_password=password,
            role_name=str(role.get("display_name") or role_id),
            login_url=f"{str(current_app.config.get('APP_BASE_URL') or 'http://localhost:3005').rstrip('/')}/login",
            request_id=diagnostic_id,
        )
        email_sent = True
    except EmailDeliveryError as exc:
        diagnostic_id = exc.diagnostic_id
        email_error_code = exc.error_code
        current_app.logger.error(
            "[%s] user_invite stage=invitation_email result=FAIL error_code=%s",
            diagnostic_id, email_error_code,
        )
    except Exception:
        email_error_code = "INVITATION_EMAIL_FAILED"
        current_app.logger.exception(
            "[%s] user_invite stage=invitation_email result=FAIL error_code=%s",
            diagnostic_id, email_error_code,
        )
    finally:
        # Drop the route's reference as soon as hashing and delivery complete.
        password = ""
        confirm_password = ""

    invitation_changes = {
        "invitation_email_status": "sent" if email_sent else "failed",
        "invitation_email_diagnostic_id": diagnostic_id,
    }
    if email_sent:
        invitation_changes["invitation_email_sent_at"] = utcnow()
    row = store.update_one("users", {"_id": row["_id"]}, invitation_changes) or row
    audit("user.invite", "user", str(row["_id"]), {
        "actor_user_id": actor.get("_id"),
        "target_user_id": row["_id"],
        "email": email,
        "username": username,
        "role_id": role_id,
        "manager_id": manager_id,
        "customer_ids": customer_ids,
        "invitation_email_status": invitation_changes["invitation_email_status"],
        "diagnostic_id": diagnostic_id,
    })

    public_row = dict(row)
    public_row.pop("password_hash", None)
    result = {
        "user": public_row,
        "invitation": {
            "email_sent": email_sent,
            "status": invitation_changes["invitation_email_status"],
            "diagnostic_id": diagnostic_id,
            **({"error_code": email_error_code} if email_error_code else {}),
        },
    }
    if email_sent:
        return success(result, "User invited successfully.", 201)
    return success(
        result,
        "User account created, but the invitation email could not be sent.",
        201,
    )


@bp.patch("/admin/users/<user_id>")
@permission_required("users.update")
def update_user(user_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("users", {"_id": user_id})
    if not existing:
        return failure("User not found", status=404)
    payload = request.get_json(silent=True) or {}
    if "incentive_percentage" in payload:
        return failure("Incentives must be configured by category", status=422, error="category_incentive_configuration_required")
    allowed = {"name", "phone", "role_id", "manager_id", "company_ids", "customer_company_ids", "customer_ids", "active", "device_access_mode", "incentive_rates"}
    changes = {key: value for key, value in payload.items() if key in allowed}
    if "device_access_mode" in changes and changes["device_access_mode"] not in {"any_authorized_device", "approved_devices_only"}:
        return failure("Invalid device access policy", status=422)
    target_role_id = str(changes.get("role_id") or existing.get("role_id") or "")
    if "manager_id" in changes or "role_id" in changes:
        try:
            changes["manager_id"] = _normalise_manager_id(
                store, changes.get("manager_id", existing.get("manager_id")),
                target_role_id=target_role_id, target_user_id=user_id,
            )
        except ValueError as exc:
            return failure(str(exc), status=422, error="invalid_manager_assignment")
    if "incentive_rates" in changes:
        if not _can_configure_incentive():
            return failure("Only a Superadmin can configure category incentive percentages", status=403, error="incentive_configuration_forbidden")
        if target_role_id not in INCENTIVE_ELIGIBLE_ROLES:
            if changes["incentive_rates"] not in (None, {}, ""):
                return failure("Superadmin accounts do not have incentive configuration", status=422, error="incentive_not_applicable")
            changes["incentive_rates"] = {}
        else:
            try:
                changes["incentive_rates"] = _normalise_incentive_rates(changes["incentive_rates"])
            except ValueError as exc:
                return failure(str(exc), status=422, error="invalid_incentive_configuration")
            unknown_categories = sorted(set(changes["incentive_rates"]) - set(INCENTIVE_CATEGORIES))
            if unknown_categories:
                return failure("Incentive configuration contains an unknown category", status=422, error="invalid_incentive_category", category_id=unknown_categories[0])
    if "role_id" in changes and target_role_id not in INCENTIVE_ELIGIBLE_ROLES:
        # A role change to Superadmin (or a non-incentive role) must never
        # retain a stale percentage from the previous role.
        changes["incentive_percentage"] = None
        changes["incentive_rates"] = {}
    elif "role_id" in changes and target_role_id in INCENTIVE_ELIGIBLE_ROLES:
        # Give a newly eligible user the migration default when no prior
        # percentage exists; preserve an already configured historical value.
        # Category configuration is explicit.  Do not synthesize a scalar
        # default when a user becomes eligible.
        changes.setdefault("incentive_rates", {})
    actor = current_user() or {}
    if ("manager_id" in changes or "role_id" in changes) and str(actor.get("role_id") or "") not in {"admin", "superadmin"}:
        return failure("Only administrators can manage the user hierarchy", status=403, error="hierarchy_management_forbidden")
    if user_id == actor.get("_id") and "role_id" in changes and changes["role_id"] != existing.get("role_id"):
        return failure("You cannot change your own role", status=403)
    assignment_field = next((field for field in ("customer_ids", "customer_company_ids", "company_ids") if field in changes), None)
    if assignment_field:
        if not can_view_all_customers(actor):
            return failure("You do not have permission to manage customer assignments", status=403)
        requested_ids = changes.get(assignment_field)
        if not isinstance(requested_ids, list):
            return failure("Customer assignments must be a list", status=422)
        desired_ids: list[str] = []
        for value in requested_ids:
            customer_id = str(value).strip()
            customer = customer_record(customer_id)
            if not customer or customer.get("is_issuer"):
                return failure("Customer assignment is invalid", status=422, error="invalid_customer_assignment")
            if customer_id not in desired_ids:
                desired_ids.append(customer_id)
        current_ids = set(customer_access_ids_for_user(user_id, include_created=False))
        for customer_id in current_ids - set(desired_ids):
            customer = customer_record(customer_id) or {}
            if str(customer.get("created_by_user_id") or "") == str(user_id):
                return failure("The customer creator must remain assigned", status=409, error="customer_owner_assignment_required")
        for customer_id in set(desired_ids) | current_ids:
            customer = customer_record(customer_id)
            if not customer:
                continue
            assigned = list(dict.fromkeys(str(value) for value in (customer.get("assigned_user_ids") or []) if value))
            if customer_id in desired_ids and user_id not in assigned:
                assigned.append(user_id)
                store.update_one("customers", {"_id": customer_id}, {"assigned_user_ids": assigned})
                audit("customer_assigned_to_user", "customer", customer_id, {"actor_user_id": actor.get("_id"), "target_user_id": user_id})
            elif customer_id not in desired_ids and user_id in assigned:
                assigned = [value for value in assigned if value != user_id]
                store.update_one("customers", {"_id": customer_id}, {"assigned_user_ids": assigned})
                audit("customer_unassigned_from_user", "customer", customer_id, {"actor_user_id": actor.get("_id"), "target_user_id": user_id})
        changes["customer_ids"] = desired_ids
        # Legacy aliases remain synchronized as compatibility bridges.
        changes["customer_company_ids"] = desired_ids
        changes["company_ids"] = desired_ids
    if changes.get("role_id") and not store.find_one("roles", {"_id": changes["role_id"]}):
        return failure("Role not found", status=422)
    row = store.update_one("users", {"_id": user_id}, changes)
    if "manager_id" in changes and changes.get("manager_id") != existing.get("manager_id"):
        audit("user.manager_changed", "user", user_id, {
            "actor_user_id": actor.get("_id"), "old_manager_id": existing.get("manager_id"),
            "new_manager_id": changes.get("manager_id"),
        })
    if "incentive_rates" in changes:
        configured = changes.get("incentive_rates") or {}
        existing_configs, _ = store.list("incentive_configurations", {"user_id": user_id}, limit=10_000)
        for config in existing_configs:
            if str(config.get("category_id")) not in configured:
                store.delete_one("incentive_configurations", {"_id": config.get("_id")})
        for category_id, rate in configured.items():
            store.update_one("incentive_configurations", {"user_id": user_id, "category_id": category_id}, {
                "user_id": user_id, "category_id": category_id, "category_name": INCENTIVE_CATEGORIES[category_id], "incentive_percentage": rate,
                "updated_by_user_id": actor.get("_id"), "updated_at": utcnow(),
            }, upsert=True)
        audit("incentive.configuration_updated", "user", user_id, {
            "old_value": existing.get("incentive_rates") or {}, "new_value": configured,
            "updated_by": actor.get("_id"),
        })
    if changes.get("active") is False:
        current_app.logger.info("session_revoked user_id=%s reason=admin_deactivated", user_id)
    audit("user.update", "user", user_id, {"fields": sorted(changes)})
    return success(row, "User updated")


@bp.get("/admin/roles")
@permission_required("roles.view")
def list_roles():
    roles, total = current_app.extensions["store"].list("roles", limit=100, sort="display_name", direction=1)
    return success({"items": roles, "total": total})


@bp.get("/admin/incentive-rules")
@permission_required("incentives.manage")
def list_incentive_rules():
    rows, total = current_app.extensions["store"].list("incentive_rules", limit=1000, sort="client_type", direction=1)
    return success({"items": rows, "total": total})


@bp.get("/admin/incentive-configurator")
@permission_required("incentives.manage")
def incentive_configurator():
    """Return a business-facing projection of the existing incentive rules.

    The projection deliberately calls the same resolver used by Order
    Confirmation creation.  The browser therefore displays effective rates
    without reimplementing rule priority or treating the UI as authoritative.
    No incentive, allocation, or historical snapshot is modified here.
    """
    store = current_app.extensions["store"]
    rules, rules_total = store.list("incentive_rules", limit=10_000, sort="client_type", direction=1)
    category_rows, _ = store.list(
        "categories", {"active": True, "calculator_enabled": True},
        limit=100, sort="sort_order", direction=1,
    )
    product_types = [
        {"id": str(row.get("_id")), "name": str(row.get("name") or row.get("_id"))}
        for row in category_rows if str(row.get("_id") or "") in INCENTIVE_CATEGORIES
    ]
    user_rows, _ = store.list("users", {"active": {"$ne": False}}, limit=10_000, sort="name", direction=1)
    managers = [row for row in user_rows if str(row.get("role_id") or "") in MANAGER_ROLE_IDS]
    users = [row for row in user_rows if str(row.get("role_id") or "") == "user"]
    manager_by_id = {str(row.get("_id")): row for row in managers if row.get("_id")}

    def public_user(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "_id": row.get("_id"), "name": row.get("name"), "email": row.get("email"),
            "role_id": row.get("role_id"), "manager_id": row.get("manager_id"),
        }

    user_configurations = []
    for user in users:
        manager = manager_by_id.get(str(user.get("manager_id") or ""))
        matrix = []
        for client_type in CLIENT_TYPES:
            for product_type in product_types:
                product_type_id = product_type["id"]
                user_rate = resolve_incentive_rate(
                    store, recipient=user, client_type=client_type,
                    category_id=product_type_id, allocation_type="creator",
                )
                manager_team_rate = None
                if manager:
                    manager_team_rate = resolve_incentive_rate(
                        store, recipient=manager, client_type=client_type,
                        category_id=product_type_id, allocation_type="manager_override",
                    )
                matrix.append({
                    "customer_type": client_type,
                    "product_type_id": product_type_id,
                    "product_type_name": product_type["name"],
                    "user_rate": user_rate,
                    "manager_team_rate": manager_team_rate,
                    "user_configured": user_rate is not None,
                    "manager_configured": manager_team_rate is not None if manager else False,
                })
        user_configurations.append({
            **public_user(user),
            "manager": public_user(manager) if manager else None,
            "configurations": matrix,
        })

    manager_configurations = []
    for manager in managers:
        connected_users = [public_user(row) for row in users if str(row.get("manager_id") or "") == str(manager.get("_id"))]
        matrix = []
        for client_type in CLIENT_TYPES:
            for product_type in product_types:
                product_type_id = product_type["id"]
                team_rate = resolve_incentive_rate(
                    store, recipient=manager, client_type=client_type,
                    category_id=product_type_id, allocation_type="manager_override",
                )
                creator_rate = resolve_incentive_rate(
                    store, recipient=manager, client_type=client_type,
                    category_id=product_type_id, allocation_type="creator",
                )
                matrix.append({
                    "customer_type": client_type,
                    "product_type_id": product_type_id,
                    "product_type_name": product_type["name"],
                    "team_rate": team_rate,
                    "creator_rate": creator_rate,
                    "team_configured": team_rate is not None,
                    "creator_configured": creator_rate is not None,
                })
        manager_configurations.append({
            **public_user(manager),
            "connected_users": connected_users,
            "configurations": matrix,
        })

    customers, _ = store.list(
        "customers", {"active": {"$ne": False}, "status": {"$ne": "archived"}},
        limit=100_000, sort="company_name", direction=1,
    )
    customer_options = [
        {"_id": row.get("_id"), "name": row.get("company_name") or row.get("name")}
        for row in customers if row.get("_id") and not row.get("is_issuer")
    ]

    # Keep the business-facing list compact while exposing the existing
    # category-specific rule records that the resolver already understands.
    # ``Configurable`` is deliberately a projection-only label; MongoDB
    # continues to store stable category IDs (or ``*`` for a default fallback).
    rule_groups = []
    group_keys: set[tuple[str, str, str, str | None]] = set()
    for client_type in CLIENT_TYPES:
        group_keys.update({("creator", client_type, "user", None), ("manager_override", client_type, "manager_sales_admin", None), ("creator", client_type, "manager_sales_admin", None)})
    for rule in rules:
        allocation = str(rule.get("allocation_type") or "creator").strip().lower()
        if allocation not in {"creator", "manager_override"}:
            continue
        client = str(rule.get("client_type") or "*").strip().upper()
        role = str(rule.get("recipient_role") or "*").strip().lower()
        customer_id = str(rule.get("customer_id") or "").strip() or None
        if client == "*":
            continue
        group_keys.add((allocation, client, role, customer_id))
    for allocation, client_type, recipient_role, customer_id in sorted(group_keys, key=lambda item: (item[0], item[1], item[2], item[3] or "")):
        scoped_rules = [
            row for row in rules
            if str(row.get("allocation_type") or "creator").strip().lower() == allocation
            and str(row.get("client_type") or "*").strip().upper() == client_type
            and str(row.get("recipient_role") or "*").strip().lower() == recipient_role
            and (str(row.get("customer_id") or "").strip() or None) == customer_id
        ]
        by_category = {
            str(row.get("category_id")): row for row in scoped_rules
            if str(row.get("category_id") or "") in INCENTIVE_CATEGORIES
        }
        wildcard = next((row for row in scoped_rules if str(row.get("category_id") or "") == "*"), None)
        customer_name = next((str(row.get("company_name") or row.get("name")) for row in customers if str(row.get("_id")) == customer_id), None)
        if allocation == "manager_override":
            incentive_type = "Manager Team Incentive"
        elif recipient_role == "manager_sales_admin":
            incentive_type = "Manager Creator Incentive"
        else:
            incentive_type = "User Incentive"
        rates = []
        for product_type in product_types:
            category_id = product_type["id"]
            row = by_category.get(category_id)
            source = row or wildcard
            raw_rate = source.get("rate") if source else None
            try:
                rate = float(raw_rate) if raw_rate is not None else None
            except (TypeError, ValueError):
                rate = None
            rates.append({
                "product_type_id": category_id,
                "product_type_name": product_type["name"],
                "rate": rate,
                "configured": bool(row),
                "rule_id": row.get("_id") if row else None,
                # A projection-only group with no persisted rule is still an
                # available (active) configuration target.  Persisted
                # inactive rules remain inactive, while missing rates are
                # represented as "Not configured" rather than hiding the
                # customer-type row from the default Active view.
                "active": bool(source is None or source.get("active") is not False),
            })
        configured_count = sum(1 for item in rates if item["configured"])
        rule_groups.append({
            "id": f"{allocation}:{client_type}:{recipient_role}:{customer_id or 'default'}",
            "incentive_type": incentive_type,
            "allocation_type": allocation,
            "recipient_role": recipient_role,
            "customer_type": client_type,
            "customer_id": customer_id,
            "customer_name": customer_name,
            "product_type_label": "Configurable" if configured_count else "Default",
            "configured_count": configured_count,
            "active": all(item["active"] for item in rates),
            "product_rates": rates,
        })
    return success({
        "customer_types": list(CLIENT_TYPES),
        "product_types": product_types,
        "users": user_configurations,
        "managers": manager_configurations,
        "customers": customer_options,
        "rules": rules,
        "rules_total": rules_total,
        "rule_groups": rule_groups,
        "resolution": {
            "basis": "OC line net amount",
            "dimensions": ["customer_type", "product_type", "recipient", "allocation_type"],
            "historical_snapshots_preserved": True,
        },
    })


@bp.post("/admin/incentive-rules/configure-products")
@permission_required("incentives.manage")
def configure_product_incentive_rules():
    """Create/update category-specific rates without changing old snapshots."""
    store = current_app.extensions["store"]
    payload = request.get_json(silent=True) or {}
    allocation_type = str(payload.get("allocation_type") or "creator").strip().lower()
    if allocation_type not in {"creator", "manager_override"}:
        return failure("Unsupported incentive allocation type", status=422)
    client_type = str(payload.get("client_type") or "").strip().upper()
    if client_type not in CLIENT_TYPES:
        return failure("Client type must be WHOLESALER, DEALER or CUSTOMER", status=422)
    recipient_role = str(payload.get("recipient_role") or "*").strip().lower()
    if recipient_role not in {"admin", "manager", "manager_sales_admin", "user", "*"}:
        return failure("Unsupported incentive recipient role", status=422)
    customer_id = str(payload.get("customer_id") or "").strip() or None
    if customer_id and not store.find_one("customers", {"_id": customer_id}):
        return failure("Customer not found", status=404)
    raw_rates = payload.get("rates")
    if not isinstance(raw_rates, dict) or not raw_rates:
        return failure("Provide at least one product-specific incentive rate", status=422)
    unknown = sorted(set(str(key).strip().lower() for key in raw_rates) - set(INCENTIVE_CATEGORIES))
    if unknown:
        return failure("Incentive configuration contains an unknown category", status=422, category_id=unknown[0])
    normalized_rates: dict[str, float] = {}
    for category_id, raw_rate in raw_rates.items():
        category = str(category_id).strip().lower()
        try:
            rate = float(raw_rate)
        except (TypeError, ValueError):
            return failure("Incentive percentage must be 0% to 6% in 0.5% steps", status=422)
        if rate not in INCENTIVE_PERCENTAGES:
            return failure("Incentive percentage must be 0% to 6% in 0.5% steps", status=422)
        normalized_rates[category] = rate
    updated = []
    now = utcnow()
    for category_id, rate in normalized_rates.items():
        identity = {
            "allocation_type": allocation_type,
            "client_type": client_type,
            "recipient_role": recipient_role,
            "category_id": category_id,
        }
        lookup = {**identity, "customer_id": customer_id} if customer_id else {
            **identity,
            "$or": [
                {"customer_id": None},
                {"customer_id": ""},
                {"customer_id": {"$exists": False}},
            ],
        }
        changes = {
            "scope": "customer" if customer_id else "client_type_category",
            "rate": rate,
            "base": "OC_NET_AMOUNT",
            "active": True,
            "updated_by_user_id": (current_user() or {}).get("_id"),
            "updated_at": now,
        }
        existing = store.find_one("incentive_rules", lookup)
        if existing:
            row = store.update_one("incentive_rules", {"_id": existing.get("_id")}, changes)
        else:
            row = store.insert_one("incentive_rules", {**identity, **({"customer_id": customer_id} if customer_id else {}), **changes})
        if row:
            updated.append(row)
    audit("incentive.product_configuration_updated", "incentive_rule", str(updated[0].get("_id")) if updated else "unknown", {"allocation_type": allocation_type, "client_type": client_type, "category_count": len(updated)})
    return success({"items": updated, "rates": normalized_rates}, "Product-specific incentive configuration saved")


@bp.post("/admin/incentive-rules")
@permission_required("incentives.manage")
def create_incentive_rule():
    """Create a centrally-resolved incentive override.

    Rules are data, not frontend constants.  The optional customer, client
    type, role and category fields map directly to the resolver precedence.
    """
    store = current_app.extensions["store"]
    payload = request.get_json(silent=True) or {}
    allocation_type = str(payload.get("allocation_type") or "creator").strip().lower()
    if allocation_type not in {"creator", "manager_override"}:
        return failure("Unsupported incentive allocation type", status=422)
    raw_client_type = str(payload.get("client_type") or "*").strip().upper()
    client_type = raw_client_type if raw_client_type == "*" or raw_client_type in CLIENT_TYPES else ""
    if not client_type:
        return failure("Client type must be WHOLESALER, DEALER, CUSTOMER or *", status=422)
    category_id = str(payload.get("category_id") or "*").strip().lower()
    if category_id != "*" and category_id not in INCENTIVE_CATEGORIES:
        return failure("Category must be blankets, mpacks, chemicals or *", status=422)
    recipient_role = str(payload.get("recipient_role") or "*").strip().lower()
    if recipient_role != "*" and recipient_role not in {"admin", "manager", "manager_sales_admin", "user"}:
        return failure("Unsupported incentive recipient role", status=422)
    customer_id = str(payload.get("customer_id") or "").strip() or None
    if customer_id and not store.find_one("customers", {"_id": customer_id}):
        return failure("Customer not found", status=404)
    try:
        rate = float(payload.get("rate"))
    except (TypeError, ValueError):
        return failure("Incentive percentage must be 0% to 6% in 0.5% steps", status=422)
    if rate not in INCENTIVE_PERCENTAGES:
        return failure("Incentive percentage must be 0% to 6% in 0.5% steps", status=422)
    active = payload.get("active", True)
    if not isinstance(active, bool):
        return failure("Incentive rule status must be active or inactive", status=422)
    existing = store.find_one("incentive_rules", {
        "allocation_type": allocation_type, "customer_id": customer_id,
        "client_type": client_type, "recipient_role": recipient_role, "category_id": category_id,
    })
    if existing:
        return failure("An incentive rule with this scope already exists", status=409)
    scope = "customer" if customer_id else "client_type_category" if client_type != "*" and category_id != "*" else "client_type" if client_type != "*" else "role" if recipient_role != "*" else "global"
    row = store.insert_one("incentive_rules", {
        "scope": scope, "allocation_type": allocation_type, "customer_id": customer_id,
        "client_type": client_type, "recipient_role": recipient_role, "category_id": category_id,
        "rate": rate, "base": "OC_NET_AMOUNT", "active": active,
        "created_at": utcnow(), "created_by_user_id": (current_user() or {}).get("_id"),
    })
    audit("incentive.rule_created", "incentive_rule", row.get("_id"), {"scope": scope, "rate": rate})
    return success(row, "Incentive rule created", 201)


@bp.patch("/admin/incentive-rules/<rule_id>")
@permission_required("incentives.manage")
def update_incentive_rule(rule_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("incentive_rules", {"_id": rule_id})
    if not existing:
        return failure("Incentive rule not found", status=404)
    payload = request.get_json(silent=True) or {}
    changes: dict[str, Any] = {}
    if "rate" in payload:
        try:
            rate = float(payload.get("rate"))
        except (TypeError, ValueError):
            return failure("Incentive percentage must be 0% to 6% in 0.5% steps", status=422)
        if rate not in INCENTIVE_PERCENTAGES:
            return failure("Incentive percentage must be 0% to 6% in 0.5% steps", status=422)
        changes["rate"] = rate
    if "active" in payload:
        if not isinstance(payload.get("active"), bool):
            return failure("Incentive rule status must be active or inactive", status=422)
        changes["active"] = payload["active"]
    if not changes:
        return failure("Provide a rate or status to update", status=422)
    changes.update({"updated_at": utcnow(), "updated_by_user_id": (current_user() or {}).get("_id")})
    row = store.update_one("incentive_rules", {"_id": rule_id}, changes)
    audit("incentive.rule_updated", "incentive_rule", rule_id, {key: value for key, value in changes.items() if key in {"rate", "active"}})
    return success(row, "Incentive rule updated")


@bp.get("/admin/pricing/client-types")
@permission_required("pricing.history")
def client_type_pricing():
    row = current_app.extensions["store"].find_one("pricing_configurations", {"_id": "client-pricing"}) or {}
    return success(row)


@bp.patch("/admin/roles/<role_id>")
@permission_required("roles.manage")
def update_role(role_id: str):
    payload = request.get_json(silent=True) or {}
    allowed_permissions = {row["_id"] for row in current_app.extensions["store"].list("permissions", limit=1000)[0]}
    permissions = payload.get("permissions")
    changes = {}
    if payload.get("display_name"):
        changes["display_name"] = str(payload["display_name"])[:100]
    if permissions is not None:
        if not isinstance(permissions, list) or not set(permissions).issubset(allowed_permissions):
            return failure("One or more permissions are invalid", status=422)
        changes["permissions"] = permissions
    row = current_app.extensions["store"].update_one("roles", {"_id": role_id}, changes)
    if not row:
        return failure("Role not found", status=404)
    audit("role.update", "role", role_id, {"fields": sorted(changes)})
    return success(row, "Role updated")


@bp.get("/admin/audit-logs")
@permission_required("audit_logs.view")
def audit_logs():
    page = max(int(request.args.get("page", 1)), 1)
    rows, total = current_app.extensions["store"].list("audit_logs", page=page, limit=50)
    return success({"items": rows, "total": total, "page": page})


@bp.get("/settings")
@permission_required("settings.view")
def get_settings():
    store = current_app.extensions["store"]
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    if "watermark_enabled" not in settings:
        store.update_one("app_settings", {"_id": "system"}, {"watermark_enabled": True})
        settings["watermark_enabled"] = True
    return success(settings)


@bp.get("/admin/email/health")
@permission_required("settings.manage")
def email_health():
    """Return safe configuration and the latest delivery result; never secrets."""
    zoho = current_app.extensions["zoho_oauth"].status()
    latest, _ = current_app.extensions["store"].list("email_logs", limit=1, sort="created_at", direction=-1)
    last = latest[0] if latest else None
    return success({
        "provider": "zoho_mail_api",
        "configuration_valid": zoho["configured"],
        "oauth_connected": zoho["connected"],
        "account_email": zoho["account_email"],
        "account_id": zoho.get("account_id"),
        "account_id_configured": zoho["account_id_configured"],
        "api_domain_status": zoho["api_domain_status"],
        "last_attempt": ({
            "status": last.get("status"),
            "stage": last.get("stage"),
            "diagnostic_id": last.get("diagnostic_id"),
            "error_code": last.get("error_code"),
            "message_type": last.get("message_type"),
            "created_at": last.get("created_at"),
        } if last else None),
    })


@bp.patch("/settings")
@permission_required("settings.manage")
def update_settings():
    payload = request.get_json(silent=True) or {}
    existing_settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    forbidden = {"master_currency", "quotation_prefix"}
    if forbidden.intersection(payload):
        return failure("Master currency and numbering prefix cannot be changed here", status=422)
    allowed = {
        "brand_name", "brand_logo_path",
        "quotation_validity_days", "commercial_conditions", "discount_rules", "surcharge_rules",
        "payment_terms", "transport_options", "issuer", "watermark_enabled",
    }
    changes = {key: value for key, value in payload.items() if key in allowed}
    if "watermark_enabled" in changes and (current_user() or {}).get("role_id") != "superadmin":
        return failure("Only a Superadmin can change the workspace watermark", status=403)
    if "watermark_enabled" in changes and not isinstance(changes["watermark_enabled"], bool):
        return failure("watermark_enabled must be a boolean", status=422)
    if "issuer" in changes:
        issuer = changes["issuer"] if isinstance(changes["issuer"], dict) else {}
        changes["issuer"] = {
            "name": "Moneda Technologies",
            "email": str(issuer.get("email") or "business@monedatechnologies.com").strip(),
            "phone": issuer.get("phone"), "address": issuer.get("address"),
            "bank_information": issuer.get("bank_information"), "tax_information": issuer.get("tax_information"),
        }
    row = current_app.extensions["store"].update_one("app_settings", {"_id": "system"}, changes)
    if "watermark_enabled" in changes:
        audit("SCREENSHOT_PROTECTION_ENABLED" if changes["watermark_enabled"] else "SCREENSHOT_PROTECTION_DISABLED", "settings", "system", {"reason": str(payload.get("reason") or "Superadmin settings change"), "previous_state": existing_settings.get("watermark_enabled", True), "new_state": changes["watermark_enabled"]})
    else:
        audit("settings.update", "settings", "system", {"fields": sorted(changes)})
    return success(row, "Settings updated")


@bp.post("/admin/import/products/validate")
@permission_required("products.create")
def validate_product_import():
    try:
        rows = _parse_product_upload(request.files.get("file"))
    except ValueError as exc:
        return failure(str(exc), status=422)
    result = _validate_product_rows(rows)
    return success(result)


def _parse_product_upload(upload: Any) -> list[dict]:
    if not upload or not upload.filename:
        raise ValueError("A CSV, JSON or Excel-compatible product file is required")
    filename = upload.filename.lower()
    content = upload.stream.read()
    if filename.endswith(".json"):
        try:
            parsed = json.loads(content.decode("utf-8-sig"))
        except Exception as exc:
            raise ValueError("JSON could not be parsed") from exc
        if isinstance(parsed, dict):
            parsed = parsed.get("products", parsed.get("items", []))
        if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
            raise ValueError("JSON must contain a products or items array")
        return [dict(row) for row in parsed]
    if filename.endswith(".xlsx"):
        try:
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            sheet = workbook.active
            values = list(sheet.values)
            headers = [str(value or "").strip() for value in values[0]] if values else []
            return [dict(zip(headers, row)) for row in values[1:]]
        except ImportError as exc:
            raise ValueError("Excel import support is not installed") from exc
        except Exception as exc:
            raise ValueError("Excel file could not be parsed") from exc
    if filename.endswith(".csv"):
        try:
            return [dict(row) for row in csv.DictReader(io.StringIO(content.decode("utf-8-sig")))]
        except Exception as exc:
            raise ValueError("CSV could not be parsed") from exc
    raise ValueError("Only CSV, JSON or XLSX product files are accepted")


def _validate_product_rows(rows: list[dict]) -> dict:
    store = current_app.extensions["store"]
    required = {"name", "category_id", "pricing_type", "unit"}
    existing_articles = {str(row.get("article_no")).strip().lower() for row in store.list("products", limit=100000)[0] if row.get("article_no")}
    seen_articles: set[str] = set()
    valid, invalid, warnings = [], [], []
    for index, raw in enumerate(rows, start=2):
        row = {str(key).strip(): value for key, value in raw.items()}
        errors = [f"Missing {field}" for field in sorted(required) if not str(row.get(field) or "").strip()]
        category_id = str(row.get("category_id") or "").strip()
        if category_id and not store.find_one("categories", {"_id": category_id, "active": True}):
            errors.append("Category is not active")
        pricing_type = str(row.get("pricing_type") or "").strip()
        if pricing_type and pricing_type not in PRICING_TYPES:
            errors.append("Unsupported pricing type")
        article = str(row.get("article_no") or "").strip().lower()
        if article and (article in existing_articles or article in seen_articles):
            errors.append("Duplicate article number")
        if row.get("price") not in (None, ""):
            try:
                if Decimal(str(row["price"])) < 0:
                    errors.append("Price cannot be negative")
            except InvalidOperation:
                errors.append("Price must be numeric")
        entry = {"row": index, "data": row, "errors": errors}
        if errors:
            invalid.append(entry)
        else:
            valid.append(entry)
            if article:
                seen_articles.add(article)
            if row.get("price") in (None, ""):
                warnings.append({"row": index, "message": "Price is empty; product will remain pending"})
    return {"valid_rows": valid, "invalid_rows": invalid, "warnings": warnings, "summary": {"valid": len(valid), "invalid": len(invalid), "warnings": len(warnings)}}


@bp.post("/admin/import/products")
@permission_required("products.create")
def commit_product_import():
    if str(request.form.get("confirm", "")).lower() not in {"true", "1", "yes"}:
        return failure("Import confirmation is required", status=422)
    try:
        rows = _parse_product_upload(request.files.get("file"))
    except ValueError as exc:
        return failure(str(exc), status=422)
    validation = _validate_product_rows(rows)
    if validation["invalid_rows"]:
        return failure("Import validation failed", validation["invalid_rows"], status=422)
    store = current_app.extensions["store"]
    inserted = []
    for entry in validation["valid_rows"]:
        raw = entry["data"]
        base_id = re.sub(r"[^a-z0-9]+", "-", str(raw["name"]).lower()).strip("-") or "product"
        product_id, suffix = base_id, 2
        while store.find_one("products", {"_id": product_id}):
            product_id, suffix = f"{base_id}-{suffix}", suffix + 1
        price = None if raw.get("price") in (None, "") else float(Decimal(str(raw["price"])).quantize(Decimal("0.01")))
        inserted.append(store.insert_one("products", {"_id": product_id, "article_no": str(raw.get("article_no", "")).strip(), "sku": str(raw.get("sku") or product_id.upper().replace("-", "_")), "name": str(raw["name"]).strip(), "category_id": str(raw["category_id"]).strip(), "description": str(raw.get("description", "")), "pricing": {"master_currency": "EUR", "price": price, "pricing_type": str(raw["pricing_type"]).strip(), "unit": str(raw["unit"]).strip()}, "tax": {"mode": None, "rate": None, "override_enabled": False}, "discount_rules": {"enabled": True, "step": 0.5, "default_max_percent": 5, "privileged_max_percent": 10}, "configuration": {}, "pricing_status": "configured" if price is not None else "pending", "active": True, "source": "admin_import"}))
    audit("product.import", "product", "bulk", {"count": len(inserted)})
    return success({"inserted": inserted, "summary": {"inserted": len(inserted), "warnings": len(validation["warnings"])}}, "Product import committed", 201)
