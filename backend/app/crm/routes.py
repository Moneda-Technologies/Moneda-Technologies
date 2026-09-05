from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import current_user, customer_id_from, enforce_customer, permission_required
from app.repositories.store import utcnow
from app.services.audit import audit


bp = Blueprint("crm", __name__, url_prefix="/api")

LEAD_STATUSES = {"Lead", "Follow Up", "Order Received", "Won", "Lost", "Closed"}
REMINDER_FREQUENCIES = {"none", "daily", "every_other_day", "every_3_days", "weekly", "every_15_days", "every_25_days", "every_30_days", "custom"}
REMINDER_STATUSES = {"Pending", "Due", "Overdue", "Completed", "Cancelled"}
REMINDER_FIELDS = {"company_id", "customer_company_id", "customer_id", "lead_id", "quotation_id", "order_id", "assigned_to", "due_date", "priority", "notes", "status", "frequency", "repeat_enabled", "repeat_frequency", "custom_interval_days"}


def _normalise_reminder_status(value: str | None) -> str:
    aliases = {"open": "Pending", "pending": "Pending", "due": "Due", "overdue": "Overdue", "completed": "Completed", "cancelled": "Cancelled"}
    return aliases.get(str(value or "Pending").lower(), str(value or "Pending"))


def _next_due(value: object, frequency: str, custom_interval_days: object = 1) -> str | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    intervals = {"daily": 1, "every_other_day": 2, "every_3_days": 3, "weekly": 7, "every_15_days": 15, "every_25_days": 25, "every_30_days": 30}
    days = intervals.get(frequency, int(custom_interval_days or 1) if frequency == "custom" else 0)
    return (parsed + timedelta(days=days)).date().isoformat() if days else None


def _list_resource(collection: str, customer_id: str, status: str | None = None):
    if not enforce_customer(customer_id):
        return None
    query: dict = {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    if status:
        query["status"] = status
    rows, total = current_app.extensions["store"].list(collection, query, page=max(int(request.args.get("page", 1)), 1), limit=min(int(request.args.get("limit", 25)), 100))
    return {"items": rows, "total": total}


@bp.get("/leads")
@permission_required("crm.view")
def list_leads():
    result = _list_resource("leads", request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id", ""), request.args.get("status"))
    return success(result) if result is not None else failure("Customer access denied", status=403)


@bp.post("/leads")
@permission_required("crm.manage")
def create_lead():
    payload = request.get_json(silent=True) or {}
    customer_id = customer_id_from(payload)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    payload["customer_id"] = customer_id
    if payload.get("status", "Lead") not in LEAD_STATUSES:
        return failure("Invalid lead status", status=422)
    payload.setdefault("status", "Lead")
    payload.setdefault("assigned_to", (current_user() or {}).get("_id"))
    payload.setdefault("salesperson_id", payload["assigned_to"])
    payload.setdefault("salesperson_name", (current_user() or {}).get("name"))
    payload.setdefault("activity", [])
    row = current_app.extensions["store"].insert_one("leads", payload)
    audit("lead.create", "lead", str(row["_id"]))
    return success(row, "Lead created", 201)


@bp.patch("/leads/<lead_id>")
@permission_required("crm.manage")
def update_lead(lead_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("leads", {"_id": lead_id})
    if not existing:
        return failure("Lead not found", status=404)
    if not enforce_customer(existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id")):
        return failure("Customer access denied", status=403)
    changes = request.get_json(silent=True) or {}
    if changes.get("status") and changes["status"] not in LEAD_STATUSES:
        return failure("Invalid lead status", status=422)
    allowed = {"quotation_id", "assigned_to", "estimated_value", "currency", "source", "status", "notes", "follow_up_date", "activity"}
    filtered = {key: value for key, value in changes.items() if key in allowed}
    if "status" in filtered and filtered["status"] != existing.get("status"):
        filtered["activity"] = [*existing.get("activity", []), {"type": "status_changed", "from": existing.get("status"), "to": filtered["status"], "at": utcnow(), "by": (current_user() or {}).get("_id")}]
    row = store.update_one("leads", {"_id": lead_id}, filtered)
    audit("lead.update", "lead", lead_id)
    return success(row, "Lead updated")


@bp.get("/reminders")
@permission_required("reminders.view")
def list_reminders():
    result = _list_resource("reminders", request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id", ""), request.args.get("status"))
    return success(result) if result is not None else failure("Customer access denied", status=403)


@bp.post("/reminders")
@permission_required("reminders.manage")
def create_reminder():
    payload = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in REMINDER_FIELDS}
    customer_id = customer_id_from(payload)
    if not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    payload["customer_id"] = customer_id
    if not payload.get("due_date"):
        return failure("Due date is required", status=422)
    frequency = payload.get("repeat_frequency", payload.get("frequency", "none"))
    if frequency not in REMINDER_FREQUENCIES:
        return failure("Invalid reminder frequency", status=422)
    if frequency == "custom" and int(payload.get("custom_interval_days", 0) or 0) < 1:
        return failure("Custom reminders require a positive interval", status=422)
    payload.setdefault("assigned_to", (current_user() or {}).get("_id"))
    payload["status"] = _normalise_reminder_status(payload.get("status"))
    payload.setdefault("priority", "normal")
    payload["repeat_frequency"] = frequency
    payload["frequency"] = frequency
    payload["repeat_enabled"] = bool(payload.get("repeat_enabled", frequency != "none"))
    row = current_app.extensions["store"].insert_one("reminders", payload)
    audit("reminder.create", "reminder", str(row["_id"]))
    return success(row, "Reminder created", 201)


@bp.patch("/reminders/<reminder_id>")
@permission_required("reminders.manage")
def update_reminder(reminder_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("reminders", {"_id": reminder_id})
    if not existing:
        return failure("Reminder not found", status=404)
    if not enforce_customer(existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id")):
        return failure("Customer access denied", status=403)
    if existing.get("status") == "completed":
        return failure("Completed reminders are immutable", status=409)
    allowed = REMINDER_FIELDS - {"company_id", "customer_company_id", "customer_id", "lead_id", "quotation_id", "order_id"}
    changes = {key: value for key, value in (request.get_json(silent=True) or {}).items() if key in allowed}
    if "status" in changes:
        changes["status"] = _normalise_reminder_status(changes["status"])
    if "repeat_frequency" in changes and changes["repeat_frequency"] not in REMINDER_FREQUENCIES:
        return failure("Invalid reminder frequency", status=422)
    if "repeat_frequency" in changes:
        changes["frequency"] = changes["repeat_frequency"]
    row = store.update_one("reminders", {"_id": reminder_id}, changes)
    audit("reminder.update", "reminder", reminder_id)
    return success(row, "Reminder updated")


@bp.post("/reminders/<reminder_id>/complete")
@permission_required("reminders.manage")
def complete_reminder(reminder_id: str):
    store = current_app.extensions["store"]
    existing = store.find_one("reminders", {"_id": reminder_id})
    if not existing:
        return failure("Reminder not found", status=404)
    if not enforce_customer(existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id")):
        return failure("Customer access denied", status=403)
    if _normalise_reminder_status(existing.get("status")) == "Completed":
        return success(existing, "Reminder was already completed")
    user = current_user() or {}
    now = utcnow()
    row = store.update_one("reminders", {"_id": reminder_id}, {"status": "Completed", "completed_at": now, "completed_by": user.get("_id")})
    frequency = existing.get("repeat_frequency", existing.get("frequency", "none"))
    next_reminder = None
    if existing.get("repeat_enabled") and frequency != "none":
        next_due = _next_due(existing.get("due_date"), frequency, existing.get("custom_interval_days"))
        if next_due:
            next_reminder = store.insert_one("reminders", {
                "customer_id": existing.get("customer_id") or existing.get("customer_company_id") or existing.get("company_id"),
                "lead_id": existing.get("lead_id"), "quotation_id": existing.get("quotation_id"), "order_id": existing.get("order_id"),
                "assigned_to": existing.get("assigned_to", user.get("_id")), "due_date": next_due,
                "priority": existing.get("priority", "normal"), "notes": existing.get("notes", ""),
                "status": "Pending", "frequency": frequency, "repeat_frequency": frequency,
                "repeat_enabled": True, "custom_interval_days": existing.get("custom_interval_days"),
            })
            if existing.get("lead_id"):
                store.update_one("leads", {"_id": existing["lead_id"]}, {"follow_up_date": next_due})
    audit("reminder.complete", "reminder", reminder_id)
    return success({"reminder": row, "next_reminder": next_reminder}, "Reminder completed")
