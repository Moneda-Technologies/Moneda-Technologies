from __future__ import annotations

from pathlib import Path
from typing import Any

from app.account.photo import photo_path
from app.account.signature import restore_signature, signature_path
from app.repositories.store import utcnow
from app.services.workdrive import FAILED, PENDING, WorkDriveError, WorkDriveService


def workdrive_public_status(user: dict[str, Any], asset: str, service: WorkDriveService) -> str:
    if not service.enabled():
        return "DISABLED"
    return str(user.get(f"{asset}_workdrive_sync_status") or PENDING)


def profile_asset_state(user: dict[str, Any], asset: str, upload_directory: str | Path,
                        service: WorkDriveService) -> str:
    """Classify an asset using persisted metadata and the physical file."""
    if not user.get(f"{asset}_path"):
        if asset == "signature" and user.get("signature_workdrive_resource_id"):
            return "REMOTE_ONLY"
        return "NO_LOCAL_ASSET"
    path = photo_path(user, upload_directory) if asset == "photo" else signature_path(user, upload_directory)
    if not path:
        return "MISSING_LOCAL"
    return workdrive_public_status(user, asset, service)


def synchronize_profile_asset(store: Any, service: WorkDriveService, user: dict[str, Any],
                              asset: str, upload_directory: str | Path) -> dict[str, Any]:
    """Synchronize one already-persisted local asset without risking local data."""
    if asset not in {"photo", "signature"}:
        raise ValueError("Unsupported profile asset")
    if not service.enabled():
        return user
    user_id = str(user.get("_id") or "")
    prefix = f"{asset}_workdrive_"
    path = photo_path(user, upload_directory) if asset == "photo" else signature_path(user, upload_directory)
    if not path:
        if asset == "signature" and user.get("signature_workdrive_resource_id"):
            try:
                data = service.download_asset(str(user["signature_workdrive_resource_id"]))
                metadata = restore_signature(
                    user_id, upload_directory, data,
                    str(user.get("signature_workdrive_filename") or user.get("signature_filename") or "signature.png"),
                    str(user.get("signature_mime_type") or "image/png"),
                )
                changes = {**metadata, f"{prefix}sync_status": "SYNCED", f"{prefix}sync_error": None, f"{prefix}updated_at": utcnow()}
                return store.update_one("users", {"_id": user_id}, changes) or {**user, **changes}
            except (WorkDriveError, ValueError):
                changes = {f"{prefix}sync_status": FAILED, f"{prefix}sync_error": "REMOTE_RESTORE_FAILED", f"{prefix}updated_at": utcnow()}
                return store.update_one("users", {"_id": user_id}, changes) or {**user, **changes}
        changes = {
            f"{prefix}sync_status": FAILED,
            f"{prefix}sync_error": "LOCAL_FILE_MISSING",
        }
        return store.update_one("users", {"_id": user_id}, changes) or {**user, **changes}
    pending = store.update_one("users", {"_id": user_id}, {
        f"{prefix}sync_status": PENDING,
        f"{prefix}sync_error": None,
    }) or user
    try:
        changes = service.sync_asset(
            pending, asset, path,
            str(pending.get(f"{asset}_mime_type") or "image/png"),
        )
    except WorkDriveError as exc:
        changes = {
            f"{prefix}sync_status": FAILED,
            f"{prefix}sync_error": exc.code,
            f"{prefix}updated_at": utcnow(),
        }
    return store.update_one("users", {"_id": user_id}, changes) or {**pending, **changes}


def workdrive_fields(asset: str) -> list[str]:
    return [
        f"{asset}_workdrive_resource_id", f"{asset}_workdrive_path",
        f"{asset}_workdrive_filename", f"{asset}_workdrive_updated_at",
        f"{asset}_workdrive_sync_status", f"{asset}_workdrive_sync_error",
    ]
