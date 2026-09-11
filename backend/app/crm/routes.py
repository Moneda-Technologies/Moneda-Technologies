from __future__ import annotations

from datetime import datetime, timedelta

from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.middleware.access import (
    can_view_all_customers, current_user, customer_id_from, customer_record,
    enforce_customer, permitted_customer_query, permission_required,
)
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


def _record_customer_id(row: dict) -> str | None:
    value = row.get("customer_id") or row.get("customer_company_id") or row.get("company_id")
    return str(value) if value else None


def _crm_scope(customer_id: str | None = None) -> dict:
    """Build an authorization scope without requiring a global customer context."""
    user = current_user() or {}
    if customer_id:
        if not enforce_customer(customer_id):
            return {"_id": "__access_denied__"}
        return {"$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    if can_view_all_customers(user):
        return {}
    store = current_app.extensions["store"]
    customer_rows, _ = store.list("customers", permitted_customer_query(user), limit=5000)
    customer_ids = [str(row.get("_id")) for row in customer_rows if row.get("_id") and not row.get("is_issuer")]
    owner_id = user.get("_id")
    clauses = [{field: owner_id} for field in ("owner_user_id", "assigned_to", "salesperson_id") if owner_id]
    if customer_ids:
        clauses.extend({field: {"$in": customer_ids}} for field in ("customer_id", "customer_company_id", "company_id"))
    return {"$or": clauses or [{"_id": "__no_access__"}]}


def _initials(name: str | None) -> str:
    compact = "".join(part for part in str(name or "").strip() if part.isalnum())
    return compact[:2].upper() or "—"


def _enriched_leads(rows: list[dict]) -> list[dict]:
    store = current_app.extensions["store"]
    customers, _ = store.list("customers", {}, limit=5000)
    companies, _ = store.list("companies", {}, limit=5000)
    customer_map = {str(row.get("_id")): row for row in [*companies, *customers] if row.get("_id")}
    users, _ = store.list("users", {}, limit=1000)
    user_map = {str(row.get("_id")): row for row in users if row.get("_id")}
    result = []
    for source in rows:
        row = {**source}
        customer_id = _record_customer_id(row)
        # Historical leads created from a quotation/order may have a missing
        # customer_id but still carry an authoritative relationship. Repair
        # only those explicit references; never infer by company-name similarity.
        if not customer_id and row.get("quotation_id"):
            quotation = store.find_one("quotations", {"_id": row.get("quotation_id")})
            customer_id = _record_customer_id(quotation or {})
        if not customer_id and row.get("order_id"):
            order = store.find_one("orders", {"_id": row.get("order_id")})
            customer_id = _record_customer_id(order or {})
        if customer_id and not _record_customer_id(source):
            repaired = store.update_one("leads", {"_id": source.get("_id")}, {"customer_id": customer_id})
            if repaired:
                current_app.logger.info("crm relationship repaired lead_id=%s customer_id=%s source=authoritative_reference", source.get("_id"), customer_id)
        customer = customer_map.get(customer_id or "")
        if customer and customer.get("is_issuer"):
            customer_id, customer = None, None
        owner_id = row.get("owner_user_id") or row.get("assigned_to") or row.get("salesperson_id")
        owner = user_map.get(str(owner_id)) if owner_id else None
        owner_name = (owner or {}).get("name") or row.get("salesperson_name") or row.get("owner_name")
        row["customer_id"] = customer_id
        row["customer_name"] = (customer or {}).get("name") or (customer or {}).get("company_name")
        row["customer_country"] = (customer or {}).get("country_name") or (customer or {}).get("country")
        row["customer_region"] = (customer or {}).get("continent") or ((customer or {}).get("region") or {}).get("continent")
        row["owner_user_id"] = str(owner_id) if owner_id else None
        row["owner_name"] = owner_name
        row["owner_email"] = (owner or {}).get("email")
        row["owner_initials"] = _initials(owner_name)
        row["value_eur"] = float(row.get("value_eur", row.get("estimated_value", row.get("value", 0))) or 0)
        row["currency"] = "EUR"
        result.append(row)
    return result


def _list_resource(collection: str, customer_id: str | None, status: str | None = None):
    scope = _crm_scope(customer_id)
    if scope.get("_id") == "__access_denied__":
        return None
    store = current_app.extensions["store"]
    rows, _ = store.list(collection, scope, limit=5000, sort="created_at", direction=-1)
    if status:
        rows = [row for row in rows if str(row.get("status") or row.get("stage") or "") == status]
    if collection == "leads":
        rows = _enriched_leads(rows)
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(max(int(request.args.get("limit", 25)), 1), 100)
    total = len(rows)
    return {"items": rows[(page - 1) * limit:page * limit], "total": total, "pagination": {"page": page, "limit": limit, "total": total}}


@bp.get("/leads")
@permission_required("crm.view")
def list_leads():
    requested = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    customer_id = None if str(requested or "").lower() in {"", "all", "all_customers"} else str(requested)
    # CRM is company-wide by default.  The global header customer is a
    # calculator/quotation context and must not silently narrow CRM results;
    # callers can still request a specific customer through the filter.
    scope = _crm_scope(customer_id)
    result = None if scope.get("_id") == "__access_denied__" else {}
    if result is not None:
        rows, _ = current_app.extensions["store"].list("leads", scope, limit=5000, sort="created_at", direction=-1)
        rows = _enriched_leads(rows)
        requested_stage = request.args.get("status") or request.args.get("stage")
        if requested_stage:
            rows = [row for row in rows if str(row.get("status") or row.get("stage") or "") == requested_stage]
        def _number(value: str | None) -> float | None:
            try: return float(value) if value not in (None, "") else None
            except (TypeError, ValueError): return None
        owner = request.args.get("owner_id") or request.args.get("owner_user_id")
        country = request.args.get("country", "").strip().casefold()
        region = request.args.get("region", "").strip().casefold()
        source = request.args.get("source", "").strip().casefold()
        min_value, max_value = _number(request.args.get("min_value")), _number(request.args.get("max_value"))
        date_from, date_to = request.args.get("date_from"), request.args.get("date_to")
        filtered = []
        for row in rows:
            value = float(row.get("value_eur", 0) or 0)
            created = str(row.get("created_at", ""))[:10]
            if owner and str(row.get("owner_user_id")) != owner: continue
            if country and country not in str(row.get("customer_country") or "").casefold(): continue
            if region and region not in str(row.get("customer_region") or "").casefold(): continue
            if source and source not in str(row.get("source") or "").casefold(): continue
            if min_value is not None and value < min_value: continue
            if max_value is not None and value > max_value: continue
            if date_from and created < date_from: continue
            if date_to and created > date_to: continue
            filtered.append(row)
        page = max(int(request.args.get("page", 1)), 1); limit = min(max(int(request.args.get("limit", 25)), 1), 100)
        result["total"] = len(filtered); result["pagination"] = {"page": page, "limit": limit, "total": len(filtered)}
        result["items"] = filtered[(page - 1) * limit:page * limit]
    return success(result) if result is not None else failure("Customer access denied", status=403)


@bp.post("/leads")
@permission_required("crm.manage")
def create_lead():
    payload = request.get_json(silent=True) or {}
    customer_id = customer_id_from(payload)
    if customer_id and not enforce_customer(customer_id):
        return failure("Customer access denied", status=403)
    user = current_user() or {}
    payload["customer_id"] = customer_id
    if payload.get("status", "Lead") not in LEAD_STATUSES:
        return failure("Invalid lead status", status=422)
    payload.setdefault("status", "Lead")
    payload["owner_user_id"] = user.get("_id")
    payload["assigned_to"] = user.get("_id")
    payload["salesperson_id"] = user.get("_id")
    payload["salesperson_name"] = user.get("name")
    payload["value_eur"] = float(payload.get("value_eur", payload.get("estimated_value", payload.get("value", 0))) or 0)
    payload["currency"] = "EUR"
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
    record_customer = _record_customer_id(existing)
    user = current_user() or {}
    if record_customer and not enforce_customer(record_customer) and str(existing.get("owner_user_id") or existing.get("assigned_to")) != str(user.get("_id")):
        return failure("Customer access denied", status=403)
    changes = request.get_json(silent=True) or {}
    if changes.get("status") and changes["status"] not in LEAD_STATUSES:
        return failure("Invalid lead status", status=422)
    allowed = {"quotation_id", "estimated_value", "value_eur", "source", "status", "notes", "follow_up_date", "next_action", "next_action_date", "activity"}
    filtered = {key: value for key, value in changes.items() if key in allowed}
    if "value_eur" in filtered or "estimated_value" in filtered:
        filtered["value_eur"] = float(filtered.get("value_eur", filtered.get("estimated_value", 0)) or 0)
        filtered["currency"] = "EUR"
    if "status" in filtered and filtered["status"] != existing.get("status"):
        filtered["activity"] = [*existing.get("activity", []), {"type": "status_changed", "from": existing.get("status"), "to": filtered["status"], "at": utcnow(), "by": (current_user() or {}).get("_id")}]
    row = store.update_one("leads", {"_id": lead_id}, filtered)
    audit("lead.update", "lead", lead_id)
    return success(row, "Lead updated")


@bp.get("/reminders")
@permission_required("reminders.view")
def list_reminders():
    requested = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    customer_id = None if str(requested or "").lower() in {"", "all", "all_customers"} else str(requested)
    # Reminders are part of the company-wide CRM view; the header customer is
    # only a calculator/quotation context unless an explicit filter is sent.
    result = _list_resource("reminders", customer_id, request.args.get("status"))
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
