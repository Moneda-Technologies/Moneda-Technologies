from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import threading
from typing import Any, Callable
from urllib.parse import urlparse

import requests


SYNCED = "SYNCED"
PENDING = "PENDING"
FAILED = "FAILED"


class WorkDriveError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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

    def enabled(self) -> bool:
        return bool(self.config.get("ZOHO_WORKDRIVE_ENABLED"))

    def configured(self) -> bool:
        return bool(self.enabled() and self.config.get("ZOHO_WORKDRIVE_CLIENT_ID") and
                    self.config.get("ZOHO_WORKDRIVE_CLIENT_SECRET") and
                    self._refresh_token() and
                    self.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID"))

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
        if not self.configured():
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive synchronization is not fully configured")
        with self._lock:
            now = datetime.now(timezone.utc)
            if not force and self._access_token and now < self._expires_at:
                return self._access_token
            accounts_base = self._region()[0]
            try:
                response = self.http.post(f"{accounts_base}/oauth/v2/token", data={
                    "grant_type": "refresh_token",
                    "client_id": self.config["ZOHO_WORKDRIVE_CLIENT_ID"],
                    "client_secret": self.config["ZOHO_WORKDRIVE_CLIENT_SECRET"],
                    "refresh_token": self._refresh_token(),
                }, timeout=self.timeout)
            except requests.RequestException as exc:
                raise WorkDriveError("NETWORK_ERROR", "WorkDrive authentication is temporarily unavailable", retryable=True) from exc
            if response.status_code != 200:
                raise WorkDriveError("AUTHENTICATION_ERROR", "WorkDrive authentication failed")
            payload = response.json()
            token = str(payload.get("access_token") or "")
            if not token:
                raise WorkDriveError("AUTHENTICATION_ERROR", "WorkDrive authentication failed")
            self._access_token = token
            self._expires_at = now + timedelta(seconds=max(60, int(payload.get("expires_in") or 3600) - 60))
            return token

    def _request(self, method: str, path: str, *, retry_auth: bool = True, **kwargs: Any) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers.update({"Authorization": f"Zoho-oauthtoken {self._token()}", "Accept": "application/vnd.api+json"})
        try:
            response = self.http.request(method, f"{self.api_base}/{path.lstrip('/')}", headers=headers, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise WorkDriveError("NETWORK_ERROR", "WorkDrive is temporarily unavailable", retryable=True) from exc
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
        raise WorkDriveError(code, "WorkDrive synchronization could not be completed", retryable=retryable)

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
        if not self.configured():
            raise WorkDriveError("CONFIGURATION_ERROR", "WorkDrive synchronization is not fully configured")
        root_id = str(self.config.get("ZOHO_WORKDRIVE_ROOT_FOLDER_ID") or "")
        response = self._request("GET", f"files/{root_id}")
        payload = response.json() if getattr(response, "content", b"") else {}
        items = self._items(payload)
        attributes = (items[0].get("attributes") or {}) if items else {}
        return {
            "healthy": True,
            "root_folder_name": attributes.get("name") or attributes.get("display_html_name"),
        }
