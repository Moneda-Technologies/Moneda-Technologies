from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import logging
import re
import secrets
import threading
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

import requests


logger = logging.getLogger(__name__)


SYNCED = "SYNCED"
PENDING = "PENDING"
FAILED = "FAILED"
MISSING_LOCAL = "MISSING_LOCAL"


class WorkDriveError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 stage: str = "unknown", http_status: int | None = None,
                 provider_code: str | None = None, endpoint_host: str | None = None,
                 endpoint_path: str | None = None, diagnostic_id: str | None = None,
                 provider_body: str | None = None, provider_message: str | None = None,
                 endpoint_method: str | None = None, endpoint_resource_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.stage = stage
        self.http_status = http_status
        self.provider_code = provider_code
        self.endpoint_host = endpoint_host
        self.endpoint_path = endpoint_path
        self.provider_body = provider_body
        self.provider_message = provider_message
        self.endpoint_method = endpoint_method
        self.endpoint_resource_id = endpoint_resource_id
        self.diagnostic_id = diagnostic_id or f"workdrive-{secrets.token_hex(6)}"


_REGIONS = {
    "us": ("https://accounts.zoho.com", "https://www.zohoapis.com/workdrive/api/v1"),
    "eu": ("https://accounts.zoho.eu", "https://www.zohoapis.eu/workdrive/api/v1"),
    "in": ("https://accounts.zoho.in", "https://www.zohoapis.in/workdrive/api/v1"),
    "au": ("https://accounts.zoho.com.au", "https://www.zohoapis.com.au/workdrive/api/v1"),
    "jp": ("https://accounts.zoho.jp", "https://www.zohoapis.jp/workdrive/api/v1"),
    "ca": ("https://accounts.zohocloud.ca", "https://www.zohoapis.ca/workdrive/api/v1"),
    "ae": ("https://accounts.zoho.ae", "https://www.zohoapis.ae/workdrive/api/v1"),
    "sa": ("https://accounts.zoho.sa", "https://www.zohoapis.sa/workdrive/api/v1"),
}

_DOWNLOAD_BASES = {
    "us": "https://download.zoho.com", "eu": "https://download.zoho.eu",
    "in": "https://download.zoho.in", "au": "https://download.zoho.com.au",
    "jp": "https://download.zoho.jp", "ca": "https://download.zohocloud.ca",
    "ae": "https://files.zoho.ae", "sa": "https://files.zoho.sa",
}


class WorkDriveService:
    """Private WorkDrive synchronization; local profile files remain authoritative."""

    def __init__(self, config: dict[str, Any], store: Any, *, session: requests.Session | None = None,
                 refresh_token_provider: Callable[[], str] | None = None) -> None:
        self.config = config
        self.store = store
        self.http = session or requests.Session()
        self.refresh_token_provider = refresh_token_provider
        self.timeout = float(config.get("ZOHO_WORKDRIVE_TIMEOUT_SECONDS") or 8)
        self._lock = threading.RLock()
        self._access_token = ""
        self._expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._effective_scopes: tuple[str, ...] = ()

    def enabled(self) -> bool:
        return bool(self.config.get("ZOHO_WORKDRIVE_ENABLED"))

    def configured(self) -> bool:
        return self.configuration_issue() is None

    def configuration_issue(self) -> tuple[str, str] | None:
        """Return a safe, actionable configuration diagnostic, if any."""
        if not self.enabled():
            return "WORKDRIVE_DISABLED", "WorkDrive synchronization is disabled"
        if not self.config.get("ZOHO_WORKDRIVE_CLIENT_ID") or not self.config.get("ZOHO_WORKDRIVE_CLIENT_SECRET"):
            return "WORKDRIVE_OAUTH_CONFIGURATION_ERROR", "WorkDrive OAuth client configuration is incomplete"
        try:
            refresh_token = self._refresh_token()
        except Exception:
            return "WORKDRIVE_AUTHORIZATION_INVALID", "WorkDrive authorization is invalid or unreadable"
        if not refresh_token:
            return "WORKDRIVE_AUTHORIZATION_REQUIRED", "WorkDrive authorization required"
        if not str(self.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID") or "").strip():
            return "WORKDRIVE_ROOT_FOLDER_REQUIRED", "WorkDrive root folder ID is not configured"
        try:
            self.api_base
        except WorkDriveError:
            return "WORKDRIVE_API_CONFIGURATION_ERROR", "WorkDrive API configuration is invalid"
        return None

    def _refresh_token(self) -> str:
        if self.refresh_token_provider:
            return str(self.refresh_token_provider() or "").strip()
        return str(self.config.get("ZOHO_WORKDRIVE_REFRESH_TOKEN") or "").strip()

    def oauth_configuration_metadata(self) -> dict[str, Any]:
        """Return credential presence/source facts without returning credentials."""
        integration = self.store.find_one("integrations", {"_id": "zoho_workdrive"}) if self.store else None
        integration = integration or {}
        if integration.get("refresh_token_encrypted"):
            token_source = "encrypted_mongodb"
        elif integration.get("refresh_token"):
            token_source = "legacy_plaintext_mongodb"
        elif self.config.get("ZOHO_WORKDRIVE_REFRESH_TOKEN"):
            token_source = "environment_fallback"
        else:
            token_source = "missing"
        return {
            "client_id_present": bool(self.config.get("ZOHO_WORKDRIVE_CLIENT_ID")),
            "client_secret_present": bool(self.config.get("ZOHO_WORKDRIVE_CLIENT_SECRET")),
            "refresh_token_present": token_source != "missing",
            "refresh_token_source": token_source,
            "integration_record_present": bool(integration),
            "integration_status": integration.get("status"),
            "account_region": str(self.config.get("ZOHO_WORKDRIVE_ACCOUNT_REGION") or "in").lower(),
            "accounts_host": urlparse(self._region()[0]).hostname,
            "uses_same_client_as_mail": (
                bool(self.config.get("ZOHO_CLIENT_ID"))
                and self.config.get("ZOHO_WORKDRIVE_CLIENT_ID") == self.config.get("ZOHO_CLIENT_ID")
                and self.config.get("ZOHO_WORKDRIVE_CLIENT_SECRET") == self.config.get("ZOHO_CLIENT_SECRET")
            ),
        }

    def _region(self) -> tuple[str, str]:
        region = str(self.config.get("ZOHO_WORKDRIVE_ACCOUNT_REGION") or "in").lower()
        return _REGIONS.get(region, _REGIONS["in"])

    @property
    def api_base(self) -> str:
        configured = str(self.config.get("ZOHO_WORKDRIVE_API_BASE_URL") or "").rstrip("/")
        value = configured or self._region()[1]
        parsed = urlparse(value)
        allowed = {urlparse(item[1]).hostname for item in _REGIONS.values()}
        if parsed.scheme != "https" or parsed.hostname not in allowed or parsed.username or parsed.password:
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive API configuration is invalid")
        return value

    def _token(self, *, force: bool = False) -> str:
        issue = self.configuration_issue()
        if issue:
            raise WorkDriveError(issue[0], issue[1])
        with self._lock:
            now = datetime.now(timezone.utc)
            if not force and self._access_token and now < self._expires_at:
                return self._access_token
            accounts_base = self._region()[0]
            endpoint = f"{accounts_base}/oauth/v2/token"
            parsed_endpoint = urlparse(endpoint)
            try:
                response = self.http.post(endpoint, data={
                    "grant_type": "refresh_token",
                    "client_id": self.config["ZOHO_WORKDRIVE_CLIENT_ID"],
                    "client_secret": self.config["ZOHO_WORKDRIVE_CLIENT_SECRET"],
                    "refresh_token": self._refresh_token(),
                }, timeout=self.timeout)
            except requests.RequestException as exc:
                diagnostic_id = f"workdrive-{secrets.token_hex(6)}"
                logger.warning(
                    "workdrive_token_refresh result=FAIL error_code=WORKDRIVE_TOKEN_REFRESH_UNAVAILABLE "
                    "stage=token_refresh endpoint_host=%s endpoint_path=%s exception=%s diagnostic_id=%s",
                    parsed_endpoint.hostname, parsed_endpoint.path, type(exc).__name__, diagnostic_id,
                )
                raise WorkDriveError(
                    "WORKDRIVE_TOKEN_REFRESH_UNAVAILABLE",
                    "WorkDrive authentication service is temporarily unavailable",
                    retryable=True, stage="token_refresh",
                    endpoint_host=parsed_endpoint.hostname, endpoint_path=parsed_endpoint.path,
                    diagnostic_id=diagnostic_id,
                ) from exc
            if response.status_code != 200:
                provider_code, provider_message = self._safe_provider_error(response)
                provider_body = self._safe_provider_body(response)
                metadata = self.oauth_configuration_metadata()
                diagnostic_id = f"workdrive-{secrets.token_hex(6)}"
                logger.warning(
                    "workdrive_token_refresh result=FAIL error_code=WORKDRIVE_TOKEN_REFRESH_FAILED "
                    "stage=token_refresh http_status=%s provider_code=%s provider_message=%s "
                    "provider_body=%s endpoint_host=%s endpoint_path=%s client_id_present=%s "
                    "client_secret_present=%s refresh_token_present=%s refresh_token_source=%s "
                    "integration_record_present=%s integration_status=%s account_region=%s "
                    "uses_same_client_as_mail=%s diagnostic_id=%s",
                    response.status_code, provider_code or "unknown", provider_message or "unknown",
                    provider_body, parsed_endpoint.hostname, parsed_endpoint.path,
                    metadata["client_id_present"], metadata["client_secret_present"],
                    metadata["refresh_token_present"], metadata["refresh_token_source"],
                    metadata["integration_record_present"], metadata["integration_status"],
                    metadata["account_region"], metadata["uses_same_client_as_mail"], diagnostic_id,
                )
                raise WorkDriveError(
                    "WORKDRIVE_TOKEN_REFRESH_FAILED",
                    "WorkDrive authentication failed. Reconnect the WorkDrive integration.",
                    stage="token_refresh", http_status=response.status_code,
                    provider_code=provider_code, endpoint_host=parsed_endpoint.hostname,
                    endpoint_path=parsed_endpoint.path, diagnostic_id=diagnostic_id,
                    provider_message=provider_message, provider_body=provider_body,
                    endpoint_method="POST",
                )
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise WorkDriveError(
                    "WORKDRIVE_TOKEN_RESPONSE_INVALID",
                    "WorkDrive authentication returned an unexpected response.",
                    stage="response_validation", http_status=response.status_code,
                    endpoint_host=parsed_endpoint.hostname, endpoint_path=parsed_endpoint.path,
                ) from exc
            token = str(payload.get("access_token") or "")
            if not token:
                raise WorkDriveError(
                    "WORKDRIVE_TOKEN_RESPONSE_INVALID",
                    "WorkDrive authentication returned an unexpected response.",
                    stage="response_validation", http_status=response.status_code,
                    endpoint_host=parsed_endpoint.hostname, endpoint_path=parsed_endpoint.path,
                )
            self._access_token = token
            raw_scopes = payload.get("scope") or ""
            if isinstance(raw_scopes, str):
                self._effective_scopes = tuple(sorted(set(raw_scopes.replace(",", " ").split())))
            self._expires_at = now + timedelta(seconds=max(60, int(payload.get("expires_in") or 3600) - 60))
            return token

    def effective_scopes(self, *, refresh: bool = False) -> tuple[str, ...]:
        """Return only non-sensitive scope metadata from the current grant."""
        self._token(force=refresh)
        return self._effective_scopes

    def download_asset(self, resource_id: str) -> bytes:
        """Download one authenticated profile asset from the regional endpoint."""
        resource_id = str(resource_id or "").strip()
        if not resource_id:
            raise WorkDriveError("RESOURCE_ID_REQUIRED", "WorkDrive asset mapping is missing")
        region = str(self.config.get("ZOHO_WORKDRIVE_ACCOUNT_REGION") or "in").lower()
        default_base = _DOWNLOAD_BASES.get(region, _DOWNLOAD_BASES["in"])
        endpoint = f"{default_base}/v1/workdrive/download/{resource_id}"
        try:
            metadata_response = self._request("GET", f"files/{resource_id}")
            items = self._items(metadata_response.json())
            supplied = str(((items[0].get("attributes") or {}).get("download_url") if items else "") or "").strip()
            if supplied:
                parsed_supplied = urlparse(supplied)
                expected_host = urlparse(default_base).hostname or ""
                if parsed_supplied.scheme == "https" and parsed_supplied.hostname and parsed_supplied.hostname.endswith(expected_host.removeprefix("download.")):
                    endpoint = supplied
        except WorkDriveError:
            pass
        try:
            response = self.http.get(endpoint, headers={
                "Authorization": f"Zoho-oauthtoken {self._token()}",
                "Accept": "application/octet-stream",
            }, timeout=self.timeout)
        except requests.RequestException as exc:
            raise WorkDriveError("WORKDRIVE_API_UNAVAILABLE", "WorkDrive asset download is temporarily unavailable", retryable=True, stage="asset_download", endpoint_host=urlparse(endpoint).hostname, endpoint_path="/v1/workdrive/download/[RESOURCE_ID]") from exc
        if response.status_code != 200:
            provider_code, _ = self._safe_provider_error(response)
            raise WorkDriveError("WORKDRIVE_ASSET_DOWNLOAD_FAILED", "WorkDrive asset download failed", stage="asset_download", http_status=response.status_code, provider_code=provider_code, endpoint_host=urlparse(endpoint).hostname, endpoint_path="/v1/workdrive/download/[RESOURCE_ID]")
        if not response.content:
            raise WorkDriveError("WORKDRIVE_ASSET_EMPTY", "WorkDrive returned an empty asset", stage="asset_download", http_status=response.status_code, endpoint_host=urlparse(endpoint).hostname, endpoint_path="/v1/workdrive/download/[RESOURCE_ID]")
        return response.content

    def diagnose_access(self) -> dict[str, Any]:
        """Return safe identity/access diagnostics; never returns OAuth credentials."""
        issue = self.configuration_issue()
        if issue:
            raise WorkDriveError(issue[0], issue[1], stage="configuration")
        self._token(force=True)
        scopes = list(self._effective_scopes)
        root_id = str(self.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID") or "")
        team_folder_id = str(self.config.get("ZOHO_WORKDRIVE_TEAM_FOLDER_ID") or "")
        account_email = None
        account_identifier = None
        team_folder_name = None
        member_role = None
        membership_active = None
        membership_status = "unknown"
        membership_reason = None
        membership_scope = "WorkDrive.teamfolders.READ"
        provider_membership_error = None
        if "WorkDrive.teamfolders.READ" not in scopes:
            membership_status = "unknown"
            membership_reason = "Required scope WorkDrive.teamfolders.READ is not granted. Reconnect WorkDrive."
        else:
            try:
                team_response = self._request("GET", f"teamfolders/{team_folder_id}")
                team_items = self._items(team_response.json())
                team_attributes = (team_items[0].get("attributes") or {}) if team_items else {}
                team_folder_name = team_attributes.get("name") or team_attributes.get("display_html_name")
                team_id = str(team_attributes.get("team_id") or team_attributes.get("parent_id") or "")
                if "WorkDrive.team.READ" in scopes and team_id:
                    current_response = self._request("GET", f"teams/{team_id}/currentuser")
                    current_items = self._items(current_response.json())
                    current = current_items[0] if current_items else {}
                    current_attributes = current.get("attributes") or {}
                    account_email = current_attributes.get("email_id") or None
                    account_identifier = current.get("id") or None
                members_response = self._request("GET", f"teamfolders/{team_folder_id}/members")
                members = self._items(members_response.json())
                if account_email:
                    for member in members:
                        attrs = member.get("attributes") or {}
                        member_email = attrs.get("email_id") or member.get("email_id")
                        if member_email and str(member_email).lower() == str(account_email).lower():
                            member_role = attrs.get("role_id") or member.get("role_id")
                            membership_active = not bool(attrs.get("is_expired") or member.get("is_expired"))
                            membership_status = "member" if membership_active else "inactive"
                            break
                    else:
                        membership_status = "not_member"
                else:
                    membership_status = "unknown"
                    membership_reason = "Zoho did not return the authenticated Team Folder member identity."
            except WorkDriveError as exc:
                membership_status = "unknown"
                membership_reason = "Zoho did not allow Team Folder identity or membership lookup."
                provider_membership_error = exc.provider_code
            else:
                provider_membership_error = None
        identity_available = bool(account_email or account_identifier)
        try:
            self.test_connection()
            resource_access = "accessible"
            provider_code = None
        except WorkDriveError as exc:
            resource_access = "denied" if exc.code == "ROOT_FOLDER_ACCESS_DENIED" else "error"
            provider_code = exc.provider_code
        if membership_status == "not_member":
            diagnostic_reason = "The authenticated WorkDrive account is not a member of the configured private Team Folder."
        elif membership_status == "inactive":
            diagnostic_reason = "The authenticated WorkDrive account has an inactive Team Folder membership."
        elif membership_status == "unknown":
            diagnostic_reason = membership_reason or "Team Folder membership could not be determined."
        elif provider_code == "R008":
            diagnostic_reason = "The account is identified, but Zoho denied access to the configured private root folder."
        else:
            diagnostic_reason = None
        return {
            "account": {"email": account_email, "identifier": account_identifier, "identity_available": identity_available},
            "authenticated_account_email": account_email,
            "team_folder_id": team_folder_id,
            "team_folder_name": team_folder_name,
            "root_folder_id": root_id,
            "effective_scopes": scopes,
            "resource_access": resource_access,
            "membership": {
                "status": membership_status,
                "required_scope": membership_scope,
                "active": membership_active,
                "role": member_role,
                "reason": membership_reason,
            },
            "provider_error_code": provider_code or locals().get("provider_membership_error"),
            "reason": diagnostic_reason,
        }

    def diagnose_resource_access(self, resources: list[dict[str, str]]) -> dict[str, Any]:
        """Read only the supplied WorkDrive resources and report safe hierarchy facts.

        This deliberately uses ``GET files/{id}`` only.  It is suitable for
        validating persisted mappings after a reconnect without creating a
        folder, listing a parent, or uploading a document.
        """
        self._token(force=True)
        inspected: list[dict[str, Any]] = []
        for resource in resources:
            resource_id = str(resource.get("resource_id") or "").strip()
            if not resource_id:
                continue
            label = str(resource.get("label") or "resource")
            expected_parent_id = str(resource.get("expected_parent_id") or "").strip() or None
            try:
                response = self._request("GET", f"files/{resource_id}")
                items = self._items(response.json())
                item = items[0] if items else {}
                attributes = item.get("attributes") or {}
                returned_id = str(item.get("id") or "")
                parent_id = str(attributes.get("parent_id") or "") or None
                resource_type = str(attributes.get("type") or "") or None
                inspected.append({
                    "label": label, "resource_id": resource_id, "status": "accessible",
                    "returned_id": returned_id or None,
                    "id_matches": returned_id == resource_id,
                    "name": attributes.get("name") or attributes.get("display_html_name"),
                    "parent_id": parent_id, "expected_parent_id": expected_parent_id,
                    "parent_matches": parent_id == expected_parent_id if expected_parent_id else None,
                    "resource_type": resource_type,
                    "is_folder": bool(attributes.get("is_folder")) or resource_type.lower() == "folder",
                })
            except WorkDriveError as exc:
                inspected.append({
                    "label": label, "resource_id": resource_id, "status": "denied" if exc.code == "RESOURCE_ACCESS_DENIED" else "error",
                    "error_code": exc.code, "http_status": exc.http_status,
                    "provider_code": exc.provider_code, "provider_message": exc.provider_message,
                    "endpoint_method": exc.endpoint_method, "endpoint_host": exc.endpoint_host,
                    "endpoint_path": exc.endpoint_path, "endpoint_resource_id": exc.endpoint_resource_id,
                    "diagnostic_id": exc.diagnostic_id,
                })
                logger.warning(
                    "workdrive_resource_diagnostic result=%s label=%s resource_id=%s error_code=%s http_status=%s provider_code=%s provider_message=%s endpoint_method=%s endpoint_host=%s endpoint_path=%s endpoint_resource_id=%s diagnostic_id=%s",
                    "DENIED" if exc.code == "RESOURCE_ACCESS_DENIED" else "ERROR", label, resource_id,
                    exc.code, exc.http_status, exc.provider_code, exc.provider_message,
                    exc.endpoint_method, exc.endpoint_host, exc.endpoint_path,
                    exc.endpoint_resource_id, exc.diagnostic_id,
                )
        return {
            "api_host": urlparse(self.api_base).hostname,
            "account_region": str(self.config.get("ZOHO_WORKDRIVE_ACCOUNT_REGION") or "in").lower(),
            "effective_scopes": list(self._effective_scopes),
            "resources": inspected,
        }

    def diagnose_child_folder(self, parent_id: str, expected_name: str) -> dict[str, Any]:
        """Inspect an immediate-child listing with cursor pagination and no writes."""
        parent_id = str(parent_id or "").strip()
        expected_name = str(expected_name or "").strip()
        if not parent_id or not expected_name:
            raise WorkDriveError("DIAGNOSTIC_INPUT_INVALID", "Folder diagnostic requires a parent ID and folder name")
        self._token(force=True)
        # `allfiles` excludes folders.  This diagnostic resolves a quotation
        # *folder*, so request `all` to mirror the WorkDrive UI hierarchy.
        params: dict[str, Any] = {"page[next]": "0", "page[limit]": 50, "filter[type]": "all"}
        matches: list[dict[str, Any]] = []
        children: list[dict[str, Any]] = []
        response_statuses: list[int] = []
        response_keys: list[list[str]] = []
        data_shapes: list[str] = []
        pagination: list[dict[str, Any]] = []
        request_pages: list[dict[str, Any]] = []
        pages = 0
        seen_tokens: set[str] = set()
        while pages < 200:
            try:
                request_pages.append({
                    "method": "GET",
                    "endpoint_host": urlparse(self.api_base).hostname,
                    "endpoint_path": f"/workdrive/api/v1/files/{parent_id}/files",
                    "query": dict(params),
                    "headers": {"Accept": "application/vnd.api+json", "Authorization": "[REDACTED]"},
                })
                response = self._request("GET", f"files/{parent_id}/files", params=params)
                payload = response.json()
                response_statuses.append(int(response.status_code))
            except WorkDriveError as exc:
                return {
                    "parent_id": parent_id, "expected_name": expected_name,
                    "pages_scanned": pages, "complete": False, "matches": matches,
                    "children": children, "child_count": len(children),
                    "response_statuses": response_statuses,
                    "response_structural_keys": response_keys,
                    "data_shapes": data_shapes, "pagination": pagination,
                    "requests": request_pages,
                    "error": {"code": exc.code, "http_status": exc.http_status,
                              "provider_code": exc.provider_code, "provider_message": exc.provider_message,
                              "diagnostic_id": exc.diagnostic_id, "endpoint_method": exc.endpoint_method,
                              "endpoint_host": exc.endpoint_host, "endpoint_path": exc.endpoint_path,
                              "endpoint_resource_id": exc.endpoint_resource_id},
                }
            pages += 1
            if isinstance(payload, dict):
                response_keys.append(sorted(str(key) for key in payload.keys()))
                raw_data = payload.get("data")
                data_shapes.append("list" if isinstance(raw_data, list) else "object" if isinstance(raw_data, dict) else type(raw_data).__name__)
            else:
                response_keys.append([])
                data_shapes.append(type(payload).__name__)
            for item in self._items(payload):
                attributes = item.get("attributes") or {}
                name = str(attributes.get("name") or attributes.get("display_html_name") or "")
                is_folder = bool(attributes.get("is_folder")) or str(attributes.get("type") or "").lower() == "folder"
                child = {
                    "resource_id": str(item.get("id") or "") or None,
                    "name": name or None,
                    "resource_type": str(attributes.get("type") or "") or None,
                    "parent_id": str(attributes.get("parent_id") or "") or None,
                    "is_folder": is_folder,
                }
                children.append(child)
                if name == expected_name:
                    actual_parent_id = str(attributes.get("parent_id") or "") or None
                    matches.append({
                        **child,
                        "parent_matches": actual_parent_id == parent_id,
                    })
            cursor = ((payload.get("links") or {}).get("cursor") or {}) if isinstance(payload, dict) else {}
            pagination.append({
                "has_next": bool(cursor.get("has_next")),
                "next_present": bool(cursor.get("next")),
                "response_link_keys": sorted(str(key) for key in ((payload.get("links") or {}).keys() if isinstance(payload, dict) and isinstance(payload.get("links"), dict) else [])),
            })
            if not cursor.get("has_next"):
                return {"parent_id": parent_id, "expected_name": expected_name, "pages_scanned": pages,
                        "complete": True, "matches": matches, "children": children,
                        "child_count": len(children), "response_statuses": response_statuses,
                        "response_structural_keys": response_keys, "data_shapes": data_shapes,
                        "pagination": pagination, "requests": request_pages}
            next_url = str(cursor.get("next") or "")
            parsed_next = urlparse(next_url)
            token = (parse_qs(parsed_next.query).get("page[next]") or [""])[0]
            expected_suffix = f"/files/{parent_id}/files"
            # Zoho may return a cursor URL with a generic WorkDrive hostname.
            # Keep the configured regional API base for the next request, but
            # accept the opaque page token only when it still targets this
            # exact parent listing endpoint.
            if parsed_next.scheme != "https" or not parsed_next.hostname or not parsed_next.path.endswith(expected_suffix) or not token or token in seen_tokens:
                return {"parent_id": parent_id, "expected_name": expected_name, "pages_scanned": pages,
                        "complete": False, "matches": matches, "children": children,
                        "child_count": len(children), "response_statuses": response_statuses,
                        "response_structural_keys": response_keys, "data_shapes": data_shapes,
                        "pagination": pagination, "requests": request_pages,
                        "pagination_error": "INVALID_CURSOR"}
            seen_tokens.add(token)
            params = {"page[next]": token, "page[limit]": 50, "filter[type]": "all"}
        return {"parent_id": parent_id, "expected_name": expected_name, "pages_scanned": pages,
                "complete": False, "matches": matches, "children": children,
                "child_count": len(children), "response_statuses": response_statuses,
                "response_structural_keys": response_keys, "data_shapes": data_shapes,
                "pagination": pagination, "requests": request_pages, "pagination_error": "PAGE_LIMIT_REACHED"}

    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update({"Authorization": f"Zoho-oauthtoken {self._token()}", "Accept": "application/vnd.api+json"})
        endpoint = f"{self.api_base}/{path.lstrip('/')}"
        parsed_endpoint = urlparse(endpoint)
        safe_path = self._safe_endpoint_path(parsed_endpoint.path)
        try:
            response = self.http.request(method, endpoint, headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            code = "WORKDRIVE_API_TIMEOUT" if isinstance(exc, requests.Timeout) else "WORKDRIVE_API_UNAVAILABLE"
            message = "WorkDrive API request timed out." if code == "WORKDRIVE_API_TIMEOUT" else "WorkDrive is temporarily unavailable."
            raise WorkDriveError(
                code, message, retryable=True, stage="api_request",
                endpoint_host=parsed_endpoint.hostname, endpoint_path=safe_path,
            ) from exc
        if response.status_code == 401 and retry_auth:
            self._token(force=True)
            return self._request(method, path, retry_auth=False, **kwargs)
        if response.status_code in {200, 201, 202, 204}:
            return response
        provider_code, provider_message = self._safe_provider_error(response)
        resource_id = self._endpoint_resource_id(path)
        code = {
            401: "AUTHENTICATION_ERROR", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
            409: "NAME_CONFLICT", 429: "RATE_LIMITED",
        }.get(response.status_code, "SERVICE_ERROR")
        # Zoho's R008 is returned for a valid OAuth grant which cannot access
        # the requested WorkDrive resource.  Calling it an authentication
        # failure sends operators toward an unnecessary reconnect and hides the
        # actual root/folder membership problem.
        if response.status_code == 401 and provider_code == "R008":
            code = "RESOURCE_ACCESS_DENIED"
        retryable = response.status_code == 429 or response.status_code >= 500
        raise WorkDriveError(
            code, "WorkDrive synchronization could not be completed", retryable=retryable,
            stage="api_request", http_status=response.status_code, provider_code=provider_code,
            endpoint_host=parsed_endpoint.hostname, endpoint_path=safe_path,
            provider_body=self._safe_provider_body(response),
            provider_message=provider_message,
            endpoint_method=method.upper(), endpoint_resource_id=resource_id,
        )

    @staticmethod
    def _safe_endpoint_path(path: str) -> str:
        parts = path.split("/")
        if "files" in parts:
            index = parts.index("files")
            if len(parts) > index + 1 and parts[index + 1]:
                parts[index + 1] = "[RESOURCE_ID]"
        return "/".join(parts)

    @staticmethod
    def _endpoint_resource_id(path: str) -> str | None:
        parts = [part for part in path.strip("/").split("/") if part]
        try:
            index = parts.index("files")
        except ValueError:
            return None
        if len(parts) > index + 1 and parts[index + 1]:
            return parts[index + 1]
        return None

    @staticmethod
    def _safe_provider_error(response: requests.Response) -> tuple[str | None, str | None]:
        """Extract only documented provider error identifiers and short titles."""
        try:
            payload = response.json()
        except (TypeError, ValueError):
            return None, None
        if not isinstance(payload, dict):
            return None, None
        errors = payload.get("errors")
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        code = first.get("id") or first.get("code") or payload.get("error")
        title = first.get("title") or first.get("detail") or payload.get("error_description")
        return (str(code)[:80] if code else None, str(title)[:200] if title else None)

    @staticmethod
    def _safe_provider_body(response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, (dict, list)):
                blocked = {"access_token", "refresh_token", "client_secret", "authorization", "cookie", "set-cookie"}

                def sanitize(value: Any) -> Any:
                    if isinstance(value, dict):
                        return {str(key): "[REDACTED]" if str(key).lower() in blocked else sanitize(item)
                                for key, item in value.items()}
                    if isinstance(value, list):
                        return [sanitize(item) for item in value]
                    return value

                return str(sanitize(payload))[:1000]
        except (TypeError, ValueError):
            pass
        return "<non-json provider response>"

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return data if isinstance(data, list) else [data] if isinstance(data, dict) else []

    def _find_child(self, parent_id: str, name: str, *, folder: bool | None = None) -> dict[str, Any] | None:
        # A category can contain more than 50 documents. Looking at only the
        # first page made a pre-existing quotation folder look absent, so
        # WorkDrive created a duplicate and decorated its name with a timestamp.
        page_size = 50
        for offset in range(0, 10_000, page_size):
            response = self._request("GET", f"files/{parent_id}/files", params={
                "page[limit]": page_size, "page[offset]": offset, "filter[type]": "all",
            })
            items = self._items(response.json())
            for item in items:
                attributes = item.get("attributes") or {}
                item_name = str(attributes.get("name") or attributes.get("display_html_name") or "")
                is_folder = bool(attributes.get("is_folder")) or str(attributes.get("type") or "").lower() == "folder"
                if item_name == name and (folder is None or folder == is_folder):
                    return item
            if len(items) < page_size:
                break
        return None

    def _folder(self, parent_id: str, name: str) -> str:
        existing = self._find_child(parent_id, name, folder=True)
        if existing:
            return str(existing.get("id"))
        payload = {"data": {"attributes": {"name": name, "parent_id": parent_id}, "type": "files"}}
        try:
            response = self._request("POST", "files", json=payload, headers={"Content-Type": "application/vnd.api+json"})
            items = self._items(response.json())
            if items and items[0].get("id"):
                return str(items[0]["id"])
        except WorkDriveError as exc:
            if exc.code != "NAME_CONFLICT":
                raise
        existing = self._find_child(parent_id, name, folder=True)
        if not existing:
            raise WorkDriveError("FOLDER_CREATE_FAILED", "WorkDrive profile folder could not be prepared")
        return str(existing["id"])

    @staticmethod
    def _user_folder_name(user: dict[str, Any]) -> str:
        name = " ".join(str(user.get("name") or "").split()) or "Unnamed User"
        email = str(user.get("email") or "").strip().lower() or str(user.get("_id") or "unknown")
        return f"{name} — {email}"

    def migrate_legacy_user_folder(self, user: dict[str, Any], legacy_folder_id: str) -> tuple[dict[str, str], dict[str, Any]]:
        """Create a canonical mapping without modifying the legacy folder tree.

        The explicit legacy-ID guard limits this to a controlled one-user
        migration. Name-based folder lookup makes retries idempotent.
        """
        current_folder_id = str(user.get("workdrive_user_folder_id") or "").strip()
        expected_legacy_id = str(legacy_folder_id or "").strip()
        if not expected_legacy_id or current_folder_id != expected_legacy_id:
            raise WorkDriveError(
                "LEGACY_FOLDER_MISMATCH",
                "The stored WorkDrive user folder does not match the expected legacy mapping",
            )
        root_folder_id = str(self.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID") or "").strip()
        if not root_folder_id:
            raise WorkDriveError("WORKDRIVE_ROOT_FOLDER_REQUIRED", "A WorkDrive root folder ID is required")

        folders = {"user": self._folder(root_folder_id, self._user_folder_name(user))}
        for key, label in (("profile", "Profile"), ("quotes", "Quotes"), ("orders", "Orders")):
            folders[key] = self._folder(folders["user"], label)
        changes = {"workdrive_user_folder_id": folders["user"]}
        changes.update({f"workdrive_{key}_folder_id": folders[key] for key in ("profile", "quotes", "orders")})
        return folders, changes

    def ensure_user_folders(self, user: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        """Prepare the stable Users/<identity>/{Profile,Quotes,Orders} tree.

        Existing stored folder IDs remain authoritative so renaming a user does
        not create a second folder or migrate/delete existing WorkDrive data.
        """
        user_id = str(user.get("_id") or "").strip()
        if not user_id:
            raise WorkDriveError("INVALID_USER", "A stable user identifier is required")
        changes: dict[str, Any] = {}
        user_folder_id = str(user.get("workdrive_user_folder_id") or "")
        if not user_folder_id:
            user_folder_id = self._folder(str(self.config["ZOHO_WORKDRIVE_ROOT_FOLDER_ID"]), self._user_folder_name(user))
            changes["workdrive_user_folder_id"] = user_folder_id
        folder_ids = {"user": user_folder_id}
        for key, label in (("profile", "Profile"), ("quotes", "Quotes"), ("orders", "Orders")):
            field = f"workdrive_{key}_folder_id"
            folder_ids[key] = str(user.get(field) or "")
            if not folder_ids[key]:
                folder_ids[key] = self._folder(user_folder_id, label)
                changes[field] = folder_ids[key]
        return folder_ids, changes

    def ensure_profile_folder(self, user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        user_id = str(user.get("_id") or "").strip()
        if not user_id:
            raise WorkDriveError("INVALID_USER", "A stable user identifier is required")
        changes: dict[str, Any] = {}
        folders, changes = self.ensure_user_folders(user)
        return folders["profile"], changes

    def ensure_document_folder(self, user: dict[str, Any], document_type: str,
                               document_number: str, *, existing_folder_id: str = "") -> tuple[str, dict[str, Any]]:
        """Return Users/<identity>/{Quotes|Orders}/<number> without duplicating folders."""
        if document_type not in {"quote", "order"}:
            raise WorkDriveError("INVALID_DOCUMENT_TYPE", "Unsupported WorkDrive document type")
        folders, changes = self.ensure_user_folders(user)
        parent = folders["quotes" if document_type == "quote" else "orders"]
        return str(existing_folder_id or "").strip() or self._folder(parent, str(document_number).strip()), changes

    def sync_asset(self, user: dict[str, Any], asset: str, local_path: Path, mime_type: str) -> dict[str, Any]:
        if asset not in {"photo", "signature"}:
            raise WorkDriveError("INVALID_ASSET", "Unsupported profile asset")
        if not self.configured():
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive synchronization is not fully configured")
        folder_id, changes = self.ensure_profile_folder(user)
        filename = f"{asset}{local_path.suffix.lower()}"
        with local_path.open("rb") as content:
            response = self._request("POST", "upload", data={
                "filename": filename, "parent_id": folder_id, "override-name-exist": "true",
            }, files={"content": (filename, content, mime_type)})
        items = self._items(response.json())
        item = items[0] if items else {}
        resource_id = str(item.get("id") or (item.get("attributes") or {}).get("resource_id") or "")
        if not resource_id:
            raise WorkDriveError("UPLOAD_FAILED", "WorkDrive did not return a file identifier", retryable=True)
        now = datetime.now(timezone.utc)
        changes.update({
            f"{asset}_workdrive_resource_id": resource_id,
            f"{asset}_workdrive_path": f"{user.get('_id')}/Profile/{filename}",
            f"{asset}_workdrive_filename": filename,
            f"{asset}_workdrive_updated_at": now,
            f"{asset}_workdrive_sync_status": SYNCED,
            f"{asset}_workdrive_sync_error": None,
        })
        return changes

    def sync_company_asset(self, asset: dict[str, Any], upload_directory: str | Path) -> dict[str, Any]:
        """Upload one local company asset into an isolated Companies tree."""
        if not self.configured():
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive synchronization is not fully configured")
        customer_id = str(asset.get("customer_id") or "").strip()
        if not customer_id:
            raise WorkDriveError("INVALID_COMPANY", "A stable company identifier is required")
        # Never derive the Companies root from the general WorkDrive root or
        # from an asset's historical mapping.  Both can be stale (and the
        # former has previously resolved to Users in production).  A live,
        # verified sibling root must be explicitly configured before any
        # company-folder creation is allowed.
        companies_root = str(self.config.get("ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID") or "").strip()
        if not companies_root:
            raise WorkDriveError(
                "WORKDRIVE_COMPANIES_ROOT_REQUIRED",
                "A verified Companies root folder ID is required; automatic Companies folder creation is disabled",
            )
        company_name = " ".join(str(asset.get("company_name") or "").split())
        if not company_name:
            customer = self.store.find_one("customers", {"_id": customer_id}) or {}
            company_name = " ".join(str(customer.get("company_name") or customer.get("name") or "").split())
        if not company_name:
            raise WorkDriveError("INVALID_COMPANY", "A canonical company name is required")
        # Resolve the complete canonical tree under one lock.  The lookup is
        # exact-name/idempotent and prevents concurrent uploads from racing
        # into duplicate company or category folders.
        with self._lock:
            company = str(asset.get("workdrive_company_folder_id") or "").strip()
            if not company:
                customer = self.store.find_one("customers", {"_id": customer_id}) or {}
                company = str(customer.get("workdrive_company_folder_id") or "").strip()
            if not company:
                # Preserve and reuse a valid mapping from an earlier asset
                # while customers are upgraded to the customer-level field.
                prior_assets, _ = self.store.list(
                    "company_assets", {"customer_id": customer_id}, limit=50,
                    sort="updated_at", direction=-1,
                )
                company = next(
                    (str(item.get("workdrive_company_folder_id") or "").strip()
                     for item in prior_assets
                     if str(item.get("workdrive_company_folder_id") or "").strip()),
                    "",
                )
            if not company:
                company = self._folder(companies_root, self._company_folder_name(customer_id, company_name))
            category = str(asset.get("category") or "document")
            label = {"logo": "Logo", "visiting_card": "Visiting Cards", "document": "Documents"}.get(category, "Documents")
            folder_id = self._folder(company, label)
        path = (Path(upload_directory).resolve() / str(asset.get("storage_path") or "")).resolve()
        try:
            path.relative_to(Path(upload_directory).resolve())
        except ValueError:
            raise WorkDriveError("INVALID_LOCAL_PATH", "Company asset path is invalid")
        if not path.is_file():
            raise WorkDriveError("LOCAL_FILE_MISSING", "Company asset file is missing")
        filename = str(asset.get("filename") or path.name)
        cardholder = " ".join(str(asset.get("cardholder_name") or "").split())
        if category == "visiting_card":
            if not cardholder:
                raise WorkDriveError("CARDHOLDER_NAME_REQUIRED", "A visiting-card cardholder name is required")
            filename = self._next_company_asset_filename(folder_id, self._cardholder_filename(cardholder, filename))
        existing = self._find_child(folder_id, filename, folder=False)
        if existing and existing.get("id"):
            return {"workdrive_company_root_id": companies_root, "workdrive_company_folder_id": company, "workdrive_company_name": company_name, "workdrive_folder_id": folder_id, "workdrive_resource_id": str(existing["id"]), "workdrive_filename": filename, "sync_status": SYNCED, "sync_error": None}
        with path.open("rb") as content:
            response = self._request("POST", "upload", data={"filename": filename, "parent_id": folder_id, "override-name-exist": "false"}, files={"content": (filename, content, str(asset.get("mime_type") or "application/octet-stream"))})
        items = self._items(response.json())
        item = items[0] if items else {}
        resource_id = str(item.get("id") or (item.get("attributes") or {}).get("resource_id") or "")
        if not resource_id:
            raise WorkDriveError("UPLOAD_FAILED", "WorkDrive did not return a company asset identifier", retryable=True)
        return {"workdrive_company_root_id": companies_root, "workdrive_company_folder_id": company, "workdrive_company_name": company_name, "workdrive_folder_id": folder_id, "workdrive_resource_id": resource_id, "workdrive_filename": filename, "sync_status": SYNCED, "sync_error": None}

    @staticmethod
    def _cardholder_filename(cardholder_name: str, original_filename: str) -> str:
        """Produce a safe, readable remote filename without changing local bytes."""
        stem = re.sub(r"[\\/:*?\"<>|]+", " ", cardholder_name)
        stem = re.sub(r"\s+", " ", stem).strip(" .")
        stem = "".join(char for char in stem if char.isalnum() or char in " ._-()")[:160].strip(" .") or "Visiting Card"
        extension = Path(original_filename).suffix
        return f"{stem}{extension}"[:180]

    def _next_company_asset_filename(self, folder_id: str, requested_filename: str) -> str:
        """Avoid overwriting a cardholder's existing file with a predictable suffix."""
        suffix = Path(requested_filename).suffix
        stem = requested_filename[:-len(suffix)] if suffix else requested_filename
        candidate = requested_filename
        index = 2
        while self._find_child(folder_id, candidate, folder=False):
            candidate = f"{stem} ({index}){suffix}"
            index += 1
        return candidate

    def ensure_customer_company_folder(self, customer: dict[str, Any]) -> dict[str, Any]:
        """Prepare one stable company root and its long-lived category folders."""
        if not self.configured():
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive synchronization is not fully configured")
        customer_id = str(customer.get("_id") or customer.get("customer_id") or "").strip()
        company_name = " ".join(str(customer.get("company_name") or customer.get("name") or "").split())
        companies_root = str(self.config.get("ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID") or "").strip()
        if not customer_id or not company_name:
            raise WorkDriveError("INVALID_COMPANY", "A stable company identifier and name are required")
        if not companies_root:
            raise WorkDriveError("WORKDRIVE_COMPANIES_ROOT_REQUIRED", "A verified Companies root folder ID is required")
        with self._lock:
            company = str(customer.get("workdrive_company_folder_id") or "").strip()
            if not company:
                company = self._folder(companies_root, self._company_folder_name(customer_id, company_name))
            return {
                "workdrive_company_root_id": companies_root,
                "workdrive_company_folder_id": company,
                "workdrive_company_name": company_name,
                "workdrive_company_logo_folder_id": self._folder(company, "Logo"),
                "workdrive_company_visiting_cards_folder_id": self._folder(company, "Visiting Cards"),
                "workdrive_company_quotations_folder_id": self._folder(company, "Quotations"),
                "workdrive_company_orders_folder_id": self._folder(company, "Orders"),
            }

    def _company_folder_name(self, customer_id: str, company_name: str) -> str:
        """Keep legacy readable names, but never merge two customers by name."""
        customers, _ = self.store.list("customers", {"$and": [
            {"_id": {"$ne": customer_id}},
            {"$or": [{"company_name": company_name}, {"name": company_name}]},
        ]}, limit=100)
        if any(str(item.get("workdrive_company_folder_id") or "").strip() for item in customers):
            return f"{company_name} — {customer_id[:8]}"
        return company_name

    def _mapped_document_folder(self, document_id: str, field: str) -> str:
        """Return a previously persisted folder for this logical document.

        Version IDs are immutable, so a later revision needs to look across
        all versions for the folder created for the original document.
        """
        rows, _ = self.store.list(
            "workdrive_document_versions", {"document_id": document_id},
            limit=100, sort="updated_at", direction=-1,
        )
        for row in rows:
            folder_id = str(row.get(field) or "").strip()
            if folder_id:
                return folder_id
        return ""

    def _resolve_document_folder(self, parent_id: str, document_id: str, document_number: str,
                                 mapping_fields: tuple[str, ...]) -> str:
        """Resolve a document folder without trusting a stale mapping blindly.

        Earlier archive rows used ``workdrive_folder_id`` for the user copy.
        Later rows use destination-specific fields.  Verify every persisted
        candidate against the expected parent and exact document number before
        reusing it; a WorkDrive auto-renamed duplicate must never become the
        canonical folder for subsequent revisions.
        """
        rows, _ = self.store.list(
            "workdrive_document_versions", {"document_id": document_id},
            limit=100, sort="updated_at", direction=-1,
        )
        seen: set[str] = set()
        for field in mapping_fields:
            for row in rows:
                folder_id = str(row.get(field) or "").strip()
                if not folder_id or folder_id in seen:
                    continue
                seen.add(folder_id)
                try:
                    response = self._request("GET", f"files/{folder_id}")
                except WorkDriveError as exc:
                    if exc.code == "NOT_FOUND":
                        continue
                    raise
                items = self._items(response.json())
                item = items[0] if items else {}
                attributes = item.get("attributes") or {}
                resource_name = str(attributes.get("name") or attributes.get("display_html_name") or "")
                resource_parent = str(attributes.get("parent_id") or "")
                is_folder = bool(attributes.get("is_folder")) or str(attributes.get("type") or "").lower() == "folder"
                if str(item.get("id") or "") == folder_id and is_folder and resource_parent == parent_id and resource_name == document_number:
                    return folder_id
        existing = self._find_child(parent_id, document_number, folder=True)
        return str(existing.get("id") or "") if existing else ""

    def archive_customer_document_version(self, customer: dict[str, Any], *, document_type: str,
                                          document_id: str, document_number: str, version_id: str,
                                          filename: str, pdf: bytes) -> dict[str, Any]:
        """Archive an immutable quotation/order version in its customer's tree."""
        if not self.configured():
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive synchronization is not fully configured")
        customer_id = str(customer.get("_id") or customer.get("customer_id") or "").strip()
        if not customer_id:
            raise WorkDriveError("INVALID_COMPANY", "A stable customer identifier is required")
        companies_root = str(self.config.get("ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID") or "").strip()
        if not companies_root:
            raise WorkDriveError("WORKDRIVE_COMPANIES_ROOT_REQUIRED", "A verified Companies root folder ID is required")
        company_name = " ".join(str(customer.get("company_name") or customer.get("name") or "").split())
        if not company_name:
            raise WorkDriveError("INVALID_COMPANY", "A canonical company name is required")
        collection = "quotation" if document_type == "quote" else "order"
        parent_field = f"workdrive_company_{collection}s_folder_id"
        with self._lock:
            company = str(customer.get("workdrive_company_folder_id") or "").strip() or self._folder(companies_root, company_name)
            parent = str(customer.get(parent_field) or "").strip() or self._folder(company, "Quotations" if document_type == "quote" else "Orders")
            existing_folder_id = self._resolve_document_folder(
                parent, document_id, document_number, ("workdrive_customer_folder_id",),
            )
            folder_id = existing_folder_id or self._folder(parent, document_number)
        mapping = self.store.find_one("workdrive_document_versions", {"document_id": document_id, "version_id": version_id}) or {}
        existing_id = str(mapping.get("workdrive_customer_file_id") or "").strip()
        if existing_id:
            return {"workdrive_company_root_id": companies_root, "workdrive_company_folder_id": company,
                    parent_field: parent, "workdrive_customer_folder_id": folder_id,
                    "workdrive_customer_file_id": existing_id, "workdrive_customer_sync_status": SYNCED,
                    "workdrive_customer_sync_error": None}
        existing = self._find_child(folder_id, filename, folder=False)
        if existing and existing.get("id"):
            file_id = str(existing["id"])
        else:
            response = self._request("POST", "upload", data={"filename": filename, "parent_id": folder_id, "override-name-exist": "false"}, files={"content": (filename, pdf, "application/pdf")})
            items = self._items(response.json())
            item = items[0] if items else {}
            file_id = str(item.get("id") or (item.get("attributes") or {}).get("resource_id") or "")
            if not file_id:
                raise WorkDriveError("UPLOAD_FAILED", "WorkDrive did not return a document identifier", retryable=True)
        self.store.update_one("workdrive_document_versions", {"document_id": document_id, "version_id": version_id}, {
            "document_type": document_type, "document_id": document_id, "version_id": version_id,
            "workdrive_customer_file_id": file_id, "workdrive_customer_folder_id": folder_id, "filename": filename,
            "workdrive_customer_sync_status": SYNCED, "workdrive_customer_sync_error": None,
        }, upsert=True)
        return {"workdrive_company_root_id": companies_root, "workdrive_company_folder_id": company,
                parent_field: parent, "workdrive_customer_folder_id": folder_id,
                "workdrive_customer_file_id": file_id, "workdrive_customer_sync_status": SYNCED, "workdrive_customer_sync_error": None}

    def delete_company_asset(self, asset: dict[str, Any]) -> dict[str, Any]:
        """Move only the persisted remote file to WorkDrive trash."""
        resource_id = str(asset.get("workdrive_resource_id") or "").strip()
        if not resource_id:
            return {"remote_deleted": False, "remote_missing": False}
        try:
            self._request(
                "PATCH", f"files/{resource_id}",
                json={"data": {"attributes": {"status": "51"}, "type": "files"}},
                headers={"Content-Type": "application/vnd.api+json"},
            )
            return {"remote_deleted": True, "remote_missing": False}
        except WorkDriveError as exc:
            if exc.code == "NOT_FOUND":
                return {"remote_deleted": False, "remote_missing": True}
            raise

    def cleanup_empty_company_asset_folder(self, asset: dict[str, Any]) -> bool:
        """Trash only an empty Logo/Visiting Card folder, never its parent."""
        folder_id = str(asset.get("workdrive_folder_id") or "").strip()
        if not folder_id:
            return False
        response = self._request("GET", f"files/{folder_id}/files", params={"page[limit]": 50, "filter[type]": "all"})
        if self._items(response.json()):
            return False
        self._request(
            "PATCH", f"files/{folder_id}",
            json={"data": {"attributes": {"status": "51"}, "type": "files"}},
            headers={"Content-Type": "application/vnd.api+json"},
        )
        return True

    def archive_document_version(self, user: dict[str, Any], *, document_type: str,
                                 document_id: str, document_number: str,
                                 version_id: str, filename: str, pdf: bytes | None) -> dict[str, Any]:
        """Upload one immutable business-document PDF, safely and idempotently."""
        if not pdf:
            return {"workdrive_sync_status": MISSING_LOCAL, "workdrive_sync_error": "PDF_SNAPSHOT_MISSING"}
        folders, folder_changes = self.ensure_user_folders(user)
        parent = folders["quotes" if document_type == "quote" else "orders"]
        with self._lock:
            existing_folder_id = self._resolve_document_folder(
                parent, document_id, document_number,
                ("workdrive_user_document_folder_id", "workdrive_folder_id"),
            )
            folder_id = existing_folder_id or self._folder(parent, document_number)
        existing = self.store.find_one(
            "workdrive_document_versions", {"document_id": document_id, "version_id": version_id}
        )
        if existing and existing.get("workdrive_sync_status") == SYNCED and existing.get("workdrive_file_id"):
            return {**folder_changes, "workdrive_user_document_folder_id": folder_id,
                    "workdrive_folder_id": folder_id, "workdrive_file_id": existing["workdrive_file_id"],
                    "workdrive_sync_status": SYNCED, "workdrive_synced_at": existing.get("workdrive_synced_at")}
        # Recover the provider-side artifact if the previous upload completed
        # but the local mapping write did not. This keeps retries idempotent.
        remote_existing = self._find_child(folder_id, filename, folder=False)
        if remote_existing:
            file_id = str(remote_existing.get("id") or "")
            if file_id:
                now = datetime.now(timezone.utc)
                self.store.update_one("workdrive_document_versions", {
                    "document_id": document_id, "version_id": version_id,
                }, {
                    "document_type": document_type, "document_id": document_id,
                    "version_id": version_id, "workdrive_file_id": file_id,
                    "workdrive_folder_id": folder_id, "workdrive_user_document_folder_id": folder_id,
                    "workdrive_sync_status": SYNCED,
                    "workdrive_synced_at": now, "workdrive_sync_error": None,
                    "filename": filename,
                }, upsert=True)
                return {**folder_changes, "workdrive_user_document_folder_id": folder_id,
                        "workdrive_folder_id": folder_id,
                        "workdrive_file_id": file_id, "workdrive_sync_status": SYNCED,
                        "workdrive_synced_at": now, "workdrive_sync_error": None}
        response = self._request("POST", "upload", data={
            "filename": filename, "parent_id": folder_id, "override-name-exist": "false",
        }, files={"content": (filename, pdf, "application/pdf")})
        items = self._items(response.json())
        item = items[0] if items else {}
        file_id = str(item.get("id") or (item.get("attributes") or {}).get("resource_id") or "")
        if not file_id:
            raise WorkDriveError("UPLOAD_FAILED", "WorkDrive did not return a document identifier", retryable=True)
        now = datetime.now(timezone.utc)
        self.store.update_one("workdrive_document_versions", {"document_id": document_id, "version_id": version_id}, {
            "document_type": document_type, "document_id": document_id, "version_id": version_id,
            "workdrive_file_id": file_id, "workdrive_folder_id": folder_id,
            "workdrive_user_document_folder_id": folder_id,
            "workdrive_sync_status": SYNCED, "workdrive_synced_at": now,
            "workdrive_sync_error": None, "filename": filename,
        }, upsert=True)
        return {**folder_changes, "workdrive_user_document_folder_id": folder_id,
                "workdrive_folder_id": folder_id, "workdrive_file_id": file_id,
                "workdrive_sync_status": SYNCED, "workdrive_synced_at": now, "workdrive_sync_error": None}

    def delete_asset(self, user: dict[str, Any], asset: str) -> None:
        resource_id = str(user.get(f"{asset}_workdrive_resource_id") or "")
        if self.configured() and resource_id:
            try:
                self._request("DELETE", f"files/{resource_id}")
            except WorkDriveError as exc:
                if exc.code != "NOT_FOUND":
                    raise

    def test_connection(self) -> dict[str, Any]:
        """Verify OAuth access and root-folder accessibility without exposing secrets."""
        issue = self.configuration_issue()
        if issue:
            raise WorkDriveError(issue[0], issue[1])
        root_id = str(self.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID") or "")
        try:
            response = self._request("GET", f"files/{root_id}")
        except WorkDriveError as exc:
            if exc.code == "AUTHENTICATION_ERROR" and exc.provider_code == "R008":
                raise WorkDriveError(
                    "ROOT_FOLDER_ACCESS_DENIED",
                    "WorkDrive root folder is not accessible to the connected account.",
                    retryable=exc.retryable, stage="root_folder",
                    http_status=exc.http_status, provider_code=exc.provider_code,
                    endpoint_host=exc.endpoint_host, endpoint_path=exc.endpoint_path,
                    diagnostic_id=exc.diagnostic_id,
                ) from exc
            mapping = {
                "AUTHENTICATION_ERROR": (
                    "WORKDRIVE_AUTHENTICATION_FAILED",
                    "WorkDrive authentication failed. Reconnect the WorkDrive integration.",
                ),
                "PERMISSION_DENIED": (
                    "ROOT_FOLDER_ACCESS_DENIED",
                    "WorkDrive root folder is not accessible to the connected account.",
                ),
                "NOT_FOUND": ("ROOT_FOLDER_NOT_FOUND", "WorkDrive root folder could not be found."),
                "SERVICE_ERROR": ("WORKDRIVE_API_ERROR", "WorkDrive API returned an error."),
            }
            if exc.code in mapping:
                code, message = mapping[exc.code]
                raise WorkDriveError(
                    code, message, retryable=exc.retryable, stage="root_folder",
                    http_status=exc.http_status, provider_code=exc.provider_code,
                    endpoint_host=exc.endpoint_host, endpoint_path=exc.endpoint_path,
                    diagnostic_id=exc.diagnostic_id,
                ) from exc
            raise
        content_type = str(response.headers.get("content-type") or "").lower()
        if "json" not in content_type:
            raise WorkDriveError(
                "WORKDRIVE_RESPONSE_INVALID", "WorkDrive API returned an unexpected response.",
                stage="response_validation", http_status=response.status_code,
                endpoint_host=urlparse(self.api_base).hostname,
                endpoint_path="/workdrive/api/v1/files/[RESOURCE_ID]",
            )
        try:
            payload = response.json() if getattr(response, "content", b"") else {}
        except (TypeError, ValueError) as exc:
            raise WorkDriveError(
                "WORKDRIVE_RESPONSE_INVALID", "WorkDrive API returned an unexpected response.",
                stage="response_validation", http_status=response.status_code,
                endpoint_host=urlparse(self.api_base).hostname,
                endpoint_path="/workdrive/api/v1/files/[RESOURCE_ID]",
            ) from exc
        items = self._items(payload)
        if not items or str(items[0].get("id") or "") != root_id:
            raise WorkDriveError(
                "WORKDRIVE_RESPONSE_INVALID", "WorkDrive API returned an unexpected response.",
                stage="response_validation", http_status=response.status_code,
                endpoint_host=urlparse(self.api_base).hostname,
                endpoint_path="/workdrive/api/v1/files/[RESOURCE_ID]",
            )
        attributes = (items[0].get("attributes") or {}) if items else {}
        resource_type = str(attributes.get("type") or "").lower()
        if attributes.get("is_folder") is False or resource_type in {"file", "document", "spreadsheet", "presentation"}:
            raise WorkDriveError(
                "ROOT_RESOURCE_NOT_FOLDER", "Configured WorkDrive root resource is not a folder.",
                stage="root_folder", http_status=response.status_code,
                endpoint_host=urlparse(self.api_base).hostname,
                endpoint_path="/workdrive/api/v1/files/[RESOURCE_ID]",
            )
        return {
            "healthy": True, "connected": True,
            "root_folder": {"id": root_id, "accessible": True},
            "root_folder_name": attributes.get("name") or attributes.get("display_html_name"),
        }
