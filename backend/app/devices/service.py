from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g, request, session

from app.api.responses import failure
from app.repositories.store import ensure_utc, utcnow
from app.services.audit import audit
from app.devices.geolocation import client_ip, public_ip as normalize_public_ip, resolve as resolve_location


PENDING = "pending"
APPROVED = "approved"
REVOKED = "revoked"
DENIED = "denied"


def _history_entry(action: str, device: dict[str, Any], *, previous_status: str | None,
                   new_status: str | None, actor_user_id: str | None = None,
                   reason: str | None = None, timestamp=None) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "action": action,
        "previous_status": previous_status,
        "new_status": new_status,
        "actor_user_id": actor_user_id,
        "timestamp": timestamp or utcnow(),
    }
    if reason:
        entry["reason"] = reason
    return entry


def _append_history(device: dict[str, Any], entry: dict[str, Any]) -> list[dict[str, Any]]:
    return [*list(device.get("device_history") or []), entry]


def policy_for(user: dict[str, Any]) -> str:
    # An active Superadmin is the recovery authority and must never be locked
    # out by the device approval workflow. This is enforced server-side and
    # cannot be changed by frontend state or a user-supplied policy value.
    if str(user.get("role_id") or "") == "superadmin":
        return "any_authorized_device"
    default = current_app.config.get("DEVICE_ACCESS_MODE", "approved_devices_only")
    configured = str(user.get("device_access_mode") or default)
    return configured if configured in {"any_authorized_device", "approved_devices_only"} else "approved_devices_only"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _device_name() -> str:
    ua = request.user_agent
    browser = ua.browser or "Browser"
    platform = ua.platform or "device"
    return f"{browser.title()} on {platform.title()}"


def _device_type() -> str:
    ua = request.user_agent.string.lower()
    if any(value in ua for value in ("mobile", "iphone", "android")):
        return "Mobile"
    if "tablet" in ua or "ipad" in ua:
        return "Tablet"
    return "Desktop"


def _client_details() -> dict[str, str]:
    """Extract coarse client details from the request user-agent.

    This intentionally records browser/OS family and versions only; it does
    not fingerprint hardware or persist a device model.
    """
    ua = request.user_agent.string or ""
    browser = request.user_agent.browser or "Browser"
    browser_version = request.user_agent.version or ""
    if not browser_version:
        match = re.search(r"(?:Chrome|Firefox|Edg|Version|Safari|OPR)[/ ]([\d.]+)", ua, re.I)
        browser_version = match.group(1) if match else ""
    platform = request.user_agent.platform or "Unknown"
    os_version = ""
    if platform.lower() == "windows":
        match = re.search(r"Windows NT ([\d.]+)", ua, re.I)
        os_version = match.group(1) if match else ""
    elif platform.lower() in {"macos", "mac"}:
        match = re.search(r"Mac OS X[ /]([\d_\.]+)", ua, re.I)
        os_version = match.group(1).replace("_", ".") if match else ""
    elif platform.lower() == "android":
        match = re.search(r"Android ([\d.]+)", ua, re.I)
        os_version = match.group(1) if match else ""
    elif platform.lower() in {"ios", "iphone", "ipad"}:
        match = re.search(r"(?:OS|CPU (?:iPhone )?OS) ([\d_]+)", ua, re.I)
        os_version = match.group(1).replace("_", ".") if match else ""
    return {
        "browser_name": browser.title(), "browser_version": str(browser_version or ""),
        "os_name": platform.title(), "os_version": os_version,
    }


def _device_from_request(user: dict[str, Any]) -> dict[str, Any] | None:
    token = request.cookies.get(current_app.config.get("DEVICE_COOKIE_NAME", "moneda_device"))
    if not token or len(token) > 256:
        return None
    return current_app.extensions["store"].find_one(
        "devices", {"user_id": str(user.get("_id")), "token_hash": _token_hash(token)}
    )


def _cookie_token() -> str:
    cookie_name = current_app.config.get("DEVICE_COOKIE_NAME", "moneda_device")
    token = request.cookies.get(cookie_name)
    if token and len(token) <= 256:
        return token
    token = secrets.token_urlsafe(32)
    g.device_cookie_value = token
    return token


def ensure_login_device(user: dict[str, Any]) -> dict[str, Any]:
    """Bind the authenticated session to a server-side browser credential.

    Only a SHA-256 hash is persisted. The browser receives the opaque token in
    an HttpOnly cookie; no MAC address or frontend storage is involved.
    """
    store = current_app.extensions["store"]
    token = _cookie_token()
    token_hash = _token_hash(token)
    user_id = str(user["_id"])
    existing = store.find_one("devices", {"user_id": user_id, "token_hash": token_hash})
    now = utcnow()
    client = _client_details()
    client_address = client_ip()
    public_ip = normalize_public_ip(client_address)
    location = resolve_location(public_ip)
    # A revoked/denied credential cannot silently become trusted again. Rotate
    # the opaque browser token so the next login creates a fresh request.
    if existing and existing.get("device_status") in {REVOKED, DENIED}:
        existing_history = _append_history(existing, _history_entry(
            "DEVICE_LOGIN_ATTEMPT", existing, previous_status=existing.get("device_status"),
            new_status=existing.get("device_status"), timestamp=now,
        ))
        store.update_one("devices", {"_id": existing["_id"]}, {
            "last_seen_at": now, "last_activity_at": now, "last_login_at": now,
            "last_login_result": str(existing.get("device_status")), "last_ip": public_ip,
            "public_ip": public_ip, **client, **location, "location": location,
            "device_history": existing_history,
        })
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        g.device_cookie_value = token
        existing = None
    if existing:
        if existing.get("device_status") != REVOKED:
            store.update_one("devices", {"_id": existing["_id"]}, {
                "last_seen_at": now, "last_activity_at": now, "last_ip": public_ip, "public_ip": public_ip,
                "user_agent": request.user_agent.string[:300],
                **client, **location, "location": location, "last_login_at": now,
                "last_login_result": str(existing.get("device_status") or PENDING),
            })
        device = {**existing, "last_seen_at": now}
        token_expires = ensure_utc(device.get("approval_token_expires_at"))
        device["device_history"] = _append_history(device, _history_entry(
            "DEVICE_LOGIN_ATTEMPT", device, previous_status=device.get("device_status"),
            new_status=device.get("device_status"), timestamp=now,
        ))
        store.update_one("devices", {"_id": existing["_id"]}, {
            "last_seen_at": now, "device_history": device["device_history"],
        })
        if device.get("device_status") == PENDING and token_expires and token_expires <= now:
            _notify_superadmins(user, device)
    else:
        status = APPROVED if policy_for(user) == "any_authorized_device" else PENDING
        device = store.insert_one("devices", {
            "user_id": user_id, "token_hash": token_hash, "device_ref": f"DVC-{secrets.token_hex(4).upper()}", "device_name": _device_name(),
            "device_status": status, "registered_at": now, "last_seen_at": now,
            "last_ip": public_ip, "public_ip": public_ip, "user_agent": request.user_agent.string[:300],
            "browser": request.user_agent.browser, "operating_system": request.user_agent.platform,
            "device_type": _device_type(), "last_activity_at": now,
            **client, "last_login_at": now, "last_login_result": status,
            "approval_required": status == PENDING,
            **location, "location": location,
            "approved_at": now if status == APPROVED else None,
            "device_history": [_history_entry("DEVICE_LOGIN_ATTEMPT", {}, previous_status=None, new_status=status, timestamp=now)],
        })
        audit("device_registered", "device", str(device["_id"]), {"user_id": user_id, "device_status": status})
        if status == PENDING:
            audit("device_approval_requested", "device", str(device["_id"]), {"user_id": user_id})
            device["device_history"].append(_history_entry("DEVICE_APPROVAL_REQUESTED", device, previous_status=status, new_status=PENDING))
            store.update_one("devices", {"_id": device["_id"]}, {"device_history": device["device_history"]})
            _notify_superadmins(user, device)
    return device


def establish_device_session(user: dict[str, Any]) -> dict[str, Any]:
    device = ensure_login_device(user)
    status = str(device.get("device_status") or PENDING)
    session["device_id"] = device["_id"]
    session["device_status"] = status
    session["device_access_mode"] = policy_for(user)
    if str(user.get("role_id") or "") == "superadmin":
        audit("superadmin_device_gate_exempted", "user", str(user.get("_id")), {"device_id": str(device.get("_id"))})
    return {"device_status": status, "application_access": status == APPROVED}


def current_device(user: dict[str, Any] | None = None) -> dict[str, Any] | None:
    user_id = str((user or {}).get("_id") or session.get("user_id") or "")
    device_id = session.get("device_id")
    if not user_id or not device_id:
        return None
    return current_app.extensions["store"].find_one("devices", {"_id": device_id, "user_id": user_id})


def device_access_status(user: dict[str, Any] | None = None) -> dict[str, Any]:
    user = user or {}
    if policy_for(user) == "any_authorized_device":
        return {"device_status": APPROVED, "application_access": True}
    device = current_device(user)
    status = str((device or {}).get("device_status") or session.get("device_status") or PENDING)
    return {
        "device_status": status, "application_access": status == APPROVED,
        "approval_request_id": (device or {}).get("approval_request_id"),
        "device_name": (device or {}).get("device_name"),
        "registered_at": (device or {}).get("registered_at"),
        "reinstated_at": (device or {}).get("reinstated_at"),
        "reinstatement_reason": (device or {}).get("reinstatement_reason"),
        "location": (device or {}).get("location"),
    }


def enforce_device_access(user: dict[str, Any] | None = None):
    user = user or {}
    if policy_for(user) == "any_authorized_device":
        return None
    status = device_access_status(user)
    if status["device_status"] == APPROVED:
        return None
    audit("device_session_blocked", "device", status.get("device_id") or None, {"user_id": user.get("_id"), "device_status": status["device_status"]})
    message = "Device approval pending" if status["device_status"] == PENDING else "This device has been denied" if status["device_status"] == DENIED else "This device has been revoked"
    error = "device_access_pending" if status["device_status"] == PENDING else "device_denied" if status["device_status"] == DENIED else "device_revoked"
    return failure(message, status=403, error=error, device_status=status["device_status"], application_access=False)


def safe_device(row: dict[str, Any], *, current_session: bool = False, include_public_ip: bool = False) -> dict[str, Any]:
    public_ref = row.get("device_ref") or f"device-{hashlib.sha256(str(row.get('_id', '')).encode()).hexdigest()[:16]}"
    history = []
    for item in row.get("device_history") or []:
        history.append({key: item.get(key) for key in ("action", "previous_status", "new_status", "reason", "timestamp")})
    location = row.get("location")
    if isinstance(location, dict):
        location = {key: location.get(key) for key in ("label", "city", "state", "country", "country_code")}
    else:
        location = {"label": "Approx. location unavailable", "city": None, "state": None, "country": None, "country_code": None}
    browser_name = row.get("browser_name") or row.get("browser") or "Browser"
    os_name = row.get("os_name") or row.get("operating_system") or "Unknown OS"
    registered_at = row.get("registered_at") or row.get("created_at")
    last_activity_at = row.get("last_seen_at") or row.get("last_activity_at")
    status = row.get("device_status") or row.get("status")
    if not location.get("label"):
        location["label"] = ", ".join(str(location.get(key)) for key in ("city", "state", "country") if location.get(key)) or "Location unavailable"
    location["approximate"] = True
    result = {"device_id": public_ref, "history_ref": public_ref, "created_at": registered_at, "last_activity_at": last_activity_at, "status": status, **{key: row.get(key) for key in (
        "device_name", "device_status", "registered_at", "approved_at", "approved_by_name",
        "denied_at", "denied_by_name", "denial_reason", "last_seen_at", "last_login_at", "last_login_result",
        "browser", "operating_system", "device_type", "revoked_at", "revoked_by_name", "revoke_reason",
        "reinstated_at", "reinstated_by_name", "reinstatement_reason", "previous_status", "new_status",
    )}, "browser_name": browser_name, "browser_version": row.get("browser_version") or "",
        "os_name": os_name, "os_version": row.get("os_version") or "", "approval_required": row.get("approval_required", row.get("device_status") == PENDING),
        "location": location, "city": location.get("city"), "state": location.get("state"),
        "country": location.get("country"), "country_code": location.get("country_code"),
        "current_session": current_session, "history": history, "location_source": row.get("location_source"),
        "location_resolved_at": row.get("location_resolved_at")}
    if include_public_ip:
        result["public_ip"] = row.get("public_ip") or row.get("last_ip")
    return result


def _admin_recipients() -> list[str]:
    rows, _ = current_app.extensions["store"].list("users", {"role_id": "superadmin", "active": True}, limit=500)
    return list(dict.fromkeys(str(row.get("email") or "").strip().lower() for row in rows if row.get("email")))


def _insert_notifications(user_ids: list[str], *, notification_type: str, title: str,
                           message: str, device: dict[str, Any]) -> None:
    store = current_app.extensions["store"]
    public_ref = str(device.get("device_ref") or "")
    for user_id in dict.fromkeys(str(value) for value in user_ids if value):
        store.insert_one("notifications", {
            "user_id": user_id, "type": notification_type, "title": title,
            "message": message, "device_id": public_ref or None, "read": False,
        })


def _location_label(device: dict[str, Any] | None = None) -> str:
    # IP geolocation is not enabled; never claim a location that has not been
    # reliably resolved by a trusted server-side integration.
    location = (device or {}).get("location")
    if isinstance(location, dict):
        label = ", ".join(str(location.get(key)) for key in ("city", "state", "country") if location.get(key))
        if label:
            return label
    return "Location unavailable"


def _localized_timestamp(user: dict[str, Any]) -> str:
    tz_name = str(user.get("timezone") or current_app.config.get("APP_TIMEZONE") or "UTC")
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    return datetime.now(timezone.utc).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S %Z")


def _send_security_email(*, recipients: list[str], subject: str, html: str, request_id: str) -> None:
    if not recipients:
        return
    try:
        current_app.extensions["email_service"].send(
            purpose="general", to=recipients, subject=subject, html=html, request_id=request_id,
        )
    except Exception:
        current_app.logger.exception("device security notification failed diagnostic_id=%s", request_id)


def notify_login_attempt(user: dict[str, Any], device: dict[str, Any], status: str) -> None:
    """Record and notify one real authentication attempt (never polling)."""
    status_labels = {
        APPROVED: "Approved device", PENDING: "New device / approval required",
        DENIED: "Denied device", REVOKED: "Revoked device",
    }
    now = utcnow()
    device_id = str(device.get("_id") or "")
    audit("DEVICE_LOGIN_ATTEMPT", "device", device_id, {
        "user_id": user.get("_id"), "device_status": status,
        "location": _location_label(device), "public_ip": device.get("public_ip") or device.get("last_ip"), "authenticated": True,
    })
    recipients = _admin_recipients()
    admin_rows, _ = current_app.extensions["store"].list("users", {"role_id": "superadmin", "active": True}, limit=500)
    _insert_notifications([str(row.get("_id")) for row in admin_rows], notification_type="device_login_attempt", title="User login detected", message=f"{user.get('name') or 'User'} · {device.get('browser') or 'Browser'} · {device.get('operating_system') or 'Unknown OS'} · {device.get('device_type') or 'Desktop'} · {status_labels.get(status, status)}", device=device)
    if not recipients:
        return
    from html import escape
    browser = escape(str(device.get("browser") or "Browser"))
    operating_system = escape(str(device.get("operating_system") or "Unknown"))
    device_type = escape(str(device.get("device_type") or "Desktop"))
    request_id = f"DVCLOGIN-{secrets.token_hex(5).upper()}"
    body = ("<div style='font-family:Arial,sans-serif'><h2>User login detected</h2>"
            f"<p><strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br>"
            f"<strong>Email:</strong> {escape(str(user.get('email') or ''))}<br>"
            f"<strong>Device:</strong> {browser} · {operating_system} · {device_type}<br>"
             f"<strong>Location:</strong> {escape(_location_label(device))}<br>"
             f"<strong>Public IP:</strong> {escape(str(device.get('public_ip') or device.get('last_ip') or 'Unavailable'))}<br>"
            f"<strong>Time:</strong> {escape(_localized_timestamp(user))}<br>"
            f"<strong>Status:</strong> {escape(status_labels.get(status, status))}</p></div>")
    _send_security_email(recipients=recipients, subject="User login detected", html=body, request_id=request_id)


def notify_reinstatement(user: dict[str, Any], device: dict[str, Any], *, actor_name: str, reason: str) -> None:
    from html import escape
    admins = _admin_recipients()
    user_email = str(user.get("email") or "").strip().lower()
    admin_rows, _ = current_app.extensions["store"].list("users", {"role_id": "superadmin", "active": True}, limit=500)
    _insert_notifications([*(str(row.get("_id")) for row in admin_rows), str(user.get("_id") or "")], notification_type="device_reinstated", title="Trusted device reinstated", message=f"{user.get('name') or 'User'} · Pending approval · {reason}", device=device)
    browser = escape(str(device.get("browser") or "Browser"))
    operating_system = escape(str(device.get("operating_system") or "Unknown"))
    device_type = escape(str(device.get("device_type") or "Desktop"))
    details = (f"<strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br>"
               f"<strong>Device:</strong> {browser} · {operating_system} · {device_type}<br>"
               "<strong>Previous status:</strong> Denied<br><strong>New status:</strong> Pending approval<br>"
               f"<strong>Reinstated by:</strong> {escape(actor_name)}<br><strong>Reason:</strong> {escape(reason)}<br>"
               f"<strong>Time:</strong> {escape(_localized_timestamp(user))}")
    _send_security_email(recipients=admins, subject="Trusted device reinstated", html=f"<h2>Trusted device reinstated</h2><p>{details}</p>", request_id=f"DVCREINSTATE-{secrets.token_hex(5).upper()}")
    if user_email:
        body = ("<h2>Your Moneda device has been reinstated</h2><p>Your previously denied device has been reinstated by a Moneda administrator.</p>"
                f"<p><strong>Device:</strong> {browser} · {operating_system} · {device_type}<br>"
                "<strong>Current status:</strong> Pending administrator approval<br>"
                f"<strong>Reason recorded by administrator:</strong> {escape(reason)}</p>"
                "<p>You may now retry login. Administrator approval is still required before access is granted.</p>")
        _send_security_email(recipients=[user_email], subject="Your Moneda device has been reinstated", html=body, request_id=f"DVCUSER-{secrets.token_hex(5).upper()}")


def notify_device_decision(user: dict[str, Any], device: dict[str, Any], *, approved: bool, reason: str = "") -> None:
    """Notify the affected user and active Superadmins after a decision."""
    from html import escape
    status = "Approved" if approved else "Denied"
    subject = f"Moneda device access {status.lower()}"
    details = (f"<strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br>"
               f"<strong>Device:</strong> {escape(str(device.get('device_name') or 'Trusted device'))}<br>"
               f"<strong>Status:</strong> {status}<br>"
               f"<strong>Reason:</strong> {escape(reason) if reason else 'No reason recorded'}<br>"
               f"<strong>Time:</strong> {escape(_localized_timestamp(user))}")
    recipients = _admin_recipients()
    admin_rows, _ = current_app.extensions["store"].list("users", {"role_id": "superadmin", "active": True}, limit=500)
    _insert_notifications([*(str(row.get("_id")) for row in admin_rows), str(user.get("_id") or "")], notification_type="device_decision", title=subject, message=f"{user.get('name') or 'User'} · {status}", device=device)
    _send_security_email(recipients=recipients, subject=subject, html=f"<h2>Trusted device decision</h2><p>{details}</p>", request_id=f"DVCDECISION-{secrets.token_hex(5).upper()}")
    email = str(user.get("email") or "").strip().lower()
    if email:
        _send_security_email(recipients=[email], subject=subject, html=f"<h2>Your Moneda device access was {status.lower()}</h2><p>{details}</p>", request_id=f"DVCUSERDECISION-{secrets.token_hex(5).upper()}")


def notify_device_revocation(user: dict[str, Any], device: dict[str, Any]) -> None:
    from html import escape
    details = (f"<strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br>"
               f"<strong>Device:</strong> {escape(str(device.get('device_name') or 'Trusted device'))}<br>"
               "<strong>Status:</strong> Revoked<br>"
               f"<strong>Revoked by:</strong> {escape(str(device.get('revoked_by_name') or 'Moneda administrator'))}<br>"
               f"<strong>Reason:</strong> {escape(str(device.get('revoke_reason') or 'No reason recorded'))}<br>"
               f"<strong>Time:</strong> {escape(_localized_timestamp(user))}")
    subject = "Moneda trusted device revoked"
    admin_rows, _ = current_app.extensions["store"].list("users", {"role_id": "superadmin", "active": True}, limit=500)
    _insert_notifications([*(str(row.get("_id")) for row in admin_rows), str(user.get("_id") or "")], notification_type="device_revoked", title=subject, message=f"{user.get('name') or 'User'} · Revoked · {device.get('revoke_reason') or 'No reason recorded'}", device=device)
    _send_security_email(recipients=_admin_recipients(), subject=subject, html=f"<h2>Trusted device revoked</h2><p>{details}</p>", request_id=f"DVCREVOKE-{secrets.token_hex(5).upper()}")
    email = str(user.get("email") or "").strip().lower()
    if email:
        _send_security_email(recipients=[email], subject=subject, html=f"<h2>Your Moneda device was revoked</h2><p>{details}</p><p>Sign in again to request approval for this device.</p>", request_id=f"DVCUSERREVOKE-{secrets.token_hex(5).upper()}")


def _notify_superadmins(user: dict[str, Any], device: dict[str, Any], *, reinstated: bool = False) -> None:
    """Create one expiring decision token and notify all active Superadmins."""
    store = current_app.extensions["store"]
    token = secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(hours=24)
    request_id = f"DVC-{secrets.token_hex(4).upper()}"
    approval_changes = {
        "approval_request_id": request_id, "approval_token_hash": _token_hash(token),
        "approval_token_expires_at": expires,
    }
    # device_ref is the stable public identifier used by the admin UI. Only
    # backfill it for legacy records; each approval request gets its own ID.
    if not device.get("device_ref"):
        approval_changes["device_ref"] = request_id
    updated = store.update_one("devices", {"_id": device["_id"], "device_status": PENDING}, approval_changes) or device
    base = str(current_app.config.get("APP_BASE_URL") or "http://localhost:3005").rstrip("/")
    from urllib.parse import quote
    action_url = f"{base}/api/v1/device-approval/{quote(token, safe='')}"
    admins, _ = store.list("users", {"role_id": "superadmin", "active": True}, limit=500)
    recipients = [str(row.get("email") or "").strip().lower() for row in admins if row.get("email")]
    if not recipients:
        return
    from html import escape
    browser = escape(str(updated.get("browser") or "Browser"))
    operating_system = escape(str(updated.get("operating_system") or "Unknown"))
    device_type = escape(str(updated.get("device_type") or "Desktop"))
    title = "Device reinstated — approval required" if reinstated else "New Moneda device approval required"
    intro = "This device was administratively reinstated and must be approved again." if reinstated else "A new device is attempting to access the Moneda Workspace."
    body = (f"<div style='font-family:Arial,sans-serif'><h2>{escape(title)}</h2>"
            f"<p>{escape(intro)}</p>"
            f"<p><strong>User:</strong> {escape(str(user.get('name') or 'User'))}<br>"
            f"<strong>Email:</strong> {escape(str(user.get('email') or ''))}<br>"
            f"<strong>Device:</strong> {browser} · {operating_system} · {device_type}<br>"
             f"<strong>Location:</strong> {escape(_location_label(updated))}<br>"
             f"<strong>Public IP:</strong> {escape(str(updated.get('public_ip') or updated.get('last_ip') or 'Unavailable'))}<br>"
             f"<strong>Original registration:</strong> {escape(str(updated.get('registered_at') or utcnow()))}<br>"
            + (f"<strong>Reinstated by:</strong> {escape(str(updated.get('reinstated_by_name') or 'Moneda administrator'))}<br><strong>Reinstatement reason:</strong> {escape(str(updated.get('reinstatement_reason') or ''))}<br>" if reinstated else "")
            + "<strong>Current status:</strong> Pending approval</p>"
            f"<p><a href='{action_url}?action=approve'>Approve device</a> &nbsp; <a href='{action_url}?action=deny'>Deny device</a></p>"
            f"<p>Request ID: {escape(request_id)}</p></div>")
    try:
        current_app.extensions["email_service"].send(
            purpose="general", to=recipients, subject="New Moneda device approval required",
            html=body, request_id=request_id,
        )
    except Exception:
        current_app.logger.exception("device approval notification failed request_id=%s", request_id)
