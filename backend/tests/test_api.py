from __future__ import annotations

import pytest

from app.communication.email import EmailDeliveryError
from app.customers.codes import customer_code
from app.repositories.store import build_store, utcnow


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
    cookie = "\n".join(response.headers.getlist("Set-Cookie"))
    assert "session=" in cookie and "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Path=/" in cookie
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


def test_logout_invalidates_session(client):
    assert client.post("/api/v1/auth/demo", json={}).status_code == 200
    assert client.get("/api/v1/me").status_code == 200
    assert client.post("/api/v1/auth/logout", json={}).status_code == 200
    assert client.get("/api/v1/me").status_code == 401


def test_otp_does_not_claim_delivery_when_mail_api_fails(app, client):
    class FailingProvider:
        def send(self, **_kwargs):
            raise RuntimeError("Mail API rejected the request")

    app.extensions["otp_service"].email_provider = FailingProvider()
    user = app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, {"email": "demo@moneda.example"})
    response = client.post("/api/v1/auth/request-otp", json={"email": user["email"], "purpose": "login"})
    assert response.status_code == 503
    assert "could not be sent" in response.json["message"].lower()
    assert app.extensions["store"].count("otp_challenges") == 1
    challenge = app.extensions["store"].find_one("otp_challenges", {"email": user["email"].lower()})
    assert challenge["used"] is True


def test_non_demo_store_fails_fast_without_mongodb_uri():
    with pytest.raises(RuntimeError, match="MONGODB_URI is required"):
        build_store({"MONGODB_URI": "", "MONGODB_DATABASE": "moneda", "DEMO_MODE": False, "TESTING": False})


def test_signup_validation_returns_exact_field_and_reference(client):
    response = client.post("/api/v1/auth/signup/start", json={"username": "bad name", "email": "invalid"})
    assert response.status_code == 422
    assert response.json["error"] == "validation_error"
    assert response.json["request_id"].startswith("signup-")
    assert response.json["errors"][0]["field"] == "name"
    assert response.json["message"] == "Name is required."


def test_signup_zoho_not_connected_returns_and_logs_trace_metadata(app, client, caplog):
    class DisconnectedZohoProvider:
        def send_signup_otp(self, **_kwargs):
            try:
                raise RuntimeError("not connected")
            except RuntimeError as cause:
                raise EmailDeliveryError(
                    "Zoho Mail is not connected", stage="oauth_configuration",
                    diagnostic_id="email-test-oauth", error_code="OAUTH_NOT_CONNECTED",
                ) from cause

    caplog.set_level("INFO")
    app.extensions["otp_service"].email_provider = DisconnectedZohoProvider()
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "OAuth Diagnostic", "username": "oauth.diagnostic", "email": "oauth-diagnostic@example.com",
    })

    assert response.status_code == 503
    assert response.json["message"] == "Email service is not connected. Please contact the administrator."
    assert response.json["request_id"].startswith("signup-")
    assert response.json["diagnostic_id"] == "email-test-oauth"
    assert response.json["stage"] == "oauth_configuration"
    assert response.json["error"] == "OAUTH_NOT_CONNECTED"
    logs = caplog.text
    assert response.json["request_id"] in logs
    assert "exception_class=RuntimeError" in logs
    assert "error_code=OAUTH_NOT_CONNECTED" in logs


def test_public_health_and_config_are_safe(client):
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json["data"]["rate_limit"]["backend"] == "memory"

    config = client.get("/api/v1/config")
    assert config.status_code == 200
    assert config.json["data"]["app"]["name"] == "Moneda Technologies"
    assert config.json["data"]["features"]["signup"] is True
    serialized = str(config.json).lower()
    assert "mail_password" not in serialized
    assert "mongodb_uri" not in serialized
    assert "secret_key" not in serialized

    assert client.get("/api/v1/me").status_code == 401


def test_admin_email_health_is_safe_and_reports_latest_attempt(app, authenticated):
    app.extensions["store"].insert_one("email_logs", {
        "status": "failed", "stage": "token_refresh", "diagnostic_id": "email-test",
        "error_code": "OAUTH_REFRESH_ERROR", "message_type": "otp_login", "created_at": utcnow(),
    })
    response = authenticated.get("/api/v1/admin/email/health")
    assert response.status_code == 200
    assert response.json["data"]["last_attempt"]["stage"] == "token_refresh"
    serialized = str(response.json).lower()
    assert "mail_password" not in serialized and "password" not in serialized


def test_order_confirmation_and_status_use_central_email_service(app, authenticated):
    order = app.extensions["store"].insert_one("orders", {
        "_id": "order-email-test", "order_number": "MON_ORD99999",
        "customer_id": COMPANY, "status": "Processing", "currency": "EUR",
        "customer_snapshot": {"name": "Test Customer", "email": "customer@example.com"},
        "totals": {"grand_total": 100}, "products_snapshot": [], "history": [],
    })
    confirmation = authenticated.post(f"/api/v1/orders/{order['_id']}/send-confirmation", json={})
    status = authenticated.post(f"/api/v1/orders/{order['_id']}/send-status", json={})
    assert confirmation.status_code == 200
    assert status.status_code == 200
    messages = app.extensions["email_provider"].messages
    assert messages[-2]["to"] == ["customer@example.com"]
    assert messages[-2]["from"] == "orders@monedatechnologies.com"
    assert messages[-1]["from"] == "orders@monedatechnologies.com"
    assert messages[-2]["cc"] == ["business@monedatechnologies.com"]
    assert messages[-2]["bcc"] == ["operations@chemo.in"]
    assert messages[-1]["cc"] == ["business@monedatechnologies.com"]
    assert messages[-1]["bcc"] == ["operations@chemo.in"]
    assert "Order confirmation" in messages[-2]["subject"]
    assert "Order status" in messages[-1]["subject"]
    assert "Processing" in messages[-1]["html"]


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


def test_cart_ignores_legacy_tax_override(app, authenticated):
    configure_product(app)
    response = authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
        "tax_rate": 0, "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    })
    assert response.status_code == 201
    assert "tax_amount" not in response.json["data"]["pricing_preview"]


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


def test_quotation_pdf_preview_returns_valid_pdf_response(app, authenticated):
    configure_product(app, base_price=10)
    payload = {
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
        "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    }
    assert authenticated.post("/api/v1/cart/items", json=payload).status_code == 201
    quote = authenticated.post("/api/v1/quotations", json={"company_id": COMPANY, "customer_id": "customer-demo-1", "currency": "EUR"})
    assert quote.status_code == 201
    response = authenticated.get(f"/api/v1/quotations/{quote.json['data']['_id']}/pdf?preview=true")
    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("application/pdf")
    assert response.data.startswith(b"%PDF-")
    assert "X-Frame-Options" not in response.headers
    assert "frame-ancestors" in response.headers["Content-Security-Policy"]
    assert authenticated.get(f"/api/v1/quotations/{quote.json['data']['_id']}").headers["X-Frame-Options"] == "DENY"


def test_sent_quotation_can_be_resent_without_duplication_and_logs_event(app, authenticated, monkeypatch):
    configure_product(app, base_price=10)
    app.extensions["store"].update_one("customers", {"_id": COMPANY}, {"email": "customer@example.com"})
    app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, {
        "name": "Athul Nair", "email": "salesperson@example.com", "phone": "+91 98765 43210",
    })
    payload = {
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
        "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    }
    assert authenticated.post("/api/v1/cart/items", json=payload).status_code == 201
    note = "Please confirm delivery schedule before dispatch.\n<script>alert(1)</script>"
    created = authenticated.post("/api/v1/quotations", json={"company_id": COMPANY, "currency": "EUR", "customer_notes": note})
    assert created.status_code == 201
    quotation = created.json["data"]
    quotation_id = quotation["_id"]
    quotation_number = quotation["quotation_number"]
    assert quotation["created_by_user_id"] == "user-demo-admin"
    assert quotation["creator_snapshot"] == {
        "name": "Athul Nair", "email": "salesperson@example.com", "phone": "+91 98765 43210",
    }
    app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, {
        "name": "Changed Later", "email": "changed@example.com", "phone": None,
    })
    historical = authenticated.get(f"/api/v1/quotations/{quotation_id}")
    assert historical.json["data"]["creator_snapshot"] == quotation["creator_snapshot"]

    first = authenticated.post(f"/api/v1/quotations/{quotation_id}/send", json={})
    second = authenticated.post(f"/api/v1/quotations/{quotation_id}/send", json={})
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json["data"]["_id"] == quotation_id
    assert second.json["data"]["quotation_number"] == quotation_number
    assert second.json["data"]["customer_notes"] == note

    rows, total = app.extensions["store"].list("quotations", {"_id": quotation_id}, limit=10)
    assert total == 1 and rows[0]["status"] == "Sent"
    logs, log_total = app.extensions["store"].list("email_logs", {"quotation_id": quotation_id}, limit=10)
    assert log_total == 2
    assert {row["event"] for row in logs} == {"initial_send", "resend"}
    assert all(row["quotation_number"] == quotation_number for row in logs)
    messages = app.extensions["email_provider"].messages
    assert messages[-1]["from"] == "quotations@monedatechnologies.com"
    assert messages[-1]["cc"] == ["business@monedatechnologies.com", "changed@example.com"]
    assert messages[-1]["bcc"] == ["operations@chemo.in"]

    def fail_resend(**_kwargs):
        raise EmailDeliveryError("forced failure", stage="message_submission", diagnostic_id="email-resend-test")

    monkeypatch.setattr(app.extensions["email_service"], "send_quotation", fail_resend)
    failed = authenticated.post(f"/api/v1/quotations/{quotation_id}/send", json={})
    assert failed.status_code == 503
    stored = app.extensions["store"].find_one("quotations", {"_id": quotation_id})
    assert stored["status"] == "Sent"
    assert stored["email_status"] == "resend_failed"
    failure_logs, failure_total = app.extensions["store"].list("email_logs", {"quotation_id": quotation_id}, limit=10)
    assert failure_total == 3
    assert any(row["event"] == "resend" and row["submission_status"] == "failed" for row in failure_logs)


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

    duplicate = authenticated.post("/api/v1/customers", json={
        "name": "Northstar Labels", "contact_name": "Test Contact",
        "email": "contact@northstar.example", "phone": "+1 202 555 0144",
        "continent": "North America", "country_code": "US", "preferred_currency": "USD",
        "payment_terms": "Advance", "address": "1 Main Street",
    })
    assert duplicate.status_code == 201
    assert duplicate.json["data"]["customer_code"] == "NORTHSTAR-2"


def test_quotation_level_tax_override_is_ignored(app, authenticated):
    configure_product(app)
    authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech-mpack", "currency": "EUR", "quantity": 1,
        "configuration": {"length": 1000, "width": 1000, "dimension_unit": "mm", "thickness_micron": 100},
    })
    response = authenticated.post("/api/v1/quotations", json={
        "company_id": COMPANY, "customer_id": "customer-demo-1", "currency": "EUR", "tax_rate": 0,
    })
    assert response.status_code == 201
    assert response.json["data"]["currency"] == "EUR"
    assert "tax_amount" not in response.json["data"]["totals"]


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
    assert line["line_total"] == 47.23


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


def test_customer_country_does_not_add_tax(app, authenticated):
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
    assert "tax_mode" not in line
    assert "tax_amount" not in line
    assert line["line_total"] == 42


def test_india_price_preview_ignores_legacy_gst_flags(authenticated):
    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"}).status_code == 200
    base = {
        "customer_id": "customer-demo-1", "currency": "INR", "quantity": 1, "discount_percent": 0,
        "tax_enabled": True,
        "configuration": {"thickness_mm": 1.96, "length": 780, "width": 1056, "dimension_unit": "mm", "format_type": "cut_format"},
    }
    exclusive_response = authenticated.post("/api/v1/products/mtech_magnum_sf/price-preview", json={
        **base, "tax_mode": "exclusive", "is_gst_inclusive": False,
    })
    inclusive_response = authenticated.post("/api/v1/products/mtech_magnum_sf/price-preview", json={
        **base, "tax_mode": "inclusive", "is_gst_inclusive": True,
    })
    assert exclusive_response.status_code == inclusive_response.status_code == 200
    exclusive = exclusive_response.json["data"]["line"]
    inclusive = inclusive_response.json["data"]["line"]
    assert exclusive["total"] == inclusive["total"] == exclusive["subtotal"]
    assert "gst_amount" not in exclusive and "tax_amount" not in exclusive
    assert "gst_amount" not in inclusive and "tax_amount" not in inclusive


def test_price_preview_quantity_ten_keeps_explicit_zero_discount(authenticated):
    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"}).status_code == 200
    response = authenticated.post("/api/v1/products/mtech_magnum_sf/price-preview", json={
        "customer_id": "customer-demo-1", "currency": "INR", "quantity": 10, "discount_percent": 0,
        "tax_enabled": True, "tax_mode": "exclusive", "is_gst_inclusive": False,
        "configuration": {"thickness_mm": 1.96, "length": 780, "width": 1056, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert response.status_code == 200
    line = response.json["data"]["line"]
    assert line["requested_discount_percent"] == 0
    assert line["discount_percent"] == 0
    assert line["discount_amount"] == 0
    assert line["discount_source"] == "default"
    assert "gst_amount" not in line

    created = authenticated.post("/api/v1/cart/items", json={
        "customer_id": "customer-demo-1", "product_id": "mtech_magnum_sf",
        "currency": "INR", "quantity": 10, "discount_percent": 0,
        "tax_enabled": True, "tax_mode": "exclusive", "is_gst_inclusive": False,
        "configuration": {"thickness_mm": 1.96, "length": 780, "width": 1056, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert created.status_code == 201
    assert created.json["data"]["discount_percent"] == 0
    assert created.json["data"]["pricing_preview"]["discount_percent"] == 0
    item_id = created.json["data"]["_id"]

    discounted = authenticated.patch(f"/api/v1/cart/items/{item_id}", json={"discount_percent": 2.5})
    assert discounted.status_code == 200
    assert discounted.json["data"]["discount_percent"] == 2.5
    assert discounted.json["data"]["pricing_preview"]["discount_percent"] == 2.5

    reset = authenticated.patch(f"/api/v1/cart/items/{item_id}", json={"discount_percent": 0})
    assert reset.status_code == 200
    assert reset.json["data"]["discount_percent"] == 0
    assert reset.json["data"]["pricing_preview"]["discount_percent"] == 0
    assert reset.json["data"]["pricing_preview"]["discount_amount"] == 0

    reloaded = authenticated.get("/api/v1/cart?customer_id=customer-demo-1&currency=INR")
    assert reloaded.status_code == 200
    assert reloaded.json["data"]["items"][0]["discount_percent"] == 0

    quotation = authenticated.post("/api/v1/quotations/preview", json={
        "customer_id": "customer-demo-1", "currency": "INR", "payment_terms": "Advance",
        "proforma_validity_days": 30, "transport_mode": "by_consignee",
    })
    assert quotation.status_code == 200
    assert quotation.json["data"]["lines"][0]["discount_percent"] == 0
    assert quotation.json["data"]["totals"]["discount_amount"] == 0


@pytest.mark.parametrize("discount", [0, 0.5, 1, 2.5])
def test_price_preview_discount_options_are_authoritative(authenticated, discount):
    authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"})
    response = authenticated.post("/api/v1/products/mtech_magnum_sf/price-preview", json={
        "customer_id": "customer-demo-1", "currency": "INR", "quantity": 1,
        "discount_percent": discount, "tax_enabled": True, "tax_mode": "exclusive",
        "configuration": {"thickness_mm": 1.96, "length": 780, "width": 1056, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert response.status_code == 200
    line = response.json["data"]["line"]
    assert line["requested_discount_percent"] == discount
    assert line["discount_percent"] == discount
    assert line["master_discount_amount"] == round(line["master_subtotal"] * discount / 100, 2)
    assert line["discount_amount"] == round(line["master_discount_amount"] * line["exchange_rate"], 2)


def test_quotation_preview_validates_without_saving(app, authenticated):
    authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech_active_sf", "currency": "EUR", "quantity": 1,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    preview = authenticated.post("/api/v1/quotations/preview", json={
        "company_id": COMPANY, "customer_id": "customer-demo-1", "currency": "INR",
        "payment_terms": "Advance", "proforma_validity_days": 30, "transport_mode": "by_consignee",
    })
    assert preview.status_code == 200
    assert preview.json["data"]["quotation_number"] == "PREVIEW"
    assert preview.json["data"]["currency"] == "EUR"
    assert "tax_amount" not in preview.json["data"]["totals"]
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


def test_india_inr_display_creates_eur_tax_free_quotation(app, authenticated):
    assert authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"}).status_code == 200
    app.extensions["store"].update_one("products", {"_id": "mtech_active_sf"}, {
        "pricing": {"pricing_type": "per_sqm", "price": 100, "master_currency": "EUR", "unit": "sqm"},
        "pricing_status": "configured",
    })
    preview = authenticated.post("/api/v1/products/mtech_active_sf/price-preview", json={
        "customer_id": "customer-demo-1", "display_currency": "INR", "quantity": 1, "discount_percent": 3,
        "tax_enabled": True, "tax_mode": "exclusive", "tax_rate": 18,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert preview.status_code == 200
    display_line = preview.json["data"]["line"]
    assert display_line["master_currency"] == "EUR"
    assert display_line["display_currency"] == "INR"
    assert display_line["quotation_currency"] == "EUR"
    assert display_line["master_final_total"] == display_line["master_total"]
    assert display_line["display_final_total"] == display_line["display_total"]
    assert display_line["discount_percent"] == 3
    assert display_line["master_total"] == 97.0
    assert display_line["master_subtotal"] == 100.0
    assert display_line["display_subtotal"] == 10000.0
    assert display_line["display_discount_amount"] == 300.0
    assert display_line["display_final_total"] == 9700.0
    assert display_line["display_total"] == display_line["total"]
    assert "final_total" not in display_line
    assert "tax_amount" not in display_line

    added = authenticated.post("/api/v1/cart/items", json={
        "customer_id": "customer-demo-1", "product_id": "mtech_active_sf", "display_currency": "INR",
        "quantity": 1, "discount_percent": 3,
        "configuration": {"thickness_mm": 1.96, "length": 1000, "width": 1000, "dimension_unit": "mm", "format_type": "cut_format"},
    })
    assert added.status_code == 201
    saved_item = added.json["data"]
    assert saved_item["currency"] == saved_item["master_currency"] == "EUR"
    assert saved_item["display_currency"] == "INR"
    assert saved_item["master_final_total"] == 97.0

    # Viewing a cart in another reference currency must convert the saved EUR
    # snapshot, not recalculate it from a newly changed product master price.
    app.extensions["store"].update_one("products", {"_id": "mtech_active_sf"}, {
        "pricing": {"pricing_type": "per_sqm", "price": 200, "master_currency": "EUR", "unit": "sqm"},
    })
    usd_cart = authenticated.get("/api/v1/cart?customer_id=customer-demo-1&currency=USD")
    assert usd_cart.status_code == 200
    assert usd_cart.json["data"]["item_count"] == 1
    usd_line = usd_cart.json["data"]["items"][0]["pricing_preview"]
    assert usd_line["master_final_total"] == 97.0
    assert usd_line["display_currency"] == "USD"
    assert usd_line["display_final_total"] == 116.4

    quote = authenticated.post("/api/v1/quotations", json={"customer_id": "customer-demo-1", "currency": "INR"})
    assert quote.status_code == 201
    document = quote.json["data"]
    assert document["currency"] == document["quotation_currency"] == "EUR"
    assert document["lines"][0]["discount_percent"] == 3
    assert document["lines"][0]["master_final_total"] == 97.0
    assert document["lines"][0]["line_total"] == 97.0
    assert document["totals"]["grand_total"] == 97.0
    assert "tax_amount" not in document["lines"][0]
    assert "tax_amount" not in document["totals"]


def test_magnum_price_preview_uses_seeded_eur_price_and_exact_area(authenticated):
    authenticated.post("/api/v1/companies/select-customer", json={"customer_id": "customer-demo-1"})
    response = authenticated.post("/api/v1/products/mtech_magnum_sf/price-preview", json={
        "customer_id": "customer-demo-1", "display_currency": "INR",
        "quantity": 1, "discount_percent": 3,
        "configuration": {
            "thickness_mm": 1.96, "length": 585, "width": 875,
            "dimension_unit": "mm", "format_type": "cut_format",
        },
    })
    assert response.status_code == 200
    line = response.json["data"]["line"]
    assert line["area_sqm"] == 0.511875
    assert line["master_currency"] == "EUR"
    assert line["master_unit_price"] == 27.13
    assert line["master_subtotal"] == 27.13
    assert line["master_discount_amount"] == 0.81
    assert line["master_final_total"] == 26.32
    assert line["display_currency"] == "INR"
    assert line["display_subtotal"] == 2713.0
    assert line["display_discount_amount"] == 81.0
    assert line["display_final_total"] == 2632.0
    assert "final_total" not in line
    assert not any(key in line for key in ("tax_amount", "gst_amount", "vat_amount"))


def test_historical_currency_and_tax_snapshot_is_not_rewritten(app, authenticated):
    historical = app.extensions["store"].insert_one("quotations", {
        "_id": "legacy-inr-tax", "quotation_number": "LEGACY-INR-001",
        "customer_id": COMPANY, "created_by_user_id": "user-demo-admin",
        "currency": "INR", "lines": [{"tax_amount": 180}],
        "totals": {"subtotal": 1000, "tax_amount": 180, "grand_total": 1180},
        "status": "Sent",
    })
    response = authenticated.get(f"/api/v1/quotations/{historical['_id']}")
    assert response.status_code == 200
    document = response.json["data"]
    assert document["currency"] == "INR"
    assert document["totals"]["tax_amount"] == 180
    assert document["totals"]["grand_total"] == 1180
