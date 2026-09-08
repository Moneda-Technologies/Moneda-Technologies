from __future__ import annotations

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import current_user, customer_id_from, customer_record, enforce_active_customer, permission_required
from app.pricing.engine import (
    PricingUnavailable, calculate_line, calculate_quote_totals,
    configuration_fingerprint, resolve_product_adjustments, with_display_currency,
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


def _commercial_snapshot_fields(line: dict) -> dict:
    """Persist the immutable EUR commercial snapshot beside display data."""
    return {
        "master_currency": "EUR",
        "master_unit_price_eur": line["master_unit_price"],
        "master_subtotal": line["master_subtotal"],
        "master_discount_amount": line["master_discount_amount"],
        "master_final_total": line["master_final_total"],
        "display_currency": line["display_currency"],
    }


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
                saved = row.get("pricing_preview") or {}
                if saved.get("master_currency") == "EUR" and saved.get("master_final_total") is not None:
                    rate, rate_meta = current_app.extensions["exchange_rate_service"].rate_for(requested_currency)
                    line = with_display_currency(saved, requested_currency, rate)
                else:
                    # One-time compatibility path for carts saved before EUR
                    # commercial snapshots were introduced.
                    _, _, line, rate_meta = _calculate({**row, "display_currency": requested_currency})
                updated = store.update_one("cart_items", {"_id": row["_id"]}, {
                    "currency": "EUR", "display_currency": requested_currency,
                    "pricing_preview": line, "exchange_rate_meta": rate_meta,
                    **_commercial_snapshot_fields(line),
                })
                current_app.logger.info(
                    "discount_debug item_id=%s cart_saved_discount=%s response_discount=%s",
                    row["_id"], row.get("discount_percent", 0), line.get("discount_percent", 0),
                )
                refreshed.append(updated or row)
        except PricingUnavailable as exc:
            return failure(str(exc), status=409)
        except (ValueError, LookupError) as exc:
            return failure(str(exc), status=422)
        rows = refreshed
    customer = customer_record(customer_id) or {}
    customer_name = customer.get("name") or customer.get("company_name") or ""
    display_totals = calculate_quote_totals([row["pricing_preview"] for row in rows]) if rows else calculate_quote_totals([])
    master_totals = {
        "subtotal": round(sum(float(row["pricing_preview"].get("master_subtotal", 0)) for row in rows), 2),
        "discount_amount": round(sum(float(row["pricing_preview"].get("master_discount_amount", 0)) for row in rows), 2),
        "transport_cost": 0.0, "transport_total": 0.0,
        "grand_total": round(sum(float(row["pricing_preview"].get("master_final_total", 0)) for row in rows), 2),
    }
    return success({
        "customer": {"id": customer_id, "name": customer_name}, "cart_id": cart["_id"],
        "customer_id": customer_id, "customer_name": customer_name,
        "customer_company_id": customer_id, "customer_company_name": customer_name,
        "company_id": customer_id, "company_name": customer_name, "items": rows,
        "item_count": len(rows), "master_currency": "EUR",
        "display_currency": requested_currency or (
            rows[0].get("display_currency") or rows[0].get("pricing_preview", {}).get("display_currency")
            if rows else "EUR"
        ),
        "totals": display_totals, "master_totals": master_totals,
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
    currency = resolve_customer_currency(customer_company, payload.get("display_currency") or payload.get("currency"), store, current_user())
    rate, rate_meta = current_app.extensions["exchange_rate_service"].rate_for(currency)
    user = current_user() or {}
    configuration = payload.get("configuration", {})
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    line = calculate_line(
        product, configuration, quantity=int(payload.get("quantity", 1)),
        discount_percent=payload.get("discount_percent", 0), currency=currency, exchange_rate=rate,
        company_tax_rate=0, company_tax_mode="no_tax",
        privileged_discount="pricing.discount.override" in user.get("permissions", []),
        adjustments=resolve_product_adjustments(store, product, configuration), business_rules=settings,
        apply_tax=False, tax_mode_override="no_tax",
    )
    line["display_currency"] = currency
    line["quotation_currency"] = "EUR"
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
        "product_id": product["_id"], "configuration_fingerprint": fingerprint,
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
            "currency": "EUR", "display_currency": merged_line["display_currency"],
            "pricing_preview": merged_line, "exchange_rate_meta": merged_rate_meta,
            **_commercial_snapshot_fields(merged_line),
        })
        current_app.logger.info(
            "cart_item_discount action=merge cart_item_id=%s customer_id=%s product_id=%s discount_percent=%s discount_source=calculator_selection",
            existing["_id"], customer_company["_id"], product["_id"], merged_line["discount_percent"],
        )
        return success(row, "Matching configuration combined in the cart")
    row = store.insert_one("cart_items", {
            "cart_id": cart["_id"], "user_id": user["_id"], "customer_id": customer_company["_id"], "product_id": product["_id"],
            "customer_company_id": customer_company["_id"], "company_id": customer_company["_id"], "customer_company_name": customer_company.get("name") or customer_company.get("company_name"), "company_name": customer_company.get("name") or customer_company.get("company_name"),
        "configuration": line["configuration"], "configuration_fingerprint": fingerprint,
        "quantity": line["requested_quantity"],
        "discount_percent": line["discount_percent"], "currency": "EUR",
        "display_currency": line["display_currency"],
        "pricing_preview": line, "exchange_rate_meta": rate_meta,
        **_commercial_snapshot_fields(line),
    })
    current_app.logger.info(
        "cart_item_discount action=create cart_item_id=%s customer_id=%s product_id=%s discount_percent=%s discount_source=calculator_selection",
        row["_id"], customer_company["_id"], product["_id"], row["discount_percent"],
    )
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
        "discount_percent": line["discount_percent"], "currency": "EUR",
        "display_currency": line["display_currency"],
        "pricing_preview": line, "exchange_rate_meta": rate_meta,
        **_commercial_snapshot_fields(line),
    })
    current_app.logger.info(
        "cart_item_discount action=update cart_item_id=%s customer_id=%s product_id=%s discount_percent=%s discount_source=calculator_selection",
        item_id, row.get("customer_id") if row else existing.get("customer_id"), product["_id"], line["discount_percent"],
    )
    current_app.logger.info(
        "cart_item_discount_update item_id=%s requested_discount=%s stored_discount=%s result=%s discount_source=saved_cart_item",
        item_id, payload.get("discount_percent", 0), row.get("discount_percent") if row else None,
        "PASS" if row and float(row.get("discount_percent", 0)) == float(line.get("discount_percent", 0)) else "FAIL",
    )
    current_app.logger.info(
        "cart_item_after_patch item_id=%s discount_percent=%s",
        item_id, row.get("discount_percent") if row else None,
    )
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
