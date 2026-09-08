from __future__ import annotations


def test_separated_catalog_contracts_are_available(authenticated):
    product_types = authenticated.get("/api/v1/catalog/product-types")
    assert product_types.status_code == 200
    assert [row["id"] for row in product_types.json["data"]] == ["blankets", "mpacks", "chemicals"]
    assert product_types.json["data"][1]["name"] == "Underpacking"

    assert authenticated.get("/api/v1/catalog/blankets/options").status_code == 200
    bars = authenticated.get("/api/v1/catalog/blankets/bars")
    assert bars.status_code == 200
    assert bars.json["data"]["total"] == 6
    assert all(row["pricing"]["unit"] == "bar" for row in bars.json["data"]["items"])

    assert authenticated.get("/api/v1/catalog/mpacks/types").status_code == 200
    assert authenticated.get("/api/v1/catalog/mpacks/options").status_code == 200
    assert authenticated.get("/api/v1/catalog/mpacks/products").json["data"]["total"] == 4
    assert authenticated.get("/api/v1/catalog/chemicals/categories").status_code == 200
    assert authenticated.get("/api/v1/catalog/chemicals/options").status_code == 200
    assert authenticated.get("/api/v1/catalog/chemicals/products").json["data"]["total"] == 22


def test_admin_can_edit_eur_product_variant_and_bar_with_history(authenticated):
    listing = authenticated.get("/api/v1/admin/pricing/products?family=bars")
    assert listing.status_code == 200
    assert listing.json["data"]["total"] == 6
    underpacking = authenticated.get("/api/v1/admin/pricing/products?family=mpacks")
    assert underpacking.json["data"]["items"][0]["family_name"] == "Underpacking"

    product_update = authenticated.patch("/api/v1/admin/pricing/products/mtech_kleber_bf", json={
        "currency": "EUR", "price_map": "variant_prices", "price_key": "0.95",
        "price_eur": 64.25, "pricing_status": "configured", "reason": "Quarterly price review",
    })
    assert product_update.status_code == 200
    assert product_update.json["data"]["pricing"]["variant_prices"]["0.95"] == 64.25

    bar_update = authenticated.patch("/api/v1/admin/pricing/products/aluminium", json={
        "currency": "EUR", "price_eur": 2.15, "pricing_status": "configured", "reason": "Supplier update",
    })
    assert bar_update.status_code == 200
    assert bar_update.json["data"]["pricing"]["price_eur"] == 2.15
    history = authenticated.get("/api/v1/admin/pricing/history/aluminium")
    assert history.status_code == 200
    assert history.json["data"]["items"][0]["new_price_eur"] == 2.15


def test_master_price_api_rejects_non_eur_and_invalid_values(authenticated):
    assert authenticated.patch("/api/v1/admin/pricing/products/aluminium", json={
        "currency": "USD", "price_eur": 3,
    }).status_code == 422
    assert authenticated.patch("/api/v1/admin/pricing/products/aluminium", json={
        "currency": "EUR", "price_eur": -1,
    }).status_code == 422
    zero = authenticated.patch("/api/v1/admin/pricing/products/aluminium", json={
        "currency": "EUR", "price_eur": 0, "pricing_status": "configured",
    })
    assert zero.status_code == 200
    assert zero.json["data"]["pricing"]["price_eur"] == 0


def test_manager_can_view_pricing_but_cannot_edit(app, authenticated):
    store = app.extensions["store"]
    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "manager_sales_admin"})
    view = authenticated.get("/api/v1/admin/pricing/products")
    assert view.status_code == 200
    edit = authenticated.patch("/api/v1/admin/pricing/products/aluminium", json={
        "currency": "EUR", "price_eur": 3, "pricing_status": "configured",
    })
    assert edit.status_code == 403


def test_exchange_cache_has_provider_dates_expiry_and_cached_fallback(app):
    service = app.extensions["exchange_rate_service"]
    live = service.get_rates(force=True)
    assert live["source"] == "live"
    for target in ("USD", "INR"):
        row = app.extensions["store"].find_one("exchange_rates", {"_id": f"EUR_{target}"})
        assert row["base_currency"] == "EUR"
        assert row["target_currency"] == target
        assert row["provider"] == "test-rates"
        assert row["fetched_at"] is not None
        assert row["expires_at"] is not None

    class UnavailableProvider:
        name = "ECB"
        source = "Frankfurter API"

        def get_rates(self, _base, _targets):
            raise RuntimeError("provider offline")

    service.provider = UnavailableProvider()
    cached = service.get_rates(force=True)
    assert cached["source"] == "cached"
    assert cached["stale"] is True
    assert "last stored rate" in cached["warning"]


def test_quotation_persists_eur_master_and_conversion_snapshot(app, authenticated):
    app.extensions["store"].update_one("products", {"_id": "mtech-mpack"}, {
        "pricing": {"pricing_type": "formula", "price": 10, "master_currency": "EUR", "unit": "sqm"},
        "pricing_status": "configured",
    })
    added = authenticated.post("/api/v1/cart/items", json={
        "customer_id": "company-moneda-demo", "product_id": "mtech-mpack", "currency": "USD", "quantity": 1,
        "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    })
    assert added.status_code == 201
    created = authenticated.post("/api/v1/quotations", json={
        "customer_id": "company-moneda-demo", "currency": "USD",
    })
    assert created.status_code == 201
    quote = created.json["data"]
    assert quote["master_currency"] == "EUR"
    assert quote["quotation_currency"] == "EUR"
    assert quote["currency"] == "EUR"
    assert quote["pricing_policy"] == "eur_only_no_tax_v1"
    assert quote["exchange_rate"] == 1
    assert quote["exchange_rate_provider"] == "master"
    assert quote["exchange_rate_source"] == "master"
    assert quote["lines"][0]["master_price_eur"] == quote["lines"][0]["master_unit_price"]
    assert quote["lines"][0]["converted_price"] == quote["lines"][0]["unit_price"]
    assert "tax_amount" not in quote["lines"][0]
    assert "tax_amount" not in quote["totals"]
