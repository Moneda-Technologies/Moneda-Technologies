from __future__ import annotations

from decimal import Decimal, InvalidOperation
import csv
import io
import json
import re
import secrets
from typing import Any

from flask import Blueprint, current_app, request, session

from app.api.responses import failure, success
from app.middleware.access import can_view_all_customers, customer_access_ids_for_user, current_user, customer_record, permission_required
from app.repositories.store import utcnow
from app.services.audit import audit
from app.catalog.service import is_legacy_product
from app.devices.service import APPROVED, DENIED, PENDING, REVOKED, safe_device, _append_history, _history_entry, notify_reinstatement, notify_device_decision, notify_device_revocation, _notify_superadmins


bp = Blueprint("admin", __name__, url_prefix="/api")

PRICING_TYPES = {
    "fixed", "quantity", "per_piece", "per_bar", "per_sqm", "per_meter", "per_litre",
    "per_kg", "per_pack", "per_packet", "per_roll", "formula", "on_request",
}


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
    rows, total = store.list("users", limit=100, sort="name", direction=1)
    roles = {row["_id"]: row for row in store.list("roles", limit=100)[0]}
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
        role = roles.get(row.get("role_id"), {})
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
    return success({"items": rows, "total": total})


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
        _notify_superadmins(user, updated, reinstated=True)
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
    email = str(payload.get("email", "")).strip().lower()
    name = str(payload.get("name", "")).strip()
    store = current_app.extensions["store"]
    if not email or "@" not in email or len(name) < 2:
        return failure("A valid name and email are required", status=422)
    if store.find_one("users", {"email": email}):
        return failure("User already exists", status=409)
    role_id = payload.get("role_id", "user")
    if not store.find_one("roles", {"_id": role_id}):
        return failure("Role not found", status=422)
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
    row = store.insert_one("users", {"email": email, "name": name, "phone": payload.get("phone", ""),
        "role_id": role_id, "customer_ids": customer_ids, "customer_company_ids": customer_ids, "company_ids": customer_ids, "active": True, "device_access_mode": device_access_mode})
    for customer_id in customer_ids:
        customer = customer_record(customer_id)
        assigned = list(dict.fromkeys(str(value) for value in (customer or {}).get("assigned_user_ids", []) if value))
        if row["_id"] not in assigned:
            assigned.append(row["_id"])
            store.update_one("customers", {"_id": customer_id}, {"assigned_user_ids": assigned})
            audit("customer_assigned_to_user", "customer", customer_id, {"actor_user_id": (current_user() or {}).get("_id"), "target_user_id": row["_id"]})
    audit("user.create", "user", str(row["_id"]))
    return success(row, "User invited. They can sign in with email OTP.", 201)


@bp.patch("/admin/users/<user_id>")
@permission_required("users.update")
def update_user(user_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("users", {"_id": user_id})
    if not existing:
        return failure("User not found", status=404)
    allowed = {"name", "phone", "role_id", "company_ids", "customer_company_ids", "customer_ids", "active", "device_access_mode"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    if "device_access_mode" in changes and changes["device_access_mode"] not in {"any_authorized_device", "approved_devices_only"}:
        return failure("Invalid device access policy", status=422)
    actor = current_user() or {}
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
    if changes.get("active") is False:
        current_app.logger.info("session_revoked user_id=%s reason=admin_deactivated", user_id)
    audit("user.update", "user", user_id, {"fields": sorted(changes)})
    return success(row, "User updated")


@bp.get("/admin/roles")
@permission_required("roles.view")
def list_roles():
    roles, total = current_app.extensions["store"].list("roles", limit=100, sort="display_name", direction=1)
    return success({"items": roles, "total": total})


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
