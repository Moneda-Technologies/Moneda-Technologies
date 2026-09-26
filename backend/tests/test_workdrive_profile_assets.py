from __future__ import annotations

from io import BytesIO
from pathlib import Path
import shutil
import struct
from uuid import uuid4

import pytest

from app.services.workdrive import WorkDriveError, WorkDriveService
from app.services.document_archive import archive_version
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
    assert data["account_email"] == "business@monedatechnologies.com"
    assert data["account_identity_verified"] is True
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


def test_workdrive_r008_is_classified_as_resource_access_denied_and_sanitized():
    config = {
        "ZOHO_WORKDRIVE_ENABLED": True, "ZOHO_WORKDRIVE_CLIENT_ID": "id",
        "ZOHO_WORKDRIVE_CLIENT_SECRET": "secret", "ZOHO_WORKDRIVE_REFRESH_TOKEN": "refresh",
        "ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "root", "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }
    service = WorkDriveService(config, None, session=Http(401))
    service.http = type("R008Http", (), {
        "request": lambda *_args, **_kwargs: Response(401, {
            "errors": [{"id": "R008", "title": "Unauthorized access", "access_token": "never-log"}],
        }),
    })()
    service._token = lambda force=False: "access"  # type: ignore[method-assign]
    with pytest.raises(WorkDriveError) as caught:
        service._request("GET", "files/company-quotes", retry_auth=False)
    assert caught.value.code == "RESOURCE_ACCESS_DENIED"
    assert caught.value.http_status == 401
    assert caught.value.provider_code == "R008"
    assert caught.value.provider_message == "Unauthorized access"
    assert "never-log" not in str(caught.value.provider_body)
    assert "[REDACTED]" in str(caught.value.provider_body)


def test_resource_diagnostic_is_read_only_and_reports_exact_mapping_failure():
    config = {
        "ZOHO_WORKDRIVE_ENABLED": True, "ZOHO_WORKDRIVE_CLIENT_ID": "id",
        "ZOHO_WORKDRIVE_CLIENT_SECRET": "secret", "ZOHO_WORKDRIVE_REFRESH_TOKEN": "refresh",
        "ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "root", "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }
    service = WorkDriveService(config, None)
    service._token = lambda force=False: "access"  # type: ignore[method-assign]
    calls = []

    def get_only(method, path, **_kwargs):
        calls.append((method, path))
        if path.endswith("company-folder"):
            raise WorkDriveError("RESOURCE_ACCESS_DENIED", "denied", http_status=401,
                                  provider_code="R008", endpoint_method="GET",
                                  endpoint_host="www.zohoapis.in", endpoint_path="/workdrive/api/v1/files/[RESOURCE_ID]",
                                  endpoint_resource_id="company-folder", diagnostic_id="company-check")
        return Response(200, {"data": [{"id": "user-folder", "attributes": {"name": "QT-1", "parent_id": "user-quotes", "type": "folder"}}]})

    service._request = get_only  # type: ignore[method-assign]
    result = service.diagnose_resource_access([
        {"label": "user", "resource_id": "user-folder", "expected_parent_id": "user-quotes"},
        {"label": "company", "resource_id": "company-folder", "expected_parent_id": "company-quotes"},
    ])
    assert calls == [("GET", "files/user-folder"), ("GET", "files/company-folder")]
    assert result["resources"][0]["status"] == "accessible"
    assert result["resources"][0]["parent_matches"] is True
    assert result["resources"][1]["status"] == "denied"
    assert result["resources"][1]["endpoint_resource_id"] == "company-folder"


def test_child_folder_diagnostic_follows_workdrive_cursor_and_validates_parent():
    config = {
        "ZOHO_WORKDRIVE_ENABLED": True, "ZOHO_WORKDRIVE_CLIENT_ID": "id",
        "ZOHO_WORKDRIVE_CLIENT_SECRET": "secret", "ZOHO_WORKDRIVE_REFRESH_TOKEN": "refresh",
        "ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "root", "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }
    service = WorkDriveService(config, None)
    service._token = lambda force=False: "access"  # type: ignore[method-assign]
    calls = []
    payloads = [
        {"data": [{"id": "other", "attributes": {"name": "Other", "parent_id": "quotes", "type": "folder"}}],
         "links": {"cursor": {"has_next": True, "next": "https://www.zohoapis.in/workdrive/api/v1/files/quotes/files?page%5Bnext%5D=next-token"}}},
        {"data": [{"id": "quote-folder", "attributes": {"name": "MT-MONEDA-005", "parent_id": "quotes", "type": "folder", "is_folder": True}}],
         "links": {"cursor": {"has_next": False}}},
    ]

    def paged(method, path, **kwargs):
        calls.append((method, path, kwargs["params"]["page[next]"]))
        return Response(200, payloads.pop(0))

    service._request = paged  # type: ignore[method-assign]
    result = service.diagnose_child_folder("quotes", "MT-MONEDA-005")
    assert calls == [("GET", "files/quotes/files", "0"), ("GET", "files/quotes/files", "next-token")]
    assert result["requests"][0] == {
        "method": "GET", "endpoint_host": "www.zohoapis.in",
        "endpoint_path": "/workdrive/api/v1/files/quotes/files",
        "query": {"page[next]": "0", "page[limit]": 50, "filter[type]": "all"},
        "headers": {"Accept": "application/vnd.api+json", "Authorization": "[REDACTED]"},
    }
    assert result["complete"] is True
    assert result["pages_scanned"] == 2
    assert result["matches"] == [{"resource_id": "quote-folder", "name": "MT-MONEDA-005", "parent_id": "quotes",
                                   "resource_type": "folder", "is_folder": True, "parent_matches": True}]


def test_quotation_mapping_repair_updates_only_verified_document_mappings(app, authenticated):
    store = app.extensions["store"]
    quotation_id = "quote-mapping-repair"
    version_id = "quote-mapping-repair-v07"
    store.insert_one("customers", {"_id": "customer-mapping-repair", "company_name": "Customer",
                                    "workdrive_company_quotations_folder_id": "company-quotes"})
    store.insert_one("users", {"_id": "owner-mapping-repair", "name": "Owner",
                                "workdrive_quotes_folder_id": "user-quotes"})
    store.insert_one("quotations", {"_id": quotation_id, "quotation_number": "MT-MONEDA-005",
                                    "customer_id": "customer-mapping-repair", "created_by_user_id": "owner-mapping-repair"})
    store.insert_one("quotation_versions", {"_id": version_id, "quotation_id": quotation_id,
                                              "workdrive_folder_id": "old-user", "workdrive_customer_folder_id": "old-company"})
    first = store.insert_one("workdrive_document_versions", {"_id": "mapping-first", "document_id": quotation_id,
                                                               "version_id": "v01", "workdrive_folder_id": "old-user",
                                                               "workdrive_customer_folder_id": "old-company"})
    second = store.insert_one("workdrive_document_versions", {"_id": "mapping-second", "document_id": quotation_id,
                                                                "version_id": version_id, "workdrive_user_document_folder_id": "old-user",
                                                                "workdrive_customer_folder_id": "old-company"})
    unrelated = store.insert_one("workdrive_document_versions", {"_id": "mapping-unrelated", "document_id": "other-quote",
                                                                   "workdrive_folder_id": "keep-user", "workdrive_customer_folder_id": "keep-company"})

    class VerifiedFolders:
        def diagnose_resource_access(self, resources):
            assert [row["resource_id"] for row in resources] == ["new-company", "new-user"]
            return {"resources": [
                {"label": "company_quote_folder", "status": "accessible", "id_matches": True,
                 "parent_matches": True, "name": "MT-MONEDA-005", "is_folder": True},
                {"label": "user_quote_folder", "status": "accessible", "id_matches": True,
                 "parent_matches": True, "name": "MT-MONEDA-005", "is_folder": True},
            ]}

    app.extensions["workdrive"] = VerifiedFolders()
    response = authenticated.post(
        f"/api/v1/admin/workdrive/quotation-archive-mapping-repair/{quotation_id}/{version_id}",
        json={"company_quote_folder_id": "new-company", "user_quote_folder_id": "new-user"},
    )
    assert response.status_code == 200
    assert response.json["data"]["mapping_rows_updated"] == 2
    for row_id in (first["_id"], second["_id"]):
        repaired = store.find_one("workdrive_document_versions", {"_id": row_id})
        assert repaired["workdrive_customer_folder_id"] == "new-company"
        assert repaired["workdrive_user_document_folder_id"] == "new-user"
        assert repaired["workdrive_folder_id"] == "new-user"
    assert store.find_one("workdrive_document_versions", {"_id": unrelated["_id"]})["workdrive_folder_id"] == "keep-user"
    version = store.find_one("quotation_versions", {"_id": version_id})
    assert version["workdrive_customer_folder_id"] == "new-company"
    assert version["workdrive_folder_id"] == "new-user"
    assert store.find_one("audit_logs", {"action": "workdrive.quotation_archive_mappings_repaired", "entity_id": quotation_id})


def test_quotation_mapping_repair_rejects_unverified_folder_without_writing(app, authenticated):
    store = app.extensions["store"]
    store.insert_one("customers", {"_id": "customer-invalid-mapping", "workdrive_company_quotations_folder_id": "company-quotes"})
    store.insert_one("users", {"_id": "owner-invalid-mapping", "workdrive_quotes_folder_id": "user-quotes"})
    store.insert_one("quotations", {"_id": "quote-invalid-mapping", "quotation_number": "QT-1",
                                    "customer_id": "customer-invalid-mapping", "created_by_user_id": "owner-invalid-mapping"})
    store.insert_one("quotation_versions", {"_id": "quote-invalid-mapping-v01", "quotation_id": "quote-invalid-mapping"})
    mapping = store.insert_one("workdrive_document_versions", {"_id": "invalid-mapping-row", "document_id": "quote-invalid-mapping",
                                                                 "workdrive_folder_id": "old-user"})

    class DeniedFolder:
        def diagnose_resource_access(self, _resources):
            return {"resources": [{"label": "company_quote_folder", "status": "denied"},
                                  {"label": "user_quote_folder", "status": "accessible", "id_matches": True,
                                   "parent_matches": True, "name": "QT-1", "is_folder": True}]}

    app.extensions["workdrive"] = DeniedFolder()
    response = authenticated.post(
        "/api/v1/admin/workdrive/quotation-archive-mapping-repair/quote-invalid-mapping/quote-invalid-mapping-v01",
        json={"company_quote_folder_id": "wrong-company", "user_quote_folder_id": "new-user"},
    )
    assert response.status_code == 409
    assert store.find_one("workdrive_document_versions", {"_id": mapping["_id"]})["workdrive_folder_id"] == "old-user"


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


def test_workdrive_refresh_access_denied_preserves_safe_provider_diagnostics(caplog):
    store = MemoryStore()
    config = {
        "ZOHO_WORKDRIVE_ENABLED": True, "ZOHO_CLIENT_ID": "shared-id", "ZOHO_CLIENT_SECRET": "shared-secret",
        "ZOHO_WORKDRIVE_CLIENT_ID": "shared-id", "ZOHO_WORKDRIVE_CLIENT_SECRET": "shared-secret",
        "ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "root", "ZOHO_WORKDRIVE_ACCOUNT_REGION": "in",
        "ZOHO_WORKDRIVE_API_BASE_URL": "https://www.zohoapis.in/workdrive/api/v1",
    }
    oauth = ZohoMailOAuth({**config, "SECRET_KEY": "test-secret", "INTEGRATION_ENCRYPTION_KEY": "test-key"}, store)
    store.insert_one("integrations", {
        "_id": "zoho_workdrive", "status": "connected",
        "refresh_token_encrypted": oauth._cipher().encrypt("private-refresh-token"),
    })

    class AccessDeniedHttp:
        def post(self, *_args, **_kwargs):
            return Response(400, {"error": "access_denied", "error_description": "Access Denied",
                                  "refresh_token": "must-not-log"})

    service = WorkDriveService(config, store, session=AccessDeniedHttp(),
                               refresh_token_provider=oauth.workdrive_refresh_token)
    with pytest.raises(WorkDriveError) as caught:
        service._token(force=True)
    assert caught.value.code == "WORKDRIVE_TOKEN_REFRESH_FAILED"
    assert caught.value.http_status == 400
    assert caught.value.provider_code == "access_denied"
    assert caught.value.provider_message == "Access Denied"
    assert caught.value.endpoint_method == "POST"
    assert service.oauth_configuration_metadata()["refresh_token_source"] == "encrypted_mongodb"
    assert service.oauth_configuration_metadata()["uses_same_client_as_mail"] is True
    assert "must-not-log" not in caplog.text
    assert "private-refresh-token" not in caplog.text
    assert "[REDACTED]" in caplog.text


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


def test_legacy_user_folder_migration_creates_canonical_tree_idempotently():
    class FolderService(WorkDriveService):
        def __init__(self):
            super().__init__({"ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "users-root"}, MemoryStore())
            self.created: dict[tuple[str, str], str] = {}

        def _folder(self, parent_id, name):
            key = (parent_id, name)
            if key not in self.created:
                self.created[key] = f"folder-{len(self.created) + 1}"
            return self.created[key]

    service = FolderService()
    user = {
        "_id": "user-demo-admin", "name": "Admin", "email": "md@monedatechnologies.com",
        "workdrive_user_folder_id": "legacy-user-folder",
        "workdrive_profile_folder_id": "legacy-profile-folder",
    }
    first, changes = service.migrate_legacy_user_folder(user, "legacy-user-folder")
    second, repeated = service.migrate_legacy_user_folder(user, "legacy-user-folder")

    assert first == second
    assert changes == repeated == {
        "workdrive_user_folder_id": first["user"],
        "workdrive_profile_folder_id": first["profile"],
        "workdrive_quotes_folder_id": first["quotes"],
        "workdrive_orders_folder_id": first["orders"],
    }
    assert ("users-root", "Admin — md@monedatechnologies.com") in service.created
    assert ("legacy-user-folder", "Profile") not in service.created
    assert len(service.created) == 4
    with pytest.raises(WorkDriveError, match="expected legacy mapping"):
        service.migrate_legacy_user_folder(user, "different-folder")


def test_company_sync_refuses_unverified_root_without_creating_folders(upload_dir):
    class GuardedService(WorkDriveService):
        def configured(self):
            return True

        def _folder(self, *_args, **_kwargs):
            raise AssertionError("folder creation must remain disabled without a verified Companies root")

    local = upload_dir / "logo.png"
    local.write_bytes(png())
    service = GuardedService({"ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "users-root"}, MemoryStore())
    with pytest.raises(WorkDriveError, match="automatic Companies folder creation is disabled"):
        service.sync_company_asset(
            {"customer_id": "customer-1", "company_name": "Test Company", "asset_type": "logo"},
            upload_dir,
        )


def test_company_sync_reuses_customer_folder_mapping_for_new_asset_rows(upload_dir):
    class CompanyFolderService(WorkDriveService):
        def __init__(self, store):
            super().__init__({"ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID": "companies-root"}, store)
            self.folders: list[tuple[str, str]] = []

        def configured(self):
            return True

        def _folder(self, parent_id, name):
            self.folders.append((parent_id, name))
            return {("companies-root", "Same Name"): "company-folder", ("company-folder", "Logo"): "logo-folder"}[(parent_id, name)]

        def _find_child(self, *_args, **_kwargs):
            return None

        def _request(self, method, path, **_kwargs):
            assert method == "POST" and path == "upload"
            return Response(200, {"data": [{"id": "remote-file"}]})

    store = MemoryStore()
    store.insert_one("customers", {"_id": "customer-1", "company_name": "Same Name", "workdrive_company_folder_id": "company-folder"})
    local = upload_dir / "asset.png"
    local.write_bytes(png())
    service = CompanyFolderService(store)

    result = service.sync_company_asset({
        "customer_id": "customer-1", "company_name": "Same Name", "category": "logo",
        "filename": "asset.png", "mime_type": "image/png", "storage_path": "asset.png",
    }, upload_dir)

    assert result["workdrive_company_folder_id"] == "company-folder"
    assert ("companies-root", "Same Name") not in service.folders
    assert service.folders == [("company-folder", "Logo")]


def test_visiting_cards_use_shared_folder_and_cardholder_filenames(upload_dir):
    class VisitingCardService(WorkDriveService):
        def __init__(self, store):
            super().__init__({"ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID": "companies-root"}, store)
            self.folders: list[tuple[str, str]] = []
            self.remote_names: set[str] = set()
            self.uploads: list[dict] = []

        def configured(self):
            return True

        def _folder(self, parent_id, name):
            self.folders.append((parent_id, name))
            return {("company-folder", "Visiting Cards"): "visiting-cards-folder"}[(parent_id, name)]

        def _find_child(self, _folder_id, name, **_kwargs):
            return {"id": "existing"} if name in self.remote_names else None

        def _request(self, method, path, **kwargs):
            assert method == "POST" and path == "upload"
            self.uploads.append(kwargs["data"])
            self.remote_names.add(kwargs["data"]["filename"])
            return Response(200, {"data": [{"id": f"file-{len(self.uploads)}"}]})

    store = MemoryStore()
    store.insert_one("customers", {"_id": "customer-1", "company_name": "BBB", "workdrive_company_folder_id": "company-folder"})
    local = upload_dir / "ddd.pdf"
    local.write_bytes(b"pdf")
    service = VisitingCardService(store)
    asset = {"customer_id": "customer-1", "company_name": "BBB", "category": "visiting_card", "cardholder_name": " Athul / Nair ", "filename": "ddd.pdf", "mime_type": "application/pdf", "storage_path": "ddd.pdf"}

    first = service.sync_company_asset(asset, upload_dir)
    second = service.sync_company_asset(asset, upload_dir)

    assert service.folders == [("company-folder", "Visiting Cards"), ("company-folder", "Visiting Cards")]
    assert service.uploads == [
        {"filename": "Athul Nair.pdf", "parent_id": "visiting-cards-folder", "override-name-exist": "false"},
        {"filename": "Athul Nair (2).pdf", "parent_id": "visiting-cards-folder", "override-name-exist": "false"},
    ]
    assert first["workdrive_filename"] == "Athul Nair.pdf"
    assert second["workdrive_filename"] == "Athul Nair (2).pdf"


def test_same_named_customers_receive_distinct_company_folder_names():
    store = MemoryStore()
    store.insert_one("customers", {"_id": "existing", "company_name": "Same Name", "workdrive_company_folder_id": "existing-folder"})

    class FolderService(WorkDriveService):
        def configured(self):
            return True

        def _folder(self, parent_id, name):
            return f"{parent_id}:{name}"

    folders = FolderService({"ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID": "companies-root"}, store).ensure_customer_company_folder({"_id": "customer-12345678", "company_name": "Same Name"})
    assert folders["workdrive_company_folder_id"] == "companies-root:Same Name — customer"


def test_customer_archive_uses_one_folder_per_quotation_and_order():
    class ArchiveTreeService(WorkDriveService):
        def __init__(self, store):
            super().__init__({"ZOHO_WORKDRIVE_COMPANIES_ROOT_FOLDER_ID": "companies-root"}, store)
            self.folders: dict[tuple[str, str], str] = {}
            self.uploads: list[dict] = []

        def configured(self):
            return True

        def _folder(self, parent_id, name):
            return self.folders.setdefault((parent_id, name), f"folder-{len(self.folders) + 1}")

        def _resolve_document_folder(self, _parent_id, document_id, _document_number, _mapping_fields):
            return self._mapped_document_folder(document_id, "workdrive_customer_folder_id")

        def _find_child(self, *_args, **_kwargs):
            return None

        def _request(self, method, path, **kwargs):
            assert method == "POST" and path == "upload"
            self.uploads.append(kwargs["data"])
            return Response(200, {"data": [{"id": f"file-{len(self.uploads)}"}]})

    store = MemoryStore()
    customer = store.insert_one("customers", {"_id": "customer-1", "company_name": "BBB", "workdrive_company_folder_id": "company-folder"})
    service = ArchiveTreeService(store)

    first = service.archive_customer_document_version(customer, document_type="quote", document_id="quote-1", document_number="QT-001", version_id="quote-1-v01", filename="QT-001-V01.pdf", pdf=b"pdf")
    revision = service.archive_customer_document_version(customer, document_type="quote", document_id="quote-1", document_number="QT-001", version_id="quote-1-v02", filename="QT-001-V02.pdf", pdf=b"pdf")
    second = service.archive_customer_document_version(customer, document_type="quote", document_id="quote-2", document_number="QT-002", version_id="quote-2-v01", filename="QT-002-V01.pdf", pdf=b"pdf")
    order = service.archive_customer_document_version(customer, document_type="order", document_id="order-1", document_number="OC-001", version_id="order-1-v01", filename="OC-001-V01.pdf", pdf=b"pdf")

    assert first["workdrive_customer_folder_id"] == revision["workdrive_customer_folder_id"]
    assert first["workdrive_customer_folder_id"] != second["workdrive_customer_folder_id"]
    assert order["workdrive_customer_folder_id"] != first["workdrive_customer_folder_id"]
    assert ("company-folder", "Quotations") in service.folders
    assert ("company-folder", "Orders") in service.folders
    assert (first["workdrive_company_quotations_folder_id"], "QT-001") in service.folders
    assert (order["workdrive_company_orders_folder_id"], "OC-001") in service.folders


def test_document_archive_reuses_the_mapped_user_folder_for_each_revision():
    class ArchiveTreeService(WorkDriveService):
        def __init__(self, store):
            super().__init__({"ZOHO_WORKDRIVE_ROOT_FOLDER_ID": "users-root"}, store)
            self.folders: dict[tuple[str, str], str] = {}
            self.uploads: list[dict] = []

        def configured(self):
            return True

        def ensure_user_folders(self, _user):
            return {"quotes": "quotes-folder", "orders": "orders-folder"}, {}

        def _folder(self, parent_id, name):
            return self.folders.setdefault((parent_id, name), f"folder-{len(self.folders) + 1}")

        def _resolve_document_folder(self, _parent_id, document_id, _document_number, _mapping_fields):
            return self._mapped_document_folder(document_id, "workdrive_user_document_folder_id")

        def _find_child(self, *_args, **_kwargs):
            return None

        def _request(self, method, path, **kwargs):
            assert method == "POST" and path == "upload"
            self.uploads.append(kwargs["data"])
            return Response(200, {"data": [{"id": f"file-{len(self.uploads)}"}]})

    store = MemoryStore()
    service = ArchiveTreeService(store)
    user = {"_id": "owner", "name": "Owner"}

    first = service.archive_document_version(user, document_type="quote", document_id="quote-1", document_number="QT-001", version_id="quote-1-v01", filename="QT-001-V01.pdf", pdf=b"pdf")
    revision = service.archive_document_version(user, document_type="quote", document_id="quote-1", document_number="QT-001", version_id="quote-1-v02", filename="QT-001-V02.pdf", pdf=b"pdf")
    other = service.archive_document_version(user, document_type="quote", document_id="quote-2", document_number="QT-002", version_id="quote-2-v01", filename="QT-002-V01.pdf", pdf=b"pdf")

    assert first["workdrive_folder_id"] == revision["workdrive_folder_id"]
    assert first["workdrive_folder_id"] != other["workdrive_folder_id"]
    assert len(service.folders) == 2


def test_folder_lookup_searches_later_workdrive_pages_before_creating_a_folder():
    class PagedService(WorkDriveService):
        def __init__(self):
            super().__init__({}, MemoryStore())
            self.offsets: list[int] = []

        def _request(self, method, path, **kwargs):
            assert method == "GET" and path == "files/quotes/files"
            offset = kwargs["params"]["page[offset]"]
            self.offsets.append(offset)
            if offset == 0:
                return Response(200, {"data": [{"id": str(index), "attributes": {"name": f"Other {index}", "is_folder": True}} for index in range(50)]})
            return Response(200, {"data": [{"id": "existing-quote-folder", "attributes": {"name": "MT-MONEDA-005", "is_folder": True}}]})

    service = PagedService()
    assert service._folder("quotes", "MT-MONEDA-005") == "existing-quote-folder"
    assert service.offsets == [0, 50]


def test_document_folder_resolution_rejects_timestamped_legacy_mapping():
    class ResolutionService(WorkDriveService):
        def _request(self, method, path, **_kwargs):
            assert method == "GET"
            if path == "files/timestamped-folder":
                return Response(200, {"data": [{"id": "timestamped-folder", "attributes": {
                    "name": "MT-MONEDA-005 25-09-2026 14:15:27:664", "parent_id": "quotes", "is_folder": True,
                }}]})
            if path == "files/canonical-folder":
                return Response(200, {"data": [{"id": "canonical-folder", "attributes": {
                    "name": "MT-MONEDA-005", "parent_id": "quotes", "is_folder": True,
                }}]})
            raise AssertionError(path)

        def _find_child(self, *_args, **_kwargs):
            return None

    store = MemoryStore()
    store.insert_one("workdrive_document_versions", {
        "document_id": "quote-1", "version_id": "quote-1-v02", "updated_at": "2026-09-25T14:15:00Z",
        "workdrive_user_document_folder_id": "timestamped-folder",
    })
    store.insert_one("workdrive_document_versions", {
        "document_id": "quote-1", "version_id": "quote-1-v01", "updated_at": "2026-09-25T14:00:00Z",
        "workdrive_folder_id": "canonical-folder",
    })
    service = ResolutionService({}, store)
    assert service._resolve_document_folder(
        "quotes", "quote-1", "MT-MONEDA-005",
        ("workdrive_user_document_folder_id", "workdrive_folder_id"),
    ) == "canonical-folder"


def test_document_archive_persists_folder_mapping_on_user_record():
    store = MemoryStore()
    store.insert_one("users", {"_id": "owner", "name": "Owner"})
    version = store.insert_one("quotation_versions", {
        "_id": "version-1", "version": 1, "pdf": b"pdf", "filename": "QT-1-V01.pdf",
    })

    class ArchiveService:
        def archive_document_version(self, *_args, **_kwargs):
            return {
                "workdrive_user_folder_id": "canonical-user",
                "workdrive_profile_folder_id": "canonical-profile",
                "workdrive_quotes_folder_id": "canonical-quotes",
                "workdrive_orders_folder_id": "canonical-orders",
                "workdrive_folder_id": "quote-folder", "workdrive_file_id": "quote-file",
                "workdrive_sync_status": "SYNCED",
            }

    result = archive_version(
        store, ArchiveService(), collection="quotation_versions", version=version,
        owner_user={"_id": "owner"}, document_type="quote", document_id="quote-1",
        document_number="QT-1",
    )
    owner = store.find_one("users", {"_id": "owner"})
    assert owner["workdrive_user_folder_id"] == "canonical-user"
    assert owner["workdrive_quotes_folder_id"] == "canonical-quotes"
    assert result["workdrive_file_id"] == "quote-file"
    assert "workdrive_user_folder_id" not in result


def test_customer_document_archive_persists_company_tree_mapping():
    store = MemoryStore()
    customer = store.insert_one("customers", {"_id": "customer-1", "company_name": "Customer One"})
    version = store.insert_one("quotation_versions", {"_id": "version-1", "version": 1, "pdf": b"pdf", "filename": "QT-1-V01.pdf"})

    class ArchiveService:
        def archive_document_version(self, _owner, **_kwargs):
            return {"workdrive_file_id": "user-file", "workdrive_folder_id": "user-quote-folder", "workdrive_sync_status": "SYNCED", "workdrive_sync_error": None}

        def archive_customer_document_version(self, archived_customer, **_kwargs):
            assert archived_customer["_id"] == "customer-1"
            return {"workdrive_company_root_id": "companies-root", "workdrive_company_folder_id": "company-folder",
                    "workdrive_company_quotations_folder_id": "quotations-folder",
                    "workdrive_company_quotation_folder_id": "quote-folder", "workdrive_customer_folder_id": "quote-folder",
                    "workdrive_customer_file_id": "quote-file", "workdrive_customer_sync_status": "SYNCED", "workdrive_customer_sync_error": None}

    result = archive_version(store, ArchiveService(), collection="quotation_versions", version=version,
        owner_user={"_id": "owner"}, document_type="quote", document_id="quote-1", document_number="QT-1", customer=customer)
    saved_customer = store.find_one("customers", {"_id": "customer-1"})
    assert saved_customer["workdrive_company_folder_id"] == "company-folder"
    assert saved_customer["workdrive_company_quotation_folder_id"] == "quote-folder"
    assert result["workdrive_file_id"] == "user-file"
    assert result["workdrive_customer_file_id"] == "quote-file"


def test_dual_document_archive_reports_partial_failure_and_allows_retry():
    store = MemoryStore()
    customer = store.insert_one("customers", {"_id": "customer-1", "company_name": "Customer One"})
    version = store.insert_one("quotation_versions", {"_id": "version-1", "version": 1, "pdf": b"pdf", "filename": "QT-1-V01.pdf"})

    class ArchiveService:
        def __init__(self): self.customer_attempts = 0
        def archive_document_version(self, _owner, **_kwargs):
            return {"workdrive_file_id": "user-file", "workdrive_folder_id": "user-folder", "workdrive_sync_status": "SYNCED"}
        def archive_customer_document_version(self, _customer, **_kwargs):
            self.customer_attempts += 1
            if self.customer_attempts == 1: raise WorkDriveError("RATE_LIMITED", "retry later", retryable=True)
            return {"workdrive_customer_file_id": "customer-file", "workdrive_customer_folder_id": "customer-folder", "workdrive_customer_sync_status": "SYNCED"}

    service = ArchiveService()
    first = archive_version(store, service, collection="quotation_versions", version=version, owner_user={"_id": "owner"}, document_type="quote", document_id="quote-1", document_number="QT-1", customer=customer)
    second = archive_version(store, service, collection="quotation_versions", version=version, owner_user={"_id": "owner"}, document_type="quote", document_id="quote-1", document_number="QT-1", customer=customer)
    assert first["workdrive_sync_status"] == "FAILED"
    assert first["workdrive_sync_error"] == "DUAL_DESTINATION_SYNC_INCOMPLETE"
    assert second["workdrive_sync_status"] == "SYNCED"
    assert second["workdrive_customer_file_id"] == "customer-file"


def test_dual_document_archive_records_company_access_denial_without_losing_user_mapping():
    store = MemoryStore()
    customer = store.insert_one("customers", {"_id": "customer-1", "company_name": "Customer One"})
    version = store.insert_one("quotation_versions", {"_id": "version-1", "version": 1, "pdf": b"pdf", "filename": "QT-1-V01.pdf"})

    class ArchiveService:
        def archive_document_version(self, _owner, **_kwargs):
            return {"workdrive_file_id": "user-file", "workdrive_folder_id": "user-folder", "workdrive_sync_status": "SYNCED"}

        def archive_customer_document_version(self, _customer, **_kwargs):
            raise WorkDriveError("RESOURCE_ACCESS_DENIED", "No access", stage="api_request", http_status=401,
                                  provider_code="R008", diagnostic_id="company-access-test")

    result = archive_version(store, ArchiveService(), collection="quotation_versions", version=version,
                             owner_user={"_id": "owner"}, document_type="quote", document_id="quote-1",
                             document_number="QT-1", customer=customer)
    assert result["workdrive_sync_status"] == "FAILED"
    assert result["workdrive_file_id"] == "user-file"
    assert result["workdrive_customer_sync_error"] == "RESOURCE_ACCESS_DENIED"
    assert result["workdrive_customer_last_http_status"] == 401
    assert result["workdrive_customer_last_provider_code"] == "R008"
    assert not result.get("workdrive_customer_file_id")


def test_company_asset_deletion_moves_file_to_trash_with_update_scope(monkeypatch):
    service = WorkDriveService({"ZOHO_WORKDRIVE_ENABLED": True}, MemoryStore())
    calls = []
    monkeypatch.setattr(service, "_request", lambda method, path, **kwargs: calls.append((method, path, kwargs)))
    result = service.delete_company_asset({"workdrive_resource_id": "remote-file"})
    assert result == {"remote_deleted": True, "remote_missing": False}
    assert calls == [("PATCH", "files/remote-file", {"json": {"data": {"attributes": {"status": "51"}, "type": "files"}}, "headers": {"Content-Type": "application/vnd.api+json"}})]


def test_company_asset_deletion_treats_missing_remote_file_as_idempotent(monkeypatch):
    service = WorkDriveService({"ZOHO_WORKDRIVE_ENABLED": True}, MemoryStore())
    def missing(*_args, **_kwargs):
        raise WorkDriveError("NOT_FOUND", "missing", http_status=404)
    monkeypatch.setattr(service, "_request", missing)
    assert service.delete_company_asset({"workdrive_resource_id": "remote-file"}) == {"remote_deleted": False, "remote_missing": True}
