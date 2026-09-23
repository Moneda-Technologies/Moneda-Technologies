from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import threading
from typing import Any, Callable
from urllib.parse import urlparse

import requests


SYNCED = "SYNCED"
PENDING = "PENDING"
FAILED = "FAILED"


class WorkDriveError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 stage: str = "unknown", http_status: int | None = None,
                 provider_code: str | None = None, endpoint_host: str | None = None,
                 endpoint_path: str | None = None, diagnostic_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.stage = stage
        self.http_status = http_status
        self.provider_code = provider_code
        self.endpoint_host = endpoint_host
        self.endpoint_path = endpoint_path
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
                raise WorkDriveError(
                    "WORKDRIVE_TOKEN_REFRESH_UNAVAILABLE",
                    "WorkDrive authentication service is temporarily unavailable",
                    retryable=True, stage="token_refresh",
                    endpoint_host=parsed_endpoint.hostname, endpoint_path=parsed_endpoint.path,
                ) from exc
            if response.status_code != 200:
                provider_code, _ = self._safe_provider_error(response)
                raise WorkDriveError(
                    "WORKDRIVE_TOKEN_REFRESH_FAILED",
                    "WorkDrive authentication failed. Reconnect the WorkDrive integration.",
                    stage="token_refresh", http_status=response.status_code,
                    provider_code=provider_code, endpoint_host=parsed_endpoint.hostname,
                    endpoint_path=parsed_endpoint.path,
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
        code = {
            401: "AUTHENTICATION_ERROR", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
            409: "NAME_CONFLICT", 429: "RATE_LIMITED",
        }.get(response.status_code, "SERVICE_ERROR")
        retryable = response.status_code == 429 or response.status_code >= 500
        provider_code, _ = self._safe_provider_error(response)
        raise WorkDriveError(
            code, "WorkDrive synchronization could not be completed", retryable=retryable,
            stage="api_request", http_status=response.status_code, provider_code=provider_code,
            endpoint_host=parsed_endpoint.hostname, endpoint_path=safe_path,
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
    def _items(payload: Any) -> list[dict[str, Any]]:
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return data if isinstance(data, list) else [data] if isinstance(data, dict) else []

    def _find_child(self, parent_id: str, name: str, *, folder: bool | None = None) -> dict[str, Any] | None:
        response = self._request("GET", f"files/{parent_id}/files", params={"page[limit]": 50, "filter[type]": "allfiles"})
        for item in self._items(response.json()):
            attributes = item.get("attributes") or {}
            item_name = str(attributes.get("name") or attributes.get("display_html_name") or "")
            is_folder = bool(attributes.get("is_folder")) or str(attributes.get("type") or "").lower() == "folder"
            if item_name == name and (folder is None or folder == is_folder):
                return item
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

    def ensure_profile_folder(self, user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        user_id = str(user.get("_id") or "").strip()
        if not user_id:
            raise WorkDriveError("INVALID_USER", "A stable user identifier is required")
        changes: dict[str, Any] = {}
        user_folder_id = str(user.get("workdrive_user_folder_id") or "")
        if not user_folder_id:
            user_folder_id = self._folder(str(self.config["ZOHO_WORKDRIVE_ROOT_FOLDER_ID"]), user_id)
            changes["workdrive_user_folder_id"] = user_folder_id
        profile_folder_id = str(user.get("workdrive_profile_folder_id") or "")
        if not profile_folder_id:
            profile_folder_id = self._folder(user_folder_id, "Profile")
            changes["workdrive_profile_folder_id"] = profile_folder_id
        return profile_folder_id, changes

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
