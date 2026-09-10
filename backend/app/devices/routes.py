from __future__ import annotations

import hashlib
from html import escape

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.devices.service import APPROVED, DENIED, PENDING, safe_device, _append_history, _history_entry, notify_device_decision
from app.middleware.access import current_user
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("devices", __name__, url_prefix="/api")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lookup(token: str):
    return current_app.extensions["store"].find_one("devices", {
        "approval_token_hash": _hash(token),
        "approval_token_expires_at": {"$gte": utcnow()},
    })


def _html(title: str, body: str, status: int = 200):
    markup = f"<!doctype html><html><head><meta charset='utf-8'><title>Moneda Technologies</title><style>body{{font-family:Arial,sans-serif;background:#f3f4f2;display:grid;place-items:center;min-height:100vh;margin:0}}main{{background:#fff;padding:36px;max-width:520px;border-radius:16px;box-shadow:0 16px 48px #0001}}h1{{font-size:24px}}button{{padding:12px 20px;border:0;border-radius:8px;background:#e3342f;color:#fff;font-weight:700;cursor:pointer;margin-right:8px}}button[name=action][value=deny]{{background:#222}}small{{color:#666}}</style></head><body><main><div style='letter-spacing:.14em;font-weight:700;color:#666'>MONEDA TECHNOLOGIES</div>{body}</main></body></html>"
    return markup, status, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


@bp.route("/device-approval/<token>", methods=["GET", "POST"])
def device_approval(token: str):
    device = _lookup(token)
    if request.method == "GET":
        if not device:
            return _html("Request unavailable", "<h1>This device request is unavailable</h1><p>It may have expired or already been handled.</p>", 410)
        user = current_app.extensions["store"].find_one("users", {"_id": device.get("user_id")}) or {}
        detail = f"<p><strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br><strong>Device:</strong> {escape(str(device.get('device_name') or 'Trusted device'))}<br><strong>Status:</strong> Pending</p>"
        form = f"<h1>Review device access</h1>{detail}<form method='post'><label>Reason (required when denying)<br><textarea name='reason' rows='3' style='width:100%;margin:10px 0' placeholder='Explain this decision'></textarea></label><br><button name='action' value='approve'>Approve device</button><button name='action' value='deny'>Deny device</button></form><p><small>Sign in as an active Superadmin before submitting a decision.</small></p>"
        return _html("Review device access", form)
    actor = current_user()
    if not actor:
        return failure("Superadmin authentication required", status=401, error="authentication_required")
    if str(actor.get("role_id")) != "superadmin" or not actor.get("active", False):
        return failure("Only active Superadmins can decide device requests", status=403, error="superadmin_required")
    action = str((request.get_json(silent=True) or {}).get("action") or request.form.get("action") or "").lower()
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or request.form.get("reason") or "").strip()
    target = APPROVED if action == "approve" else DENIED if action in {"deny", "reject"} else None
    if not target:
        return failure("Choose approve or deny", status=422)
    if not device:
        return failure("This device request has expired or already been handled", status=409, error="decision_already_completed")
    if target == DENIED and not reason:
        return failure("A denial reason is required", status=422, error="decision_reason_required")
    now = utcnow()
    previous_status = str(device.get("device_status") or PENDING)
    changes = {"device_status": target, "device_history": _append_history(device, _history_entry(
        "DEVICE_APPROVED" if target == APPROVED else "DEVICE_DENIED", device,
        previous_status=previous_status, new_status=target,
        actor_user_id=str(actor.get("_id") or ""), reason=reason or None,
    ))}
    if target == APPROVED:
        changes.update({"approved_at": now, "approved_by": actor.get("_id"), "approved_by_user_id": actor.get("_id"), "approved_by_name": actor.get("name") or actor.get("username"), "previous_status": previous_status, "new_status": target})
        event, message = "device_approved", "Device approved successfully"
    else:
        changes.update({"denied_at": now, "denied_by": actor.get("_id"), "denied_by_user_id": actor.get("_id"), "denied_by_name": actor.get("name") or actor.get("username"), "denial_reason": reason, "previous_status": previous_status, "new_status": target})
        event, message = "device_rejected", "Device access denied"
    updated = current_app.extensions["store"].update_one(
        "devices", {"_id": device["_id"], "device_status": PENDING, "approval_token_hash": _hash(token)},
        changes, unset_fields=["approval_token_hash", "approval_token_expires_at"],
    )
    if not updated:
        return failure("Decision already completed by another administrator", status=409, error="decision_already_completed")
    audit("DEVICE_APPROVED" if target == APPROVED else "DEVICE_DENIED", "device", str(device["_id"]), {"user_id": device.get("user_id"), "actor_user_id": actor.get("_id"), "reason": reason or None, "previous_status": previous_status, "new_status": target})
    if target == DENIED:
        audit("DEVICE_SESSION_TERMINATED", "device", str(device["_id"]), {"user_id": device.get("user_id"), "actor_user_id": actor.get("_id"), "reason": reason})
    user = current_app.extensions["store"].find_one("users", {"_id": device.get("user_id")}) or {}
    notify_device_decision(user, updated, approved=target == APPROVED, reason=reason)
    if request.is_json:
        return success({"device": safe_device(updated), "decision": target}, message)
    verb = "Approved" if target == APPROVED else "Denied"
    body = f"<h1>{escape(message)}</h1><p><strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br><strong>Device:</strong> {escape(str(device.get('device_name') or 'Trusted device'))}<br><strong>{verb} by:</strong> {escape(str(actor.get('name') or actor.get('username') or 'Superadmin'))}<br><strong>Status:</strong> {escape(target.title())}</p>"
    return _html(message, body)
