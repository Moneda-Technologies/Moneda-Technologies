from __future__ import annotations

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import current_user, customer_id_from, customer_record, enforce_active_customer, permission_required
from app.pricing.engine import (
    PricingUnavailable, calculate_line, calculate_quote_totals,
    company_tax_values, configuration_fingerprint, resolve_product_adjustments,
)
from app.exchange_rates.service import ExchangeRateUnavailable
from app.services.audit import audit
from app.customers.metadata import resolve_customer_currency


bp = Blueprint("pricing", __name__, url_prefix="/api")


def _active_cart(user_id: str, customer_id: str) -> dict:
    """Return the single cart owned by this authenticated user/customer pair."""
    store = current_app.extensions["store"]
    cart = store.find_one("carts", {"user_id": user_id, "customer_id": customer_id})
    if cart:
        return cart
    try:
        return store.insert_one("carts", {"user_id": user_id, "customer_id": customer_id, "active": True})
    except Exception:
        # Mongo's unique pair index may win a concurrent create; read the
        # winner rather than exposing a race to the client.
        cart = store.find_one("carts", {"user_id": user_id, "customer_id": customer_id})
        if cart:
            return cart
        raise


def _cart_items(user_id: str, customer_id: str, cart_id: str) -> list[dict]:
    store = current_app.extensions["store"]
    rows, _ = store.list("cart_items", {
        "user_id": user_id,
        "$or": [
            {"cart_id": cart_id},
            {"customer_id": customer_id},
            {"customer_company_id": customer_id},
            {"company_id": customer_id},
        ],
    }, limit=250, sort="created_at", direction=1)
    for row in rows:
        if row.get("cart_id") != cart_id:
            store.update_one("cart_items", {"_id": row["_id"]}, {"cart_id": cart_id})
            row["cart_id"] = cart_id
    return rows


@bp.get("/exchange-rates")
@permission_required("pricing.view")
def exchange_rates():
    force = request.args.get("refresh") == "true"
    try:
        return success(current_app.extensions["exchange_rate_service"].get_rates(force=force))
    except ExchangeRateUnavailable:
        return failure(
            "No live or cached exchange rate is currently available.",
            status=503, error="exchange_rate_unavailable",
        )


@bp.get("/cart")
@permission_required("cart.view")
def get_cart():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    user = current_user() or {}
    store = current_app.extensions["store"]
    cart = _active_cart(user["_id"], customer_id)
    rows = _cart_items(user["_id"], customer_id, cart["_id"])
    requested_currency = request.args.get("currency", "").upper()
    if requested_currency:
        refreshed = []
        try:
            for row in rows:
                _, _, line, rate_meta = _calculate({**row, "currency": requested_currency})
                updated = store.update_one("cart_items", {"_id": row["_id"]}, {
                    "currency": requested_currency, "pricing_preview": line, "exchange_rate_meta": rate_meta,
                })
                refreshed.append(updated or row)
        except PricingUnavailable as exc:
            return failure(str(exc), status=409)
        except (ValueError, LookupError) as exc:
            return failure(str(exc), status=422)
        rows = refreshed
    customer = customer_record(customer_id) or {}
    customer_name = customer.get("name") or customer.get("company_name") or ""
    return success({
        "customer": {"id": customer_id, "name": customer_name}, "cart_id": cart["_id"],
        "customer_id": customer_id, "customer_name": customer_name,
        "customer_company_id": customer_id, "customer_company_name": customer_name,
        "company_id": customer_id, "company_name": customer_name, "items": rows,
        "item_count": len(rows),
        "totals": calculate_quote_totals([row["pricing_preview"] for row in rows]) if rows else calculate_quote_totals([]),
    })


def _calculate(payload: dict):
    store = current_app.extensions["store"]
    customer_id = customer_id_from(payload)
    if not enforce_active_customer(customer_id):
        raise PermissionError("Customer access denied")
    customer_company = customer_record(customer_id)
    product = store.find_one("products", {"_id": payload.get("product_id"), "active": True})
    if not customer_company or not product:
        raise LookupError("Product or customer company not found")
    if "tax_rate" in payload:
        raise ValueError("Only the product can override the normal tax rate")
    currency = resolve_customer_currency(customer_company, payload.get("currency"), store, current_user())
    india_customer = customer_company.get("country_code") == "IN" or (customer_company.get("region") or {}).get("country_code") == "IN"
    tax_enabled = currency == "INR" and (bool(payload.get("tax_enabled")) if "tax_enabled" in payload else india_customer)
    tax_mode = str(payload.get("tax_mode", "exclusive")).lower()
    if tax_mode not in {"exclusive", "inclusive"}:
        tax_mode = "exclusive"
    rate, rate_meta = current_app.extensions["exchange_rate_service"].rate_for(currency)
    user = current_user() or {}
    configuration = payload.get("configuration", {})
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    company_tax_rate, company_tax_mode = company_tax_values(customer_company)
    line = calculate_line(
        product, configuration, quantity=int(payload.get("quantity", 1)),
        discount_percent=payload.get("discount_percent", 0), currency=currency, exchange_rate=rate,
        company_tax_rate=company_tax_rate, company_tax_mode=company_tax_mode,
        privileged_discount="pricing.discount.override" in user.get("permissions", []),
        adjustments=resolve_product_adjustments(store, product, configuration), business_rules=settings,
        apply_tax=tax_enabled, tax_mode_override=tax_mode,
    )
    return customer_company, product, line, rate_meta


@bp.post("/cart/items")
@permission_required("cart.manage")
def add_cart_item():
    payload = request.get_json(silent=True) or {}
    try:
        customer_company, product, line, rate_meta = _calculate(payload)
    except PermissionError as exc:
        return failure(str(exc), status=403)
    except LookupError as exc:
        return failure(str(exc), status=404)
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except (ValueError, TypeError) as exc:
        return failure(str(exc), status=422)
    user = current_user() or {}
    store = current_app.extensions["store"]
    cart = _active_cart(user["_id"], customer_company["_id"])
    fingerprint = configuration_fingerprint(product, payload.get("configuration", {}))
    existing = store.find_one("cart_items", {
        "user_id": user["_id"], "$or": [{"cart_id": cart["_id"]}, {"customer_id": customer_company["_id"]}, {"customer_company_id": customer_company["_id"]}, {"company_id": customer_company["_id"]}],
        "product_id": product["_id"], "currency": line["currency"], "configuration_fingerprint": fingerprint,
    })
    if existing:
        merged_payload = {
            **payload,
            "quantity": int(existing.get("quantity", 1)) + int(line["requested_quantity"]),
            "discount_percent": line["requested_discount_percent"],
        }
        _, _, merged_line, merged_rate_meta = _calculate(merged_payload)
        row = store.update_one("cart_items", {"_id": existing["_id"]}, {
            "cart_id": cart["_id"],
            "configuration": merged_line["configuration"],
            "quantity": merged_line["requested_quantity"], "discount_percent": merged_line["discount_percent"],
            "tax_enabled": line["currency"] == "INR" and bool(payload.get("tax_enabled", False)),
            "tax_mode": str(payload.get("tax_mode", "exclusive")).lower() if str(payload.get("tax_mode", "exclusive")).lower() in {"exclusive", "inclusive"} else "exclusive",
            "pricing_preview": merged_line, "exchange_rate_meta": merged_rate_meta,
        })
        return success(row, "Matching configuration combined in the cart")
    row = store.insert_one("cart_items", {
            "cart_id": cart["_id"], "user_id": user["_id"], "customer_id": customer_company["_id"], "product_id": product["_id"],
            "customer_company_id": customer_company["_id"], "company_id": customer_company["_id"], "customer_company_name": customer_company.get("name") or customer_company.get("company_name"), "company_name": customer_company.get("name") or customer_company.get("company_name"),
        "configuration": line["configuration"], "configuration_fingerprint": fingerprint,
        "quantity": line["requested_quantity"],
        "discount_percent": line["discount_percent"], "currency": line["currency"],
        "tax_enabled": line["currency"] == "INR" and bool(payload.get("tax_enabled", False)),
        "tax_mode": str(payload.get("tax_mode", "exclusive")).lower() if str(payload.get("tax_mode", "exclusive")).lower() in {"exclusive", "inclusive"} else "exclusive",
        "pricing_preview": line, "exchange_rate_meta": rate_meta,
    })
    return success(row, "Added to quotation", 201)


@bp.patch("/cart/items/<item_id>")
@permission_required("cart.manage")
def update_cart_item(item_id: str):
    store = current_app.extensions["store"]
    user = current_user() or {}
    existing = store.find_one("cart_items", {"_id": item_id, "user_id": user["_id"]})
    if not existing:
        return failure("Cart item not found", status=404)
    if not enforce_active_customer(existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id")):
        return failure("Customer access denied", status=403)
    payload = {**existing, **(request.get_json(silent=True) or {})}
    try:
        _, product, line, rate_meta = _calculate(payload)
    except PermissionError as exc:
        return failure(str(exc), status=403)
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except (ValueError, TypeError, LookupError) as exc:
        return failure(str(exc), status=422)
    row = store.update_one("cart_items", {"_id": item_id}, {
        "cart_id": existing.get("cart_id") or _active_cart(user["_id"], existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id"))["_id"],
        "customer_id": existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id"),
        "product_id": product["_id"],
        "configuration": line["configuration"],
        "configuration_fingerprint": configuration_fingerprint(product, payload.get("configuration", {})),
        "quantity": line["requested_quantity"],
        "discount_percent": line["discount_percent"], "currency": line["currency"],
        "tax_enabled": line["currency"] == "INR" and bool(payload.get("tax_enabled", False)),
        "tax_mode": str(payload.get("tax_mode", "exclusive")).lower() if str(payload.get("tax_mode", "exclusive")).lower() in {"exclusive", "inclusive"} else "exclusive",
        "pricing_preview": line, "exchange_rate_meta": rate_meta,
    })
    return success(row, "Cart updated")


@bp.delete("/cart/items/<item_id>")
@permission_required("cart.manage")
def remove_cart_item(item_id: str):
    user = current_user() or {}
    store = current_app.extensions["store"]
    existing = store.find_one("cart_items", {"_id": item_id, "user_id": user["_id"]})
    if not existing:
        return failure("Cart item not found", status=404)
    if not enforce_active_customer(existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id")):
        return failure("Customer access denied", status=403)
    removed = store.delete_one("cart_items", {"_id": item_id, "user_id": user["_id"]})
    return success(message="Item removed") if removed else failure("Cart item not found", status=404)


@bp.delete("/cart")
@permission_required("cart.manage")
def clear_cart():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    if not enforce_active_customer(customer_id):
        return failure("Customer access denied", status=403)
    store = current_app.extensions["store"]
    user = current_user() or {}
    cart = _active_cart(user["_id"], customer_id)
    rows = _cart_items(user["_id"], customer_id, cart["_id"])
    for row in rows:
        store.delete_one("cart_items", {"_id": row["_id"]})
    return success({"removed": len(rows)}, "Cart cleared")
