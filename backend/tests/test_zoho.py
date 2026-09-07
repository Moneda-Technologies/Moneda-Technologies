from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse
import logging

import pytest

from app.communication.email import EmailDeliveryError
from app.communication.zoho import ZOHO_SCOPES, ZohoMailApiProvider, ZohoMailOAuth
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
    }
    assert account_calls[0][0] == "https://mail.zoho.in/api/accounts"
    assert account_calls[0][1]["headers"]["Authorization"] == "Zoho-oauthtoken access-token"


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
    assert first["checks"][-1] == {"stage": "message_submission", "result": "PASS"}
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
    assert sender.value.stage == "sender"


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
    assert callback.headers["Location"] == "http://localhost:3005/settings?zoho=connected"
    assert "secret-code" not in str(app.extensions["store"].list("audit_logs")[0])

    connect_without_returned_state = authenticated.get("/api/v1/integrations/zoho/connect")
    assert connect_without_returned_state.status_code == 302
    callback_without_state = authenticated.get("/api/v1/integrations/zoho/callback?code=secret-code-2&location=in&accounts-server=https://accounts.zoho.in")
    assert callback_without_state.status_code == 302
    assert callback_without_state.headers["Location"] == "http://localhost:3005/settings?zoho=connected"

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
