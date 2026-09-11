from __future__ import annotations

import hashlib
from html import escape

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.devices.service import (
    APPROVAL_APPROVED, APPROVAL_DECLINED, APPROVAL_EXPIRED, APPROVAL_PENDING,
    APPROVED, DENIED, PENDING, safe_device, _append_history, _history_entry,
    notify_device_decision,
)
from app.middleware.access import current_user
from app.repositories.store import ensure_utc, utcnow
from app.services.audit import audit


bp = Blueprint("devices", __name__, url_prefix="/api")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lookup(token: str) -> dict | None:
    """Resolve a bearer token to its isolated approval and device records."""
    if not token or len(token) > 256:
        return None
    store = current_app.extensions["store"]
    approval = store.find_one("login_approvals", {"token_hash": _hash(token)})
    if approval:
        device = store.find_one("devices", {"_id": approval.get("device_id"), "user_id": approval.get("user_id")}) or {}
        expires = ensure_utc(approval.get("expires_at"))
        if approval.get("status") == APPROVAL_PENDING and expires and expires <= utcnow():
            approval = store.update_one("login_approvals", {"_id": approval["_id"], "status": APPROVAL_PENDING}, {"status": APPROVAL_EXPIRED, "expired_at": utcnow()}) or {**approval, "status": APPROVAL_EXPIRED}
            if device and device.get("device_status") == PENDING and device.get("approval_attempt_id") == approval.get("_id"):
                store.update_one("devices", {"_id": device["_id"], "device_status": PENDING}, {"device_status": DENIED, "approval_state": APPROVAL_EXPIRED})
                device = store.find_one("devices", {"_id": device["_id"]}) or device
        return {"approval": approval, "device": device}
    device = store.find_one("devices", {"approval_token_hash": _hash(token)})
    return {"approval": None, "device": device} if device else None


def _html(title: str, body: str, status: int = 200):
    markup = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
              "<title>Moneda Technologies</title><style>body{font-family:Arial,sans-serif;background:#f3f4f2;display:grid;place-items:center;min-height:100vh;margin:0}"
              "main{background:#fff;padding:36px;max-width:560px;border-radius:16px;box-shadow:0 16px 48px #0001}h1{font-size:24px}"
              "button{padding:12px 20px;border:0;border-radius:8px;background:#16865b;color:#fff;font-weight:700;cursor:pointer;margin-right:8px}"
              "button[name=action][value=deny]{background:#b42318}small{color:#666}</style></head><body><main>"
              "<div style='letter-spacing:.14em;font-weight:700;color:#666'>MONEDA TECHNOLOGIES</div>" + body + "</main></body></html>")
    return markup, status, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"}


def _decision(token: str, action: str, reason: str, actor: dict | None):
    target = APPROVED if action == "approve" else DENIED if action in {"deny", "decline", "reject"} else None
    if not target:
        return failure("Choose accept or decline", status=422, error="invalid_decision")
    resolved = _lookup(token)
    if not resolved:
        return failure("This login approval link is invalid", status=410, error="approval_link_invalid")
    approval = resolved.get("approval") or {}
    device = resolved.get("device") or {}
    if approval.get("status") == APPROVAL_EXPIRED:
        return failure("This login approval link has expired", status=410, error="approval_expired")
    if approval and approval.get("status") != APPROVAL_PENDING:
        return failure("This login approval has already been processed", status=409, error="decision_already_completed")
    if not approval and str(device.get("device_status") or "") != PENDING:
        return failure("This login approval has already been processed", status=409, error="decision_already_completed")
    if target == DENIED and not reason:
        reason = "Declined from the Moneda approval link."
    now = utcnow()
    previous_status = str(device.get("device_status") or PENDING)
    if approval:
        approval_id = approval.get("_id")
        decided = current_app.extensions["store"].update_one("login_approvals", {
            "_id": approval_id, "status": APPROVAL_PENDING, "token_hash": _hash(token),
        }, {"status": APPROVAL_APPROVED if target == APPROVED else APPROVAL_DECLINED,
            "decided_at": now, "decided_by_user_id": (actor or {}).get("_id")})
        if not decided:
            return failure("This login approval has already been processed", status=409, error="decision_already_completed")
    changes = {"device_status": target, "approval_state": APPROVAL_APPROVED if target == APPROVED else APPROVAL_DECLINED,
               "device_history": _append_history(device, _history_entry(
                   "DEVICE_APPROVED" if target == APPROVED else "DEVICE_DENIED", device,
                   previous_status=previous_status, new_status=target,
                   actor_user_id=str((actor or {}).get("_id") or "email_approval"), reason=reason or None,
               ))}
    if target == APPROVED:
        changes.update({"approved_at": now, "approved_by": (actor or {}).get("_id"), "approved_by_user_id": (actor or {}).get("_id"),
                        "approved_by_name": (actor or {}).get("name") or (actor or {}).get("username") or "Email approval",
                        "previous_status": previous_status, "new_status": target})
    else:
        changes.update({"denied_at": now, "denied_by": (actor or {}).get("_id"), "denied_by_user_id": (actor or {}).get("_id"),
                        "denied_by_name": (actor or {}).get("name") or (actor or {}).get("username") or "Email approval",
                        "denial_reason": reason, "previous_status": previous_status, "new_status": target})
    # The device credential is shared by concurrent sessions; the approval
    # transaction above is what isolates this token. Once any valid pending
    # transaction is accepted, that credential becomes trusted.
    query = {"_id": device.get("_id"), "device_status": PENDING}
    updated = current_app.extensions["store"].update_one("devices", query, changes) if device else None
    if not updated and device and target == APPROVED and device.get("device_status") == APPROVED:
        updated = device
    if device and not updated:
        return failure("Decision already completed by another administrator", status=409, error="decision_already_completed")
    audit("DEVICE_APPROVED" if target == APPROVED else "DEVICE_DENIED", "device", str((device or {}).get("_id") or ""), {
        "user_id": (device or {}).get("user_id"), "actor_user_id": (actor or {}).get("_id") or "email_approval",
        "reason": reason or None, "previous_status": previous_status, "new_status": target,
    })
    user = current_app.extensions["store"].find_one("users", {"_id": (device or {}).get("user_id")}) or {}
    notify_device_decision(user, updated or device, approved=target == APPROVED, reason=reason)
    return updated or device, user, target


@bp.route("/device-approval/<token>", methods=["GET", "POST"])
@bp.route("/auth/login-approval/<token>", methods=["GET", "POST"])
def device_approval(token: str):
    resolved = _lookup(token)
    action = str(request.args.get("action") or "").lower() if request.method == "GET" else str((request.get_json(silent=True) or {}).get("action") or request.form.get("action") or "").lower()
    # Email links are bearer capabilities: the high-entropy one-time token is
    # the authorization. Existing authenticated Superadmin POSTs remain valid.
    actor = current_user()
    if action and request.method in {"GET", "POST"}:
        if actor and (str(actor.get("role_id")) != "superadmin" or not actor.get("active", False)):
            return failure("Only active Superadmins can decide login requests", status=403, error="superadmin_required")
        payload = request.get_json(silent=True) or {}
        result = _decision(token, action, str(payload.get("reason") or request.form.get("reason") or "").strip(), actor)
        if not isinstance(result, tuple) or len(result) != 3 or isinstance(result[1], int):
            return result
        updated, user, target = result
        message = "Login accepted" if target == APPROVED else "Login declined"
        if request.is_json:
            return success({"device": safe_device(updated), "decision": target}, message)
        return _html(message, f"<h1>{escape(message)}</h1><p><strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br><strong>Device:</strong> {escape(str(updated.get('device_name') or 'Trusted device'))}<br><strong>Status:</strong> {escape(target.title())}</p>")
    if not resolved:
        return _html("Approval unavailable", "<h1>This login approval link is invalid or unavailable</h1><p>It may have expired or already been handled.</p>", 410)
    approval, device = resolved.get("approval") or {}, resolved.get("device") or {}
    if approval.get("status") == APPROVAL_EXPIRED:
        return _html("Approval expired", "<h1>This login approval link has expired</h1><p>Ask the user to sign in again to create a new request.</p>", 410)
    user = current_app.extensions["store"].find_one("users", {"_id": device.get("user_id")}) or {}
    detail = f"<p><strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br><strong>Email:</strong> {escape(str(user.get('email') or ''))}<br><strong>Device:</strong> {escape(str(device.get('device_name') or 'Trusted device'))}<br><strong>Status:</strong> Pending approval</p>"
    form = f"<h1>Review login access</h1>{detail}<form method='post'><label>Reason (optional for decline)<br><textarea name='reason' rows='3' style='width:100%;margin:10px 0' placeholder='Explain this decision'></textarea></label><br><button name='action' value='approve'>Accept Login</button><button name='action' value='deny'>Decline Login</button></form><p><small>These links are one-time and expire after 20 minutes.</small></p>"
    return _html("Review login access", form)
