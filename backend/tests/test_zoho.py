from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse
import logging

import pytest

from app.communication.email import EmailDeliveryError, EmailService, RecordingEmailProvider
from app.communication.zoho import (
    WORKDRIVE_SCOPES, ZOHO_SCOPES, ZohoIntegrationError, ZohoMailApiProvider, ZohoMailOAuth,
)
from app import _OAuthAccessLogFilter
from app.repositories.store import MemoryStore


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


def zoho_config(**overrides):
    config = {
        "SECRET_KEY": "unit-test-secret",
        "INTEGRATION_ENCRYPTION_KEY": "unit-test-integration-key",
        "ZOHO_CLIENT_ID": "client-id",
        "ZOHO_CLIENT_SECRET": "client-secret",
        "ZOHO_REFRESH_TOKEN": "",
        "ZOHO_ACCOUNT_ID": "",
        "ZOHO_ACCOUNTS_BASE_URL": "https://accounts.zoho.in",
        "ZOHO_OAUTH_REDIRECT_URI": "http://localhost:5005/api/v1/integrations/zoho/callback",
        "ZOHO_MAIL_API_BASE_URL": "",
        "ZOHO_FROM_ADDRESS": "business@monedatechnologies.com",
    }
    config.update(overrides)
    return config


def connected_store(oauth: ZohoMailOAuth) -> MemoryStore:
    store = oauth.store
    store.insert_one("integrations", {
        "_id": "zoho_mail", "provider": "zoho_mail_api", "status": "connected",
        "refresh_token_encrypted": oauth._cipher().encrypt("refresh-token"),
        "account_id": "123", "account_email": "business@monedatechnologies.com",
        "from_address": "business@monedatechnologies.com", "api_domain": "https://mail.zoho.in",
        "allowed_from_addresses": [
            "business@monedatechnologies.com", "otp@monedatechnologies.com",
            "quotations@monedatechnologies.com", "orders@monedatechnologies.com",
        ],
        "scopes": list(ZOHO_SCOPES),
    })
    return store


def test_oauth_authorization_url_is_server_bound_and_least_privilege():
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    parsed = urlparse(oauth.authorization_url("csrf-state"))
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.zoho.in"
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["redirect_uri"] == ["http://localhost:5005/api/v1/integrations/zoho/callback"]
    assert set(query["scope"][0].split(",")) == set(ZOHO_SCOPES)
    assert "ZohoMail.messages.ALL" not in query["scope"][0]
    assert query["state"] == ["csrf-state"]


def _configure_route_oauth(app):
    oauth = app.extensions["zoho_oauth"]
    oauth.client_id = "route-client-id"
    oauth.client_secret = "route-client-secret"
    oauth.redirect_uri = "http://localhost:5005/api/v1/integrations/zoho/callback"
    app.config["ZOHO_WORKDRIVE_OAUTH_REDIRECT_URI"] = "http://localhost:5005/api/v1/integrations"
    app.config["ZOHO_WORKDRIVE_CLIENT_ID"] = "workdrive-client-id"
    app.config["ZOHO_WORKDRIVE_CLIENT_SECRET"] = "workdrive-client-secret"
    return oauth


def test_workdrive_connect_uses_dedicated_callback_scopes_and_offline_consent(app, client):
    oauth = _configure_route_oauth(app)
    assert client.post("/api/auth/demo", json={}).status_code == 200

    response = client.get("/api/v1/integrations/workdrive/connect")
    assert response.status_code == 302
    parsed = urlparse(response.headers["Location"])
    query = parse_qs(parsed.query)
    assert parsed.geturl().startswith("https://accounts.zoho.in/oauth/v2/auth?")
    assert query["redirect_uri"] == ["http://localhost:5005/api/v1/integrations"]
    assert query["redirect_uri"] != [oauth.redirect_uri]
    assert query["client_id"] == ["workdrive-client-id"]
    assert set(query["scope"][0].split(",")) == set(WORKDRIVE_SCOPES)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    rows, total = app.extensions["store"].list("oauth_states", {"provider": "zoho_workdrive"})
    assert total == 1
    assert rows[0]["provider"] == "zoho_workdrive"


def test_mail_authorization_and_callback_remain_mail_specific(app, client):
    oauth = _configure_route_oauth(app)
    assert client.post("/api/auth/demo", json={}).status_code == 200
    response = client.get("/api/v1/integrations/zoho/connect")
    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["Location"]).query)
    assert query["redirect_uri"] == ["http://localhost:5005/api/v1/integrations/zoho/callback"]
    assert set(query["scope"][0].split(",")) == set(ZOHO_SCOPES)


def test_workdrive_callback_exchanges_without_mail_lookup_and_encrypts_token(app, client, monkeypatch):
    oauth = _configure_route_oauth(app)
    mail_record = {
        "_id": "zoho_mail", "provider": "zoho_mail_api", "status": "connected",
        "refresh_token_encrypted": "existing-mail-ciphertext", "account_id": "mail-account",
    }
    app.extensions["store"].insert_one("integrations", mail_record)
    mail_before = app.extensions["store"].find_one("integrations", {"_id": "zoho_mail"})
    assert client.post("/api/auth/demo", json={}).status_code == 200
    start = client.get("/api/v1/integrations/workdrive/connect")
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
    token_calls = []

    def fake_post(url, **kwargs):
        token_calls.append((url, kwargs.get("data") or {}))
        return FakeResponse({"access_token": "workdrive-access", "refresh_token": "workdrive-refresh", "expires_in": 3600})

    monkeypatch.setattr("app.communication.zoho.requests.post", fake_post)
    monkeypatch.setattr(oauth, "lookup_account", lambda *args, **kwargs: pytest.fail("Mail account lookup must not run"))
    response = client.get(
        "/api/v1/integrations",
        query_string={"state": state, "code": "workdrive-code", "location": "in",
                      "accounts-server": "https://accounts.zoho.in"},
    )
    assert response.status_code == 302
    assert response.headers["Location"].startswith("http://localhost:3005/settings?oauth_provider=zoho_workdrive&workdrive=connected")
    assert "/integrations/zoho/callback" not in response.headers["Location"]
    assert token_calls[0][0] == "https://accounts.zoho.in/oauth/v2/token"
    assert token_calls[0][1]["redirect_uri"] == "http://localhost:5005/api/v1/integrations"
    assert token_calls[0][1]["client_id"] == "workdrive-client-id"
    assert token_calls[0][1]["client_secret"] == "workdrive-client-secret"
    stored = app.extensions["store"].find_one("integrations", {"_id": "zoho_workdrive"})
    assert stored["provider"] == "zoho_workdrive"
    assert stored["refresh_token"] is None
    assert stored["refresh_token_encrypted"] != "workdrive-refresh"
    assert "workdrive-refresh" not in str(stored)
    assert oauth._cipher().decrypt(stored["refresh_token_encrypted"], "test") == "workdrive-refresh"
    assert app.extensions["store"].find_one("integrations", {"_id": "zoho_mail"}) == mail_before


def test_workdrive_callback_rejects_invalid_state_without_token_exchange(app, client, monkeypatch):
    _configure_route_oauth(app)
    assert client.post("/api/auth/demo", json={}).status_code == 200
    assert client.get("/api/v1/integrations/workdrive/connect").status_code == 302
    monkeypatch.setattr("app.communication.zoho.requests.post", lambda *args, **kwargs: pytest.fail("Token exchange must not run"))
    response = client.get("/api/v1/integrations", query_string={"state": "invalid", "code": "unused"})
    assert response.status_code == 302
    assert "workdrive=error" in response.headers["Location"]
    assert "error_code=OAUTH_STATE_ERROR" in response.headers["Location"]


def test_workdrive_callback_rejects_mail_provider_state(app, client, monkeypatch):
    oauth = _configure_route_oauth(app)
    assert client.post("/api/auth/demo", json={}).status_code == 200
    mail_state, transaction_id, _challenge = oauth.create_state_transaction(
        "user-demo-admin", provider="zoho_mail",
    )
    with client.session_transaction() as browser_session:
        browser_session["workdrive_oauth_transaction"] = transaction_id
    monkeypatch.setattr("app.communication.zoho.requests.post", lambda *args, **kwargs: pytest.fail("Token exchange must not run"))
    response = client.get("/api/v1/integrations", query_string={"state": mail_state, "code": "unused"})
    assert response.status_code == 302
    assert "error_code=OAUTH_STATE_ERROR" in response.headers["Location"]


def test_mail_callback_rejects_workdrive_provider_state(app, client, monkeypatch):
    oauth = _configure_route_oauth(app)
    assert client.post("/api/auth/demo", json={}).status_code == 200
    workdrive_state, transaction_id, _challenge = oauth.create_state_transaction(
        "user-demo-admin", provider="zoho_workdrive",
    )
    with client.session_transaction() as browser_session:
        browser_session["zoho_oauth_transaction"] = transaction_id
    monkeypatch.setattr("app.communication.zoho.requests.post", lambda *args, **kwargs: pytest.fail("Token exchange must not run"))
    response = client.get(
        "/api/v1/integrations/zoho/callback",
        query_string={"state": workdrive_state, "code": "unused"},
    )
    assert response.status_code == 302
    assert "error_code=OAUTH_STATE_ERROR" in response.headers["Location"]


def test_workdrive_connect_reports_specific_missing_client_configuration(app, client):
    _configure_route_oauth(app)
    app.config["ZOHO_WORKDRIVE_CLIENT_ID"] = ""
    app.config["ZOHO_WORKDRIVE_CLIENT_SECRET"] = ""
    assert client.post("/api/auth/demo", json={}).status_code == 200
    response = client.get("/api/v1/integrations/workdrive/connect")
    assert response.status_code == 503
    assert response.json["error"] == "WORKDRIVE_OAUTH_CONFIGURATION_ERROR"


def test_workdrive_callback_handles_oauth_denial_without_exposing_details(app, client):
    _configure_route_oauth(app)
    assert client.post("/api/auth/demo", json={}).status_code == 200
    start = client.get("/api/v1/integrations/workdrive/connect")
    state = parse_qs(urlparse(start.headers["Location"]).query)["state"][0]
    response = client.get(
        "/api/v1/integrations", query_string={"state": state, "error": "access_denied"},
    )
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("http://localhost:3005/settings?oauth_provider=zoho_workdrive&workdrive=error")
    assert "access_denied" not in location
    assert "error_code=OAUTH_CALLBACK_ERROR" in location


def test_email_routing_is_persisted_and_audited_for_superadmin(app):
    client = app.test_client()
    assert client.post("/api/auth/demo", json={}).status_code == 200
    initial = client.get("/api/v1/integrations/zoho/routing")
    assert initial.status_code == 200
    assert any(row["email"] == "business@monedatechnologies.com" for row in initial.json["data"]["cc"])
    added = client.post("/api/v1/integrations/zoho/routing", json={"group": "cc", "email": "custom@example.com", "display_name": "Custom"})
    assert added.status_code == 201
    assert client.post("/api/v1/integrations/zoho/routing", json={"group": "cc", "email": "custom@example.com"}).status_code == 409
    assert client.patch("/api/v1/integrations/zoho/routing", json={"group": "cc", "email": "custom@example.com", "enabled": False}).status_code == 422
    assert client.patch("/api/v1/integrations/zoho/routing", json={"group": "cc", "email": "custom@example.com", "enabled": False, "reason": "Temporarily disabled"}).status_code == 200
    assert client.delete("/api/v1/integrations/zoho/routing", json={"group": "cc", "email": "custom@example.com", "reason": "No longer required"}).status_code == 200
    assert app.extensions["store"].find_one("email_routing_recipients", {"email": "custom@example.com"}) is None


def test_oauth_authorization_url_can_bind_server_side_pkce():
    store = MemoryStore()
    oauth = ZohoMailOAuth(zoho_config(), store)
    state, transaction_id, challenge = oauth.create_state_transaction("admin-1")
    parsed = urlparse(oauth.authorization_url(state, code_challenge=challenge))
    query = parse_qs(parsed.query)
    assert query["code_challenge"] == [challenge]
    assert query["code_challenge_method"] == ["S256"]
    record = oauth.consume_transaction_record(transaction_id, "admin-1")
    verifier = oauth.code_verifier_from_transaction(record or {})
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    assert expected == challenge


def test_oauth_callback_credentials_are_redacted_from_access_logs():
    record = logging.LogRecord(
        "werkzeug", logging.INFO, __file__, 1, "%s", (
            'GET /api/v1/integrations/zoho/callback?code=secret-code&state=secret-state HTTP/1.1',
        ), None,
    )
    assert _OAuthAccessLogFilter().filter(record) is True
    rendered = record.getMessage()
    assert "secret-code" not in rendered
    assert "secret-state" not in rendered
    assert rendered.count("[REDACTED]") == 2


def test_oauth_state_is_hashed_server_side_one_time_and_user_bound():
    store = MemoryStore()
    oauth = ZohoMailOAuth(zoho_config(), store)
    state = oauth.create_state("admin-1")
    rows, total = store.list("oauth_states")
    assert total == 1
    assert state not in str(rows[0])
    assert oauth.consume_state(state, "different-admin") is False
    assert oauth.consume_state(state, "admin-1") is True
    assert oauth.consume_state(state, "admin-1") is False
    second = oauth.create_state("admin-1")
    assert oauth.consume_state(second, "admin-1") is True
    assert oauth.consume_state(second, "admin-1") is False
    third = oauth.create_state("admin-1")
    third_row = store.list("oauth_states")[0][0]
    store.update_one("oauth_states", {"_id": third_row["_id"]}, {"expires_at": third_row["expires_at"].replace(tzinfo=None)})
    assert oauth.consume_state(third, "admin-1") is True


def test_oauth_transaction_fallback_is_user_bound_and_one_time():
    store = MemoryStore()
    oauth = ZohoMailOAuth(zoho_config(), store)
    state, transaction_id, code_challenge = oauth.create_state_transaction("admin-1")
    assert state not in str(store.list("oauth_states")[0][0])
    assert transaction_id not in str(store.list("oauth_states")[0][0])
    assert code_challenge not in str(store.list("oauth_states")[0][0])
    assert oauth.consume_transaction_record(transaction_id, "different-admin") is None
    assert oauth.consume_transaction_record(transaction_id, "admin-1") is not None
    assert oauth.consume_transaction_record(transaction_id, "admin-1") is None


def test_exchange_encrypts_refresh_token_discovers_account_and_captures_region(monkeypatch):
    store = MemoryStore()
    oauth = ZohoMailOAuth(zoho_config(), store)
    workdrive_record = {
        "_id": "zoho_workdrive", "provider": "zoho_workdrive", "status": "connected",
        "refresh_token_encrypted": "existing-workdrive-ciphertext",
    }
    store.insert_one("integrations", workdrive_record)
    workdrive_before = store.find_one("integrations", {"_id": "zoho_workdrive"})
    token_requests = []

    def fake_post(url, **kwargs):
        token_requests.append((url, kwargs.get("data") or {}))
        return FakeResponse({
            "access_token": "access-token", "refresh_token": "refresh-token",
            "expires_in": 3600, "api_domain": "https://www.zohoapis.in",
        })

    monkeypatch.setattr("app.communication.zoho.requests.post", fake_post)
    monkeypatch.setattr("app.communication.zoho.requests.get", lambda url, **kwargs: FakeResponse({
        "status": {"code": 200, "description": "success"},
        "data": [{"accountId": "987654", "emailAddress": "business@monedatechnologies.com"}],
    }))

    result = oauth.exchange_code("authorization-code", code_verifier="server-verifier")
    assert token_requests[0][1]["code_verifier"] == "server-verifier"
    stored = store.find_one("integrations", {"_id": "zoho_mail"})
    assert result["connected"] is True
    assert result["account_email"] == "business@monedatechnologies.com"
    assert stored["account_id"] == "987654"
    assert stored["api_domain"] == "https://mail.zoho.in"
    assert stored["oauth_api_domain"] == "https://www.zohoapis.in"
    assert stored["refresh_token"] is None
    assert stored["refresh_token_encrypted"] != "refresh-token"
    assert "authorization-code" not in str(stored)
    assert oauth.get_valid_zoho_access_token() == "access-token"
    assert store.find_one("integrations", {"_id": "zoho_workdrive"}) == workdrive_before


def test_mail_exchange_retries_transient_account_lookup_before_persisting(monkeypatch):
    store = MemoryStore()
    oauth = ZohoMailOAuth(zoho_config(), store)
    monkeypatch.setattr("app.communication.zoho.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.communication.zoho.requests.post", lambda url, **kwargs: FakeResponse({
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
    }))
    responses = iter([
        FakeResponse({"status": {"code": 500, "description": "Internal Error"}}, 500),
        FakeResponse({
            "status": {"code": 200, "description": "success"},
            "data": [{"accountId": "987654", "emailAddress": "business@monedatechnologies.com"}],
        }),
    ])
    monkeypatch.setattr("app.communication.zoho.requests.get", lambda url, **kwargs: next(responses))

    result = oauth.exchange_code("authorization-code")

    assert result["connected"] is True
    stored = store.find_one("integrations", {"_id": "zoho_mail"})
    assert oauth._cipher().decrypt(stored["refresh_token_encrypted"], "test") == "new-refresh"


def test_failed_mail_account_validation_preserves_existing_credentials(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    store = connected_store(oauth)
    before = store.find_one("integrations", {"_id": "zoho_mail"})
    monkeypatch.setattr("app.communication.zoho.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("app.communication.zoho.requests.post", lambda url, **kwargs: FakeResponse({
        "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
    }))
    monkeypatch.setattr("app.communication.zoho.requests.get", lambda url, **kwargs: FakeResponse(
        {"status": {"code": 500, "description": "Internal Error"}}, 500,
    ))

    with pytest.raises(ZohoIntegrationError) as raised:
        oauth.exchange_code("authorization-code")

    assert raised.value.code == "ZOHO_ACCOUNT_LOOKUP_ERROR"
    after = store.find_one("integrations", {"_id": "zoho_mail"})
    assert after["refresh_token_encrypted"] == before["refresh_token_encrypted"]
    assert after["account_id"] == before["account_id"]
    assert oauth._cipher().decrypt(after["refresh_token_encrypted"], "test") == "refresh-token"


def test_account_lookup_extracts_account_id_from_documented_email_address_list(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    account_calls = []

    def fake_get(url, **kwargs):
        account_calls.append((url, kwargs))
        return FakeResponse({
            "status": {"code": 200, "description": "success"},
            "data": [{
                "accountId": "2560636000000008002",
                "emailAddress": [
                    {"isAlias": False, "isPrimary": True, "mailId": "business@monedatechnologies.com"},
                ],
                "zuid": 809451734,
                "policyId": {"zoid": 3226386},
            }],
        })

    monkeypatch.setattr("app.communication.zoho.requests.get", fake_get)
    result = oauth.lookup_account("access-token", mail_api_base="https://mail.zoho.in")

    assert result == {
        "account_id": "2560636000000008002",
        "account_email": "business@monedatechnologies.com",
        "allowed_from_addresses": ["business@monedatechnologies.com"],
    }
    assert account_calls[0][0] == "https://mail.zoho.in/api/accounts"
    assert account_calls[0][1]["headers"]["Authorization"] == "Zoho-oauthtoken access-token"


def test_account_lookup_discovers_primary_and_sender_aliases(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    monkeypatch.setattr("app.communication.zoho.requests.get", lambda url, **kwargs: FakeResponse({
        "status": {"code": 200, "description": "success"},
        "data": [{
            "accountId": "123",
            "emailAddress": [
                {"isPrimary": True, "mailId": "business@monedatechnologies.com"},
                {"isAlias": True, "mailId": "otp@monedatechnologies.com"},
                {"isAlias": True, "mailId": "quotations@monedatechnologies.com"},
            ],
            "sendMailDetails": [{"fromAddress": "orders@monedatechnologies.com"}],
        }],
    }))

    result = oauth.lookup_account("access-token")

    assert result["account_id"] == "123"
    assert set(result["allowed_from_addresses"]) == {
        "business@monedatechnologies.com", "otp@monedatechnologies.com",
        "quotations@monedatechnologies.com", "orders@monedatechnologies.com",
    }


def test_central_email_service_routes_all_four_sender_purposes():
    provider = RecordingEmailProvider()
    service = EmailService(provider, zoho_config())

    service.send_signup_otp(to=["user@example.com"], html="otp")
    service.send_quotation(to=["user@example.com"], subject="Quote", html="quote")
    service.send_order_confirmation(to=["user@example.com"], subject="Order", html="order")
    service.send_test_email(to=["user@example.com"])

    assert [message["from"] for message in provider.messages] == [
        "otp@monedatechnologies.com",
        "quotations@monedatechnologies.com",
        "orders@monedatechnologies.com",
        "business@monedatechnologies.com",
    ]
    assert [message["from_name"] for message in provider.messages] == [
        "Moneda OTP", "Moneda Quotations", "Moneda Orders", "Moneda Technologies",
    ]
    assert provider.messages[0]["cc"] == [] and provider.messages[0]["bcc"] == []
    assert provider.messages[1]["cc"] == ["business@monedatechnologies.com"]
    assert provider.messages[1]["bcc"] == ["operations@chemo.in"]
    assert provider.messages[2]["cc"] == ["business@monedatechnologies.com"]
    assert provider.messages[2]["bcc"] == ["operations@chemo.in"]
    assert provider.messages[3]["cc"] == ["business@monedatechnologies.com"]
    assert provider.messages[3]["bcc"] == ["operations@chemo.in"]


def test_provider_refreshes_once_caches_token_and_submits_safe_json(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    store = connected_store(oauth)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/oauth/v2/token"):
            return FakeResponse({"access_token": "access-token", "expires_in": 3600, "api_domain": "https://www.zohoapis.in"})
        return FakeResponse({"status": {"code": 200, "description": "success"}, "data": {"messageId": "m-1"}})

    monkeypatch.setattr("app.communication.zoho.requests.post", fake_post)
    provider = ZohoMailApiProvider(zoho_config(), store, oauth=oauth)
    first = provider.send(
        to=["customer@example.com"], cc=["copy@example.com"], bcc=["audit@example.com"],
        subject="Test", html="<p>ok</p>", request_id="test-request",
    )
    second = provider.send(to=["customer@example.com"], subject="Again", html="<p>ok</p>")

    token_calls = [call for call in calls if call[0].endswith("/oauth/v2/token")]
    message_calls = [call for call in calls if call[0].endswith("/messages")]
    assert len(token_calls) == 1
    assert len(message_calls) == 2
    assert message_calls[0][0] == "https://mail.zoho.in/api/accounts/123/messages"
    assert message_calls[0][1]["headers"]["Authorization"] == "Zoho-oauthtoken access-token"
    assert message_calls[0][1]["json"]["fromAddress"] == "business@monedatechnologies.com"
    assert message_calls[0][1]["json"]["toAddress"] == "customer@example.com"
    assert message_calls[0][1]["json"]["ccAddress"] == "copy@example.com"
    assert message_calls[0][1]["json"]["bccAddress"] == "audit@example.com"
    assert first["provider"] == "zoho_mail_api"
    assert first["stage"] == "message_submission"
    assert {"stage": "message_submission", "result": "PASS"} in first["checks"]
    assert first["checks"][-1] == {"stage": "email_submission_success", "result": "PASS"}
    assert "access-token" not in str(first)
    assert any(check.get("source") == "cache" for check in second["checks"])


def test_not_connected_and_sender_mismatch_have_specific_error_codes(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    provider = ZohoMailApiProvider(zoho_config(), oauth.store, oauth=oauth)
    with pytest.raises(EmailDeliveryError) as disconnected:
        provider.send(to=["customer@example.com"], subject="Test", html="<p>ok</p>")
    assert disconnected.value.error_code == "OAUTH_NOT_CONNECTED"
    assert disconnected.value.stage == "oauth_configuration"

    store = connected_store(oauth)
    provider = ZohoMailApiProvider(zoho_config(), store, oauth=oauth)
    with pytest.raises(EmailDeliveryError) as sender:
        provider.send(
            to=["customer@example.com"], from_address="unverified@example.com",
            subject="Test", html="<p>ok</p>",
        )
    assert sender.value.error_code == "SENDER_INVALID"
    assert sender.value.stage == "email_alias_validation"


def test_configured_missing_alias_fails_without_sender_fallback(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    store = connected_store(oauth)
    store.update_one("integrations", {"_id": "zoho_mail"}, {
        "allowed_from_addresses": ["business@monedatechnologies.com"],
    })
    monkeypatch.setattr("app.communication.zoho.requests.post", lambda url, **kwargs: FakeResponse({
        "access_token": "access-token", "expires_in": 3600,
    }))
    provider = ZohoMailApiProvider(zoho_config(), store, oauth=oauth)

    with pytest.raises(EmailDeliveryError) as missing:
        provider.send(
            to=["customer@example.com"], from_address="otp@monedatechnologies.com",
            subject="OTP", html="<p>code omitted</p>",
        )

    assert missing.value.error_code == "OTP_SENDER_ALIAS_UNAVAILABLE"
    assert missing.value.stage == "email_alias_validation"


def test_disconnect_revokes_and_removes_local_credentials(monkeypatch):
    oauth = ZohoMailOAuth(zoho_config(), MemoryStore())
    store = connected_store(oauth)
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse({})

    monkeypatch.setattr("app.communication.zoho.requests.post", fake_post)
    result = oauth.disconnect()
    stored = store.find_one("integrations", {"_id": "zoho_mail"})
    assert result["connected"] is False
    assert result["revoked"] is True
    assert calls[0][0].endswith("/oauth/v2/token/revoke")
    assert calls[0][1]["data"] == {"token": "refresh-token"}
    assert stored["refresh_token_encrypted"] is None
    assert stored["account_id"] is None
    assert oauth.status()["status"] == "not_connected"


def test_admin_status_connect_callback_and_disconnect_routes_are_safe(app, authenticated, monkeypatch):
    oauth = app.extensions["zoho_oauth"]
    oauth.client_id = "client-id"
    oauth.client_secret = "client-secret"
    oauth.redirect_uri = "http://localhost:5005/api/v1/integrations/zoho/callback"
    oauth._encryption_secret = "test-key"
    oauth.config["ZOHO_ACCOUNTS_BASE_URL"] = "https://accounts.zoho.in"
    oauth.config["ZOHO_MAIL_API_BASE_URL"] = ""

    status = authenticated.get("/api/v1/integrations/zoho/status")
    assert status.status_code == 200
    serialized = str(status.json).lower()
    assert "client-secret" not in serialized
    assert "refresh_token" not in serialized
    assert "access_token" not in serialized

    connect = authenticated.get("/api/v1/integrations/zoho/connect")
    assert connect.status_code == 302
    assert connect.headers["Location"].startswith("https://accounts.zoho.in/oauth/v2/auth?")
    state = parse_qs(urlparse(connect.headers["Location"]).query)["state"][0]
    monkeypatch.setattr(oauth, "exchange_code", lambda code, code_verifier=None, diagnostic_id=None: {"connected": True})
    callback = authenticated.get(f"/api/v1/integrations/zoho/callback?code=secret-code&state={state}")
    assert callback.status_code == 302
    assert callback.headers["Location"] == "http://localhost:3005/settings?oauth_provider=zoho_mail&zoho=connected"
    assert "secret-code" not in str(app.extensions["store"].list("audit_logs")[0])

    connect_without_returned_state = authenticated.get("/api/v1/integrations/zoho/connect")
    assert connect_without_returned_state.status_code == 302
    callback_without_state = authenticated.get("/api/v1/integrations/zoho/callback?code=secret-code-2&location=in&accounts-server=https://accounts.zoho.in")
    assert callback_without_state.status_code == 302
    assert "oauth_provider=zoho_mail" in callback_without_state.headers["Location"]
    assert "zoho=error" in callback_without_state.headers["Location"]
    assert "error_code=OAUTH_STATE_ERROR" in callback_without_state.headers["Location"]

    invalid = authenticated.get("/api/v1/integrations/zoho/callback?code=ignored&state=wrong")
    assert invalid.status_code == 302
    assert "stage=state_validation" in invalid.headers["Location"]

    monkeypatch.setattr(oauth, "disconnect", lambda: {"connected": False, "revoked": True, "diagnostic_id": "zoho-test"})
    disconnected = authenticated.post("/api/v1/integrations/zoho/disconnect", json={})
    assert disconnected.status_code == 200
    assert disconnected.json["data"]["connected"] is False


def test_integration_routes_require_admin(client):
    assert client.get("/api/v1/integrations/zoho/status").status_code == 401
    assert client.get("/api/v1/integrations/zoho/connect").status_code == 401
    assert client.post("/api/v1/integrations/zoho/test", json={"to": "person@example.com"}).status_code == 401
    assert client.post("/api/v1/integrations/zoho/disconnect", json={}).status_code == 401
