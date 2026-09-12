from __future__ import annotations

import base64
from datetime import timedelta
from io import BytesIO
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

import pytest
from pypdf import PdfReader
from werkzeug.security import generate_password_hash

from app.communication.email import EmailDeliveryError
from app.customers.codes import customer_code
from app.finance.service import IncentiveConfigurationError, create_incentive_for_order
from app.middleware.access import repair_customer_assignments
from app.orders.routes import _next_oc_number
from app.repositories.store import build_store, utcnow


COMPANY = "company-moneda-demo"


def add_test_user(app, user_id: str, role_id: str = "user"):
    return app.extensions["store"].insert_one("users", {
        "_id": user_id, "name": user_id, "email": f"{user_id}@monedatechnologies.com",
        "username": user_id, "username_normalized": user_id, "password_hash": generate_password_hash("Secure123"),
        "role_id": role_id, "active": True, "email_verified": True,
        "customer_ids": [], "customer_company_ids": [], "company_ids": [],
    })


def configure_product(app, product_id="mtech-mpack", base_price=10):
    product = app.extensions["store"].find_one("products", {"_id": product_id}) or {}
    configuration = dict(product.get("configuration", {}))
    # Legacy pricing tests intentionally replace the official machine matrix
    # with a synthetic formula product.
    configuration.pop("machine_sizes", None)
    configuration.pop("machine_price_list", None)
    app.extensions["store"].update_one("products", {"_id": product_id}, {
        "pricing": {"pricing_type": "formula", "price": base_price, "master_currency": "EUR", "unit": "sqm"},
        "pricing_status": "configured",
        "configuration": configuration,
    })


def test_authentication_required(client):
    response = client.get("/api/v1/products")
    assert response.status_code == 401
    assert response.json["success"] is False


def test_country_catalogue_endpoint_is_complete(authenticated):
    response = authenticated.get("/api/v1/countries")
    assert response.status_code == 200
    payload = response.json["data"]
    assert payload["total"] == 250
    by_code = {row["code"]: row for row in payload["countries"]}
    assert by_code["IN"]["phone_country_code"] == "+91"
    assert by_code["GB"]["region"] == "Europe"
    assert by_code["NG"]["region"] == "Africa"


def test_customer_creation_accepts_unknown_contacts_and_normalizes_country(authenticated):
    response = authenticated.post("/api/v1/customers", json={
        "company_name": "No Contact Details Ltd", "contact_name": "Purchasing", "email": "-", "phone": "-",
        "country_code": "IN", "payment_terms": "Advance", "address": "Mumbai",
    })
    assert response.status_code == 201
    customer = response.json["data"]
    assert customer["email"] == "-"
    assert customer["phone"] == "-"
    assert customer["country_name"] == "India"
    assert customer["continent"] == "Asia"
    assert customer["preferred_currency"] == "INR"


def test_customer_creation_rejects_unknown_country_code(authenticated):
    response = authenticated.post("/api/v1/customers", json={
        "company_name": "Invalid Country Ltd", "contact_name": "Purchasing",
        "email": "buyer@example.com", "phone": "+49 30 123456", "country_code": "ZZ",
        "payment_terms": "Advance", "address": "Berlin", "preferred_currency": "EUR",
    })
    assert response.status_code == 422
    assert response.json["error"] == "COUNTRY_INVALID"


def test_customer_creation_rejects_country_region_mismatch(authenticated):
    response = authenticated.post("/api/v1/customers", json={
        "company_name": "Mismatched Region Ltd", "contact_name": "Purchasing",
        "email": "buyer@example.com", "phone": "+49 30 123456", "country_code": "IN",
        "continent": "Europe", "payment_terms": "Advance", "address": "Berlin", "preferred_currency": "EUR",
    })
    assert response.status_code == 422
    assert response.json["error"] == "COUNTRY_CONTINENT_MISMATCH"


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


def test_workspace_watermark_defaults_on_and_is_superadmin_configurable(authenticated):
    me = authenticated.get("/api/v1/me")
    assert me.status_code == 200
    assert me.json["data"]["watermark_enabled"] is True

    disabled = authenticated.patch("/api/v1/settings", json={"watermark_enabled": False})
    assert disabled.status_code == 200
    assert disabled.json["data"]["watermark_enabled"] is False
    assert authenticated.get("/api/v1/me").json["data"]["watermark_enabled"] is False

    invalid = authenticated.patch("/api/v1/settings", json={"watermark_enabled": "false"})
    assert invalid.status_code == 422

    enabled = authenticated.patch("/api/v1/settings", json={"watermark_enabled": True})
    assert enabled.status_code == 200


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
        "name": "OAuth Diagnostic", "username": "oauth.diagnostic", "email": "oauth-diagnostic@monedatechnologies.com",
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


@pytest.mark.parametrize("email", [
    "person@gmail.com", "person@yahoo.com", "person@sub.chemo.in",
    "person@monedatechnologies.com.fake.com", "person@fake-moneda.com",
])
def test_signup_rejects_non_moneda_email_domains(client, email):
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "Disallowed Domain", "username": "disallowed.domain", "email": email,
    })
    assert response.status_code == 422
    assert response.json["error"] == "signup_email_domain_not_allowed"


@pytest.mark.parametrize("email", ["USER@MONEDATECHNOLOGIES.COM", "User@MonedaTechnologies.com", "user@CHEMO.IN"])
def test_signup_accepts_allowed_email_domains(app, client, email):
    class RecordingOtp:
        def request(self, *_args, **_kwargs):
            return True

    app.extensions["otp_service"] = RecordingOtp()
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "Allowed Domain", "username": f"allowed.{email.split('@')[0].lower()}", "email": email,
    })
    assert response.status_code == 200


def test_signup_rejects_taken_username_before_otp(client, app):
    before = app.extensions["store"].count("otp_challenges")
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "Duplicate Username", "username": "Admin", "email": "new.admin@monedatechnologies.com",
    })
    assert response.status_code == 409
    assert response.json["error"] == "username_in_use"
    assert "already taken" in response.json["message"].lower()
    assert app.extensions["store"].count("otp_challenges") == before


def test_signup_completion_establishes_persistent_authenticated_session(app, client, monkeypatch):
    monkeypatch.setattr("app.auth.service.secrets.randbelow", lambda _limit: 123456)
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "New Moneda User", "username": "new.moneda.user", "email": "new.user@monedatechnologies.com",
    })
    assert response.status_code == 200
    pending_id = response.json["data"]["pending_signup_id"]
    assert client.post("/api/v1/auth/signup/verify-email", json={"pending_signup_id": pending_id, "otp": "123456"}).status_code == 200

    complete = client.post("/api/v1/auth/signup/complete", json={
        "pending_signup_id": pending_id, "password": "SecurePassword123!", "confirm_password": "SecurePassword123!",
    })
    assert complete.status_code == 201
    assert complete.json["data"]["authenticated"] is True
    assert complete.json["data"]["next_step"] == "customer-selection"
    assert client.get("/api/v1/me").status_code == 200

    cookie = "\n".join(complete.headers.getlist("Set-Cookie"))
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie and "Expires=" in cookie
    expires = parsedate_to_datetime(cookie.split("Expires=", 1)[1].split(";", 1)[0])
    assert 29 <= (expires - utcnow()).total_seconds() / 86400 <= 31

    retry = client.post("/api/v1/auth/signup/complete", json={
        "pending_signup_id": pending_id, "password": "SecurePassword123!", "confirm_password": "SecurePassword123!",
    })
    assert retry.status_code == 410


def test_signup_rejects_password_without_required_character_classes(app, client, monkeypatch):
    monkeypatch.setattr("app.auth.service.secrets.randbelow", lambda _limit: 123456)
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "Weak Password", "username": "weak.password.user", "email": "weak.password@monedatechnologies.com",
    })
    pending_id = response.json["data"]["pending_signup_id"]
    assert client.post("/api/v1/auth/signup/verify-email", json={"pending_signup_id": pending_id, "otp": "123456"}).status_code == 200

    complete = client.post("/api/v1/auth/signup/complete", json={
        "pending_signup_id": pending_id, "password": "abcdefgh", "confirm_password": "abcdefgh",
    })
    assert complete.status_code == 422
    assert complete.json["error"] == "password_policy"
    assert complete.json["error_code"] == "PASSWORD_POLICY"
    assert complete.json["stage"] == "password_validation"
    assert "uppercase" in complete.json["message"].lower()
    assert client.get("/api/v1/me").status_code == 401


@pytest.mark.parametrize("password_payload", [
    {"password": "Secure123", "confirm_password": "Different123"},
    {"confirm_password": "Secure123"},
    {"password": "Secure123"},
])
def test_signup_completion_rejects_mismatch_or_missing_password_fields(app, client, monkeypatch, password_payload):
    monkeypatch.setattr("app.auth.service.secrets.randbelow", lambda _limit: 123456)
    response = client.post("/api/v1/auth/signup/start", json={
        "name": "Completion Validation", "username": f"completion.{len(password_payload)}", "email": "completion.validation@monedatechnologies.com",
    })
    pending_id = response.json["data"]["pending_signup_id"]
    assert client.post("/api/v1/auth/signup/verify-email", json={"pending_signup_id": pending_id, "otp": "123456"}).status_code == 200
    payload = {"pending_signup_id": pending_id, **password_payload}
    complete = client.post("/api/v1/auth/signup/complete", json=payload)
    assert complete.status_code == 422
    assert client.get("/api/v1/me").status_code == 401


def test_customer_creation_assigns_creator_and_me_uses_canonical_access(app, client):
    add_test_user(app, "customer-creator")
    with client.session_transaction() as session:
        session["user_id"] = "customer-creator"
        session["role_id"] = "user"
        session.permanent = True
    response = client.post("/api/v1/customers", json={
        "name": "Assigned Customer", "contact_name": "Contact", "email": "contact@example.com",
        "phone": "+91 9876543210", "address": "Road", "continent": "Asia", "country_code": "IN",
        "preferred_currency": "INR", "payment_terms": "Advance",
    })
    assert response.status_code == 201
    customer_id = response.json["data"]["_id"]
    assert response.json["data"]["assigned_user_ids"] == ["customer-creator"]
    me = client.get("/api/v1/me")
    assert me.status_code == 200
    assert [row["_id"] for row in me.json["data"]["customers"]] == [customer_id]
    assert me.json["data"]["user"]["customer_access_count"] == 1


def test_admin_customer_assignment_is_server_enforced_and_idempotent(app, client):
    add_test_user(app, "assigned-user")
    store = app.extensions["store"]
    store.insert_one("customers", {
        "_id": "assignment-customer", "name": "Assignment Customer", "company_name": "Assignment Customer",
        "contact_name": "Contact", "email": "assignment@example.com", "phone": "+91 9876543210",
        "address": "Road", "continent": "Asia", "country_code": "IN", "country_name": "India",
        "preferred_currency": "INR", "default_currency": "INR", "status": "active", "active": True,
        "assigned_user_ids": [],
    })
    assert client.post("/api/v1/auth/demo", json={}).status_code == 200
    first = client.patch("/api/v1/admin/users/assigned-user", json={"customer_ids": ["assignment-customer", "assignment-customer"]})
    assert first.status_code == 200
    assert store.find_one("customers", {"_id": "assignment-customer"})["assigned_user_ids"] == ["assigned-user"]
    second = client.patch("/api/v1/admin/users/assigned-user", json={"customer_ids": []})
    assert second.status_code == 200
    assert store.find_one("customers", {"_id": "assignment-customer"})["assigned_user_ids"] == []
    with client.session_transaction() as session:
        session["user_id"] = "assigned-user"
        session["role_id"] = "user"
    denied = client.patch("/api/v1/admin/users/user-demo-admin", json={"customer_ids": ["assignment-customer"]})
    assert denied.status_code == 403


def test_customer_access_migration_promotes_explicit_legacy_user_relationship(app, client):
    add_test_user(app, "legacy-assigned-user")
    store = app.extensions["store"]
    store.insert_one("customers", {
        "_id": "legacy-assigned-customer", "name": "Legacy Assigned Customer", "company_name": "Legacy Assigned Customer",
        "contact_name": "Contact", "email": "legacy@example.com", "phone": "+91 9876543210", "address": "Road",
        "country_code": "IN", "preferred_currency": "INR", "default_currency": "INR", "status": "active", "active": True,
        "assigned_user_ids": [],
    })
    store.update_one("users", {"_id": "legacy-assigned-user"}, {"customer_ids": ["legacy-assigned-customer"]})
    assert repair_customer_assignments(store) == 1
    with client.session_transaction() as session:
        session["user_id"] = "legacy-assigned-user"
    response = client.get("/api/v1/customers")
    assert response.status_code == 200
    assert [row["_id"] for row in response.json["data"]["items"]] == ["legacy-assigned-customer"]


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
    assert products.json["data"]["pagination"]["total"] == 37
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


def test_mpack_machine_price_list_is_structured_and_server_authoritative(app, authenticated):
    product = app.extensions["store"].find_one("products", {"_id": "mtech-mpack"})
    assert product["pricing_status"] == "configured"
    assert product["pricing"] == {
        "master_currency": "EUR", "pricing_type": "per_pack", "unit": "box",
        "price": None, "dimension_prices": {},
    }
    assert product["configuration"]["machine_price_list"]["valid_from"] == "2026-07-01"
    assert product["configuration"]["machine_price_list"]["valid_until"] == "2026-12-31"
    assert len(product["configuration"]["machine_sizes"]) == 63
    underpacking = authenticated.get("/api/v1/catalog/mpacks/products").json["data"]["items"]
    assert [row["_id"] for row in underpacking] == ["mtech-mpack"]

    configuration = {
        "manufacturer": "Heidelberg", "machine_model": "SPEEDMASTER 74 - CD",
        "width_mm": 760, "length_mm": 620, "thickness_mm": 0.1,
        # Deliberately malicious/stale browser values must be overwritten.
        "price_per_box_eur": 0.01, "total_eur": 0.01,
    }
    response = authenticated.post("/api/v1/products/mtech-mpack/price-preview", json={
        "company_id": COMPANY, "quantity": 2, "discount_percent": 0,
        "display_currency": "EUR", "configuration": configuration,
    })
    assert response.status_code == 200
    line = response.json["data"]["line"]
    assert line["pricing_unit"] == "box"
    assert line["price_per_sheet_eur"] == 0.221
    assert line["price_per_box_eur"] == 22.06
    assert line["discounted_price_per_sheet_eur"] == 0.221
    assert line["discounted_price_per_box_eur"] == 22.06
    assert line["sheets_per_box"] == 100
    assert line["master_subtotal"] == 44.12
    assert line["master_final_total"] == 44.12
    assert line["configuration"]["price_per_box_eur"] == 22.06
    assert line["configuration"]["total_eur"] == 44.12

    added = authenticated.post("/api/v1/cart/items", json={
        "company_id": COMPANY, "product_id": "mtech-mpack", "quantity": 2,
        "discount_percent": 0, "display_currency": "EUR", "configuration": configuration,
    })
    assert added.status_code == 201
    assert added.json["data"]["pricing_preview"]["description"] == "Calibrated underpacking material."
    assert added.json["data"]["configuration"]["price_per_box_eur"] == 22.06
    assert added.json["data"]["configuration"]["total_eur"] == 44.12


def test_mpack_rejects_machine_size_not_in_official_matrix(authenticated):
    response = authenticated.post("/api/v1/products/mtech-mpack/price-preview", json={
        "company_id": COMPANY, "quantity": 1, "display_currency": "EUR",
        "configuration": {
            "manufacturer": "Heidelberg", "machine_model": "SPEEDMASTER 74 - CD",
            "width_mm": 999, "length_mm": 620, "thickness_mm": 0.1,
        },
    })
    assert response.status_code == 422
    assert response.json["message"] == "Size is not available for the selected machine model"


def test_blanket_machine_catalog_stores_structured_manufacturer_and_model(authenticated):
    created = authenticated.post("/api/v1/machines", json={
        "manufacturer": "Heidelberg", "machine_model": "Speedmaster 74",
    })
    assert created.status_code == 201
    assert created.json["data"]["manufacturer"] == "Heidelberg"
    assert created.json["data"]["machine_model"] == "Speedmaster 74"
    duplicate = authenticated.post("/api/v1/machines", json={
        "manufacturer": "Heidelberg", "machine_model": "Speedmaster 74",
    })
    assert duplicate.status_code == 200
    assert duplicate.json["data"]["_id"] == created.json["data"]["_id"]
    machines = authenticated.get("/api/v1/machines")
    assert any(row["machine_model"] == "Speedmaster 74" for row in machines.json["data"]["items"])


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
                    "format_type": "bar_format", "machine": "Heidelberg - GTO 46", "bar_1_id": "aluminium", "bar_2_id": "steel",
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
                    "dimension_unit": "mm", "format_type": "bar_format", "machine": "Heidelberg - GTO 46",
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


def test_order_confirmation_pdf_is_persisted_and_attached(app, authenticated):
    store = app.extensions["store"]
    quote = store.insert_one("quotations", {
        "_id": "oc-document-quote", "quotation_number": "MT-DOC-001", "status": "Sent",
        "customer_id": COMPANY, "currency": "EUR", "payment_terms": "Advance",
        "customer_snapshot": {"name": "Document Customer", "customer_code": "DOC-001", "email": "customer@example.com", "address": "1 Test Street"},
        "lines": [{"product_name": "Test Product", "description": "Document line", "quantity": 2, "unit_price": 25, "line_total": 50}],
        "totals": {"subtotal": 50, "grand_total": 50}, "created_by_user_id": "user-demo-admin",
    })
    response = authenticated.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={})
    assert response.status_code == 201
    order = response.json["data"]
    document = store.find_one("order_documents", {"order_id": order["_id"]})
    assert document and document["document_type"] == "order_confirmation"
    pdf_bytes = base64.b64decode(document["content_base64"])
    text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_bytes)).pages)
    assert order["order_number"] in text
    assert "Document Customer" in text
    assert "Test Product" in text
    download = authenticated.get(f"/api/v1/orders/{order['_id']}/pdf")
    assert download.status_code == 200 and download.headers["Content-Type"].startswith("application/pdf")
    message = app.extensions["email_provider"].messages[-1]
    assert message["attachments"] and message["attachments"][0]["filename"] == document["filename"]
    attached = base64.b64decode(message["attachments"][0]["content"])
    assert attached == pdf_bytes


def test_superadmin_can_confirm_incentive_payment_and_activate(app, authenticated):
    store = app.extensions["store"]
    order = store.insert_one("orders", {
        "_id": "incentive-confirm-order", "order_number": "MT-OC-CONFIRM-001",
        "customer_id": COMPANY, "order_amount": 100, "totals": {"grand_total": 100},
    })
    incentive = store.insert_one("incentives", {
        "_id": "incentive-confirm-test", "order_id": order["_id"], "customer_id": COMPANY,
        "salesperson_id": "user-demo-admin", "order_amount": 100,
        "incentive_percentage_snapshot": 2, "gross_incentive_amount": 2,
        "net_payable_incentive": 2, "status": "PENDING PAYMENT",
    })
    payment = store.insert_one("payments", {
        "_id": "incentive-confirm-payment", "order_id": order["_id"], "customer_id": COMPANY,
        "amount": 100, "payment_date": utcnow(), "status": "CONFIRMED", "confirmed_at": utcnow(), "utr": "UTR-1",
    })
    response = authenticated.post(f"/api/v1/incentives/{incentive['_id']}/confirm-payment", json={"payment_id": payment["_id"]})
    assert response.status_code == 200
    assert response.json["data"]["status"] == "ACTIVE"
    assert response.json["data"]["payment_confirmed_by_user_id"] == "user-demo-admin"


def test_order_email_failure_keeps_oc_and_resend_reuses_pdf(app, authenticated, monkeypatch):
    store = app.extensions["store"]
    quote = store.insert_one("quotations", {
        "_id": "oc-resend-quote", "quotation_number": "MT-RESEND-001", "status": "Sent",
        "customer_id": COMPANY, "currency": "EUR", "payment_terms": "Advance",
        "customer_snapshot": {"name": "Retry Customer", "email": "customer@example.com"},
        "lines": [], "totals": {"grand_total": 20}, "created_by_user_id": "user-demo-admin",
    })
    service = app.extensions["email_service"]
    original = service.send_order_confirmation

    def fail_once(**kwargs):
        raise EmailDeliveryError("temporary failure", stage="message_submission", diagnostic_id="email-retry", error_code="MESSAGE_SUBMISSION_FAILED")

    monkeypatch.setattr(service, "send_order_confirmation", fail_once)
    converted = authenticated.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={})
    assert converted.status_code == 201
    order = converted.json["data"]
    persisted = store.find_one("orders", {"_id": order["_id"]})
    assert persisted and persisted["email_status"] == "Failed"
    assert store.find_one("order_documents", {"order_id": order["_id"]})
    monkeypatch.setattr(service, "send_order_confirmation", original)
    resent = authenticated.post(f"/api/v1/orders/{order['_id']}/resend-confirmation", json={})
    assert resent.status_code == 200
    assert store.count("orders", {"quotation_id": quote["_id"]}) == 1
    assert store.count("incentives", {"order_id": order["_id"]}) == 1


def _login_as_user(client, user_id: str):
    with client.session_transaction() as session:
        session["user_id"] = user_id


def test_user_can_convert_own_authorized_quotation(app, client):
    user = add_test_user(app, "quotation-owner")
    app.extensions["store"].update_one("users", {"_id": user["_id"]}, {"device_access_mode": "any_authorized_device"})
    app.extensions["store"].update_one("customers", {"_id": "customer-demo-1"}, {"assigned_user_ids": [user["_id"]]})
    quote = app.extensions["store"].insert_one("quotations", {
        "_id": "owner-convert-quote", "quotation_number": "MT-OWNER-001", "status": "Draft",
        "customer_id": "customer-demo-1", "currency": "EUR", "lines": [],
        "totals": {"grand_total": 100}, "created_by_user_id": user["_id"], "payment_terms": "Advance",
    })
    _login_as_user(client, user["_id"])
    configuration = client.get(f"/api/v1/quotations/{quote['_id']}/order-configuration")
    assert configuration.status_code == 200
    converted = client.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={})
    assert converted.status_code == 201
    assert converted.json["data"]["quotation_id"] == quote["_id"]
    assert app.extensions["store"].count("orders", {"quotation_id": quote["_id"]}) == 1


def test_user_cannot_convert_another_users_quotation_outside_scope(app, client):
    owner = add_test_user(app, "quotation-owner-2")
    actor = add_test_user(app, "quotation-actor-2")
    app.extensions["store"].update_one("users", {"_id": owner["_id"]}, {"device_access_mode": "any_authorized_device"})
    app.extensions["store"].update_one("users", {"_id": actor["_id"]}, {"device_access_mode": "any_authorized_device"})
    app.extensions["store"].update_one("customers", {"_id": "customer-demo-1"}, {"assigned_user_ids": [owner["_id"]]})
    quote = app.extensions["store"].insert_one("quotations", {
        "_id": "foreign-convert-quote", "quotation_number": "MT-FOREIGN-001", "status": "Draft",
        "customer_id": "customer-demo-1", "currency": "EUR", "lines": [],
        "totals": {"grand_total": 100}, "created_by_user_id": owner["_id"], "payment_terms": "Advance",
    })
    _login_as_user(client, actor["_id"])
    denied = client.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={})
    assert denied.status_code == 403
    assert app.extensions["store"].count("orders", {"quotation_id": quote["_id"]}) == 0


def test_order_confirmation_configuration_is_server_authoritative(app, authenticated):
    store = app.extensions["store"]
    customer = store.find_one("customers", {"_id": "customer-demo-1"}) or {}
    quote = store.insert_one("quotations", {
        "_id": "oc-config-quote", "quotation_number": "MT-OC-TEST-001", "status": "Sent",
        "customer_id": "customer-demo-1", "currency": "EUR", "lines": [],
        "totals": {"grand_total": 458.70}, "payment_terms": "Advance",
        "customer_snapshot": {"company_name": customer.get("name", "Customer"), "email": "customer@example.com"},
        "created_by_user_id": "user-demo-admin",
    })
    store.insert_one("email_logs", {
        "_id": "oc-config-email", "quotation_id": quote["_id"], "purpose": "quotation",
        "recipient": "customer@example.com", "cc": ["sales@example.com"], "bcc": ["accounts@example.com"],
        "created_at": utcnow(),
    })
    response = authenticated.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={
        "oc_number": "CLIENT-CANNOT-OVERRIDE", "order_amount": 1, "payment_terms": "60 Days",
        "additional_recipients": ["extra@example.com"],
    })
    assert response.status_code == 201
    order = response.json["data"]
    assert order["order_number"].startswith("MT-OC-") and order["order_number"].endswith("-001")
    assert order["order_amount"] == 458.70
    assert order["payment_terms"] == "60 Days"
    assert order["to"] == ["customer@example.com", "extra@example.com"]
    assert order["cc"] == ["sales@example.com"]
    assert order["bcc"] == ["accounts@example.com"]


def test_order_confirmation_rejects_more_than_five_additional_recipients(authenticated, app):
    quote = app.extensions["store"].insert_one("quotations", {
        "_id": "oc-recipient-limit", "quotation_number": "MT-OC-TEST-002", "status": "Sent",
        "customer_id": "customer-demo-1", "currency": "EUR", "lines": [],
        "totals": {"grand_total": 10}, "payment_terms": "Advance",
        "customer_snapshot": {"email": "customer@example.com"}, "created_by_user_id": "user-demo-admin",
    })
    response = authenticated.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={
        "additional_recipients": [f"extra{index}@example.com" for index in range(6)],
    })
    assert response.status_code == 422


def test_order_confirmation_rejects_invalid_additional_recipient(authenticated, app):
    quote = app.extensions["store"].insert_one("quotations", {
        "_id": "oc-recipient-invalid", "quotation_number": "MT-OC-TEST-003", "status": "Sent",
        "customer_id": "customer-demo-1", "currency": "EUR", "lines": [],
        "totals": {"grand_total": 10}, "payment_terms": "Advance",
        "customer_snapshot": {"email": "customer@example.com"}, "created_by_user_id": "user-demo-admin",
    })
    response = authenticated.post(f"/api/v1/quotations/{quote['_id']}/convert-to-order", json={
        "additional_recipients": ["not-an-email"],
    })
    assert response.status_code == 422


def test_customer_oc_sequence_is_unique_under_concurrency(app):
    store = app.extensions["store"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        numbers = list(executor.map(lambda _: _next_oc_number(store, "customer-demo-1"), range(8)))
    assert len(numbers) == len(set(numbers)) == 8


def test_category_incentive_rates_are_snapshotted_per_order_line(app):
    store = app.extensions["store"]
    salesperson = add_test_user(app, "category-salesperson")
    salesperson["incentive_rates"] = {"blankets": 5, "mpacks": 3, "chemicals": 6}
    store.update_one("users", {"_id": salesperson["_id"]}, {"incentive_rates": salesperson["incentive_rates"]})
    for product_id, category_id in (("category-blanket", "blankets"), ("category-mpack", "mpacks"), ("category-chemical", "chemicals")):
        store.insert_one("products", {"_id": product_id, "name": product_id, "category_id": category_id})
    order = {
        "_id": "category-snapshot-order", "order_number": "MT-OC-CATEGORY-001", "customer_id": COMPANY,
        "order_amount": 17_000, "products_snapshot": [
            {"product_id": "category-blanket", "line_total": 10_000},
            {"product_id": "category-mpack", "line_total": 5_000},
            {"product_id": "category-chemical", "line_total": 2_000},
        ],
    }
    incentive = create_incentive_for_order(store, order, salesperson)
    assert incentive["status"] == "PENDING PAYMENT"
    assert incentive["gross_incentive_amount"] == 770.0
    assert [(line["category_id"], line["incentive_rate_snapshot"], line["incentive_amount"]) for line in incentive["incentive_lines"]] == [
        ("blankets", 5.0, 500.0), ("mpacks", 3.0, 150.0), ("chemicals", 6.0, 120.0),
    ]
    store.update_one("users", {"_id": salesperson["_id"]}, {"incentive_rates": {"blankets": 6, "mpacks": 6, "chemicals": 6}})
    persisted = store.find_one("incentives", {"_id": incentive["_id"]})
    assert [line["incentive_rate_snapshot"] for line in persisted["incentive_lines"]] == [5.0, 3.0, 6.0]


def test_missing_category_rate_blocks_incentive_creation(app):
    store = app.extensions["store"]
    salesperson = add_test_user(app, "missing-category-rate")
    salesperson["incentive_rates"] = {"blankets": 5}
    store.insert_one("products", {"_id": "missing-chemical", "name": "Missing Chemical", "category_id": "chemicals"})
    with pytest.raises(IncentiveConfigurationError, match="Chemical"):
        create_incentive_for_order(store, {
            "_id": "missing-rate-order", "order_number": "MT-OC-MISSING-001", "customer_id": COMPANY,
            "order_amount": 100, "products_snapshot": [{"product_id": "missing-chemical", "line_total": 100}],
        }, salesperson)


def test_payment_must_be_submitted_before_superadmin_confirmation(app, authenticated):
    store = app.extensions["store"]
    order = store.insert_one("orders", {"_id": "payment-lifecycle-order", "order_number": "MT-OC-PAYMENT-001", "customer_id": COMPANY, "order_amount": 100, "totals": {"grand_total": 100}})
    incentive = store.insert_one("incentives", {"_id": "payment-lifecycle-incentive", "order_id": order["_id"], "customer_id": COMPANY, "status": "PENDING PAYMENT", "gross_incentive_amount": 2, "credit_note_deduction": 0, "paid_amount": 0, "net_payable_incentive": 2})
    payment = store.insert_one("payments", {"_id": "payment-lifecycle-payment", "order_id": order["_id"], "customer_id": COMPANY, "amount": 100, "payment_date": utcnow(), "status": "PAYMENT RECORDED"})
    not_ready = authenticated.post(f"/api/v1/payments/{payment['_id']}/confirm", json={})
    assert not_ready.status_code == 409
    assert store.find_one("payments", {"_id": payment["_id"]})["status"] == "PAYMENT RECORDED"
    submitted = authenticated.post(f"/api/v1/payments/{payment['_id']}/submit", json={})
    assert submitted.status_code == 200
    confirmed = authenticated.post(f"/api/v1/payments/{payment['_id']}/confirm", json={})
    assert confirmed.status_code == 200
    persisted_payment = store.find_one("payments", {"_id": payment["_id"]})
    persisted_incentive = store.find_one("incentives", {"_id": incentive["_id"]})
    assert persisted_payment["status"] == "CONFIRMED"
    assert persisted_incentive["status"] == "ACTIVE"
    assert persisted_incentive["incentive_due_date"] == persisted_incentive["payment_confirmation_date"] + timedelta(days=30)
    assert store.count("incentives", {"order_id": order["_id"]}) == 1


def test_non_superadmin_cannot_confirm_payment(app, client):
    actor = add_test_user(app, "payment-review-user", role_id="user")
    store = app.extensions["store"]
    payment = store.insert_one("payments", {"_id": "unauthorized-confirm-payment", "order_id": "missing-order", "customer_id": COMPANY, "amount": 10, "payment_date": utcnow(), "status": "AWAITING SUPERADMIN CONFIRMATION"})
    _login_as_user(client, actor["_id"])
    response = client.post(f"/api/v1/payments/{payment['_id']}/confirm", json={})
    assert response.status_code == 403
    assert store.find_one("payments", {"_id": payment["_id"]})["status"] == "AWAITING SUPERADMIN CONFIRMATION"


def test_category_incentive_configuration_is_superadmin_only(app, client):
    store = app.extensions["store"]
    target = add_test_user(app, "category-config-target", role_id="user")
    admin = add_test_user(app, "category-config-admin", role_id="admin")
    _login_as_user(client, admin["_id"])
    listed = client.get("/api/v1/admin/users")
    assert listed.status_code == 200
    listed_target = next(row for row in listed.json["data"]["items"] if row["_id"] == target["_id"])
    assert "incentive_rates" not in listed_target
    denied = client.patch(f"/api/v1/admin/users/{target['_id']}", json={"incentive_rates": {"blankets": 5}})
    assert denied.status_code == 403

    superadmin = store.find_one("users", {"_id": "user-demo-admin"})
    _login_as_user(client, superadmin["_id"])
    saved = client.patch(f"/api/v1/admin/users/{target['_id']}", json={"incentive_rates": {"blankets": 5, "mpacks": 3, "chemicals": 6}})
    assert saved.status_code == 200
    persisted = store.find_one("users", {"_id": target["_id"]})
    assert persisted["incentive_rates"] == {"blankets": 5.0, "mpacks": 3.0, "chemicals": 6.0}
    invalid = client.patch(f"/api/v1/admin/users/{target['_id']}", json={"incentive_rates": {"blankets": 6.5}})
    assert invalid.status_code == 422
    superadmin_target = client.patch(f"/api/v1/admin/users/{superadmin['_id']}", json={"incentive_rates": {"blankets": 5}})
    assert superadmin_target.status_code == 422


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
