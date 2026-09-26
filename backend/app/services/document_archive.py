from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from app.services.workdrive import FAILED, MISSING_LOCAL, PENDING, SYNCED, WorkDriveError


logger = logging.getLogger(__name__)

IGNORED_DIFF_KEYS = {"_id", "created_at", "updated_at", "workdrive_file_id", "workdrive_folder_id",
                     "workdrive_sync_status", "workdrive_synced_at", "workdrive_sync_error"}


def _diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    if isinstance(old, dict) and isinstance(new, dict):
        result: list[dict[str, Any]] = []
        for key in sorted(set(old) | set(new)):
            if key in IGNORED_DIFF_KEYS:
                continue
            child = f"{path}.{key}" if path else key
            result.extend(_diff(old.get(key), new.get(key), child))
        return result
    if isinstance(old, list) and isinstance(new, list):
        result = []
        for index in range(max(len(old), len(new))):
            result.extend(_diff(old[index] if index < len(old) else None, new[index] if index < len(new) else None, f"{path}[{index}]"))
        return result
    if old != new:
        return [{"field": path, "old_value": deepcopy(old), "new_value": deepcopy(new)}]
    return []


def build_revision_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    changes = _diff(previous or {}, current)
    labels = []
    for change in changes[:5]:
        field = str(change["field"]).replace("_", " ")
        labels.append(field.split(".")[-1])
    summary = "Original snapshot" if previous is None else (f"{', '.join(dict.fromkeys(labels))} updated" if labels else "No business fields changed")
    return changes, summary


def archive_version(store: Any, workdrive: Any, *, collection: str, version: dict[str, Any],
                    owner_user: dict[str, Any], document_type: str, document_id: str,
                    document_number: str, customer: dict[str, Any] | None = None) -> dict[str, Any]:
    """Best-effort archive: Mongo remains authoritative when WorkDrive fails."""
    version_id = str(version["_id"])
    version_number = int(version.get("version") or 1)
    archive_context = f"{document_type}_id={document_id} document_number={document_number} version={version_number} version_id={version_id}"
    # Do not short-circuit from version metadata alone. Older rows can contain
    # a synced file ID while the deterministic mapping row is missing (for
    # example, if the first local mapping write was interrupted). Let the
    # WorkDrive service reconcile the mapping and remote filename idempotently.
    if not version.get("pdf"):
        changes = {"workdrive_sync_status": MISSING_LOCAL, "workdrive_sync_error": "PDF_SNAPSHOT_MISSING"}
        logger.warning("workdrive_document_archive result=SKIPPED reason=PDF_SNAPSHOT_MISSING %s", archive_context)
    else:
        archive_args = {"document_type": document_type, "document_id": document_id,
            "document_number": document_number, "version_id": version_id,
            "filename": str(version.get("filename") or f"{document_number}-V{int(version.get('version') or 1):02d}.pdf"),
            "pdf": version["pdf"]}
        logger.info("workdrive_document_archive result=START %s has_customer=%s", archive_context, bool(customer))
        def safe_error_fields(exc: WorkDriveError) -> dict[str, Any]:
            """Persist retry-safe provider diagnostics; never persist credentials."""
            return {
                "workdrive_last_http_status": exc.http_status,
                "workdrive_last_provider_code": exc.provider_code,
                "workdrive_last_diagnostic_id": exc.diagnostic_id,
            }

        try:
            user_changes = workdrive.archive_document_version(owner_user, **archive_args)
        except WorkDriveError as exc:
            user_changes = {"workdrive_sync_status": FAILED, "workdrive_sync_error": exc.code,
                            **safe_error_fields(exc)}
            logger.warning(
                "workdrive_document_archive destination=user result=FAIL %s error_code=%s provider_status=%s provider_code=%s provider_message=%s provider_body=%s endpoint_host=%s endpoint_path=%s diagnostic_id=%s",
                archive_context, exc.code, exc.http_status, exc.provider_code, exc.provider_message,
                exc.provider_body, exc.endpoint_host, exc.endpoint_path, exc.diagnostic_id,
            )
        except Exception:
            user_changes = {"workdrive_sync_status": PENDING, "workdrive_sync_error": "ARCHIVE_UNAVAILABLE"}
            logger.exception("workdrive_document_archive destination=user result=ERROR %s", archive_context)
        logger.info(
            "workdrive_document_archive destination=user result=%s %s folder_id=%s file_id=%s",
            user_changes.get("workdrive_sync_status"), archive_context,
            user_changes.get("workdrive_user_document_folder_id") or user_changes.get("workdrive_folder_id"), user_changes.get("workdrive_file_id"),
        )
        if customer:
            try:
                customer_changes = workdrive.archive_customer_document_version(customer, **archive_args)
            except WorkDriveError as exc:
                customer_changes = {
                    "workdrive_customer_sync_status": FAILED,
                    "workdrive_customer_sync_error": exc.code,
                    "workdrive_customer_last_http_status": exc.http_status,
                    "workdrive_customer_last_provider_code": exc.provider_code,
                    "workdrive_customer_last_diagnostic_id": exc.diagnostic_id,
                }
                logger.warning(
                    "workdrive_document_archive destination=company result=FAIL %s error_code=%s provider_status=%s provider_code=%s provider_message=%s provider_body=%s endpoint_method=%s endpoint_path=%s endpoint_resource_id=%s diagnostic_id=%s",
                    archive_context, exc.code, exc.http_status, exc.provider_code, exc.provider_message,
                    exc.provider_body, exc.endpoint_method, exc.endpoint_path, exc.endpoint_resource_id, exc.diagnostic_id,
                )
            except Exception:
                customer_changes = {"workdrive_customer_sync_status": PENDING, "workdrive_customer_sync_error": "ARCHIVE_UNAVAILABLE"}
                logger.exception("workdrive_document_archive destination=company result=ERROR %s", archive_context)
            logger.info(
                "workdrive_document_archive destination=company result=%s %s folder_id=%s file_id=%s",
                customer_changes.get("workdrive_customer_sync_status"), archive_context,
                customer_changes.get("workdrive_customer_folder_id"), customer_changes.get("workdrive_customer_file_id"),
            )
            user_ok = user_changes.get("workdrive_sync_status") == SYNCED
            customer_ok = customer_changes.get("workdrive_customer_sync_status") == SYNCED
            changes = {**user_changes, **customer_changes,
                       "workdrive_sync_status": SYNCED if user_ok and customer_ok else FAILED if not user_ok or not customer_ok else PENDING,
                       "workdrive_sync_error": None if user_ok and customer_ok else "DUAL_DESTINATION_SYNC_INCOMPLETE"}
        else:
            changes = user_changes
    folder_fields = {
        key: changes.pop(key) for key in list(changes)
        if key in {
            "workdrive_user_folder_id", "workdrive_profile_folder_id",
            "workdrive_quotes_folder_id", "workdrive_orders_folder_id",
        }
    }
    if folder_fields and owner_user.get("_id"):
        store.update_one("users", {"_id": owner_user["_id"]}, folder_fields)
    company_folder_fields = {key: changes.pop(key) for key in list(changes) if key.startswith("workdrive_company_")}
    if company_folder_fields and customer and customer.get("_id"):
        store.update_one("customers", {"_id": customer["_id"]}, company_folder_fields)
    store.update_one(collection, {"_id": version_id}, changes)
    logger.info(
        "workdrive_document_archive result=%s %s user_status=%s company_status=%s",
        changes.get("workdrive_sync_status"), archive_context, user_changes.get("workdrive_sync_status") if version.get("pdf") else None,
        customer_changes.get("workdrive_customer_sync_status") if version.get("pdf") and customer else None,
    )
    return {**version, **changes}
