"""Scoped bank-detail storage and redaction rules.

Bank details belong to the employee/user record they describe.  Managers get
delegated access to their own team, but that delegation never changes
ownership or grants access to another manager's team.
"""

from __future__ import annotations

from typing import Any

from app.services.business_logic import GLOBAL_ROLE_IDS, accessible_user_ids


BANK_DETAIL_FIELDS = ("account_holder", "bank_name", "account_number", "ifsc", "iban", "swift", "notes")
SENSITIVE_BANK_FIELDS = {"account_number", "iban", "swift"}


def _role(user: dict[str, Any] | None) -> str:
    return str((user or {}).get("role_id") or "")


def can_view_bank_details(store: Any, actor: dict[str, Any] | None, target_user_id: str) -> bool:
    actor = actor or {}
    target = str(target_user_id or "").strip()
    if not target:
        return False
    if _role(actor) in GLOBAL_ROLE_IDS:
        return True
    return target in set(accessible_user_ids(store, actor))


def can_edit_bank_details(store: Any, actor: dict[str, Any] | None, target_user_id: str) -> bool:
    actor = actor or {}
    actor_id = str(actor.get("_id") or "").strip()
    target = str(target_user_id or "").strip()
    if not actor_id or not target:
        return False
    if _role(actor) in GLOBAL_ROLE_IDS:
        return True
    # A manager may update their own details and details for users directly
    # managed by them.  Ordinary users may update only their own record.
    return target in set(accessible_user_ids(store, actor))


def mask_value(value: Any, *, keep: int = 4) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    compact = "".join(char for char in raw if char.isalnum())
    if len(compact) <= keep:
        return "•" * max(1, len(compact))
    return "•" * max(4, len(compact) - keep) + compact[-keep:]


def serialize_bank_details(row: dict[str, Any] | None, *, include_sensitive: bool = False) -> dict[str, Any]:
    row = row or {}
    result = {"_id": row.get("_id"), "user_id": row.get("user_id"), "updated_at": row.get("updated_at"), "created_at": row.get("created_at")}
    for field in BANK_DETAIL_FIELDS:
        value = row.get(field)
        if field in SENSITIVE_BANK_FIELDS and not include_sensitive:
            result[field] = mask_value(value)
            result[f"{field}_last4"] = mask_value(value, keep=4)[-4:] if value else ""
        else:
            result[field] = value or ""
    result["has_details"] = any(str(row.get(field) or "").strip() for field in BANK_DETAIL_FIELDS if field != "notes")
    return result


def normalize_bank_changes(payload: dict[str, Any]) -> dict[str, str]:
    changes: dict[str, str] = {}
    for field in BANK_DETAIL_FIELDS:
        if field in payload:
            changes[field] = str(payload.get(field) or "").strip()[:500]
    return changes


def audit_bank_diff(old: dict[str, Any] | None, new: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Return a safe audit payload without storing raw account identifiers."""
    old = old or {}
    changed: dict[str, dict[str, str]] = {}
    for field in fields:
        old_value = old.get(field)
        new_value = new.get(field)
        if field in SENSITIVE_BANK_FIELDS:
            changed[field] = {"old": mask_value(old_value), "new": mask_value(new_value)}
        else:
            changed[field] = {"old": str(old_value or ""), "new": str(new_value or "")}
    return {"fields": changed}
