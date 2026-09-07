from __future__ import annotations

"""Server-side Zoho Mail OAuth and Mail API transport."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re
import secrets
import threading
from typing import Any
from urllib.parse import urlencode, urlparse

from cryptography.fernet import Fernet, InvalidToken
import requests

from app.communication.email import EmailDeliveryError, EmailProvider, email_diagnostic_id


logger = logging.getLogger(__name__)

# Sending and automatic account discovery are separate Zoho APIs. These are the
# least-privilege scopes required for both operations; messages.ALL is not used.
ZOHO_SCOPES = ("ZohoMail.messages.CREATE", "ZohoMail.accounts.READ")
ZOHO_SCOPE = ",".join(ZOHO_SCOPES)
INTEGRATION_ID = "zoho_mail"

ALLOWED_ACCOUNTS_HOSTS = {
    "accounts.zoho.com", "accounts.zoho.eu", "accounts.zoho.in",
    "accounts.zoho.com.au", "accounts.zoho.jp", "accounts.zohocloud.ca",
    "accounts.zoho.com.cn", "accounts.zoho.ae", "accounts.zoho.sa",
}
MAIL_HOST_BY_SUFFIX = {
    ".com.au": "mail.zoho.com.au",
    ".com.cn": "mail.zoho.com.cn",
    ".eu": "mail.zoho.eu",
    ".in": "mail.zoho.in",
    ".jp": "mail.zoho.jp",
    ".ca": "mail.zohocloud.ca",
    ".ae": "mail.zoho.ae",
    ".sa": "mail.zoho.sa",
    ".com": "mail.zoho.com",
}
ALLOWED_MAIL_HOSTS = set(MAIL_HOST_BY_SUFFIX.values())


def integration_diagnostic_id() -> str:
    return f"zoho-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4)}"


class ZohoIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str, *, stage: str, diagnostic_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.diagnostic_id = diagnostic_id or integration_diagnostic_id()


class _CredentialCipher:
    """Authenticated encryption for credentials persisted in MongoDB."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise ValueError("Integration encryption key is missing")
        material = hashlib.sha256(f"moneda-zoho-v1:{secret}".encode("utf-8")).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(material))

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str, diagnostic_id: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise ZohoIntegrationError(
                "OAUTH_CONFIGURATION_ERROR",
                "Stored Zoho credentials cannot be decrypted with the configured integration key",
                stage="oauth_configuration",
                diagnostic_id=diagnostic_id,
            ) from exc


def _host_suffix(host: str) -> str | None:
    for suffix in MAIL_HOST_BY_SUFFIX:
        if host.endswith(suffix):
            return suffix
    return None


def _https_base(value: str, *, allowed_hosts: set[str], label: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.hostname not in allowed_hosts or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an approved HTTPS Zoho endpoint")
    return f"https://{parsed.hostname}"


class ZohoMailOAuth:
    def __init__(self, config: dict[str, Any], store: Any) -> None:
        self.config = config
        self.store = store
        self.client_id = str(config.get("ZOHO_CLIENT_ID") or "").strip()
        self.client_secret = str(config.get("ZOHO_CLIENT_SECRET") or "").strip()
        self.redirect_uri = str(config.get("ZOHO_OAUTH_REDIRECT_URI") or "").strip()
        self.from_address = str(config.get("ZOHO_FROM_ADDRESS") or "business@monedatechnologies.com").strip().lower()
        self._encryption_secret = str(config.get("INTEGRATION_ENCRYPTION_KEY") or config.get("SECRET_KEY") or "").strip()
        self._token_lock = threading.RLock()
        self._access_token: str | None = None
        self._access_token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._last_token_source = "none"

    @property
    def accounts_base_url(self) -> str:
        raw = str(self.config.get("ZOHO_ACCOUNTS_BASE_URL") or "https://accounts.zoho.in")
        return _https_base(raw, allowed_hosts=ALLOWED_ACCOUNTS_HOSTS, label="ZOHO_ACCOUNTS_BASE_URL")

    def configured(self) -> bool:
        try:
            accounts_valid = bool(self.accounts_base_url)
            redirect = urlparse(self.redirect_uri)
            redirect_valid = redirect.scheme == "https" or (
                redirect.scheme == "http" and redirect.hostname in {"localhost", "127.0.0.1", "::1"}
            )
        except ValueError:
            accounts_valid = False
            redirect_valid = False
        return bool(
            self.client_id and self.client_secret and self.redirect_uri and self.from_address
            and self._encryption_secret and accounts_valid and redirect_valid
        )

    def integration(self) -> dict[str, Any] | None:
        return self.store.find_one("integrations", {"_id": INTEGRATION_ID})

    def _cipher(self) -> _CredentialCipher:
        try:
            return _CredentialCipher(self._encryption_secret)
        except ValueError as exc:
            raise ZohoIntegrationError(
                "OAUTH_CONFIGURATION_ERROR", str(exc), stage="oauth_configuration"
            ) from exc

    def _stored_refresh_token(self, diagnostic_id: str) -> str:
        row = self.integration() or {}
        if row.get("status") == "not_connected":
            return ""
        encrypted = str(row.get("refresh_token_encrypted") or "").strip()
        if encrypted:
            return self._cipher().decrypt(encrypted, diagnostic_id)
        legacy = str(row.get("refresh_token") or "").strip()
        if legacy:
            # Migrate the unfinished plaintext representation in place.
            self.store.update_one(
                "integrations", {"_id": INTEGRATION_ID},
                {"refresh_token_encrypted": self._cipher().encrypt(legacy), "refresh_token": None},
            )
            return legacy
        return str(self.config.get("ZOHO_REFRESH_TOKEN") or "").strip()

    def _account_id(self) -> str:
        row = self.integration() or {}
        if row.get("status") == "not_connected":
            return ""
        return str(row.get("account_id") or self.config.get("ZOHO_ACCOUNT_ID") or "").strip()

    def _mail_base_from_oauth_domain(self, oauth_api_domain: str) -> str | None:
        parsed = urlparse(oauth_api_domain.strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            return None
        if host in ALLOWED_MAIL_HOSTS:
            return f"https://{host}"
        if not (host == "api.zoho.com" or host.startswith("api.zoho.") or host == "www.zohoapis.com" or host.startswith("www.zohoapis.")):
            return None
        suffix = _host_suffix(host)
        return f"https://{MAIL_HOST_BY_SUFFIX[suffix]}" if suffix else None

    def mail_api_base_url(self, oauth_api_domain: str | None = None) -> str:
        row = self.integration() or {}
        if oauth_api_domain:
            mapped = self._mail_base_from_oauth_domain(oauth_api_domain)
            if mapped:
                return mapped
        stored = str(row.get("api_domain") or "").strip()
        if stored:
            try:
                return _https_base(stored, allowed_hosts=ALLOWED_MAIL_HOSTS, label="stored Zoho Mail API domain")
            except ValueError:
                pass
        configured = str(self.config.get("ZOHO_MAIL_API_BASE_URL") or "").strip()
        if configured:
            return _https_base(configured, allowed_hosts=ALLOWED_MAIL_HOSTS, label="ZOHO_MAIL_API_BASE_URL")
        suffix = _host_suffix(urlparse(self.accounts_base_url).hostname or "") or ".in"
        return f"https://{MAIL_HOST_BY_SUFFIX[suffix]}"

    def status(self) -> dict[str, Any]:
        row = self.integration() or {}
        diagnostic_id = integration_diagnostic_id()
        try:
            has_refresh_token = bool(self._stored_refresh_token(diagnostic_id))
            configuration_error = None
        except ZohoIntegrationError as exc:
            has_refresh_token = False
            configuration_error = {"code": exc.code, "stage": exc.stage, "diagnostic_id": exc.diagnostic_id}
        account_id = self._account_id()
        connected = bool(self.configured() and has_refresh_token and account_id and row.get("status") != "not_connected")
        last_error = row.get("last_error") or configuration_error
        state = "connected" if connected else "error" if last_error else "not_connected"
        account_email = str(row.get("account_email") or row.get("from_address") or self.from_address or "")
        try:
            api_domain = str(row.get("api_domain") or self.mail_api_base_url())
            api_domain_state = "configured" if (row.get("api_domain") or self.config.get("ZOHO_MAIL_API_BASE_URL")) else "derived"
        except ValueError:
            api_domain = ""
            api_domain_state = "missing"
        return {
            "provider": "zoho_mail_api",
            "status": state,
            "configured": self.configured(),
            "connected": connected,
            "oauth": "connected" if has_refresh_token else "not_connected",
            "account_email": account_email,
            "account_id": account_id or None,
            "account_id_configured": bool(account_id),
            "account_id_status": "configured" if account_id else "missing",
            "api_domain": api_domain,
            "api_domain_status": api_domain_state,
            "scopes": list(row.get("scopes") or ZOHO_SCOPES),
            "connected_at": row.get("connected_at"),
            "updated_at": row.get("updated_at"),
            "last_error": last_error,
        }

    def authorization_url(self, state: str, *, code_challenge: str | None = None) -> str:
        if not self.configured():
            raise ZohoIntegrationError(
                "OAUTH_CONFIGURATION_ERROR",
                "Zoho OAuth client configuration is incomplete",
                stage="oauth_configuration",
            )
        if not state:
            raise ZohoIntegrationError(
                "OAUTH_CONFIGURATION_ERROR",
                "Zoho OAuth state is required",
                stage="authorization",
            )
        query_params = {
            "response_type": "code",
            "client_id": self.client_id,
            "scope": ZOHO_SCOPE,
            "redirect_uri": self.redirect_uri,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        if code_challenge:
            query_params.update({"code_challenge": code_challenge, "code_challenge_method": "S256"})
        query = urlencode(query_params)
        return f"{self.accounts_base_url}/oauth/v2/auth?{query}"

    def create_state(self, user_id: str, *, lifetime_seconds: int = 600,
                     return_path: str = "/settings") -> str:
        """Create a Zoho state value and persist its transaction server-side.

        This method remains as a backwards-compatible convenience for callers
        that only need the authorization state.  The OAuth start route uses
        ``create_state_transaction`` so it can also bind a callback to the
        initiating signed Flask session when Zoho omits ``state`` from its
        documented server-based callback.
        """
        state, _transaction_id, _code_challenge = self.create_state_transaction(
            user_id, lifetime_seconds=lifetime_seconds, return_path=return_path,
        )
        return state

    def create_state_transaction(self, user_id: str, *, lifetime_seconds: int = 600,
                                 return_path: str = "/settings") -> tuple[str, str, str]:
        """Create state, a session-bound transaction id, and a PKCE challenge.

        Neither replayable value is stored in plaintext.  The transaction id
        is the only value copied into the signed browser session; MongoDB is
        authoritative for expiry, user binding, and one-time consumption. The
        PKCE verifier is encrypted at rest and never sent to the browser.
        """
        state = secrets.token_urlsafe(48)
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        transaction_id = secrets.token_urlsafe(32)
        transaction_hash = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        created_at = datetime.now(timezone.utc)
        self.store.insert_one("oauth_states", {
            # Store only a hash. The browser receives the opaque state, but the
            # database never needs the replayable value in plaintext.
            "_id": f"zoho:{state_hash}", "state_hash": state_hash, "provider": "zoho",
            "transaction_hash": transaction_hash,
            "code_verifier_encrypted": self._cipher().encrypt(code_verifier),
            "user_id": user_id, "return_path": return_path,
            "created_at": created_at,
            "expires_at": created_at + timedelta(seconds=lifetime_seconds),
            "consumed": False,
        })
        return state, transaction_id, code_challenge

    def consume_state(self, state: str, user_id: str | None) -> bool:
        return self.consume_state_record(state, user_id) is not None

    def consume_state_record(self, state: str, user_id: str | None,
                             *, transaction_id: str | None = None) -> dict[str, Any] | None:
        if not state or not user_id:
            return None
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        query: dict[str, Any] = {
            "_id": f"zoho:{state_hash}", "user_id": user_id, "consumed": False,
        }
        if transaction_id:
            query["transaction_hash"] = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
        # update_one is backed by MongoDB's atomic find-and-update operation;
        # MemoryStore performs the same conditional update under its lock.
        # Including user_id prevents a different session from consuming it.
        row = self.store.update_one(
            "oauth_states", query,
            {"consumed": True, "consumed_at": datetime.now(timezone.utc)},
        )
        return self._validated_consumed_state(row)

    def consume_transaction_record(self, transaction_id: str | None,
                                   user_id: str | None) -> dict[str, Any] | None:
        """Consume an initiated OAuth transaction when Zoho omits ``state``.

        The transaction pointer comes from the signed Flask session, while the
        Mongo row enforces the initiating user, expiry, and one-time use.  A
        missing/unknown pointer therefore cannot turn the callback into an
        unauthenticated code-exchange endpoint.
        """
        if not transaction_id or not user_id:
            return None
        transaction_hash = hashlib.sha256(transaction_id.encode("utf-8")).hexdigest()
        row = self.store.update_one(
            "oauth_states",
            {"transaction_hash": transaction_hash, "user_id": user_id, "consumed": False},
            {"consumed": True, "consumed_at": datetime.now(timezone.utc)},
        )
        return self._validated_consumed_state(row)

    def code_verifier_from_transaction(self, state_record: dict[str, Any], *,
                                       diagnostic_id: str | None = None) -> str:
        """Decrypt the one-time PKCE verifier after transaction validation."""
        encrypted = str(state_record.get("code_verifier_encrypted") or "").strip()
        if not encrypted:
            raise ZohoIntegrationError(
                "OAUTH_STATE_ERROR", "Zoho OAuth transaction is missing its PKCE verifier",
                stage="state_validation", diagnostic_id=diagnostic_id,
            )
        return self._cipher().decrypt(encrypted, diagnostic_id or integration_diagnostic_id())

    @staticmethod
    def _validated_consumed_state(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if not row:
            return None
        expires_at = row.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            # PyMongo returns UTC datetimes as naive unless tz_aware is enabled.
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if not isinstance(expires_at, datetime) or expires_at < datetime.now(timezone.utc):
            return None
        return row

    def callback_context_valid(self, *, location: str | None, accounts_server: str | None) -> bool:
        """Validate Zoho's regional callback hints when they are supplied."""
        try:
            expected_accounts = self.accounts_base_url
        except ValueError:
            return False
        if accounts_server:
            try:
                if _https_base(accounts_server, allowed_hosts=ALLOWED_ACCOUNTS_HOSTS, label="accounts-server") != expected_accounts:
                    return False
            except ValueError:
                return False
        if location:
            expected_location = _host_suffix(urlparse(expected_accounts).hostname or "")
            expected_code = expected_location.lstrip(".") if expected_location else ""
            if str(location).strip().casefold() != expected_code.casefold():
                return False
        return True

    def exchange_code(self, code: str, *, code_verifier: str | None = None,
                      diagnostic_id: str | None = None) -> dict[str, Any]:
        diagnostic_id = diagnostic_id or integration_diagnostic_id()
        if not self.configured():
            raise ZohoIntegrationError(
                "OAUTH_CONFIGURATION_ERROR", "Zoho OAuth client configuration is incomplete",
                stage="oauth_configuration", diagnostic_id=diagnostic_id,
            )
        try:
            token_data = {
                "code": code, "grant_type": "authorization_code", "client_id": self.client_id,
                "client_secret": self.client_secret, "redirect_uri": self.redirect_uri,
            }
            if code_verifier:
                token_data["code_verifier"] = code_verifier
            response = requests.post(
                f"{self.accounts_base_url}/oauth/v2/token",
                data=token_data,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ZohoIntegrationError(
                "OAUTH_TOKEN_EXCHANGE_ERROR", "Zoho token exchange could not be reached",
                stage="token_exchange", diagnostic_id=diagnostic_id,
            ) from exc
        payload = self._json_response(
            response, code="OAUTH_TOKEN_EXCHANGE_ERROR", stage="token_exchange", diagnostic_id=diagnostic_id
        )
        refresh_token = str(payload.get("refresh_token") or "").strip()
        access_token = str(payload.get("access_token") or "").strip()
        if not refresh_token or not access_token:
            raise ZohoIntegrationError(
                "OAUTH_TOKEN_EXCHANGE_ERROR", "Zoho did not return the required OAuth tokens",
                stage="token_exchange", diagnostic_id=diagnostic_id,
            )
        oauth_api_domain = str(payload.get("api_domain") or "").strip()
        if oauth_api_domain:
            mail_api_base = self._mail_base_from_oauth_domain(oauth_api_domain)
            if not mail_api_base:
                raise ZohoIntegrationError(
                    "OAUTH_TOKEN_EXCHANGE_ERROR", "Zoho returned an unsupported API domain",
                    stage="token_exchange", diagnostic_id=diagnostic_id,
                )
        else:
            mail_api_base = self.mail_api_base_url()
        account = self.lookup_account(access_token, mail_api_base=mail_api_base, diagnostic_id=diagnostic_id)
        now = datetime.now(timezone.utc)
        document = {
            "provider": "zoho_mail_api", "status": "connected",
            "refresh_token_encrypted": self._cipher().encrypt(refresh_token), "refresh_token": None,
            "account_email": account["account_email"], "account_id": account["account_id"],
            "from_address": self.from_address, "api_domain": mail_api_base,
            "oauth_api_domain": oauth_api_domain or None, "scopes": list(ZOHO_SCOPES),
            "connected_at": now, "last_error": None,
        }
        self.store.update_one("integrations", {"_id": INTEGRATION_ID}, document, upsert=True)
        self._cache_access_token(access_token, payload.get("expires_in"))
        logger.info(
            "zoho oauth connection success diagnostic_id=%s stage=connection result=PASS account_id_configured=true api_domain=%s",
            diagnostic_id, mail_api_base,
        )
        return self.status()

    def lookup_account(self, access_token: str, *, mail_api_base: str | None = None,
                       diagnostic_id: str | None = None) -> dict[str, str]:
        diagnostic_id = diagnostic_id or integration_diagnostic_id()
        api_base = mail_api_base or self.mail_api_base_url()
        endpoint = f"{api_base}/api/accounts"
        endpoint_host = urlparse(endpoint).hostname or "unknown"
        logger.info(
            "account_lookup_started diagnostic_id=%s account_lookup_endpoint_host=%s",
            diagnostic_id, endpoint_host,
        )
        try:
            response = requests.get(
                endpoint,
                headers={"Accept": "application/json", "Authorization": f"Zoho-oauthtoken {access_token}"},
                timeout=20,
            )
        except requests.RequestException as exc:
            logger.error(
                "account_lookup_result=unreachable account_lookup_success=false diagnostic_id=%s account_lookup_endpoint_host=%s exception_class=%s",
                diagnostic_id, endpoint_host, type(exc).__name__,
            )
            raise ZohoIntegrationError(
                "ZOHO_ACCOUNT_LOOKUP_ERROR", "Zoho account lookup could not be reached",
                stage="account_lookup", diagnostic_id=diagnostic_id,
            ) from exc
        try:
            payload = self._json_response(
                response, code="ZOHO_ACCOUNT_LOOKUP_ERROR", stage="account_lookup", diagnostic_id=diagnostic_id
            )
        except ZohoIntegrationError:
            logger.error(
                "account_lookup_result=%s account_lookup_success=false account_lookup_endpoint_host=%s zoho_error_code=%s diagnostic_id=%s response=%s",
                response.status_code, endpoint_host, self._zoho_error_code(response),
                diagnostic_id, self._sanitized_response_text(response),
            )
            raise
        data: Any = payload.get("data") or payload.get("accounts") or []
        if isinstance(data, dict):
            data = data.get("accounts") or data.get("data") or [data]
        rows = data if isinstance(data, list) else []
        expected = self.from_address.casefold()
        logger.info(
            "account_lookup_result=%s account_lookup_success=%s account_lookup_accounts_count=%s diagnostic_id=%s account_lookup_endpoint_host=%s",
            response.status_code, response.status_code < 400, len(rows), diagnostic_id, endpoint_host,
        )
        selected = next((row for row in rows if expected in self._row_emails(row)), None)
        if not selected:
            logger.error(
                "account_lookup_result=%s account_lookup_success=false account_lookup_accounts_count=%s diagnostic_id=%s account_lookup_endpoint_host=%s error_code=ZOHO_ACCOUNT_LOOKUP_ERROR reason=sender_not_found",
                response.status_code, len(rows), diagnostic_id, endpoint_host,
            )
            raise ZohoIntegrationError(
                "ZOHO_ACCOUNT_LOOKUP_ERROR",
                "The authorized Zoho account does not contain the configured sender mailbox",
                stage="account_lookup", diagnostic_id=diagnostic_id,
            )
        account_id = str(selected.get("accountId") or selected.get("account_id") or selected.get("id") or "").strip()
        if not account_id:
            logger.error(
                "account_lookup_result=%s account_lookup_success=false account_lookup_accounts_count=%s diagnostic_id=%s account_lookup_endpoint_host=%s error_code=ZOHO_ACCOUNT_LOOKUP_ERROR reason=account_id_missing",
                response.status_code, len(rows), diagnostic_id, endpoint_host,
            )
            raise ZohoIntegrationError(
                "ZOHO_ACCOUNT_LOOKUP_ERROR", "Zoho account lookup did not return an account ID",
                stage="account_lookup", diagnostic_id=diagnostic_id,
            )
        return {"account_id": account_id, "account_email": self._row_email(selected)}

    @staticmethod
    def _row_email(row: dict[str, Any]) -> str:
        emails = ZohoMailOAuth._row_emails(row)
        primary = str(row.get("primaryEmailAddress") or row.get("mailboxAddress") or row.get("incomingUserName") or "").strip().lower()
        return primary if primary in emails else next(iter(emails), primary)

    @staticmethod
    def _row_emails(row: dict[str, Any]) -> set[str]:
        emails: set[str] = set()

        def add(value: Any) -> None:
            email = str(value or "").strip().lower()
            if "@" in email:
                emails.add(email)

        for key in ("primaryEmailAddress", "mailboxAddress", "incomingUserName", "email_address"):
            add(row.get(key))
        email_address = row.get("emailAddress")
        if isinstance(email_address, list):
            for item in email_address:
                if isinstance(item, dict):
                    add(item.get("mailId") or item.get("email") or item.get("emailAddress"))
                else:
                    add(item)
        else:
            add(email_address)
        send_mail_details = row.get("sendMailDetails")
        if isinstance(send_mail_details, list):
            for item in send_mail_details:
                if isinstance(item, dict):
                    add(item.get("fromAddress") or item.get("userName"))
        return emails

    def _cache_access_token(self, access_token: str, expires_in: Any) -> None:
        try:
            seconds = max(int(expires_in or 3600), 120)
        except (TypeError, ValueError):
            seconds = 3600
        with self._token_lock:
            self._access_token = access_token
            self._access_token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            self._last_token_source = "token_exchange"

    def access_token_with_source(self) -> tuple[str, str]:
        with self._token_lock:
            if self._access_token and self._access_token_expires_at > datetime.now(timezone.utc) + timedelta(seconds=60):
                self._last_token_source = "cache"
                return self._access_token, "cache"
            diagnostic_id = integration_diagnostic_id()
            refresh_token = self._stored_refresh_token(diagnostic_id)
            if not refresh_token:
                raise ZohoIntegrationError(
                    "OAUTH_NOT_CONNECTED", "Zoho Mail is not connected; authorize the integration first",
                    stage="oauth_configuration", diagnostic_id=diagnostic_id,
                )
            if not self.configured():
                raise ZohoIntegrationError(
                    "OAUTH_CONFIGURATION_ERROR", "Zoho OAuth client configuration is incomplete",
                    stage="oauth_configuration", diagnostic_id=diagnostic_id,
                )
            try:
                response = requests.post(
                    f"{self.accounts_base_url}/oauth/v2/token",
                    data={
                        "refresh_token": refresh_token, "grant_type": "refresh_token",
                        "client_id": self.client_id, "client_secret": self.client_secret,
                    },
                    timeout=20,
                )
            except requests.RequestException as exc:
                raise ZohoIntegrationError(
                    "OAUTH_REFRESH_ERROR", "Zoho access token refresh could not be reached",
                    stage="token_refresh", diagnostic_id=diagnostic_id,
                ) from exc
            payload = self._json_response(
                response, code="OAUTH_REFRESH_ERROR", stage="token_refresh", diagnostic_id=diagnostic_id
            )
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise ZohoIntegrationError(
                    "OAUTH_REFRESH_ERROR", "Zoho token refresh did not return an access token",
                    stage="token_refresh", diagnostic_id=diagnostic_id,
                )
            oauth_api_domain = str(payload.get("api_domain") or "").strip()
            if oauth_api_domain:
                mail_api_base = self._mail_base_from_oauth_domain(oauth_api_domain)
                if not mail_api_base:
                    raise ZohoIntegrationError(
                        "OAUTH_REFRESH_ERROR", "Zoho returned an unsupported API domain",
                        stage="token_refresh", diagnostic_id=diagnostic_id,
                    )
                if self.integration():
                    self.store.update_one(
                        "integrations", {"_id": INTEGRATION_ID},
                        {"oauth_api_domain": oauth_api_domain, "api_domain": mail_api_base},
                    )
            self._cache_access_token(token, payload.get("expires_in"))
            self._last_token_source = "refresh"
            return token, "refresh"

    def get_valid_zoho_access_token(self) -> str:
        return self.access_token_with_source()[0]

    # Compatibility with the unfinished migration implementation.
    def access_token(self) -> str:
        return self.get_valid_zoho_access_token()

    def record_error(self, exc: ZohoIntegrationError) -> None:
        row = self.integration() or {}
        status = "connected" if self.status().get("connected") else "error"
        self.store.update_one("integrations", {"_id": INTEGRATION_ID}, {
            "provider": "zoho_mail_api", "status": status,
            "last_error": {"code": exc.code, "stage": exc.stage, "diagnostic_id": exc.diagnostic_id},
            **({"account_id": row.get("account_id")} if row.get("account_id") else {}),
        }, upsert=True)

    def disconnect(self) -> dict[str, Any]:
        diagnostic_id = integration_diagnostic_id()
        refresh_token = ""
        try:
            refresh_token = self._stored_refresh_token(diagnostic_id)
        except ZohoIntegrationError:
            pass
        revoked = False
        if refresh_token and self.configured():
            try:
                response = requests.post(
                    f"{self.accounts_base_url}/oauth/v2/token/revoke",
                    data={"token": refresh_token}, timeout=20,
                )
                revoked = response.status_code < 400
            except requests.RequestException:
                logger.warning(
                    "zoho oauth disconnect diagnostic_id=%s stage=token_revocation result=FAIL exception_class=RequestException",
                    diagnostic_id,
                )
        self.store.update_one("integrations", {"_id": INTEGRATION_ID}, {
            "provider": "zoho_mail_api", "status": "not_connected",
            "refresh_token_encrypted": None, "refresh_token": None, "account_id": None,
            "account_email": self.from_address, "from_address": self.from_address,
            "api_domain": None, "oauth_api_domain": None, "scopes": [],
            "connected_at": None, "disconnected_at": datetime.now(timezone.utc), "last_error": None,
        }, upsert=True)
        with self._token_lock:
            self._access_token = None
            self._access_token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        logger.info(
            "zoho oauth disconnect diagnostic_id=%s stage=disconnect result=PASS remote_revoked=%s",
            diagnostic_id, revoked,
        )
        return {"connected": False, "revoked": revoked, "diagnostic_id": diagnostic_id}

    @staticmethod
    def _json_response(response: requests.Response, *, code: str, stage: str,
                       diagnostic_id: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        rejected = response.status_code >= 400 or bool(payload.get("error")) or status.get("code") not in (None, 200)
        if rejected:
            description = payload.get("error") or payload.get("message") or status.get("description") or "Zoho rejected the request"
            normalized_description = str(description).casefold()
            if response.status_code == 429:
                error_code = "ZOHO_MAIL_API_RATE_LIMIT"
            elif response.status_code in {401, 403} and stage not in {"token_exchange", "token_refresh"}:
                error_code = "ZOHO_MAIL_API_AUTH_ERROR"
            elif stage == "message_submission" and any(term in normalized_description for term in ("sender", "from address", "fromaddress")):
                error_code = "SENDER_INVALID"
            else:
                error_code = code
            raise ZohoIntegrationError(
                error_code, f"Zoho request failed: {str(description)[:160]}",
                stage=stage, diagnostic_id=diagnostic_id,
            )
        return payload

    @staticmethod
    def _zoho_error_code(response: requests.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            return "unavailable"
        status = payload.get("status") if isinstance(payload.get("status"), dict) else {}
        value = payload.get("errorCode") or payload.get("error_code") or payload.get("error") or status.get("code")
        return str(value or "unavailable")[:80]

    @staticmethod
    def _sanitized_response_text(response: requests.Response) -> str:
        text = str(getattr(response, "text", "") or "")
        text = re.sub(
            r'(?i)("?(?:access_token|refresh_token|authorization_code|client_secret|code)"?\s*[:=]\s*"?)[^",}\s]+',
            r"\1[REDACTED]",
            text,
        )
        text = re.sub(r"(?i)(Authorization\s*:\s*)(?:Zoho-oauthtoken|Bearer)\s+[^\s,}]+", r"\1[REDACTED]", text)
        return text[:500]


class ZohoMailApiProvider(EmailProvider):
    def __init__(self, config: dict[str, Any], store: Any, *, oauth: ZohoMailOAuth | None = None) -> None:
        self.oauth = oauth or ZohoMailOAuth(config, store)
        self.config = config

    def send(self, *, to: list[str], subject: str, html: str,
             attachments: list[dict[str, Any]] | None = None, from_address: str | None = None,
             cc: list[str] | None = None, bcc: list[str] | None = None,
             request_id: str | None = None) -> dict[str, Any]:
        diagnostic_id = email_diagnostic_id()
        trace_id = request_id or "email-untracked"
        checks: list[dict[str, str]] = []

        def passed(stage: str, **extra: str) -> None:
            checks.append({"stage": stage, "result": "PASS", **extra})
            logger.info(
                "[%s] email diagnostic_id=%s provider=zoho_mail_api stage=%s result=PASS",
                trace_id, diagnostic_id, stage,
            )

        try:
            if not self.oauth.configured():
                raise ZohoIntegrationError(
                    "OAUTH_CONFIGURATION_ERROR", "Zoho OAuth client configuration is incomplete",
                    stage="oauth_configuration", diagnostic_id=diagnostic_id,
                )
            passed("configuration")
            integration_status = self.oauth.status()
            if not integration_status["connected"]:
                raise ZohoIntegrationError(
                    "OAUTH_NOT_CONNECTED", "Zoho Mail is not connected; authorize the integration first",
                    stage="oauth_configuration", diagnostic_id=diagnostic_id,
                )
            passed("oauth")
            sender = str(integration_status.get("account_email") or self.oauth.from_address).strip().lower()
            requested_sender = str(from_address or sender).strip().lower()
            if not to or not sender:
                raise ZohoIntegrationError(
                    "OAUTH_CONFIGURATION_ERROR", "Zoho sender and recipient are required",
                    stage="configuration", diagnostic_id=diagnostic_id,
                )
            if requested_sender != sender or sender != self.oauth.from_address:
                raise ZohoIntegrationError(
                    "SENDER_INVALID", "The requested sender is not the authenticated Zoho mailbox",
                    stage="sender", diagnostic_id=diagnostic_id,
                )
            token, token_source = self.oauth.access_token_with_source()
            passed("token_refresh", source=token_source)
            account_id = self.oauth._account_id()
            if not account_id:
                raise ZohoIntegrationError(
                    "ZOHO_ACCOUNT_LOOKUP_ERROR", "Zoho account ID is missing",
                    stage="account_lookup", diagnostic_id=diagnostic_id,
                )
            passed("account_id")
            passed("sender")
            api_base = self.oauth.mail_api_base_url()
            payload: dict[str, Any] = {
                "fromAddress": sender, "toAddress": ",".join(to), "subject": subject,
                "content": html, "mailFormat": "html", "encoding": "UTF-8",
            }
            if cc:
                payload["ccAddress"] = ",".join(cc)
            if bcc:
                payload["bccAddress"] = ",".join(bcc)
            if attachments:
                payload["attachments"] = [
                    self._upload_attachment(token, account_id, item, trace_id, diagnostic_id)
                    for item in attachments
                ]
            passed("api_request")
            try:
                response = requests.post(
                    f"{api_base}/api/accounts/{account_id}/messages",
                    headers={
                        "Accept": "application/json", "Content-Type": "application/json",
                        "Authorization": f"Zoho-oauthtoken {token}",
                    },
                    json=payload, timeout=30,
                )
            except requests.RequestException as exc:
                raise ZohoIntegrationError(
                    "ZOHO_MAIL_API_ERROR", "Zoho Mail API could not be reached",
                    stage="api_request", diagnostic_id=diagnostic_id,
                ) from exc
            result = self.oauth._json_response(
                response, code="MESSAGE_SUBMISSION_FAILED", stage="message_submission",
                diagnostic_id=diagnostic_id,
            )
            passed("message_submission")
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            provider_id = str(data.get("messageId") or data.get("mailId") or diagnostic_id)
            return {
                "id": f"zoho-{provider_id}", "diagnostic_id": diagnostic_id,
                "stage": "message_submission", "provider": "zoho_mail_api",
                "from": sender, "to": to, "subject": subject, "checks": checks,
            }
        except ZohoIntegrationError as exc:
            logger.error(
                "[%s] email diagnostic_id=%s provider=zoho_mail_api stage=%s result=FAIL error_code=%s exception_class=%s",
                trace_id, diagnostic_id, exc.stage, exc.code, type(exc.__cause__ or exc).__name__,
            )
            raise EmailDeliveryError(
                str(exc), stage=exc.stage, diagnostic_id=diagnostic_id, error_code=exc.code,
            ) from exc

    def _upload_attachment(self, access_token: str, account_id: str, item: dict[str, Any],
                           trace_id: str, diagnostic_id: str) -> dict[str, Any]:
        content = item.get("content", b"")
        if isinstance(content, str):
            content = base64.b64decode(content)
        filename = str(item.get("filename") or "attachment.pdf")
        try:
            response = requests.post(
                f"{self.oauth.mail_api_base_url()}/api/accounts/{account_id}/messages/attachments",
                params={"fileName": filename, "isInline": "false"},
                headers={
                    "Accept": "application/json", "Content-Type": "application/octet-stream",
                    "Authorization": f"Zoho-oauthtoken {access_token}",
                },
                data=content, timeout=30,
            )
        except requests.RequestException as exc:
            raise ZohoIntegrationError(
                "ZOHO_MAIL_API_ERROR", "Zoho attachment upload could not be reached",
                stage="attachment_upload", diagnostic_id=diagnostic_id,
            ) from exc
        payload = self.oauth._json_response(
            response, code="ZOHO_MAIL_API_ERROR", stage="attachment_upload", diagnostic_id=diagnostic_id
        )
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        logger.info(
            "[%s] email diagnostic_id=%s provider=zoho_mail_api stage=attachment_upload result=PASS",
            trace_id, diagnostic_id,
        )
        attachment = {key: data[key] for key in ("storeName", "attachmentPath", "attachmentName") if data.get(key)}
        if len(attachment) != 3:
            raise ZohoIntegrationError(
                "ZOHO_MAIL_API_ERROR", "Zoho attachment upload returned incomplete metadata",
                stage="attachment_upload", diagnostic_id=diagnostic_id,
            )
        return attachment
