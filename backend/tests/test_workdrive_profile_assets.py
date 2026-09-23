from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import struct
from uuid import uuid4

import pytest

from app.services.workdrive import WorkDriveError, WorkDriveService
from app.account.profile_sync import profile_asset_state
from app.communication.zoho import ZohoMailOAuth
from app.repositories.store import MemoryStore


def png(width: int = 10, height: int = 5) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + struct.pack(">II", width, height) + b"\x08\x06\x00\x00\x00"


@pytest.fixture()
def upload_dir():
    path = Path.cwd() / ".test-profile-assets" / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_profile_asset_state_requires_a_real_local_file(upload_dir):
    service = WorkDriveService({"ZOHO_WORKDRIVE_ENABLED": True}, None)
    user = {"_id": "u1", "signature_path": "signatures/u1.png", "signature_workdrive_sync_status": "PENDING"}
    assert profile_asset_state(user, "signature", upload_dir, service) == "MISSING_LOCAL"
    (upload_dir / "signatures").mkdir()
    (upload_dir / "signatures" / "u1.png").write_bytes(png())
    assert profile_asset_state(user, "signature", upload_dir, service) == "PENDING"
    user["signature_workdrive_sync_status"] = "SYNCED"
    assert profile_asset_state(user, "signature", upload_dir, service) == "SYNCED"
    user["signature_workdrive_sync_status"] = "FAILED"
    assert profile_asset_state(user, "signature", upload_dir, service) == "FAILED"
    assert profile_asset_state({"_id": "u1"}, "signature", upload_dir, service) == "NO_LOCAL_ASSET"


class SuccessfulWorkDrive:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def enabled(self):
        return True

    def configured(self):
        return True

    def sync_asset(self, user, asset, local_path: Path, _mime_type):
        self.calls.append((str(user["_id"]), asset))
        assert local_path.is_file()
        return {
            "workdrive_user_folder_id": "user-folder",
            "workdrive_profile_folder_id": "profile-folder",
            f"{asset}_workdrive_resource_id": f"resource-{asset}",
            f"{asset}_workdrive_path": f"{user['_id']}/Profile/{asset}.png",
            f"{asset}_workdrive_filename": f"{asset}.png",
            f"{asset}_workdrive_sync_status": "SYNCED",
            f"{asset}_workdrive_sync_error": None,
        }

    def delete_asset(self, _user, _asset):
        return None


class FailedWorkDrive(SuccessfulWorkDrive):
    def sync_asset(self, user, asset, local_path, mime_type):
        raise WorkDriveError("RATE_LIMITED", "secret upstream detail", retryable=True)


def configure_uploads(app, tmp_path, service=None):
    app.config["UPLOAD_DIRECTORY"] = tmp_path
    if service is not None:
        app.extensions["workdrive"] = service


def test_signature_upload_keeps_previous_local_behavior_when_workdrive_disabled(app, authenticated, upload_dir):
    configure_uploads(app, upload_dir)
    response = authenticated.post("/api/v1/profile/signature", data={"file": (BytesIO(png()), "signature.png")}, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.json["data"]["workdrive_sync_status"] == "DISABLED"
    assert (upload_dir / "signatures" / "user-demo-admin.png").is_file()


@pytest.mark.parametrize("asset", ["signature", "photo"])
def test_profile_assets_save_locally_then_sync_to_workdrive(app, authenticated, upload_dir, asset):
    service = SuccessfulWorkDrive()
    configure_uploads(app, upload_dir, service)
    response = authenticated.post(f"/api/v1/profile/{asset}", data={"file": (BytesIO(png()), f"{asset}.png")}, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.json["data"]["workdrive_sync_status"] == "SYNCED"
    folder = "signatures" if asset == "signature" else "profile-photos"
    assert (upload_dir / folder / "user-demo-admin.png").is_file()
    user = app.extensions["store"].find_one("users", {"_id": "user-demo-admin"})
    assert user[f"{asset}_workdrive_resource_id"] == f"resource-{asset}"
    assert service.calls == [("user-demo-admin", asset)]


def test_workdrive_failure_never_destroys_successful_local_upload_or_leaks_error(app, authenticated, upload_dir):
    configure_uploads(app, upload_dir, FailedWorkDrive())
    response = authenticated.post("/api/v1/profile/photo", data={"file": (BytesIO(png()), "photo.png")}, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.json["data"]["workdrive_sync_status"] == "FAILED"
    assert "secret upstream detail" not in response.get_data(as_text=True)
    assert (upload_dir / "profile-photos" / "user-demo-admin.png").is_file()
    user = app.extensions["store"].find_one("users", {"_id": "user-demo-admin"})
    assert user["photo_workdrive_sync_error"] == "RATE_LIMITED"


def test_invalid_and_oversized_profile_images_are_rejected(app, authenticated, upload_dir):
    configure_uploads(app, upload_dir)
    invalid = authenticated.post("/api/v1/profile/photo", data={"file": (BytesIO(b"not-an-image"), "photo.png")}, content_type="multipart/form-data")
    assert invalid.status_code == 422
    oversized = authenticated.post("/api/v1/profile/photo", data={"file": (BytesIO(png() + b"x" * (2 * 1024 * 1024)), "photo.png")}, content_type="multipart/form-data")
    assert oversized.status_code == 422


def test_other_user_asset_management_is_superadmin_only(app, authenticated, upload_dir):
    configure_uploads(app, upload_dir, SuccessfulWorkDrive())
    store = app.extensions["store"]
    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "user"})
    denied = authenticated.post("/api/v1/admin/users/user-demo-manager/profile/photo", data={"file": (BytesIO(png()), "photo.png")}, content_type="multipart/form-data")
    assert denied.status_code == 403


def test_superadmin_can_manage_another_users_asset(app, authenticated, upload_dir):
    service = SuccessfulWorkDrive()
    configure_uploads(app, upload_dir, service)
    app.extensions["store"].insert_one("users", {"_id": "asset-target", "name": "Target", "email": "target@example.com", "role_id": "user", "active": True})
    response = authenticated.post("/api/v1/admin/users/asset-target/profile/photo", data={"file": (BytesIO(png()), "photo.png")}, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.json["data"]["workdrive_sync_status"] == "SYNCED"
    assert service.calls == [("asset-target", "photo")]


def test_resync_is_superadmin_only_and_handles_existing_users_without_metadata(app, authenticated, upload_dir):
    service = SuccessfulWorkDrive()
    configure_uploads(app, upload_dir, service)
    app.extensions["store"].insert_one("users", {"_id": "resync-target", "name": "Target", "email": "resync@example.com", "role_id": "user", "active": True})
    skipped = authenticated.post("/api/v1/admin/workdrive/resync-user/resync-target")
    assert skipped.status_code == 200
    assert skipped.json["data"]["results"] == {"photo": "SKIPPED", "signature": "SKIPPED"}
    app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, {"role_id": "user"})
    assert authenticated.post("/api/v1/admin/workdrive/resync-all").status_code == 403


def test_workdrive_settings_status_is_safe_and_permissioned(app, authenticated):
    store = app.extensions["store"]
    store.insert_one("integrations", {
        "_id": "zoho_workdrive", "provider": "zoho_workdrive", "status": "connected",
        "account_email": "business@monedatechnologies.com", "refresh_token_encrypted": "ciphertext",
        "refresh_token": None, "connected_at": "2026-09-23T10:00:00Z",
    })
    response = authenticated.get("/api/v1/admin/workdrive/status")
    assert response.status_code == 200
    data = response.json["data"]
    assert data["status"] == "connected"
    assert "refresh_token" not in str(data)
    assert "client_secret" not in str(data)

    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "user"})
    assert authenticated.get("/api/v1/admin/workdrive/status").status_code == 403


def test_workdrive_connection_test_returns_safe_health_result(app, authenticated):
    class HealthyService:
        def test_connection(self):
            return {"healthy": True, "root_folder_name": "Moneda"}

    app.extensions["workdrive"] = HealthyService()
    response = authenticated.post("/api/v1/admin/workdrive/test", json={})
    assert response.status_code == 200
    assert response.json["data"] == {
        "healthy": True, "connected": True, "root_folder": None,
        "root_folder_name": "Moneda",
    }
    assert "token" not in response.get_data(as_text=True).lower()


def test_workdrive_connection_test_reports_configuration_error_clearly(app, authenticated):
    class UnconfiguredService:
        def test_connection(self):
            raise WorkDriveError("CONFIGURATION_ERROR", "missing secret detail")

    app.extensions["workdrive"] = UnconfiguredService()
    response = authenticated.post("/api/v1/admin/workdrive/test", json={})
    assert response.status_code == 503
    assert response.json["error"] == "WORKDRIVE_CONFIGURATION_ERROR"
    assert "missing secret detail" not in response.get_data(as_text=True)


def test_workdrive_missing_root_folder_reports_specific_safe_error(app, authenticated):
    service = WorkDriveService({
        "ZOHO_WORKDRIVE_ENABLED": True,
        "ZOHO_WORKDRIVE_CLIENT_ID": "client",
        "ZOHO_WORKDRIVE_CLIENT_SECRET": "secret",
        "ZOHO_WORKDRIVE_REFRESH_TOKEN": "refresh",
        "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }, app.extensions["store"])
    app.extensions["workdrive"] = service
    response = authenticated.post("/api/v1/admin/workdrive/test", json={})
    assert response.status_code == 503
    assert response.json["error"] == "WORKDRIVE_ROOT_FOLDER_REQUIRED"
    assert "root folder ID is not configured" in response.json["message"]


def test_workdrive_connection_test_returns_safe_diagnostic_metadata(app, authenticated):
    class UnauthorizedService:
        def test_connection(self):
            raise WorkDriveError(
                "WORKDRIVE_AUTHENTICATION_FAILED",
                "WorkDrive authentication failed. Reconnect the WorkDrive integration.",
                stage="root_folder", http_status=401, provider_code="R008",
                endpoint_host="www.zohoapis.in",
                endpoint_path="/workdrive/api/v1/files/[RESOURCE_ID]",
                diagnostic_id="workdrive-safe-test",
            )

    app.extensions["workdrive"] = UnauthorizedService()
    response = authenticated.post("/api/v1/admin/workdrive/test", json={})
    assert response.status_code == 503
    assert response.json["error"] == "WORKDRIVE_AUTHENTICATION_FAILED"
    assert response.json["error_code"] == "WORKDRIVE_AUTHENTICATION_FAILED"
    assert response.json["stage"] == "root_folder"
    assert response.json["diagnostic_id"] == "workdrive-safe-test"
    text = response.get_data(as_text=True).lower()
    assert "access_token" not in text
    assert "refresh_token" not in text
    assert "authorization" not in text


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class Http:
    def __init__(self, status):
        self.status = status

    def request(self, *_args, **_kwargs):
        return Response(self.status)


@pytest.mark.parametrize("status,code,retryable", [
    (401, "AUTHENTICATION_ERROR", False), (403, "PERMISSION_DENIED", False),
    (404, "NOT_FOUND", False), (409, "NAME_CONFLICT", False),
    (429, "RATE_LIMITED", True), (500, "SERVICE_ERROR", True),
])
def test_workdrive_http_errors_are_classified_without_response_details(status, code, retryable):
    config = {
        "ZOHO_WORKDRIVE_ENABLED": True, "ZOHO_WORKDRIVE_CLIENT_ID": "id",
        "ZOHO_WORKDRIVE_CLIENT_SECRET": "secret", "ZOHO_WORKDRIVE_REFRESH_TOKEN": "refresh",
        "ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "root", "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }
    service = WorkDriveService(config, None, session=Http(status))
    service._token = lambda force=False: "access"  # type: ignore[method-assign]
    with pytest.raises(WorkDriveError) as caught:
        service._request("GET", "files/example", retry_auth=False)
    assert caught.value.code == code
    assert caught.value.retryable is retryable


def test_enabled_workdrive_with_missing_credentials_fails_safely(app, authenticated, upload_dir):
    service = WorkDriveService({"ZOHO_WORKDRIVE_ENABLED": True}, app.extensions["store"])
    configure_uploads(app, upload_dir, service)
    response = authenticated.post("/api/v1/profile/signature", data={"file": (BytesIO(png()), "signature.png")}, content_type="multipart/form-data")
    assert response.status_code == 200
    assert response.json["data"]["workdrive_sync_status"] == "FAILED"
    text = response.get_data(as_text=True)
    assert "refresh" not in text.lower()
    assert "client_secret" not in text.lower()


def test_workdrive_service_uses_encrypted_oauth_grant_instead_of_mail_or_env_token():
    store = MemoryStore()
    config = {
        "SECRET_KEY": "test-secret", "INTEGRATION_ENCRYPTION_KEY": "test-encryption",
        "ZOHO_CLIENT_ID": "mail-id", "ZOHO_CLIENT_SECRET": "mail-secret",
        "ZOHO_REFRESH_TOKEN": "mail-refresh", "ZOHO_WORKDRIVE_ENABLED": True,
        "ZOHO_WORKDRIVE_CLIENT_ID": "workdrive-id", "ZOHO_WORKDRIVE_CLIENT_SECRET": "workdrive-secret",
        "ZOHO_WORKDRIVE_REFRESH_TOKEN": "fallback-workdrive-refresh",
        "ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "root", "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }
    oauth = ZohoMailOAuth(config, store)
    store.insert_one("integrations", {
        "_id": "zoho_workdrive", "provider": "zoho_workdrive", "status": "connected",
        "refresh_token_encrypted": oauth._cipher().encrypt("stored-workdrive-refresh"),
    })
    service = WorkDriveService(config, store, refresh_token_provider=oauth.workdrive_refresh_token)
    assert service.configured() is True
    assert service._refresh_token() == "stored-workdrive-refresh"
    assert service._refresh_token() != config["ZOHO_REFRESH_TOKEN"]
    assert service._refresh_token() != config["ZOHO_WORKDRIVE_REFRESH_TOKEN"]


def test_workdrive_india_region_and_configured_api_endpoint_are_preserved():
    service = WorkDriveService({
        "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }, MemoryStore())
    assert service._region()[0] == "https://accounts.zoho.in"
    assert service.api_base == "https://www.zohoapis.in/workdrive/api/v1"


def test_repeated_sync_uses_name_override_instead_of_creating_duplicate_files(upload_dir):
    local = upload_dir / "signature.png"
    local.write_bytes(png())

    class CapturingService(WorkDriveService):
        def configured(self):
            return True

        def ensure_profile_folder(self, _user):
            return "profile-folder", {}

        def _request(self, method, path, **kwargs):
            assert method == "POST" and path == "upload"
            calls.append(kwargs["data"])
            return Response(200, {"data": [{"id": "same-resource-id", "type": "files"}]})

    calls = []
    service = CapturingService({"ZOHO_WORKDRIVE_ENABLED": True}, None)
    user = {"_id": "stable-user"}
    first = service.sync_asset(user, "signature", local, "image/png")
    second = service.sync_asset({**user, **first}, "signature", local, "image/png")
    assert first["signature_workdrive_resource_id"] == second["signature_workdrive_resource_id"] == "same-resource-id"
    assert calls == [
        {"filename": "signature.png", "parent_id": "profile-folder", "override-name-exist": "true"},
        {"filename": "signature.png", "parent_id": "profile-folder", "override-name-exist": "true"},
    ]
