from __future__ import annotations

from app.customers.codes import customer_code


COMPANY = "company-moneda-demo"


def configure_product(app, product_id="mtech-mpack", base_price=10):
    app.extensions["store"].update_one("products", {"_id": product_id}, {
        "pricing": {"pricing_type": "formula", "price": base_price, "master_currency": "EUR", "unit": "sqm"},
        "pricing_status": "configured",
    })


def test_authentication_required(client):
    response = client.get("/api/v1/products")
    assert response.status_code == 401
    assert response.json["success"] is False


def test_password_login_accepts_username_and_user_id(client):
    response = client.post("/api/v1/auth/login", json={"identifier": "Admin", "password": "123@Admin"})
    assert response.status_code == 200
    assert response.json["data"]["next_step"] == "company-selection"
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json["data"]["user"]["username"] == "Admin"

    client.post("/api/v1/auth/logout", json={})
    by_id = client.post("/api/v1/auth/login", json={"identifier": "user-demo-admin", "password": "123@Admin"})
    assert by_id.status_code == 200

    client.post("/api/v1/auth/logout", json={})
    by_username = client.post("/api/v1/auth/login", json={"username": "Username Admin", "password": "123@Admin"})
    assert by_username.status_code == 200


def test_password_login_rejects_invalid_credentials(client):
    response = client.post("/api/v1/auth/login", json={"identifier": "Admin", "password": "wrong"})
    assert response.status_code == 401
    assert response.json["message"] == "Invalid username or password"


def test_demo_session_and_seeded_catalog(authenticated):
    me = authenticated.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json["data"]["user"]["role_id"] == "superadmin"
    products = authenticated.get("/api/v1/products?limit=100")
    assert products.json["data"]["pagination"]["total"] == 39
    categories = authenticated.get("/api/v1/categories")
    assert len(categories.json["data"]) == 3
    assert {item["_id"] for item in categories.json["data"]} == {"blankets", "mpacks", "chemicals"}
    families = authenticated.get("/api/v1/catalog/families")
    assert [item["id"] for item in families.json["data"]] == ["blankets", "mpacks", "chemicals"]
    blanket_categories = authenticated.get("/api/v1/catalog/blankets/categories")
    assert len(blanket_categories.json["data"]) == 7
    g3 = authenticated.get("/api/v1/catalog/blankets/products?category=cold_hot_set").json["data"]["items"]
    assert [item["_id"] for item in g3] == ["mtech_web_x_press_g3"]
    assert g3[0]["configuration"]["thicknesses_mm"] == [1.7, 1.96]


def test_admin_price_change_records_history(app, authenticated):
    response = authenticated.patch("/api/v1/products/mtech-mpack/pricing", json={
        "price": 12.5, "pricing_type": "formula", "tax_override_enabled": True, "tax_mode": "exclusive", "tax_rate": 12,
        "reason": "Approved test price",
    })
    assert response.status_code == 200
    assert response.json["data"]["pricing"]["price"] == 12.5
    assert app.extensions["store"].count("price_history", {"product_id": "mtech-mpack"}) == 1


def test_cart_rejects_quotation_tax_override(app, authenticated):
    configure_product(app)
    response = authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
        "tax_rate": 0, "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    })
    assert response.status_code == 422
    assert "Only the product" in response.json["message"]


def test_quotation_sequence_and_snapshot_immutability(app, authenticated):
    configure_product(app, base_price=10)
    payload = {
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 2,
        "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    }
    assert authenticated.post("/api/v1/cart/items", json=payload).status_code == 201
    quote = authenticated.post("/api/v1/quotations", json={"company_id": COMPANY, "customer_id": "customer-demo-1", "currency": "EUR"})
    assert quote.status_code == 201
    assert quote.json["data"]["quotation_number"] == "MT-MONEDA-DEMO-001"
    quote_id = quote.json["data"]["_id"]
    assert quote.json["data"]["lines"][0]["master_unit_price"] == 10
    configure_product(app, base_price=50)
    stored = authenticated.get(f"/api/v1/quotations/{quote_id}")
    assert stored.json["data"]["lines"][0]["master_unit_price"] == 10


def test_customer_specific_quotation_sequences_are_independent(app, authenticated):
    configure_product(app, base_price=10)

    def add_item(customer_id: str):
        response = authenticated.post("/api/v1/cart/items", json={
            "customer_id": customer_id, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
            "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
        })
        assert response.status_code == 201

    def create_quote(customer_id: str):
        response = authenticated.post("/api/v1/quotations", json={"customer_id": customer_id, "currency": "EUR"})
        assert response.status_code == 201
        return response.json["data"]

    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"}).status_code == 200
    add_item("customer-demo-1")
    first_northstar = create_quote("customer-demo-1")
    add_item("customer-demo-1")
    second_northstar = create_quote("customer-demo-1")

    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-2"}).status_code == 200
    add_item("customer-demo-2")
    first_orbit = create_quote("customer-demo-2")

    assert first_northstar["quotation_number"] == "MT-NORTHSTAR-001"
    assert second_northstar["quotation_number"] == "MT-NORTHSTAR-002"
    assert first_orbit["quotation_number"] == "MT-ORBIT-001"


def test_legacy_quotation_numbers_are_preserved(app, authenticated):
    legacy = app.extensions["store"].insert_one("quotations", {
        "quotation_number": "MON_Q0028", "customer_id": COMPANY,
        "currency": "EUR", "lines": [], "totals": {"grand_total": 0},
    })
    response = authenticated.get(f"/api/v1/quotations/{legacy['_id']}")
    assert response.status_code == 200
    assert response.json["data"]["quotation_number"] == "MON_Q0028"


def test_customer_carts_are_isolated_and_persistent(app, authenticated):
    item_payload = {
        "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 1,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    }
    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"}).status_code == 200
    assert authenticated.post("/api/v1/cart/items", json={"customer_id": "customer-demo-1", **item_payload}).status_code == 201
    northstar = authenticated.get("/api/v1/cart?customer_id=customer-demo-1").json["data"]
    assert northstar["customer"]["id"] == "customer-demo-1"
    assert northstar["item_count"] == 1

    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-2"}).status_code == 200
    orbit_empty = authenticated.get("/api/v1/cart?customer_id=customer-demo-2").json["data"]
    assert orbit_empty["item_count"] == 0
    assert authenticated.post("/api/v1/cart/items", json={"customer_id": "customer-demo-2", **item_payload}).status_code == 201

    assert authenticated.get("/api/v1/cart?customer_id=customer-demo-1").status_code == 403
    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"}).status_code == 200
    northstar_again = authenticated.get("/api/v1/cart?customer_id=customer-demo-1").json["data"]
    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-2"}).status_code == 200
    orbit_again = authenticated.get("/api/v1/cart?customer_id=customer-demo-2").json["data"]
    assert northstar_again["item_count"] == 1
    assert orbit_again["item_count"] == 1
    assert northstar_again["cart_id"] != orbit_again["cart_id"]
    assert app.extensions["store"].count("carts") == 2


def test_customer_code_is_stable_when_customer_name_changes(app, authenticated):
    assert customer_code("ABC Packaging & Printing") == "ABC-PACKAGING"
    before = app.extensions["store"].find_one("customers", {"_id": "customer-demo-1"})
    assert before["customer_code"] == "NORTHSTAR"

    response = authenticated.patch("/api/v1/customers/customer-demo-1", json={"name": "Northstar Printworks International"})
    assert response.status_code == 200
    assert response.json["data"]["customer_code"] == "NORTHSTAR"
    stored = app.extensions["store"].find_one("customers", {"_id": "customer-demo-1"})
    assert stored["customer_code"] == "NORTHSTAR"

    duplicate = authenticated.post("/api/v1/customers", json={"name": "Northstar Labels"})
    assert duplicate.status_code == 201
    assert duplicate.json["data"]["customer_code"] == "NORTHSTAR-2"


def test_quotation_level_tax_override_rejected(app, authenticated):
    configure_product(app)
    authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
        "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    })
    response = authenticated.post("/api/v1/quotations", json={
        "company_id": COMPANY, "customer_id": "customer-demo-1", "currency": "EUR", "tax_rate": 0,
    })
    assert response.status_code == 422
    assert "Quotation-level" in response.json["message"]


def test_company_scope_is_enforced(authenticated):
    response = authenticated.get("/api/v1/customers?company_id=unknown-company")
    assert response.status_code == 403


def test_cart_requires_server_active_customer_context(app, authenticated):
    with authenticated.session_transaction() as session:
        session.pop("active_customer_id", None)
        session.pop("selected_customer_company_id", None)
        session.pop("active_company_id", None)

    assert authenticated.get(f"/api/v1/cart?customer_id={COMPANY}").status_code == 403
    response = authenticated.post("/api/v1/cart/items", json={
        "customer_id": COMPANY, "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 1,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert response.status_code == 403


def test_customer_selection_rejects_customer_outside_user_scope(authenticated):
    response = authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "unknown-customer"})
    assert response.status_code == 403


def test_blanket_bar_format_uses_two_independent_embedded_bars(authenticated):
    response = authenticated.post("/api/v1/products/mtech_active_sf/price-preview", json={
        "company_id": COMPANY, "currency": "EUR", "quantity": 1,
        "configuration": {
            "thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm",
            "format_type": "bar_format", "bar_1_id": "aluminium", "bar_2_id": "steel",
        },
    })
    assert response.status_code == 200
    line = response.json["data"]["line"]
    assert line["base_unit_price_master"] == 42
    assert line["adjustment_amount_master"] == 5.23
    assert [item["product_id"] for item in line["adjustments"]] == ["aluminium", "steel"]
    assert line["line_total"] == 55.73


def test_cut_format_has_no_bars_and_no_surcharge(authenticated):
    response = authenticated.post("/api/v1/products/mtech_active_sf/price-preview", json={
        "company_id": COMPANY, "currency": "EUR", "quantity": 1,
        "configuration": {
            "thickness_mm": 1.96, "length": 1000, "width": 1000,
            "dimension_unit": "mm", "format_type": "cut_format",
        },
    })
    line = response.json["data"]["line"]
    assert line["master_unit_price"] == 42
    assert line["adjustments"] == []


def test_cart_edit_can_change_the_product(app, authenticated):
    created = authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 1,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert created.status_code == 201
    item_id = created.json["data"]["_id"]
    updated = authenticated.patch(f"/api/v1/cart/items/{item_id}", json={
        "company_id": COMPANY, "product_id": "mtech_active_prime", "currency": "EUR", "quantity": 2,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert updated.status_code == 200
    assert updated.json["data"]["product_id"] == "mtech_active_prime"
    assert updated.json["data"]["pricing_preview"]["product_name"] == "MTech Active Prime"
    assert updated.json["data"]["pricing_preview"]["requested_quantity"] == 2


def test_international_company_defaults_to_no_tax(app, authenticated):
    company = app.extensions["store"].insert_one("companies", {
        "_id": "company-europe", "name": "European Printer", "country": "Germany",
        "default_currency": "EUR", "tax_enabled": False, "default_tax_rate": 0,
        "default_tax_mode": "no_tax", "active": True,
    })
    assert authenticated.post("/api/v1/companies/select", json={"company_id": company["_id"]}).status_code == 200
    response = authenticated.post("/api/v1/products/mtech_active_sf/price-preview", json={
        "company_id": company["_id"], "currency": "EUR", "quantity": 1,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    line = response.json["data"]["line"]
    assert line["tax_mode"] == "no_tax"
    assert line["tax_amount"] == 0
    assert line["line_total"] == 42


def test_quotation_preview_validates_without_saving(app, authenticated):
    authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 1,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    preview = authenticated.post("/api/v1/quotations/preview", json={
        "company_id": COMPANY, "customer_id": "customer-demo-1", "currency": "EUR",
        "payment_terms": "Advance", "proforma_validity_days": 30, "transport_mode": "by_consignee",
    })
    assert preview.status_code == 200
    assert preview.json["data"]["quotation_number"] == "PREVIEW"
    assert preview.json["data"]["transport"]["label"] == "By Consignee"
    assert app.extensions["store"].count("quotations") == 0


def test_quotation_snapshot_contains_complete_cut_and_bar_lines(authenticated):
    cut = authenticated.post("/api/v1/cart/items", json={
        "customer_id": COMPANY, "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 2,
        "configuration": {
            "thickness_mm": 1.96, "length": 1054, "width": 843,
            "dimension_unit": "mm", "format_type": "cut_format",
        },
    })
    bar = authenticated.post("/api/v1/cart/items", json={
        "customer_id": COMPANY, "product_id": "mtech_active_prime", "currency": "EUR", "quantity": 1,
        "configuration": {
            "thickness_mm": 1.96, "length": 1054, "width": 890,
            "dimension_unit": "mm", "format_type": "bar_format",
            "bar_1_id": "aluminium", "use_second_bar": False,
        },
    })
    assert cut.status_code == 201
    assert bar.status_code == 201

    quote = authenticated.post("/api/v1/quotations", json={"customer_id": COMPANY, "currency": "EUR"})
    assert quote.status_code == 201
    lines = quote.json["data"]["lines"]
    assert len(lines) == 2

    cut_line = next(line for line in lines if line["format"] == "cut_format")
    bar_line = next(line for line in lines if line["format"] == "bar_format")
    assert cut_line["article_no"] and cut_line["product_name"] and cut_line["description"]
    assert cut_line["dimensions"] == {"length": 1054, "width": 843, "unit": "mm"}
    assert cut_line["thickness"] == 1.96
    assert cut_line["bars"] == []
    assert cut_line["quantity"] == 2
    assert cut_line["unit_price"] > 0 and cut_line["line_total"] > 0

    assert bar_line["dimensions"] == {"length": 1054, "width": 890, "unit": "mm"}
    assert bar_line["bars"] == [{"name": "Aluminium", "quantity": 2}]
    assert bar_line["quantity"] == 1


def test_converted_order_keeps_quotation_number(authenticated):
    authenticated.post("/api/v1/cart/items", json={
        "customer_id": COMPANY, "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 1,
        "configuration": {
            "thickness_mm": 1.96, "length": 1000, "width": 1000,
            "dimension_unit": "mm", "format_type": "cut_format",
        },
    })
    quote = authenticated.post("/api/v1/quotations", json={"customer_id": COMPANY, "currency": "EUR"}).json["data"]
    converted = authenticated.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={})
    assert converted.status_code == 201
    order = converted.json["data"]
    assert order["quotation_id"] == quote["_id"]
    assert order["quotation_number"] == quote["quotation_number"]
