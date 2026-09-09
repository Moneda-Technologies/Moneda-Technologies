from __future__ import annotations

import re

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import current_user, customer_id_from, customer_record, enforce_active_customer, login_required, permission_required
from app.pricing.engine import PricingUnavailable, calculate_line, resolve_mpack_selection, resolve_product_adjustments, validate_blanket_machine_selection
from app.customers.metadata import resolve_customer_currency
from app.customers.countries import countries as country_catalogue
from app.catalog.service import get_active_catalog_product, get_active_catalog_products


bp = Blueprint("catalog", __name__, url_prefix="/api")


def _log_mpack_catalog_state(product: dict) -> None:
    if product.get("category_id") != "mpacks":
        return
    configuration = product.get("configuration") or {}
    machine_sizes = configuration.get("machine_sizes") or []
    current_app.logger.info(
        "mpack_pricing_catalog product_id=%s pricing_status=%s "
        "structured_machine_pricing_available=%s machine_size_count=%s",
        product.get("_id"), product.get("pricing_status", "unknown"),
        bool(machine_sizes), len(machine_sizes),
    )


@bp.get("/countries")
@permission_required("customers.view")
def countries():
    rows = list(country_catalogue())
    return success({"countries": rows, "total": len(rows)})


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
    payload = request.get_json(silent=True) or {}
    manufacturer = " ".join(str(payload.get("manufacturer", "")).split()).strip()
    machine_model = " ".join(str(payload.get("machine_model", "")).split()).strip()
    raw_name = str(payload.get("name", ""))
    if manufacturer or machine_model:
        if not manufacturer or not machine_model:
            return failure("Machine manufacturer and model are both required", status=422)
        name = f"{manufacturer} - {machine_model}"
    else:
        name = " ".join(raw_name.split()).strip()
    if not name:
        return failure("Machine name is required", status=422)
    if len(name) > 240 or len(manufacturer) > 120 or len(machine_model) > 120:
        return failure("Machine manufacturer and model must be 120 characters or fewer", status=422)
    store = current_app.extensions["store"]
    query = {
        "manufacturer": {"$regex": f"^{re.escape(manufacturer)}$"},
        "machine_model": {"$regex": f"^{re.escape(machine_model)}$"},
    } if manufacturer else {"name": {"$regex": f"^{re.escape(name)}$"}}
    existing = store.find_one("machines", query)
    if existing:
        return success(existing, "Machine already recorded")
    user = current_user() or {}
    row = store.insert_one("machines", {
        "name": name, "manufacturer": manufacturer or None, "machine_model": machine_model or None,
        "active": True, "source": "user",
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
    if category_id and category_id != "all" and not current_app.extensions["store"].find_one("blanket_categories", {"_id": category_id, "active": True}):
        return failure("Blanket category not found", status=404)
    rows = get_active_catalog_products(current_app.extensions["store"], "blankets")
    if category_id and category_id != "all":
        rows = [row for row in rows if category_id in row.get("configuration", {}).get("category_ids", [])]
    term = str(request.args.get("search", "")).strip().lower()
    if term:
        rows = [row for row in rows if term in " ".join((str(row.get("name", "")), str(row.get("article_no", "")), str(row.get("sku", "")), str(row.get("description", "")))).lower()]
    return success({"items": rows, "total": len(rows)})


@bp.get("/catalog/blankets/products/<product_id>")
@permission_required("products.view")
def blanket_product_detail(product_id: str):
    row = get_active_catalog_product(current_app.extensions["store"], product_id)
    if row and row.get("category_id") != "blankets":
        row = None
    return success(row) if row else failure("Blanket product not found", status=404)


def _family_products(family_id: str):
    rows = get_active_catalog_products(current_app.extensions["store"], family_id)
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
    rows = get_active_catalog_products(current_app.extensions["store"], query.get("category_id"))
    if query.get("pricing_status"):
        rows = [row for row in rows if row.get("pricing_status") == query["pricing_status"]]
    if query.get("$or"):
        term = str(request.args.get("search", "")).lower()
        rows = [row for row in rows if term in " ".join(str(row.get(field, "")) for field in ("name", "article_no", "sku", "description", "category_id")).lower()]
    total = len(rows)
    start = (page - 1) * limit
    rows = rows[start:start + limit]
    for row in rows:
        _log_mpack_catalog_state(row)
    return success({"items": rows, "pagination": {"page": page, "limit": limit, "total": total, "pages": (total + limit - 1) // limit}})


@bp.get("/products/<product_id>")
@permission_required("products.view")
def product_detail(product_id: str):
    row = get_active_catalog_product(current_app.extensions["store"], product_id)
    if row:
        _log_mpack_catalog_state(row)
    return success(row) if row else failure("Product not found", status=404)


@bp.post("/products/<product_id>/price-preview")
@permission_required("pricing.view")
def price_preview(product_id: str):
    payload = request.get_json(silent=True) or {}
    store = current_app.extensions["store"]
    product_row = get_active_catalog_product(store, product_id)
    customer_id = customer_id_from(payload)
    customer_company = customer_record(customer_id)
    if not product_row:
        return failure("PRODUCT_NOT_FOUND", status=422, error="PRODUCT_NOT_FOUND")
    if not customer_company:
        return failure("Customer not found", status=404)
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    try:
        current_app.logger.info(
            "discount_debug item_id=%s backend_received_discount=%s",
            payload.get("item_id") or "preview", payload.get("discount_percent", 0),
        )
        currency = resolve_customer_currency(customer_company, payload.get("display_currency") or payload.get("currency"), store, current_user())
        rate, rate_meta = current_app.extensions["exchange_rate_service"].rate_for(currency)
        user = current_user() or {}
        configuration = payload.get("configuration", {})
        if product_row.get("category_id") == "mpacks":
            try:
                mpack_selection = resolve_mpack_selection(product_row, configuration)
            except ValueError:
                current_app.logger.info(
                    "mpack_pricing_resolution product_id=%s manufacturer=%s model=%s "
                    "size=%sx%s thickness=%s result=NO_MATCH",
                    product_id, configuration.get("manufacturer", ""), configuration.get("machine_model", ""),
                    configuration.get("width_mm", ""), configuration.get("length_mm", ""),
                    configuration.get("thickness_mm", configuration.get("thickness_micron", "")),
                )
                raise
            if mpack_selection:
                current_app.logger.info(
                    "mpack_pricing_resolution product_id=%s manufacturer=%s model=%s "
                    "size=%sx%s thickness=%s price_eur=%s result=PASS",
                    product_id, mpack_selection["manufacturer"], mpack_selection["machine_model"],
                    mpack_selection["width_mm"], mpack_selection["length_mm"],
                    mpack_selection["thickness_mm"], mpack_selection["price_per_box_eur"],
                )
            else:
                current_app.logger.info(
                    "mpack_pricing_resolution product_id=%s manufacturer=%s model=%s "
                    "size=%sx%s thickness=%s structured_machine_pricing_available=false result=NO_MATCH",
                    product_id, configuration.get("manufacturer", ""), configuration.get("machine_model", ""),
                    configuration.get("width_mm", ""), configuration.get("length_mm", ""),
                    configuration.get("thickness_mm", configuration.get("thickness_micron", "")),
                )
        settings = store.find_one("app_settings", {"_id": "system"}) or {}
        if product_row.get("category_id") == "blankets":
            machine_rows, _ = store.list("machines", {"active": {"$ne": False}}, limit=2000, sort="name", direction=1)
            validate_blanket_machine_selection(product_row, configuration, machine_rows)
        line = calculate_line(
            product_row, configuration, quantity=int(payload.get("quantity", 1)),
            discount_percent=payload.get("discount_percent", 0), currency=currency, exchange_rate=rate,
            company_tax_rate=0, company_tax_mode="no_tax",
            privileged_discount="pricing.discount.override" in user.get("permissions", []),
            adjustments=resolve_product_adjustments(store, product_row, configuration), business_rules=settings,
            apply_tax=False, tax_mode_override="no_tax",
        )
        line["display_currency"] = currency
        line["quotation_currency"] = "EUR"
        current_app.logger.info(
            "price_preview_context customer_id=%s product_id=%s master_currency=EUR master_unit_price_eur=%s master_subtotal_eur=%s discount_percent=%s discount_amount_eur=%s master_final_total_eur=%s display_currency=%s display_subtotal=%s display_discount_amount=%s display_final_total=%s result=PASS",
            customer_id, product_id, line["master_unit_price"], line["master_subtotal"], line["discount_percent"],
            line["master_discount_amount"], line["master_final_total"], currency, line["display_subtotal"],
            line["display_discount_amount"], line["display_final_total"],
        )
        current_app.logger.info(
            "discount_debug item_id=%s pricing_engine_discount=%s response_discount=%s",
            payload.get("item_id") or "preview", line["discount_percent"], line["discount_percent"],
        )
        line.pop("final_total", None)
        return success({"line": line, "rate": rate_meta})
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except ValueError as exc:
        return failure(str(exc), status=422)
