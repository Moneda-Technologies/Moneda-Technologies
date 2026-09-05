from __future__ import annotations

import re

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import current_user, customer_id_from, customer_record, enforce_active_customer, login_required, permission_required
from app.pricing.engine import PricingUnavailable, calculate_line, company_tax_values, resolve_product_adjustments


bp = Blueprint("catalog", __name__, url_prefix="/api")


@bp.get("/categories")
@login_required
def categories():
    rows, _ = current_app.extensions["store"].list("categories", {"active": True}, limit=100, sort="sort_order", direction=1)
    return success(rows)


@bp.get("/machines")
@permission_required("calculator.view")
def machines():
    rows, total = current_app.extensions["store"].list(
        "machines", {"active": {"$ne": False}}, limit=2000, sort="name", direction=1,
    )
    return success({"items": rows, "total": total})


@bp.post("/machines")
@permission_required("calculator.view")
def create_machine():
    raw_name = str((request.get_json(silent=True) or {}).get("name", ""))
    name = " ".join(raw_name.split()).strip()
    if not name:
        return failure("Machine name is required", status=422)
    if len(name) > 120:
        return failure("Machine name must be 120 characters or fewer", status=422)
    store = current_app.extensions["store"]
    existing = store.find_one("machines", {"name": {"$regex": f"^{re.escape(name)}$"}})
    if existing:
        return success(existing, "Machine already recorded")
    user = current_user() or {}
    row = store.insert_one("machines", {
        "name": name, "active": True, "source": "user",
        "created_by": user.get("_id"), "created_by_name": user.get("name"),
    })
    return success(row, "Machine recorded", 201)


@bp.get("/catalog/families")
@permission_required("products.view")
def catalog_families():
    rows, _ = current_app.extensions["store"].list(
        "categories", {"active": True, "calculator_enabled": True}, limit=10, sort="sort_order", direction=1,
    )
    return success([{"id": row["_id"], "name": row["name"]} for row in rows])


@bp.get("/catalog/product-types")
@permission_required("products.view")
def product_types_v1():
    rows, _ = current_app.extensions["store"].list(
        "categories", {"active": True, "calculator_enabled": True}, limit=10, sort="sort_order", direction=1,
    )
    return success([{
        "id": row["_id"], "name": row["name"], "description": row.get("description", ""),
        "icon": row.get("icon"), "sort_order": row.get("sort_order"),
    } for row in rows])


@bp.get("/catalog/blankets/categories")
@permission_required("products.view")
def blanket_categories():
    rows, _ = current_app.extensions["store"].list(
        "blanket_categories", {"active": True}, limit=20, sort="sort_order", direction=1,
    )
    return success([{"id": row["_id"], "name": row["name"]} for row in rows])


@bp.get("/catalog/blankets/options")
@permission_required("products.view")
def blanket_options_v1():
    row = current_app.extensions["store"].find_one("catalog_options", {"_id": "blankets"})
    return success(row) if row else failure("Blanket options not found", status=404)


@bp.get("/catalog/blankets/bars")
@permission_required("products.view")
def blanket_bars_v1():
    rows, _ = current_app.extensions["store"].list(
        "blanket_bars", {"active": True}, limit=1000, sort="article_no", direction=1,
    )
    return success({"items": rows, "total": len(rows)})


@bp.get("/catalog/blankets/products")
@permission_required("products.view")
def blanket_products():
    category_id = str(request.args.get("category", "")).strip()
    if category_id and not current_app.extensions["store"].find_one("blanket_categories", {"_id": category_id, "active": True}):
        return failure("Blanket category not found", status=404)
    rows, _ = current_app.extensions["store"].list(
        "products", {"category_id": "blankets", "active": True}, limit=1000, sort="name", direction=1,
    )
    if category_id:
        rows = [row for row in rows if category_id in row.get("configuration", {}).get("category_ids", [])]
    term = str(request.args.get("search", "")).strip().lower()
    if term:
        rows = [row for row in rows if term in " ".join((str(row.get("name", "")), str(row.get("article_no", "")), str(row.get("sku", "")), str(row.get("description", "")))).lower()]
    return success({"items": rows, "total": len(rows)})


@bp.get("/catalog/blankets/products/<product_id>")
@permission_required("products.view")
def blanket_product_detail(product_id: str):
    row = current_app.extensions["store"].find_one(
        "products", {"_id": product_id, "category_id": "blankets", "active": True},
    )
    return success(row) if row else failure("Blanket product not found", status=404)


def _family_products(family_id: str):
    rows, _ = current_app.extensions["store"].list(
        "products", {"category_id": family_id, "active": True}, limit=10_000, sort="name", direction=1,
    )
    category_id = str(request.args.get("category", "")).strip()
    if category_id:
        rows = [row for row in rows if row.get("configuration", {}).get("sub_category_id") == category_id]
    term = str(request.args.get("search", "")).strip().lower()
    if term:
        rows = [row for row in rows if term in " ".join((
            str(row.get("name", "")), str(row.get("article_no", "")), str(row.get("sku", "")),
            str(row.get("description", "")),
        )).lower()]
    return success({"items": rows, "total": len(rows)})


@bp.get("/catalog/mpacks/types")
@permission_required("products.view")
def mpack_types_v1():
    rows, _ = current_app.extensions["store"].list("mpack_types", {"active": True}, limit=1000, sort="sort_order", direction=1)
    return success([{"id": row["_id"], **{key: value for key, value in row.items() if key != "_id"}} for row in rows])


@bp.get("/catalog/mpacks/options")
@permission_required("products.view")
def mpack_options_v1():
    row = current_app.extensions["store"].find_one("catalog_options", {"_id": "mpacks"})
    return success(row) if row else failure("Underpacking options not found", status=404)


@bp.get("/catalog/mpacks/products")
@permission_required("products.view")
def mpack_products_v1():
    return _family_products("mpacks")


@bp.get("/catalog/chemicals/categories")
@permission_required("products.view")
def chemical_categories_v1():
    rows, _ = current_app.extensions["store"].list(
        "chemical_categories", {"active": True}, limit=1000, sort="sort_order", direction=1,
    )
    return success([{"id": row["_id"], **{key: value for key, value in row.items() if key != "_id"}} for row in rows])


@bp.get("/catalog/chemicals/options")
@permission_required("products.view")
def chemical_options_v1():
    row = current_app.extensions["store"].find_one("catalog_options", {"_id": "chemicals"})
    return success(row) if row else failure("Chemical options not found", status=404)


@bp.get("/catalog/chemicals/products")
@permission_required("products.view")
def chemical_products_v1():
    return _family_products("chemicals")


@bp.get("/products")
@permission_required("products.view")
def products():
    store = current_app.extensions["store"]
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(max(int(request.args.get("limit", 24)), 1), 100)
    query: dict = {"active": True}
    if request.args.get("category"):
        category_id = request.args["category"]
        if not store.find_one("categories", {"_id": category_id, "active": True, "calculator_enabled": True}):
            return failure("Category is not available in the calculator", status=404)
        query["category_id"] = category_id
    if request.args.get("pricing_status"):
        query["pricing_status"] = request.args["pricing_status"]
    if request.args.get("search"):
        term = re.escape(request.args["search"][:100])
        query["$or"] = [{field: {"$regex": term}} for field in ("name", "article_no", "sku", "description", "category_id")]
    rows, total = current_app.extensions["store"].list("products", query, page=page, limit=limit, sort="name", direction=1)
    return success({"items": rows, "pagination": {"page": page, "limit": limit, "total": total, "pages": (total + limit - 1) // limit}})


@bp.get("/products/<product_id>")
@permission_required("products.view")
def product_detail(product_id: str):
    row = current_app.extensions["store"].find_one("products", {"_id": product_id, "active": True})
    return success(row) if row else failure("Product not found", status=404)


@bp.post("/products/<product_id>/price-preview")
@permission_required("pricing.view")
def price_preview(product_id: str):
    payload = request.get_json(silent=True) or {}
    if "tax_rate" in payload:
        return failure("Tax rate is controlled by the product and cannot be overridden here", status=422)
    if "tax_mode" in payload and str(payload.get("tax_mode")).lower() not in {"exclusive", "inclusive"}:
        return failure("Tax mode must be exclusive or inclusive", status=422)
    store = current_app.extensions["store"]
    product_row = store.find_one("products", {"_id": product_id, "active": True})
    customer_id = customer_id_from(payload)
    customer_company = customer_record(customer_id)
    if not product_row or not customer_company:
        return failure("Product or customer not found", status=404)
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    try:
        currency = str(payload.get("currency", customer_company.get("default_currency", "EUR"))).upper()
        # New clients explicitly send the toggle. Modern customer-scoped
        # requests default to tax-free; the legacy company_id bridge keeps
        # compatibility with older integrations.
        apply_tax = (
            currency == "INR" and bool(payload.get("tax_enabled", False))
            if "tax_enabled" in payload else (False if "customer_company_id" in payload else True)
        )
        tax_mode = str(payload["tax_mode"]).lower() if "tax_mode" in payload else None
        rate, rate_meta = current_app.extensions["exchange_rate_service"].rate_for(currency)
        user = current_user() or {}
        configuration = payload.get("configuration", {})
        settings = store.find_one("app_settings", {"_id": "system"}) or {}
        company_tax_rate, company_tax_mode = company_tax_values(customer_company)
        line = calculate_line(
            product_row, configuration, quantity=int(payload.get("quantity", 1)),
            discount_percent=payload.get("discount_percent", 0), currency=currency, exchange_rate=rate,
            company_tax_rate=company_tax_rate, company_tax_mode=company_tax_mode,
            privileged_discount="pricing.discount.override" in user.get("permissions", []),
            adjustments=resolve_product_adjustments(store, product_row, configuration), business_rules=settings,
            apply_tax=apply_tax, tax_mode_override=tax_mode,
        )
        return success({"line": line, "rate": rate_meta})
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except ValueError as exc:
        return failure(str(exc), status=422)
