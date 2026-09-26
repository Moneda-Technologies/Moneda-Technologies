from __future__ import annotations

from decimal import Decimal, InvalidOperation
from datetime import datetime, time, timedelta, timezone
import csv
import io
import json
import re
import secrets
from typing import Any

from flask import Blueprint, current_app, request, session, Response
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
from app.middleware.access import can_view_all_customers, customer_access_ids_for_user, current_user, customer_record, permission_required, superadmin_required
from app.repositories.store import utcnow
from app.services.audit import audit
from app.catalog.service import is_legacy_product
from app.finance.service import INCENTIVE_CATEGORIES, INCENTIVE_ELIGIBLE_ROLES, INCENTIVE_PERCENTAGES
from app.services.business_logic import (
    CLIENT_TYPES,
    INCENTIVE_CONFIGURATION_SCOPES,
    INCENTIVE_CONFIGURATION_STATUSES,
    INCENTIVE_HALF_STEPS,
    IncentiveConfigurationValidationError,
    MANAGER_ROLE_IDS,
    resolve_incentive_maximum,
    normalize_client_type,
    resolve_incentive_configuration,
    resolve_incentive_rate,
    valid_manager,
)
from app.devices.service import APPROVED, DENIED, PENDING, REVOKED, safe_device, _append_history, _history_entry, notify_reinstatement, notify_device_decision, notify_device_revocation, _notify_superadmins, _create_login_approval
from app.account.signature import SignatureValidationError, save_signature
from app.account.photo import read_photo, save_photo
from app.account.profile_sync import profile_asset_state, synchronize_profile_asset, workdrive_public_status
from app.services.workdrive import WorkDriveError, FAILED as WORKDRIVE_FAILED, PENDING as WORKDRIVE_PENDING, SYNCED as WORKDRIVE_SYNCED


bp = Blueprint("admin", __name__, url_prefix="/api")

PRICING_TYPES = {
    "fixed", "quantity", "per_piece", "per_bar", "per_sqm", "per_meter", "per_litre",
    "per_kg", "per_pack", "per_packet", "per_roll", "formula", "on_request",
}

PRICE_LIST_ACCOUNT_TYPES = ("DISTRIBUTOR", "DEALER")
PRICE_LIST_CATEGORIES = (
    ("blankets", "Blankets", "Shared between Distributor and Dealer"),
    ("mpacks", "Underpacking", "Separate account-type prices when configured"),
    ("chemicals", "Chemicals & Maintenance", "Account-type pricing namespace"),
)


def _mpack_source_machine_rows(store, price_config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the normalized MPack machine/size catalogue from persisted sources.

    The source matrix is written by the idempotent catalog seed from the two
    audited price-list PDFs.  Keeping this lookup here prevents the admin UI
    from depending on a stale, single-product configuration snapshot and lets
    future source rows appear without a frontend model allow-list.
    """
    config = price_config or store.find_one("pricing_configurations", {"_id": "client-pricing"}) or {}
    source_matrix = config.get("mpack_source_matrix") or {}
    rows: dict[tuple[str, str, int, int], dict[str, Any]] = {}
    for account_type in PRICE_LIST_ACCOUNT_TYPES:
        for raw_key, source in (source_matrix.get(account_type) or {}).items():
            if not isinstance(source, dict):
                continue
            parts = str(raw_key).split("|")
            if len(parts) != 3 or "::" not in parts[0] or "x" not in parts[1].lower():
                continue
            manufacturer, machine_model = parts[0].split("::", 1)
            width_text, length_text = parts[1].lower().split("x", 1)
            try:
                width, length, micron = int(width_text), int(length_text), int(float(parts[2]))
            except (TypeError, ValueError):
                continue
            identity = (manufacturer.strip(), machine_model.strip(), width, length)
            row = rows.setdefault(identity, {
                "manufacturer": identity[0], "machine_model": identity[1],
                "width_mm": width, "length_mm": length, "prices": {},
            })
            # Both official lists currently define the same quantity per
            # micron.  Preserve the first source value for validation/fallback;
            # account-specific prices are resolved from source_matrix below.
            row["prices"].setdefault(micron, {
                "thickness_micron": micron,
                "sheets_per_box": source.get("sheets_per_box"),
            })
    if rows:
        normalized = list(rows.values())
        for row in normalized:
            row["prices"] = list((row.get("prices") or {}).values())
        return normalized

    # Backward-compatible read path for an older deployment before the source
    # matrix was seeded.  New startups reconcile this snapshot automatically.
    product = store.find_one("products", {"_id": "mtech-mpack"}) or {}
    return product.get("configuration", {}).get("machine_sizes") or []


def _mpack_machine_name(row: dict[str, Any]) -> str:
    return " ".join(f"{row.get('manufacturer', '')} - {row.get('machine_model', '')}".split()).casefold()


def _mpack_matrix_for_rows(
    account_type: str,
    selected_rows: list[dict[str, Any]],
    price_config: dict[str, Any],
) -> dict[str, Any]:
    """Build the existing MPack size x micron matrix for one model.

    The source matrix remains authoritative for the catalogue identity and
    quantities.  Account-type overrides are layered on top exactly as they
    are in the legacy single-machine response; this helper only packages the
    same data for the grouped manufacturer response.
    """
    matrix_overrides = price_config.get("mpack_price_matrix") or {}
    source_matrix = price_config.get("mpack_source_matrix") or {}
    rows = []
    thicknesses = sorted({
        float(price.get("thickness_micron"))
        for selected in selected_rows
        for price in (selected.get("prices") or [])
        if price.get("thickness_micron") is not None
    })
    thicknesses = [int(value) if value.is_integer() else value for value in thicknesses]
    machine_scope = "::".join(
        str(selected_rows[0].get(field) or "").strip()
        for field in ("manufacturer", "machine_model")
    )
    for selected in selected_rows:
        prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
        box_prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
        source_prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
        source_box_prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
        sheets_per_box = {}
        for price in selected.get("prices") or []:
            thickness_micron = price.get("thickness_micron")
            if thickness_micron is None:
                continue
            key = f"{machine_scope}|{selected.get('width_mm')}x{selected.get('length_mm')}|{thickness_micron}"
            legacy_key = f"{selected.get('width_mm')}x{selected.get('length_mm')}|{thickness_micron}"
            for scope in PRICE_LIST_ACCOUNT_TYPES:
                scoped_overrides = matrix_overrides.get(scope) or {}
                override = scoped_overrides.get(key)
                if override is None:
                    override = scoped_overrides.get(legacy_key)
                source = (source_matrix.get(scope) or {}).get(key)
                if source is None:
                    source = (source_matrix.get(scope) or {}).get(legacy_key)
                source = source if isinstance(source, dict) else {}
                current = ({**source, **override} if isinstance(override, dict)
                           else {**source, **({"price_per_box_eur": override} if override is not None else {})})
                thickness_key = str(int(float(thickness_micron))) if float(thickness_micron).is_integer() else str(thickness_micron)
                prices_eur[scope][thickness_key] = current.get("price_per_sheet_eur")
                box_prices_eur[scope][thickness_key] = current.get("price_per_box_eur")
                source_prices_eur[scope][thickness_key] = source.get("price_per_sheet_eur")
                source_box_prices_eur[scope][thickness_key] = source.get("price_per_box_eur")
                if scope == account_type:
                    sheets_per_box[thickness_key] = current.get("sheets_per_box") or price.get("sheets_per_box")
        rows.append({
            "size": f"{selected.get('width_mm')} x {selected.get('length_mm')} mm",
            "width_mm": selected.get("width_mm"),
            "length_mm": selected.get("length_mm"),
            "sheets_per_box": sheets_per_box,
            "prices_eur": prices_eur,
            "box_prices_eur": box_prices_eur,
            "source_prices_eur": source_prices_eur,
            "source_box_prices_eur": source_box_prices_eur,
        })
    source_metadata = next(iter((source_matrix.get(account_type) or {}).values()), {})
    source_metadata = source_metadata if isinstance(source_metadata, dict) else {}
    return {
        "thicknesses": thicknesses,
        "matrix": rows,
        "source": {key: source_metadata.get(key) for key in ("source", "source_document", "version", "valid_from", "valid_until")},
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
            raise ValueError("Incentive percentage must be 0% to 100% in 0.5% steps") from None
        if rate not in INCENTIVE_PERCENTAGES:
            raise ValueError("Incentive percentage must be 0% to 100% in 0.5% steps")
        rates[key] = rate
    return rates


def _incentive_configuration_status(value: Any, *, default: str = "ENABLED") -> str:
    status = str(value or default).strip().upper().replace(" ", "_")
    if status == "ACTIVE":
        status = "ENABLED"
    if status == "INACTIVE":
        status = "DISABLED"
    return status if status in INCENTIVE_CONFIGURATION_STATUSES else default


def _incentive_configuration_rate(value: Any, *, required: bool = False) -> float | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ValueError("An enabled incentive configuration requires a rate")
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError("Incentive rate must use 0.5% steps between 0% and 100%") from None
    if rate not in INCENTIVE_HALF_STEPS:
        raise ValueError("Incentive rate must use 0.5% steps between 0% and 100%")
    return rate


def _incentive_configuration_date(value: Any, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid ISO date/time") from None
    return parsed


def _incentive_configuration_key(
    user_id: str, allocation_type: str, scope: str, *,
    customer_id: str | None = None, product_id: str | None = None,
    category_id: str | None = None, client_type: str | None = None,
    role: str | None = None,
) -> str:
    # JSON gives us a deterministic, collision-resistant key without relying
    # on user-controlled separators in IDs.  It is used only for upsert
    # identity; the dimension fields remain queryable MongoDB fields.
    return json.dumps([
        str(user_id), str(allocation_type), str(scope), str(customer_id or ""),
        str(product_id or ""), str(category_id or ""), str(client_type or "*"),
        str(role or "*"),
    ], separators=(",", ":"))


def _public_incentive_configuration(row: dict[str, Any], maximum_rate: float | None = None) -> dict[str, Any]:
    result = {key: row.get(key) for key in (
        "_id", "user_id", "recipient_user_id", "allocation_type", "scope",
        "customer_id", "product_id", "category_id", "client_type", "role",
        "recipient_role", "status", "rate", "effective_from", "effective_to",
        "configuration_key", "updated_at", "created_at",
    ) if key in row}
    result["maximum_rate"] = maximum_rate
    result["source"] = "INDIVIDUAL" if row.get("user_id") or row.get("recipient_user_id") else "DEFAULT"
    return result


def _configuration_validation_category(store, *, scope: str, category_id: str | None, product_id: str | None) -> str:
    """Resolve the dimension used when checking an employee ceiling.

    A person/default configuration does not have a category of its own, so it
    must be checked against the generic ceiling rather than an arbitrary
    blanket ceiling.  Product-scoped rows can derive their category from the
    persisted product record.  This keeps maximum validation data-driven.
    """
    if category_id:
        return str(category_id).strip().lower()
    if scope == "product" and product_id:
        product = store.find_one("products", {"_id": product_id}) or {}
        return str(
            product.get("category_id")
            or product.get("family_id")
            or product.get("product_type")
            or "*"
        ).strip().lower()
    return "*"


def _configuration_validation_maximum(
    store, *, recipient: dict[str, Any], client_type: str,
    category_id: str, customer_id: str | None, product_id: str | None,
    allocation_type: str,
) -> float | None:
    """Return the strictest applicable ceiling for a broad configuration.

    A person, client-type, or customer override can apply to more than one
    product family.  Validate it against every known family rather than
    silently choosing one (or defaulting to blankets).
    """
    categories = [category_id] if category_id != "*" else list(INCENTIVE_CATEGORIES)
    maxima = []
    for category in categories:
        maximum = resolve_incentive_maximum(
            store, recipient=recipient, client_type=client_type,
            category_id=category, customer_id=customer_id,
            product_id=product_id, allocation_type=allocation_type,
        )
        if maximum is not None:
            maxima.append(maximum)
    return min(maxima) if maxima else None


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


def _master_price(value: Any, *, decimal_places: int = 2) -> float | None:
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
    quantum = Decimal("1").scaleb(-decimal_places)
    return float(price.quantize(quantum))


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


@bp.get("/admin/price-lists")
@permission_required("pricing.history")
def price_lists():
    """Return the canonical account-type/category price-list hierarchy.

    This endpoint is deliberately a view over the existing product catalogue
    and persisted account-type source matrix; it does not create duplicate
    product records. Blankets and bars carry a shared scope, while
    underpacking/chemicals retain their account-type namespace.
    """
    store = current_app.extensions["store"]
    account_type = str(request.args.get("account_type") or "").strip().upper()
    category = str(request.args.get("category") or "").strip().lower()
    if account_type and account_type not in PRICE_LIST_ACCOUNT_TYPES:
        return failure("Price list account type must be Distributor or Dealer", status=422)
    if category and category not in {item[0] for item in PRICE_LIST_CATEGORIES}:
        return failure("Unknown price list category", status=422)
    if not account_type:
        return success({"account_types": [
            {"code": "DISTRIBUTOR", "label": "Distributor", "description": "Pricing used by Distributor customers."},
            {"code": "DEALER", "label": "Dealer", "description": "Pricing used by Dealer customers."},
        ], "categories": [], "items": [], "currency": "EUR", "valid_from": "2026-07-01", "valid_until": "2026-12-31"})
    if not category:
        return success({"account_type": account_type, "categories": [
            {"id": key, "name": name, "description": description}
            for key, name, description in PRICE_LIST_CATEGORIES
        ], "items": [], "currency": "EUR", "valid_from": "2026-07-01", "valid_until": "2026-12-31"})
    if category == "mpacks" and not request.args.get("machine"):
        price_config = store.find_one("pricing_configurations", {"_id": "client-pricing"}) or {}
        machine_rows = _mpack_source_machine_rows(store, price_config)
        requested_manufacturer = " ".join(str(request.args.get("manufacturer") or "").split()).casefold()
        manufacturer_counts: dict[str, int] = {}
        for row in machine_rows:
            manufacturer = " ".join(str(row.get("manufacturer") or "").split())
            if manufacturer:
                manufacturer_counts[manufacturer] = manufacturer_counts.get(manufacturer, 0) + 1
        manufacturers = [
            {"id": name, "name": name, "size_rows": count}
            for name, count in sorted(manufacturer_counts.items(), key=lambda item: item[0].casefold())
        ]
        if requested_manufacturer:
            machine_rows = [
                row for row in machine_rows
                if " ".join(str(row.get("manufacturer") or "").split()).casefold() == requested_manufacturer
            ]
        machines = []
        seen = set()
        for row in machine_rows:
            key = f"{row.get('manufacturer')}::{row.get('machine_model')}"
            if key in seen:
                continue
            seen.add(key)
            machines.append({"id": key, "manufacturer": row.get("manufacturer"), "machine_model": row.get("machine_model"), "label": f"{row.get('manufacturer')} - {row.get('machine_model')}", "sizes": sum(1 for candidate in machine_rows if candidate.get("manufacturer") == row.get("manufacturer") and candidate.get("machine_model") == row.get("machine_model"))})
        selected_manufacturer = next((item["name"] for item in manufacturers if item["name"].casefold() == requested_manufacturer), None)
        if selected_manufacturer:
            # The manufacturer is the detail entry point.  Group the already
            # filtered source rows by model and return every model matrix in
            # one response; the legacy `machines` list is retained for API
            # compatibility with older clients.
            model_groups: dict[str, list[dict[str, Any]]] = {}
            model_names: dict[str, str] = {}
            for row in machine_rows:
                model = " ".join(str(row.get("machine_model") or "").split())
                if not model:
                    continue
                model_key = model.casefold()
                model_groups.setdefault(model_key, []).append(row)
                model_names.setdefault(model_key, model)
            models = []
            for model_key in sorted(model_groups, key=lambda value: value.casefold()):
                matrix = _mpack_matrix_for_rows(account_type, model_groups[model_key], price_config)
                models.append({
                    "model": model_names[model_key],
                    "machine_model": model_names[model_key],
                    "rows": matrix["matrix"],
                    "thicknesses": matrix["thicknesses"],
                    "source": matrix["source"],
                })
            return success({
                "account_type": account_type,
                "category": category,
                "product": "MPACK",
                "manufacturers": manufacturers,
                "manufacturer": selected_manufacturer,
                "models": models,
                "machines": machines,
                "items": [],
                "total": len(models),
                "currency": "EUR",
            })
        return success({"account_type": account_type, "category": category, "product": "MPACK", "manufacturers": manufacturers, "manufacturer": None, "models": [], "machines": machines, "items": [], "total": len(machines), "currency": "EUR"})
    products, _ = store.list("products", {"category_id": category}, limit=100_000, sort="name", direction=1)
    products = [row for row in products if not is_legacy_product(row) and not (category == "mpacks" and row.get("_id") != "mtech-mpack")]
    bars = []
    if category == "blankets":
        bars = store.list("blanket_bars", limit=10_000, sort="article_no", direction=1)[0]
    if category == "mpacks" and request.args.get("machine"):
        machine = str(request.args.get("machine") or "").strip().casefold()
        machine_label = machine.replace("::", " - ")
        price_config = store.find_one("pricing_configurations", {"_id": "client-pricing"}) or {}
        machine_rows = _mpack_source_machine_rows(store, price_config)
        selected_rows = [row for row in machine_rows if _mpack_machine_name(row) in {machine, machine_label} or str(row.get("machine_model") or "").casefold() == machine]
        if not selected_rows:
            return failure("Mpack machine not found", status=404)
        matrix_overrides = price_config.get("mpack_price_matrix") or {}
        source_matrix = price_config.get("mpack_source_matrix") or {}
        rows = []
        thicknesses = sorted({float(price.get("thickness_micron")) for selected in selected_rows for price in (selected.get("prices") or []) if price.get("thickness_micron") is not None})
        thicknesses = [int(value) if value.is_integer() else value for value in thicknesses]
        machine_scope = "::".join(str(selected_rows[0].get(field) or "").strip() for field in ("manufacturer", "machine_model"))
        for selected in selected_rows:
            prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
            box_prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
            source_prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
            source_box_prices_eur = {scope: {} for scope in PRICE_LIST_ACCOUNT_TYPES}
            sheets_per_box = {}
            for price in selected.get("prices") or []:
                thickness_micron = price.get("thickness_micron")
                if thickness_micron is None:
                    continue
                key = f"{machine_scope}|{selected.get('width_mm')}x{selected.get('length_mm')}|{thickness_micron}"
                legacy_key = f"{selected.get('width_mm')}x{selected.get('length_mm')}|{thickness_micron}"
                for scope in PRICE_LIST_ACCOUNT_TYPES:
                    scoped_overrides = matrix_overrides.get(scope) or {}
                    override = scoped_overrides.get(key)
                    if override is None:
                        override = scoped_overrides.get(legacy_key)
                    source = (source_matrix.get(scope) or {}).get(key)
                    if source is None:
                        source = (source_matrix.get(scope) or {}).get(legacy_key)
                    source = source if isinstance(source, dict) else {}
                    current = ({**source, **override} if isinstance(override, dict)
                               else {**source, **({"price_per_box_eur": override} if override is not None else {})})
                    thickness_key = str(int(float(thickness_micron))) if float(thickness_micron).is_integer() else str(thickness_micron)
                    prices_eur[scope][thickness_key] = current.get("price_per_sheet_eur")
                    box_prices_eur[scope][thickness_key] = current.get("price_per_box_eur")
                    source_prices_eur[scope][thickness_key] = source.get("price_per_sheet_eur")
                    source_box_prices_eur[scope][thickness_key] = source.get("price_per_box_eur")
                    # Quantities belong to the selected account-type source.
                    # Do not let the final Distributor/Dealer loop iteration
                    # overwrite the quantity shown for the current list.
                    if scope == account_type:
                        sheets_per_box[thickness_key] = current.get("sheets_per_box") or price.get("sheets_per_box")
            rows.append({"size": f"{selected.get('width_mm')} x {selected.get('length_mm')} mm", "width_mm": selected.get("width_mm"), "length_mm": selected.get("length_mm"), "sheets_per_box": sheets_per_box, "prices_eur": prices_eur, "box_prices_eur": box_prices_eur, "source_prices_eur": source_prices_eur, "source_box_prices_eur": source_box_prices_eur})
        selected = selected_rows[0]
        source_metadata = next(iter((source_matrix.get(account_type) or {}).values()), {})
        return success({"account_type": account_type, "category": category, "product": "MPACK", "machine": {"manufacturer": selected.get("manufacturer"), "machine_model": selected.get("machine_model")}, "thicknesses": thicknesses, "matrix": rows, "currency": "EUR", "source": {key: source_metadata.get(key) for key in ("source", "source_document", "version", "valid_from", "valid_until")}})
    items = [_resource_payload("product", row) for row in products] + [_resource_payload("bar", row) for row in bars]
    pricing_config = store.find_one("pricing_configurations", {"_id": "client-pricing"}) or {}
    dealer_override = (pricing_config.get("dealer_underpacking") or {}).get("prices") or {}
    for item in items:
        shared = category == "blankets"
        item["account_type"] = account_type
        item["pricing_scope"] = "SHARED" if shared else "ACCOUNT_TYPE"
        # Keep the source audit explicit.  The two supplied RGF PDFs are
        # regional lists with different values, so we must not claim that one
        # PDF was silently imported as the shared canonical list.
        item["source_document"] = "price_list_sources.json" if shared else ("dealer_underpacking_pricing.json" if account_type == "DEALER" else "pricing_eur.json")
        item["valid_from"] = "2026-07-01"
        item["valid_until"] = "2026-12-31"
        if category == "mpacks" and account_type == "DEALER":
            item["configured_override_count"] = sum(1 for value in dealer_override.values() if value is not None)
            item["pricing_note"] = "Dealer-specific values are shown only when configured; no values are invented."
        elif shared:
            item["pricing_note"] = "Shared blanket scope. Supplied Dealer and Distributor PDFs differ by region; source choice is pending, so existing canonical values were preserved."
    return success({"account_type": account_type, "category": category, "items": items, "total": len(items), "currency": "EUR", "valid_from": "2026-07-01", "valid_until": "2026-12-31"})


@bp.patch("/admin/price-lists/mpack")
@superadmin_required
def update_mpack_price_list():
    """Persist one account-type MPack EUR cell without duplicating catalogue rows."""
    payload = request.get_json(silent=True) or {}
    account_type = str(payload.get("account_type") or "").strip().upper()
    if account_type not in PRICE_LIST_ACCOUNT_TYPES:
        return failure("Price list account type must be Distributor or Dealer", status=422)
    machine = payload.get("machine") or {}
    width = payload.get("width_mm")
    length = payload.get("length_mm")
    thickness = payload.get("thickness_micron")
    if not machine or width in (None, "") or length in (None, "") or thickness in (None, ""):
        return failure("Machine, size and thickness are required", status=422)
    store = current_app.extensions["store"]
    price_config = store.find_one("pricing_configurations", {"_id": "client-pricing"}) or {}
    machine_rows = _mpack_source_machine_rows(store, price_config)
    machine_name = " ".join(f"{machine.get('manufacturer', '')} - {machine.get('machine_model', '')}".split()).casefold() if isinstance(machine, dict) else str(machine).strip().casefold()
    valid_machine = False
    valid_cell = False
    matched_machine = None
    for source_row in machine_rows:
        source_name = " ".join(f"{source_row.get('manufacturer', '')} - {source_row.get('machine_model', '')}".split()).casefold()
        if machine_name not in {source_name, str(source_row.get("machine_model") or "").casefold()}:
            continue
        valid_machine = True
        matched_machine = source_row
        if int(source_row.get("width_mm") or 0) == int(width) and int(source_row.get("length_mm") or 0) == int(length):
            valid_cell = any(int(price.get("thickness_micron") or 0) == int(thickness) for price in (source_row.get("prices") or []))
            break
    if not valid_machine or not valid_cell:
        return failure("Machine, size and thickness are not present in the official MPack catalogue", status=422)
    if "price_per_sheet_eur" not in payload and "price_per_box_eur" not in payload and "price_eur" not in payload:
        return failure("A per-sheet or per-box EUR price is required", status=422)
    try:
        sheet_price = _master_price(payload.get("price_per_sheet_eur"), decimal_places=3) if "price_per_sheet_eur" in payload else None
        box_price = _master_price(payload.get("price_per_box_eur", payload.get("price_eur"))) if ("price_per_box_eur" in payload or "price_eur" in payload) else None
    except ValueError as exc:
        return failure(str(exc), status=422)
    machine_scope = "::".join(str(matched_machine.get(field) or "").strip() for field in ("manufacturer", "machine_model"))
    key = f"{machine_scope}|{int(width)}x{int(length)}|{int(thickness)}"
    legacy_key = f"{int(width)}x{int(length)}|{int(thickness)}"
    existing = store.find_one("pricing_configurations", {"_id": "client-pricing"}) or {"_id": "client-pricing"}
    matrix = {**(existing.get("mpack_price_matrix") or {})}
    source = ((existing.get("mpack_source_matrix") or {}).get(account_type) or {}).get(key) or {}
    if not source:
        source = ((existing.get("mpack_source_matrix") or {}).get(account_type) or {}).get(legacy_key) or {}
    old_override = (matrix.get(account_type) or {}).get(key)
    if old_override is None:
        old_override = (matrix.get(account_type) or {}).get(legacy_key)
    old_current = ({**source, **old_override} if isinstance(old_override, dict)
                   else {**source, **({"price_per_box_eur": old_override} if old_override is not None else {})})
    source_price_row = next(
        price for price in matched_machine.get("prices") or []
        if int(price.get("thickness_micron") or 0) == int(thickness)
    )
    configured = {
        "price_per_sheet_eur": sheet_price if "price_per_sheet_eur" in payload else old_current.get("price_per_sheet_eur"),
        "price_per_box_eur": box_price if ("price_per_box_eur" in payload or "price_eur" in payload) else old_current.get("price_per_box_eur"),
        "sheets_per_box": int(source.get("sheets_per_box") or source_price_row.get("sheets_per_box")),
    }
    scoped = {**(matrix.get(account_type) or {}), key: configured}
    matrix[account_type] = scoped
    now = utcnow()
    store.update_one("pricing_configurations", {"_id": "client-pricing"}, {"mpack_price_matrix": matrix, "updated_at": now}, upsert=True)
    history = store.insert_one("price_history", {"resource_id": "mtech-mpack", "product_id": "mtech-mpack", "entity_type": "mpack_matrix", "account_type": account_type, "machine": {"manufacturer": matched_machine.get("manufacturer"), "machine_model": matched_machine.get("machine_model")}, "size": {"width_mm": int(width), "length_mm": int(length)}, "thickness_micron": int(thickness), "old_price_eur": old_current.get("price_per_sheet_eur"), "new_price_eur": configured.get("price_per_sheet_eur"), "old_price_per_box_eur": old_current.get("price_per_box_eur"), "new_price_per_box_eur": configured.get("price_per_box_eur"), "source_price_per_sheet_eur": source.get("price_per_sheet_eur"), "source_price_per_box_eur": source.get("price_per_box_eur"), "currency": "EUR", "changed_by": (current_user() or {}).get("_id"), "changed_by_name": (current_user() or {}).get("name"), "changed_at": now, "effective_from": now, "reason": str(payload.get("reason") or "").strip()[:500], "source": "admin"})
    audit("pricing.mpack_matrix.update", "mpack_matrix", key, {"account_type": account_type, "history_id": history.get("_id")})
    return success({"account_type": account_type, "key": key, **configured}, "MPack EUR price updated")


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
@superadmin_required
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
        if row.get("photo_path"):
            row["profile_photo_url"] = f"/admin/users/{row.get('_id')}/profile/photo/file?v={str(row.get('photo_updated_at') or 'current')}"
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


@bp.get("/admin/users/<user_id>/profile/photo/file")
@permission_required("users.view")
def admin_user_profile_photo_file(user_id: str):
    user = current_app.extensions["store"].find_one("users", {"_id": user_id})
    if not user:
        return failure("User not found", status=404)
    loaded = read_photo(user, current_app.config["UPLOAD_DIRECTORY"])
    if not loaded:
        return failure("Profile photo is not configured", status=404, error="PHOTO_NOT_CONFIGURED")
    metadata, data = loaded
    response = Response(data, mimetype=metadata["mime_type"])
    response.headers["Cache-Control"] = "private, max-age=60"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.get("/admin/users/<user_id>/devices")
@permission_required("users.view")
def list_user_devices(user_id: str):
    store = current_app.extensions["store"]
    actor = current_user() or {}
    if str(actor.get("role_id") or "") not in {"admin", "superadmin"}:
        return failure("Only administrators can inspect trusted devices", status=403)
    if not store.find_one("users", {"_id": user_id}):
        return failure("User not found", status=404)
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
    if str(device.get("device_status") or "").lower() != REVOKED:
        return failure("Only revoked devices can be deleted", status=409, error="invalid_device_state")
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return failure("A reason is required", status=422, error="decision_reason_required")
    audit("DEVICE_DELETED", "device", str(device.get("_id")), {
        "target_user_id": user_id, "device_id": device.get("device_ref") or device.get("_id"),
        "browser": device.get("browser"), "operating_system": device.get("operating_system"),
        "device_type": device.get("device_type"), "previous_status": REVOKED,
        "actor_user_id": actor.get("_id"), "reason": reason,
    })
    if not store.delete_one("devices", {"_id": device.get("_id"), "user_id": user_id, "device_status": REVOKED}):
        return failure("Device could not be deleted", status=409, error="device_delete_conflict")
    return success({"deleted": True, "device_id": device.get("device_ref") or device.get("_id")}, "Revoked device deleted")


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
    if changes.get("active") is False and str(user_id) == str(actor.get("_id")):
        return failure("You cannot deactivate your own account", status=403, error="self_deactivation_forbidden")
    if changes.get("active") is False and existing.get("role_id") == "superadmin":
        active_superadmins = store.count("users", {"role_id": "superadmin", "active": {"$ne": False}})
        if active_superadmins <= 1:
            return failure("At least one active Superadmin must remain", status=409, error="last_superadmin")
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
                # Preserve the row for auditability and historical tracing.
                # A missing category in this legacy payload means that the
                # current individual override is no longer configured.
                store.update_one("incentive_configurations", {"_id": config.get("_id")}, {
                    "status": "NOT_CONFIGURED", "rate": None,
                    "updated_by_user_id": actor.get("_id"), "updated_at": utcnow(),
                })
        for category_id, rate in configured.items():
            scope = "category"
            allocation = "creator"
            key = _incentive_configuration_key(
                user_id, allocation, scope, category_id=category_id,
                role=target_role_id,
            )
            existing_config = store.find_one("incentive_configurations", {"configuration_key": key})
            values = {
                "user_id": user_id, "recipient_user_id": user_id,
                "recipient_role": target_role_id, "role": target_role_id,
                "allocation_type": allocation, "scope": scope,
                "category_id": category_id,
                "category_name": INCENTIVE_CATEGORIES[category_id],
                "status": "ENABLED", "rate": float(rate),
                # Keep the legacy field for older projections/clients.
                "incentive_percentage": float(rate),
                "configuration_key": key,
                "updated_by_user_id": actor.get("_id"), "updated_at": utcnow(),
            }
            if existing_config:
                store.update_one("incentive_configurations", {"_id": existing_config.get("_id")}, values)
            else:
                store.insert_one("incentive_configurations", {**values, "created_by_user_id": actor.get("_id"), "created_at": utcnow()})
        audit("incentive.configuration_updated", "user", user_id, {
            "old_value": existing.get("incentive_rates") or {}, "new_value": configured,
            "updated_by": actor.get("_id"),
        })
    if changes.get("active") is False:
        current_app.logger.info("session_revoked user_id=%s reason=admin_deactivated", user_id)
    audit("user.update", "user", user_id, {"fields": sorted(changes)})
    # Never expose credential material through a mutation response.  The
    # store returns the complete document after an update, which includes the
    # password hash retained on the user record.  Keep the response contract
    # useful for the UI while explicitly projecting that field out.
    public_row = dict(row or {})
    public_row.pop("password_hash", None)
    return success(public_row, "User updated")


@bp.get("/admin/users/<user_id>/incentive-configuration")
@permission_required("incentives.manage")
def get_user_incentive_configuration(user_id: str):
    """Return one employee's actual incentive configuration and ceilings.

    This endpoint is intentionally separate from the legacy category-rule
    projection.  It exposes individual rows (including INHERIT/DISABLED)
    without exposing credentials or rewriting historical allocations.
    """
    if not _can_configure_incentive():
        return failure("Only a Superadmin can configure individual incentives", status=403, error="incentive_configuration_forbidden")
    store = current_app.extensions["store"]
    user = store.find_one("users", {"_id": user_id})
    if not user:
        return failure("User not found", status=404)
    role_id = str(user.get("role_id") or "").strip().lower()
    if role_id not in INCENTIVE_ELIGIBLE_ROLES:
        return failure("This account is not eligible for individual incentives", status=422, error="incentive_not_applicable")
    configurations, _ = store.list("incentive_configurations", {
        "$or": [{"user_id": user_id}, {"recipient_user_id": user_id}],
    }, limit=10_000, sort="updated_at", direction=-1)
    maximum_rules, _ = store.list("incentive_rules", {}, limit=100_000, sort="updated_at", direction=-1)
    maximum_rules = [row for row in maximum_rules if str(row.get("rule_kind") or "").strip().lower() in {"maximum", "max", "ceiling"}]
    public_maximums = []
    for row in maximum_rules:
        row_role = str(row.get("recipient_role") or "*").strip().lower()
        if row_role not in {"*", role_id, "manager" if role_id == "manager_sales_admin" else role_id}:
            continue
        allocation = str(row.get("allocation_type") or "creator").strip().lower()
        maximum = row.get("maximum_rate", row.get("rate"))
        try:
            maximum = float(maximum)
        except (TypeError, ValueError):
            continue
        public_maximums.append({
            "_id": row.get("_id"), "allocation_type": allocation,
            "scope": row.get("scope") or "maximum", "client_type": row.get("client_type") or "*",
            "category_id": row.get("category_id") or "*", "customer_id": row.get("customer_id"),
            "product_id": row.get("product_id"), "recipient_role": row.get("recipient_role") or "*",
            "maximum_rate": maximum,
        })
    items = []
    for row in configurations:
        maximum = resolve_incentive_maximum(
            store, recipient=user, client_type=row.get("client_type") or "WHOLESALER",
            category_id=row.get("category_id") or "*", customer_id=row.get("customer_id"),
            product_id=row.get("product_id"), allocation_type=row.get("allocation_type") or "creator",
        )
        items.append(_public_incentive_configuration(row, maximum))
    public_user = {key: user.get(key) for key in ("_id", "name", "email", "role_id", "manager_id")}
    return success({
        "user": public_user,
        "status": next((str(row.get("status")).upper() for row in configurations if not row.get("customer_id") and not row.get("product_id") and not row.get("category_id") and not row.get("client_type")), "INHERIT"),
        "configurations": items,
        "maximum_rules": public_maximums,
    })


@bp.put("/admin/users/<user_id>/incentive-configuration")
@permission_required("incentives.manage")
def update_user_incentive_configuration(user_id: str):
    """Persist optional employee incentive overrides without deleting rows.

    ``replace`` marks omitted rows NOT_CONFIGURED rather than deleting them;
    this preserves auditability and leaves existing OC snapshots immutable.
    """
    if not _can_configure_incentive():
        return failure("Only a Superadmin can configure individual incentives", status=403, error="incentive_configuration_forbidden")
    store = current_app.extensions["store"]
    target = store.find_one("users", {"_id": user_id})
    if not target:
        return failure("User not found", status=404)
    role_id = str(target.get("role_id") or "").strip().lower()
    if role_id not in INCENTIVE_ELIGIBLE_ROLES:
        return failure("This account is not eligible for individual incentives", status=422, error="incentive_not_applicable")
    payload = request.get_json(silent=True) or {}
    raw_items = payload.get("configurations", payload.get("items", payload.get("configs")))
    if raw_items is None:
        raw_items = [{
            "scope": "person", "allocation_type": payload.get("allocation_type", "creator"),
            "status": payload.get("status", "INHERIT"), "rate": payload.get("default_rate"),
        }]
    if not isinstance(raw_items, list):
        return failure("Incentive configurations must be a list", status=422, error="invalid_incentive_configuration")
    normalized: list[dict[str, Any]] = []
    now = utcnow()
    for raw in raw_items:
        if not isinstance(raw, dict):
            return failure("Each incentive configuration must be an object", status=422, error="invalid_incentive_configuration")
        scope = str(raw.get("scope") or "person").strip().lower()
        # A user-scoped default is represented as person, never as a global
        # default.  This keeps the resolver precedence unambiguous.
        if scope == "default":
            scope = "person"
        if scope not in INCENTIVE_CONFIGURATION_SCOPES - {"default"}:
            return failure("Unsupported incentive configuration scope", status=422, error="invalid_incentive_scope")
        allocation = str(raw.get("allocation_type") or "creator").strip().lower()
        if allocation not in {"creator", "manager_override"}:
            return failure("Unsupported incentive allocation type", status=422, error="invalid_incentive_allocation")
        status = _incentive_configuration_status(raw.get("status"), default="INHERIT")
        if status not in INCENTIVE_CONFIGURATION_STATUSES:
            return failure("Unsupported incentive configuration status", status=422, error="invalid_incentive_status")
        try:
            rate = _incentive_configuration_rate(raw.get("rate", raw.get("default_rate")), required=status == "ENABLED")
            effective_from = _incentive_configuration_date(raw.get("effective_from"), "effective_from")
            effective_to = _incentive_configuration_date(raw.get("effective_to"), "effective_to")
        except ValueError as exc:
            return failure(str(exc), status=422, error="invalid_incentive_configuration")
        if effective_from and effective_to and effective_from > effective_to:
            return failure("effective_from must not be later than effective_to", status=422, error="invalid_incentive_date_range")
        client_type = str(raw.get("client_type") or "*").strip().upper()
        if client_type != "*" and client_type not in CLIENT_TYPES:
            return failure("Customer type must be WHOLESALER, DEALER, CUSTOMER or *", status=422, error="invalid_customer_type")
        customer_id = str(raw.get("customer_id") or "").strip() or None
        product_id = str(raw.get("product_id") or "").strip() or None
        category_id = str(raw.get("category_id") or "").strip().lower() or None
        if scope == "customer":
            if not customer_id:
                return failure("Customer scope requires a customer", status=422, error="invalid_incentive_scope")
            customer = store.find_one("customers", {"_id": customer_id})
            if not customer or customer.get("is_issuer"):
                return failure("Customer not found", status=404)
        if scope == "product" and not product_id:
            return failure("Product scope requires a product", status=422, error="invalid_incentive_scope")
        if scope == "category" and category_id not in INCENTIVE_CATEGORIES:
            return failure("Category scope requires a valid product category", status=422, error="invalid_incentive_scope")
        if scope == "client_type" and client_type == "*":
            return failure("Client type scope requires a customer type", status=422, error="invalid_incentive_scope")
        if scope not in {"customer", "product", "category"}:
            customer_id = customer_id if scope == "client_type" else None
            product_id = product_id if scope == "product" else None
            category_id = category_id if scope == "category" else None
        if scope == "customer":
            product_id = product_id or None
        if status != "ENABLED":
            rate = None
        if rate is not None:
            validation_category_id = _configuration_validation_category(
                store, scope=scope, category_id=category_id, product_id=product_id,
            )
            maximum = _configuration_validation_maximum(
                store, recipient=target,
                client_type=client_type if client_type != "*" else "WHOLESALER",
                category_id=validation_category_id, customer_id=customer_id,
                product_id=product_id, allocation_type=allocation,
            )
            if maximum is not None and rate > maximum:
                return failure(f"Incentive rate {rate:g}% exceeds the configured maximum of {maximum:g}%", status=422, error="incentive_rate_exceeds_maximum", maximum_rate=maximum)
        identity = {
            "user_id": user_id, "allocation_type": allocation, "scope": scope,
            "customer_id": customer_id, "product_id": product_id, "category_id": category_id,
            "client_type": client_type if client_type != "*" else None, "role": role_id,
        }
        normalized.append({
            **identity,
            "recipient_user_id": user_id,
            "recipient_role": role_id,
            "status": status,
            "rate": rate,
            "effective_from": effective_from,
            "effective_to": effective_to,
            "configuration_key": _incentive_configuration_key(
                user_id, allocation, scope,
                customer_id=customer_id, product_id=product_id,
                category_id=category_id,
                client_type=client_type if client_type != "*" else None,
                role=role_id,
            ),
            "updated_by_user_id": (current_user() or {}).get("_id"),
            "updated_at": now,
        })
    existing, _ = store.list("incentive_configurations", {"user_id": user_id}, limit=10_000)
    incoming_keys = {row["configuration_key"] for row in normalized}
    updated = []
    for row in normalized:
        existing_row = store.find_one("incentive_configurations", {"configuration_key": row["configuration_key"]})
        if existing_row:
            updated.append(store.update_one("incentive_configurations", {"_id": existing_row.get("_id")}, row))
        else:
            updated.append(store.insert_one("incentive_configurations", {**row, "created_by_user_id": (current_user() or {}).get("_id")}))
    if bool(payload.get("replace")):
        for row in existing:
            if row.get("configuration_key") in incoming_keys:
                continue
            store.update_one("incentive_configurations", {"_id": row.get("_id")}, {
                "status": "NOT_CONFIGURED", "rate": None,
                "updated_by_user_id": (current_user() or {}).get("_id"), "updated_at": now,
            })
    audit("incentive.individual_configuration_updated", "user", user_id, {
        "actor_user_id": (current_user() or {}).get("_id"), "configuration_count": len(normalized),
        "replace": bool(payload.get("replace")),
    })
    return success({"user_id": user_id, "configurations": updated}, "Individual incentive configuration saved")


@bp.post("/admin/users/<user_id>/password")
@superadmin_required
def set_user_password(user_id: str):
    """Allow only a Superadmin to set another user's password.

    Plaintext credentials are accepted only for the duration of this request;
    neither the hash nor the password is returned or written to audit data.
    """
    store = current_app.extensions["store"]
    # Inactive accounts remain administratively manageable; changing a
    # password must not require temporarily re-enabling the account.
    target = store.find_one("users", {"_id": user_id})
    if not target:
        return failure("User not found", status=404)
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password") or "")
    confirmation = str(payload.get("confirm_password") or "")
    if password != confirmation:
        return failure("Passwords do not match", status=422, error="password_confirmation_mismatch")
    if not is_valid_signup_password(password):
        return failure(SIGNUP_PASSWORD_POLICY_MESSAGE, status=422, error="password_policy_invalid")
    actor = current_user() or {}
    row = store.update_one("users", {"_id": user_id}, {"password_hash": generate_password_hash(password), "password_changed_at": utcnow()})
    audit("auth.password_changed_by_superadmin", "user", user_id, {"actor_user_id": actor.get("_id")})
    return success({"user_id": user_id, "updated": bool(row)}, "Password updated")


@bp.get("/admin/roles")
@superadmin_required
def list_roles():
    store = current_app.extensions["store"]
    roles, total = store.list("roles", limit=100, sort="display_name", direction=1)
    for role in roles:
        role["assigned_user_count"] = store.count("users", {"role_id": role.get("_id"), "active": {"$ne": False}})
    return success({"items": roles, "total": total})


@bp.post("/admin/roles")
@superadmin_required
def create_role():
    store = current_app.extensions["store"]
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("display_name") or payload.get("name") or "").strip()
    key = str(payload.get("_id") or payload.get("key") or "").strip().casefold()
    if not name or not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", key):
        return failure("Role name and a valid role key are required", status=422, error="invalid_role")
    if key in {"superadmin", "admin", "manager", "manager_sales_admin", "user"}:
        return failure("Protected system role key cannot be used", status=422, error="protected_role")
    if store.find_one("roles", {"_id": key}) or store.find_one("roles", {"display_name": name}):
        return failure("A role with this key or name already exists", status=409, error="duplicate_role")
    permissions = payload.get("permissions") or []
    allowed = {row["_id"] for row in store.list("permissions", limit=1000)[0]}
    if not isinstance(permissions, list) or not set(map(str, permissions)).issubset(allowed):
        return failure("One or more permissions are invalid", status=422, error="invalid_permission")
    row = store.insert_one("roles", {"_id": key, "display_name": name, "description": str(payload.get("description") or "").strip()[:500], "permissions": sorted(set(map(str, permissions))), "system": False})
    audit("role.create", "role", key, {"permissions": row.get("permissions", []), "actor_user_id": (current_user() or {}).get("_id")})
    return success(row, "Role created", 201)


@bp.get("/admin/user-role-options")
@permission_required("users.view")
def user_role_options():
    """Safe role labels for user administration; never exposes permissions."""
    roles, _ = current_app.extensions["store"].list("roles", limit=100, sort="display_name", direction=1)
    actor = current_user() or {}
    if str(actor.get("role_id") or "") != "superadmin":
        roles = [row for row in roles if str(row.get("_id") or "") != "superadmin"]
    items = [{"_id": row.get("_id"), "display_name": row.get("display_name") or row.get("name") or row.get("_id")} for row in roles]
    return success({"items": items, "total": len(items)})


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
                user_resolution = resolve_incentive_configuration(
                    store, recipient=user, client_type=client_type,
                    category_id=product_type_id, allocation_type="creator",
                )
                user_rate = user_resolution.get("rate") if user_resolution.get("eligible") else None
                manager_team_rate = None
                if manager:
                    manager_resolution = resolve_incentive_configuration(
                        store, recipient=manager, client_type=client_type,
                        category_id=product_type_id, allocation_type="manager_override",
                    )
                    manager_team_rate = manager_resolution.get("rate") if manager_resolution.get("eligible") else None
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
                team_resolution = resolve_incentive_configuration(
                    store, recipient=manager, client_type=client_type,
                    category_id=product_type_id, allocation_type="manager_override",
                )
                creator_resolution = resolve_incentive_configuration(
                    store, recipient=manager, client_type=client_type,
                    category_id=product_type_id, allocation_type="creator",
                )
                team_rate = team_resolution.get("rate") if team_resolution.get("eligible") else None
                creator_rate = creator_resolution.get("rate") if creator_resolution.get("eligible") else None
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

    # Separate ceilings from employee configuration in the admin projection.
    # Both are sourced from MongoDB so the UI never turns a display constant
    # into an incentive calculation.
    maximum_rules = [
        row for row in rules
        if str(row.get("rule_kind") or "").strip().lower() in {"maximum", "max", "ceiling"}
    ]
    individual_rows, _ = store.list("incentive_configurations", {}, limit=100_000, sort="updated_at", direction=-1)
    individual_configurations = []
    all_users_by_id = {str(row.get("_id")): row for row in user_rows if row.get("_id")}
    for row in individual_rows:
        target = all_users_by_id.get(str(row.get("user_id") or row.get("recipient_user_id") or ""))
        if not target:
            continue
        maximum = resolve_incentive_maximum(
            store, recipient=target, client_type=row.get("client_type") or "WHOLESALER",
            category_id=row.get("category_id") or "blankets", customer_id=row.get("customer_id"),
            product_id=row.get("product_id"), allocation_type=row.get("allocation_type") or "creator",
        )
        individual_configurations.append({
            **_public_incentive_configuration(row, maximum),
            "user_name": target.get("name"), "user_email": target.get("email"),
            "role_id": target.get("role_id"),
        })

    # Keep the business-facing list compact while exposing the existing
    # category-specific rule records that the resolver already understands.
    # ``Configurable`` is deliberately a projection-only label; MongoDB
    # continues to store stable category IDs (or ``*`` for a default fallback).
    rule_groups = []
    group_keys: set[tuple[str, str, str, str | None]] = set()
    for client_type in CLIENT_TYPES:
        group_keys.update({("creator", client_type, "user", None), ("manager_override", client_type, "manager_sales_admin", None), ("creator", client_type, "manager_sales_admin", None)})
    for rule in rules:
        if str(rule.get("rule_kind") or "default").strip().lower() in {"maximum", "max", "ceiling"}:
            continue
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
            if str(row.get("rule_kind") or "default").strip().lower() not in {"maximum", "max", "ceiling"}
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
        "maximum_rules": maximum_rules,
        "individual_configurations": individual_configurations,
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
    rule_kind = str(payload.get("rule_kind") or "default").strip().lower()
    if rule_kind in {"max", "ceiling"}:
        rule_kind = "maximum"
    if rule_kind not in {"default", "maximum"}:
        return failure("Unsupported incentive rule kind", status=422, error="invalid_incentive_rule_kind")
    allocation_type = str(payload.get("allocation_type") or "creator").strip().lower()
    if allocation_type not in {"creator", "manager_override"}:
        return failure("Unsupported incentive allocation type", status=422)
    client_type = str(payload.get("client_type") or "").strip().upper()
    if client_type not in CLIENT_TYPES:
        return failure("Customer type must be WHOLESALER, DEALER or CUSTOMER", status=422)
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
            return failure("Incentive percentage must be 0% to 100% in 0.5% steps", status=422)
        if rate not in INCENTIVE_PERCENTAGES:
            return failure("Incentive percentage must be 0% to 100% in 0.5% steps", status=422)
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
    rule_kind = str(payload.get("rule_kind") or "default").strip().lower()
    if rule_kind in {"max", "ceiling"}:
        rule_kind = "maximum"
    if rule_kind not in {"default", "maximum"}:
        return failure("Unsupported incentive rule kind", status=422, error="invalid_incentive_rule_kind")
    allocation_type = str(payload.get("allocation_type") or "creator").strip().lower()
    if allocation_type not in {"creator", "manager_override"}:
        return failure("Unsupported incentive allocation type", status=422)
    raw_client_type = str(payload.get("client_type") or "*").strip().upper()
    client_type = raw_client_type if raw_client_type == "*" or raw_client_type in CLIENT_TYPES else ""
    if not client_type:
        return failure("Customer type must be WHOLESALER, DEALER, CUSTOMER or *", status=422)
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
        rate = _incentive_configuration_rate(payload.get("maximum_rate", payload.get("rate")), required=True) if rule_kind == "maximum" else float(payload.get("rate"))
    except (TypeError, ValueError):
        return failure("Incentive percentage must be 0% to 100% in 0.5% steps", status=422)
    if rule_kind == "default" and rate not in INCENTIVE_PERCENTAGES:
        return failure("Incentive percentage must be 0% to 100% in 0.5% steps", status=422)
    active = payload.get("active", True)
    if not isinstance(active, bool):
        return failure("Incentive rule status must be active or inactive", status=422)
    try:
        effective_from = _incentive_configuration_date(payload.get("effective_from"), "effective_from")
        effective_to = _incentive_configuration_date(payload.get("effective_to"), "effective_to")
    except ValueError as exc:
        return failure(str(exc), status=422, error="invalid_incentive_date_range")
    if effective_from and effective_to and effective_from > effective_to:
        return failure("effective_from must not be later than effective_to", status=422, error="invalid_incentive_date_range")
    existing = store.find_one("incentive_rules", {
        "rule_kind": rule_kind, "allocation_type": allocation_type, "customer_id": customer_id,
        "client_type": client_type, "recipient_role": recipient_role, "category_id": category_id,
    })
    if existing:
        return failure("An incentive rule with this scope already exists", status=409)
    scope = "maximum" if rule_kind == "maximum" else "customer" if customer_id else "client_type_category" if client_type != "*" and category_id != "*" else "client_type" if client_type != "*" else "role" if recipient_role != "*" else "global"
    row = store.insert_one("incentive_rules", {
        "rule_kind": rule_kind,
        "scope": scope, "allocation_type": allocation_type, "customer_id": customer_id,
        "client_type": client_type, "recipient_role": recipient_role, "category_id": category_id,
        "rate": rate, "maximum_rate": rate if rule_kind == "maximum" else None,
        "base": "OC_NET_AMOUNT", "active": active,
        "effective_from": effective_from, "effective_to": effective_to,
        "created_at": utcnow(), "created_by_user_id": (current_user() or {}).get("_id"),
    })
    audit("incentive.rule_created", "incentive_rule", row.get("_id"), {"scope": scope, "rule_kind": rule_kind, "rate": rate})
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
            rate = _incentive_configuration_rate(payload.get("rate"), required=True) if str(existing.get("rule_kind") or "default").lower() == "maximum" else float(payload.get("rate"))
        except (TypeError, ValueError):
            return failure("Incentive percentage must be 0% to 100% in 0.5% steps", status=422)
        if str(existing.get("rule_kind") or "default").lower() != "maximum" and rate not in INCENTIVE_PERCENTAGES:
            return failure("Incentive percentage must be 0% to 100% in 0.5% steps", status=422)
        changes["rate"] = rate
        if str(existing.get("rule_kind") or "default").lower() == "maximum":
            changes["maximum_rate"] = rate
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
@superadmin_required
def update_role(role_id: str):
    payload = request.get_json(silent=True) or {}
    allowed_permissions = {row["_id"] for row in current_app.extensions["store"].list("permissions", limit=1000)[0]}
    permissions = payload.get("permissions")
    role = current_app.extensions["store"].find_one("roles", {"_id": role_id})
    if not role:
        return failure("Role not found", status=404)
    changes = {}
    if payload.get("display_name"):
        changes["display_name"] = str(payload["display_name"])[:100]
    if permissions is not None:
        if not isinstance(permissions, list) or not set(permissions).issubset(allowed_permissions):
            return failure("One or more permissions are invalid", status=422)
        changes["permissions"] = permissions
    if role.get("system") and "display_name" in changes:
        return failure("System role names cannot be changed", status=403)
    if "description" in payload:
        changes["description"] = str(payload.get("description") or "").strip()[:500]
    row = current_app.extensions["store"].update_one("roles", {"_id": role_id}, changes)
    if not row:
        return failure("Role not found", status=404)
    audit("role.update", "role", role_id, {"fields": sorted(changes)})
    return success(row, "Role updated")


@bp.delete("/admin/roles/<role_id>")
@superadmin_required
def delete_role(role_id: str):
    store = current_app.extensions["store"]
    role = store.find_one("roles", {"_id": role_id})
    if not role:
        return failure("Role not found", status=404)
    if role.get("system") or role_id in {"superadmin", "admin", "manager", "manager_sales_admin", "user"}:
        return failure("Protected system roles cannot be deleted", status=403, error="protected_role")
    assigned = store.count("users", {"role_id": role_id})
    if assigned:
        return failure(f"This role cannot be deleted because it is assigned to {assigned} users", status=409, error="role_in_use", assigned_user_count=assigned)
    if not store.delete_one("roles", {"_id": role_id}):
        return failure("Role could not be deleted", status=409)
    audit("role.delete", "role", role_id, {"actor_user_id": (current_user() or {}).get("_id")})
    return success({"deleted": True, "role_id": role_id}, "Role deleted")


@bp.get("/admin/audit-logs")
@permission_required("audit_logs.view")
def audit_logs():
    try:
        page = max(int(request.args.get("page", 1)), 1)
        page_size = min(max(int(request.args.get("page_size", 25)), 25), 100)
    except (TypeError, ValueError):
        return failure("Page and page size must be valid integers", status=422)
    user_id = str(request.args.get("user") or "").strip()
    action = str(request.args.get("action") or "").strip()
    resource = str(request.args.get("resource") or "").strip()
    result = str(request.args.get("result") or "").strip()
    search = str(request.args.get("search") or "").strip()
    from_date = str(request.args.get("from_date") or "").strip()
    to_date = str(request.args.get("to_date") or "").strip()
    clauses: list[dict[str, Any]] = []
    if user_id:
        clauses.append({"$or": [{"user_id": user_id}, {"actor_id": user_id}]})
    if action:
        clauses.append({"action": action})
    if resource:
        clauses.append({"$or": [{"entity_type": resource}, {"resource_type": resource}]})
    if result:
        result_pattern = f"^{re.escape(result)}$"
        result_clauses: list[dict[str, Any]] = [{"status": {"$regex": result_pattern, "$options": "i"}}, {"result": {"$regex": result_pattern, "$options": "i"}}]
        if result.upper() == "RECORDED":
            result_clauses.append({"$and": [{"status": {"$exists": False}}, {"result": {"$exists": False}}]})
        clauses.append({"$or": result_clauses})
    if from_date:
        try:
            start = datetime.combine(datetime.fromisoformat(from_date).date(), time.min, tzinfo=timezone.utc)
        except ValueError:
            return failure("From date must use YYYY-MM-DD", status=422)
        clauses.append({"$or": [{"created_at": {"$gte": start}}, {"timestamp": {"$gte": from_date}}]})
    if to_date:
        try:
            end = datetime.combine(datetime.fromisoformat(to_date).date() + timedelta(days=1), time.min, tzinfo=timezone.utc) - timedelta(microseconds=1)
        except ValueError:
            return failure("To date must use YYYY-MM-DD", status=422)
        clauses.append({"$or": [{"created_at": {"$lte": end}}, {"timestamp": {"$lte": f"{to_date}T23:59:59.999999"}}]})
    if search:
        pattern = re.escape(search)
        clauses.append({"$or": [{field: {"$regex": pattern, "$options": "i"}} for field in ("action", "entity_type", "resource_type", "entity_id", "user_id", "actor_id", "ip_address")]})
    query = {"$and": clauses} if clauses else {}
    rows, total = current_app.extensions["store"].list("audit_logs", query, page=page, limit=page_size, sort="created_at", direction=-1)
    total_pages = (total + page_size - 1) // page_size if total else 0
    return success({"items": rows, "total": total, "page": page, "page_size": page_size, "pages": total_pages, "has_next": page < total_pages})


@bp.get("/settings")
@permission_required("settings.view")
def get_settings():
    store = current_app.extensions["store"]
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    if "watermark_enabled" not in settings:
        store.update_one("app_settings", {"_id": "system"}, {"watermark_enabled": True})
        settings["watermark_enabled"] = True
    return success(settings)


@bp.get("/admin/customer-incentive-visibility")
@superadmin_required
def customer_incentive_visibility():
    settings = current_app.extensions["store"].find_one("app_settings", {"_id": "system"}) or {}
    actor = current_user() or {}
    return success({
        "show_customer_incentives_to_manager": bool(settings.get("show_customer_incentives_to_manager", False)),
        "show_customer_incentives_to_salesperson": bool(settings.get("show_customer_incentives_to_salesperson", False)),
        "can_manage": str(actor.get("role_id") or "") == "superadmin",
    })


@bp.patch("/admin/customer-incentive-visibility")
@superadmin_required
def update_customer_incentive_visibility():
    payload = request.get_json(silent=True) or {}
    allowed = {"show_customer_incentives_to_manager", "show_customer_incentives_to_salesperson"}
    if any(key in payload and not isinstance(payload[key], bool) for key in allowed):
        return failure("Customer incentive visibility settings must be boolean", status=422)
    changes = {key: payload[key] for key in allowed if key in payload}
    if not changes:
        return failure("At least one visibility setting is required", status=422)
    store = current_app.extensions["store"]
    row = store.update_one("app_settings", {"_id": "system"}, changes)
    audit("customer_incentive.visibility.update", "settings", "system", {"fields": sorted(changes), "changed_by": (current_user() or {}).get("_id")})
    return success(row or changes, "Customer incentive visibility updated")


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


@bp.post("/admin/users/<user_id>/profile/<asset>")
@superadmin_required
def admin_upload_profile_asset(user_id: str, asset: str):
    """Superadmin-only repair/management path; normal users remain self-only."""
    if asset not in {"photo", "signature"}:
        return failure("Unsupported profile asset", status=404)
    store = current_app.extensions["store"]
    target = store.find_one("users", {"_id": user_id})
    if not target:
        return failure("User not found", status=404)
    upload = request.files.get("file")
    if not upload:
        return failure("Choose an image", status=422, error="PROFILE_ASSET_REQUIRED")
    try:
        changes = (save_photo if asset == "photo" else save_signature)(
            user_id, current_app.config["UPLOAD_DIRECTORY"], upload,
        )
    except SignatureValidationError as exc:
        return failure(str(exc), status=422, error="INVALID_PROFILE_ASSET")
    changes[f"{asset}_updated_at"] = utcnow()
    row = store.update_one("users", {"_id": user_id}, changes)
    row = synchronize_profile_asset(
        store, current_app.extensions["workdrive"], row or {**target, **changes},
        asset, current_app.config["UPLOAD_DIRECTORY"],
    )
    audit(f"profile.{asset}.update_by_superadmin", "user", user_id, {
        "actor_user_id": (current_user() or {}).get("_id"), "filename": changes.get(f"{asset}_filename"),
    })
    return success({
        "asset": asset, "configured": True,
        "workdrive_sync_status": workdrive_public_status(row, asset, current_app.extensions["workdrive"]),
    }, f"User {asset} saved")


@bp.get("/admin/workdrive/status")
@permission_required("settings.view")
def workdrive_status():
    store = current_app.extensions["store"]
    service = current_app.extensions["workdrive"]
    integration = store.find_one("integrations", {"_id": "zoho_workdrive"}) or {}
    configuration_issue = service.configuration_issue()
    users, _ = store.list("users", {}, limit=5000)
    states = {
        asset: {"SYNCED": 0, "PENDING": 0, "FAILED": 0, "MISSING_LOCAL": 0, "NO_LOCAL_ASSET": 0}
        for asset in ("photo", "signature")
    }
    for user in users:
        for asset in ("photo", "signature"):
            state = profile_asset_state(user, asset, current_app.config["UPLOAD_DIRECTORY"], service)
            states[asset][state] = states[asset].get(state, 0) + 1
    statuses = [state for values in states.values() for state, count in values.items() for _ in range(count)]
    account_email = integration.get("account_email")
    identity_verified = bool(account_email)
    if not account_email and integration.get("status") == "connected" and service.configured():
        try:
            diagnostic = service.diagnose_access()
            account_email = diagnostic.get("authenticated_account_email")
            identity_verified = bool(account_email)
        except WorkDriveError:
            account_email = None
    return success({
        "status": "connected" if integration.get("status") == "connected" else "not_connected",
        "connected": integration.get("status") == "connected",
        "enabled": bool(current_app.config.get("ZOHO_WORKDRIVE_ENABLED")),
        "configured": service.configured(),
        "configuration_error": configuration_issue[0] if configuration_issue else None,
        "account_email": account_email,
        "account_identity_verified": identity_verified,
        "connected_at": integration.get("connected_at"),
        "last_tested_at": integration.get("last_tested_at"),
        "last_test_status": integration.get("last_test_status"),
        "last_test_error_code": integration.get("last_test_error_code"),
        "last_test_diagnostic_id": integration.get("last_test_diagnostic_id"),
        "root_folder_name": integration.get("root_folder_name"),
        "root_folder_configured": bool(current_app.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID")),
        "sync": {
            "photos": "Synced to WorkDrive",
            "signatures": "Synced to WorkDrive",
            "status": ("failed" if WORKDRIVE_FAILED in statuses else "pending" if WORKDRIVE_PENDING in statuses else "synced"),
            "last_successful_at": integration.get("last_successful_sync_at"),
            "pending": statuses.count(WORKDRIVE_PENDING), "failed": statuses.count(WORKDRIVE_FAILED),
            "missing_local": sum(values["MISSING_LOCAL"] for values in states.values()),
            "no_local_asset": sum(values["NO_LOCAL_ASSET"] for values in states.values()),
            "by_asset": {asset: {key: value for key, value in values.items() if key != "NO_LOCAL_ASSET"}
                         for asset, values in states.items()},
        },
    }, "WorkDrive status")


@bp.post("/admin/workdrive/test")
@permission_required("settings.manage")
def workdrive_test():
    service = current_app.extensions["workdrive"]
    store = current_app.extensions["store"]
    try:
        result = service.test_connection()
        changes = {"last_tested_at": utcnow(), "last_test_status": "healthy"}
        if result.get("root_folder_name"):
            changes["root_folder_name"] = result["root_folder_name"]
        store.update_one("integrations", {"_id": "zoho_workdrive"}, changes)
        audit("workdrive.connection_test", "integration", "zoho_workdrive", {"result": "healthy"})
        return success({
            "healthy": True, "connected": True,
            "root_folder": result.get("root_folder"),
            "root_folder_name": result.get("root_folder_name"),
        }, "WorkDrive connection is healthy")
    except WorkDriveError as exc:
        safe_failure = {
            "last_tested_at": utcnow(), "last_test_status": "error",
            "last_test_error_code": exc.code, "last_test_diagnostic_id": exc.diagnostic_id,
        }
        store.update_one("integrations", {"_id": "zoho_workdrive"}, safe_failure, upsert=True)
        audit("workdrive.connection_test", "integration", "zoho_workdrive", {
            "result": "error", "error_code": exc.code, "stage": exc.stage,
            "diagnostic_id": exc.diagnostic_id,
        })
        current_app.logger.error(
            "workdrive_test_failed stage=%s http_status=%s error_code=%s "
            "response_status=%s provider_code=%s endpoint_host=%s endpoint_path=%s "
            "diagnostic_id=%s",
            exc.stage, exc.http_status or "none", exc.code, exc.http_status or "none",
            exc.provider_code or "none", exc.endpoint_host or "none",
            exc.endpoint_path or "none", exc.diagnostic_id,
        )
        if exc.code == "CONFIGURATION_ERROR":
            return failure(
                "Zoho WorkDrive configuration is incomplete", status=503,
                error="WORKDRIVE_CONFIGURATION_ERROR", error_code="WORKDRIVE_CONFIGURATION_ERROR",
                stage=exc.stage, diagnostic_id=exc.diagnostic_id,
            )
        safe_codes = {
            "WORKDRIVE_DISABLED", "WORKDRIVE_OAUTH_CONFIGURATION_ERROR",
            "WORKDRIVE_AUTHORIZATION_INVALID", "WORKDRIVE_AUTHORIZATION_REQUIRED",
            "WORKDRIVE_ROOT_FOLDER_REQUIRED", "WORKDRIVE_API_CONFIGURATION_ERROR",
            "WORKDRIVE_TOKEN_REFRESH_UNAVAILABLE", "WORKDRIVE_TOKEN_REFRESH_FAILED",
            "WORKDRIVE_TOKEN_RESPONSE_INVALID", "WORKDRIVE_API_TIMEOUT",
            "WORKDRIVE_API_UNAVAILABLE", "WORKDRIVE_AUTHENTICATION_FAILED",
            "WORKDRIVE_API_ERROR", "WORKDRIVE_RESPONSE_INVALID",
            "ROOT_FOLDER_NOT_FOUND", "ROOT_FOLDER_ACCESS_DENIED", "ROOT_RESOURCE_NOT_FOLDER",
        }
        code = exc.code if exc.code in safe_codes else "WORKDRIVE_CONNECTION_ERROR"
        message = str(exc) if exc.code in safe_codes else "WorkDrive connection requires attention"
        return failure(
            message, status=503, error=code, error_code=code,
            stage=exc.stage, diagnostic_id=exc.diagnostic_id,
        )


@bp.get("/admin/workdrive/diagnostic")
@permission_required("settings.manage")
def workdrive_diagnostic():
    """Expose non-secret WorkDrive identity, scope, and membership diagnostics."""
    service = current_app.extensions["workdrive"]
    try:
        return success(service.diagnose_access(), "WorkDrive diagnostic")
    except WorkDriveError as exc:
        current_app.logger.error(
            "workdrive_diagnostic_failed stage=%s http_status=%s error_code=%s "
            "provider_code=%s endpoint_host=%s endpoint_path=%s diagnostic_id=%s",
            exc.stage, exc.http_status or "none", exc.code, exc.provider_code or "none",
            exc.endpoint_host or "none", exc.endpoint_path or "none", exc.diagnostic_id,
        )
        return failure(
            str(exc), status=503, error=exc.code, error_code=exc.code,
            stage=exc.stage, diagnostic_id=exc.diagnostic_id,
        )


@bp.get("/admin/workdrive/quotation-archive-diagnostic/<quotation_id>/<version_id>")
@permission_required("settings.manage")
def workdrive_quotation_archive_diagnostic(quotation_id: str, version_id: str):
    """Read-only validation of the two persisted quotation archive trees."""
    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    version = store.find_one("quotation_versions", {"_id": version_id, "quotation_id": quotation_id})
    if not quotation or not version:
        return failure("Quotation version not found", status=404)
    customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    customer = store.find_one("customers", {"_id": customer_id}) if customer_id else None
    owner_id = quotation.get("created_by_user_id") or quotation.get("user_id") or quotation.get("salesperson_id")
    owner = store.find_one("users", {"_id": owner_id}) if owner_id else None
    mappings, _ = store.list("workdrive_document_versions", {"document_id": quotation_id}, limit=100, sort="updated_at", direction=-1)
    company_quote_folder = next((str(row.get("workdrive_customer_folder_id") or "").strip() for row in mappings
                                 if str(row.get("workdrive_customer_folder_id") or "").strip()), "")
    user_quote_folder = str(version.get("workdrive_user_document_folder_id") or version.get("workdrive_folder_id") or "").strip()
    if not user_quote_folder:
        user_quote_folder = next((str(row.get("workdrive_user_document_folder_id") or row.get("workdrive_folder_id") or "").strip()
                                  for row in mappings if str(row.get("workdrive_user_document_folder_id") or row.get("workdrive_folder_id") or "").strip()), "")
    resources = [
        {"label": "configured_root", "resource_id": str(current_app.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID") or "")},
        {"label": "companies_root", "resource_id": str(current_app.config.get("ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID") or "")},
        {"label": "company_folder", "resource_id": str((customer or {}).get("workdrive_company_folder_id") or ""),
         "expected_parent_id": str(current_app.config.get("ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID") or "")},
        {"label": "company_quotations_folder", "resource_id": str((customer or {}).get("workdrive_company_quotations_folder_id") or ""),
         "expected_parent_id": str((customer or {}).get("workdrive_company_folder_id") or "")},
        {"label": "company_quote_folder", "resource_id": company_quote_folder,
         "expected_parent_id": str((customer or {}).get("workdrive_company_quotations_folder_id") or "")},
        {"label": "user_quotes_folder", "resource_id": str((owner or {}).get("workdrive_quotes_folder_id") or "")},
        {"label": "user_quote_folder", "resource_id": user_quote_folder,
         "expected_parent_id": str((owner or {}).get("workdrive_quotes_folder_id") or "")},
    ]
    try:
        result = current_app.extensions["workdrive"].diagnose_resource_access(resources)
        result["folder_discovery"] = {
            "company": current_app.extensions["workdrive"].diagnose_child_folder(
                str((customer or {}).get("workdrive_company_quotations_folder_id") or ""),
                str(quotation.get("quotation_number") or ""),
            ),
            "user": current_app.extensions["workdrive"].diagnose_child_folder(
                str((owner or {}).get("workdrive_quotes_folder_id") or ""),
                str(quotation.get("quotation_number") or ""),
            ),
        }
        result["persisted_folder_ids"] = {
            "company_quote_folder_id": company_quote_folder or None,
            "user_quote_folder_id": user_quote_folder or None,
        }
    except WorkDriveError as exc:
        current_app.logger.warning(
            "workdrive_quotation_archive_diagnostic result=ERROR quotation_id=%s version_id=%s error_code=%s http_status=%s provider_code=%s endpoint_host=%s endpoint_path=%s diagnostic_id=%s",
            quotation_id, version_id, exc.code, exc.http_status, exc.provider_code,
            exc.endpoint_host, exc.endpoint_path, exc.diagnostic_id,
        )
        return failure("WorkDrive resource diagnostic could not be started", status=503,
                       error=exc.code, diagnostic_id=exc.diagnostic_id)
    return success(result, "WorkDrive quotation archive resource diagnostic")


@bp.post("/admin/workdrive/quotation-archive-mapping-repair/<quotation_id>/<version_id>")
@permission_required("settings.manage")
def repair_quotation_archive_mappings(quotation_id: str, version_id: str):
    """Repair two verified quote-folder mappings; never archives or creates resources."""
    payload = request.get_json(silent=True) or {}
    company_folder_id = str(payload.get("company_quote_folder_id") or "").strip()
    user_folder_id = str(payload.get("user_quote_folder_id") or "").strip()
    if not company_folder_id or not user_folder_id:
        return failure("Both verified quote folder IDs are required", status=400)

    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    version = store.find_one("quotation_versions", {"_id": version_id, "quotation_id": quotation_id})
    if not quotation or not version:
        return failure("Quotation version not found", status=404)
    customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    customer = store.find_one("customers", {"_id": customer_id}) if customer_id else None
    owner_id = quotation.get("created_by_user_id") or quotation.get("user_id") or quotation.get("salesperson_id")
    owner = store.find_one("users", {"_id": owner_id}) if owner_id else None
    company_parent_id = str((customer or {}).get("workdrive_company_quotations_folder_id") or "").strip()
    user_parent_id = str((owner or {}).get("workdrive_quotes_folder_id") or "").strip()
    quotation_number = str(quotation.get("quotation_number") or "").strip()
    if not customer or not owner or not company_parent_id or not user_parent_id or not quotation_number:
        return failure("The existing quotation archive parent mappings are incomplete", status=409)

    try:
        diagnostic = current_app.extensions["workdrive"].diagnose_resource_access([
            {"label": "company_quote_folder", "resource_id": company_folder_id,
             "expected_parent_id": company_parent_id},
            {"label": "user_quote_folder", "resource_id": user_folder_id,
             "expected_parent_id": user_parent_id},
        ])
    except WorkDriveError as exc:
        current_app.logger.warning(
            "workdrive_quotation_mapping_repair result=VALIDATION_ERROR quotation_id=%s version_id=%s error_code=%s http_status=%s provider_code=%s diagnostic_id=%s",
            quotation_id, version_id, exc.code, exc.http_status, exc.provider_code, exc.diagnostic_id,
        )
        return failure("WorkDrive folder validation could not be started", status=503,
                       error=exc.code, diagnostic_id=exc.diagnostic_id)

    folders = {str(item.get("label") or ""): item for item in diagnostic.get("resources", [])}
    invalid = [label for label in ("company_quote_folder", "user_quote_folder") if not (
        folders.get(label, {}).get("status") == "accessible"
        and folders[label].get("id_matches") is True
        and folders[label].get("parent_matches") is True
        and folders[label].get("name") == quotation_number
        and folders[label].get("is_folder") is True
    )]
    if invalid:
        return failure("Verified WorkDrive folders did not match the expected quotation hierarchy", status=409,
                       error="WORKDRIVE_FOLDER_VALIDATION_FAILED",
                       data={"invalid_destinations": invalid, "resources": list(folders.values())})

    mappings, _ = store.list("workdrive_document_versions", {"document_id": quotation_id}, limit=100)
    mapping_changes = {
        "workdrive_customer_folder_id": company_folder_id,
        "workdrive_user_document_folder_id": user_folder_id,
        # Compatibility field used by historical user-archive rows.
        "workdrive_folder_id": user_folder_id,
    }
    updated_mapping_ids = []
    for mapping in mappings:
        if store.update_one("workdrive_document_versions", {"_id": mapping["_id"], "document_id": quotation_id}, mapping_changes):
            updated_mapping_ids.append(str(mapping["_id"]))
    version_changes = {**mapping_changes, "workdrive_customer_folder_id": company_folder_id}
    store.update_one("quotation_versions", {"_id": version_id, "quotation_id": quotation_id}, version_changes)
    audit("workdrive.quotation_archive_mappings_repaired", "quotation", quotation_id, {
        "version_id": version_id, "quotation_number": quotation_number,
        "mapping_rows_updated": len(updated_mapping_ids),
        "company_quote_folder_id": company_folder_id, "user_quote_folder_id": user_folder_id,
    })
    return success({
        "quotation_id": quotation_id, "version_id": version_id,
        "quotation_number": quotation_number, "mapping_rows_updated": len(updated_mapping_ids),
        "updated_mapping_ids": updated_mapping_ids,
        "resources": list(folders.values()),
    }, "Verified quotation archive folder mappings updated")
@bp.post("/admin/workdrive/resync-user/<user_id>")
@superadmin_required
def resync_workdrive_user(user_id: str):
    store = current_app.extensions["store"]
    user = store.find_one("users", {"_id": user_id})
    if not user:
        return failure("User not found", status=404)
    service = current_app.extensions["workdrive"]
    if not service.enabled():
        return failure("WorkDrive synchronization is disabled", status=409, error="WORKDRIVE_DISABLED")
    results = {}
    for asset in ("photo", "signature"):
        if user.get(f"{asset}_path") or user.get(f"{asset}_workdrive_resource_id"):
            user = synchronize_profile_asset(store, service, user, asset, current_app.config["UPLOAD_DIRECTORY"])
            results[asset] = workdrive_public_status(user, asset, service)
        else:
            results[asset] = "SKIPPED"
    audit("workdrive.resync_user", "user", user_id, {"results": results})
    return success({"user_id": user_id, "results": results}, "WorkDrive synchronization completed")


@bp.post("/admin/workdrive/resync-all")
@superadmin_required
def resync_workdrive_all():
    service = current_app.extensions["workdrive"]
    if not service.enabled():
        return failure("WorkDrive synchronization is disabled", status=409, error="WORKDRIVE_DISABLED")
    store = current_app.extensions["store"]
    users, _ = store.list("users", {}, limit=5000)
    report = {"users": len(users), "assets": 0, "synced": 0, "failed": 0, "skipped": 0,
              "skipped_no_local_asset": 0, "stale_missing_local": 0, "skipped_reasons": {}}
    for user in users:
        for asset in ("photo", "signature"):
            state = profile_asset_state(user, asset, current_app.config["UPLOAD_DIRECTORY"], service)
            if state == "NO_LOCAL_ASSET":
                report["skipped"] += 1
                report["skipped_no_local_asset"] += 1
                report["skipped_reasons"][f"{user.get('_id')}:{asset}"] = state
                continue
            if state == "MISSING_LOCAL":
                report["stale_missing_local"] += 1
            if state == "REMOTE_ONLY":
                report["stale_missing_local"] += 1
            report["assets"] += 1
            user = synchronize_profile_asset(store, service, user, asset, current_app.config["UPLOAD_DIRECTORY"])
            if workdrive_public_status(user, asset, service) == "SYNCED":
                report["synced"] += 1
            else:
                report["failed"] += 1
    audit("workdrive.resync_all", "users", "all", report)
    return success(report, "WorkDrive synchronization completed")


@bp.post("/admin/workdrive/resync-failed")
@permission_required("settings.manage")
def resync_workdrive_failed():
    service = current_app.extensions["workdrive"]
    if not service.enabled():
        return failure("WorkDrive synchronization is disabled", status=409, error="WORKDRIVE_DISABLED")
    store = current_app.extensions["store"]
    users, _ = store.list("users", {}, limit=5000)
    report = {"assets": 0, "synced": 0, "failed": 0, "skipped": 0,
              "skipped_no_local_asset": 0, "stale_missing_local": 0, "skipped_reasons": {}}
    for user in users:
        for asset in ("photo", "signature"):
            state = profile_asset_state(user, asset, current_app.config["UPLOAD_DIRECTORY"], service)
            if state in {"NO_LOCAL_ASSET", "MISSING_LOCAL"} and not user.get(f"{asset}_workdrive_resource_id"):
                report["skipped"] += 1
                if state == "NO_LOCAL_ASSET":
                    report["skipped_no_local_asset"] += 1
                elif state == "MISSING_LOCAL":
                    report["stale_missing_local"] += 1
                report["skipped_reasons"][f"{user.get('_id')}:{asset}"] = state
                continue
            if user.get(f"{asset}_workdrive_sync_status") != WORKDRIVE_FAILED and state != "REMOTE_ONLY":
                report["skipped"] += 1
                report["skipped_reasons"][f"{user.get('_id')}:{asset}"] = "NOT_FAILED"
                continue
            report["assets"] += 1
            user = synchronize_profile_asset(store, service, user, asset, current_app.config["UPLOAD_DIRECTORY"])
            if workdrive_public_status(user, asset, service) == WORKDRIVE_SYNCED:
                report["synced"] += 1
            else:
                report["failed"] += 1
    audit("workdrive.resync_failed", "users", "failed", report)
    return success(report, "Failed WorkDrive synchronizations retried")
