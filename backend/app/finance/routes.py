from __future__ import annotations

from datetime import datetime, timezone
from email_validator import EmailNotValidError, validate_email
from html import escape
from flask import Blueprint, current_app, request

from app.api.responses import failure, success
from app.finance.service import (
    PAYMENT_STATUS_AWAITING,
    PAYMENT_STATUS_CONFIRMED,
    PAYMENT_STATUS_REJECTED,
    PAYMENT_STATUS_VOIDED,
    PAYMENT_STATUS_DELETED,
    activate_incentive,
    has_payout_link,
    is_voided_or_deleted_payment,
    is_confirmed_payment,
    incentive_transaction_id,
    linked_records_for_order,
    money,
    payment_amount,
    payment_rollup,
    recompute_incentive_totals,
    sync_order_payment_state,
)
from app.finance.bank_details import (
    BANK_DETAIL_FIELDS,
    audit_bank_diff,
    can_edit_bank_details,
    can_view_bank_details,
    normalize_bank_changes,
    serialize_bank_details,
)
from app.middleware.access import current_user, customer_access_ids_for_user, customer_record, enforce_customer, login_required, permission_required, permission_required_any
from app.repositories.store import utcnow
from app.services.audit import audit
from app.services.business_logic import accessible_user_ids


bp = Blueprint("finance", __name__, url_prefix="/api")


def _store():
    return current_app.extensions["store"]


def _email(value: object) -> str:
    try:
        return validate_email(str(value or "").strip(), check_deliverability=False).normalized
    except EmailNotValidError:
        return ""


def _parse_date(value: object, *, required: bool = True):
    raw = str(value or "").strip()
    if not raw:
        return utcnow() if not required else None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _order(order_id: str):
    return _store().find_one("orders", {"_id": order_id}) or _store().find_one("order_confirmations", {"_id": order_id})


def _incentive_order(row: dict):
    return _order(incentive_transaction_id(row))


def _incentive_recipient_ids(row: dict) -> set[str]:
    """Read explicit allocation recipients before falling back to legacy owner fields."""
    recipients = {
        str(row.get(field) or "")
        for field in ("recipient_user_id", "salesperson_id", "manager_user_id", "creator_user_id")
        if row.get(field)
    }
    for line in row.get("incentive_lines") or []:
        if isinstance(line, dict) and line.get("recipient_user_id"):
            recipients.add(str(line["recipient_user_id"]))
    transaction_id = incentive_transaction_id(row)
    for allocation in linked_records_for_order(_store(), "incentive_allocations", transaction_id) if transaction_id else []:
        if allocation.get("incentive_id") == row.get("_id") and allocation.get("recipient_user_id"):
            recipients.add(str(allocation["recipient_user_id"]))
    return recipients


def _superadmin() -> bool:
    return str((current_user() or {}).get("role_id") or "") == "superadmin"


def _customer_incentive_visibility(actor: dict | None = None) -> bool:
    """Return whether this actor may receive customer-incentive details."""
    actor = actor or current_user() or {}
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    settings = _store().find_one("app_settings", {"_id": "system"}) or {}
    role = str(actor.get("role_id") or "")
    if role in {"manager", "manager_sales_admin"}:
        return bool(settings.get("show_customer_incentives_to_manager", False))
    if role == "user":
        return bool(settings.get("show_customer_incentives_to_salesperson", False))
    return False


def _customer_incentive_line(line: dict) -> bool:
    return str(line.get("recipient_type") or "").upper() == "CUSTOMER" or str(line.get("category_id") or "") == "customer_incentive"


CUSTOMER_INCENTIVE_SNAPSHOT_FIELDS = (
    "customer_incentive_enabled_snapshot", "customer_incentive_bearer_name_snapshot",
    "customer_incentive_designation_snapshot", "customer_incentive_bearer_designation_snapshot",
    "customer_incentive_percentage_snapshot", "customer_incentive_base_amount_eur",
    "customer_incentive_amount_snapshot", "customer_incentive_currency_snapshot",
)


def _serialize_incentive_for_actor(
    row: dict,
    actor: dict | None = None,
    *,
    view: str = "internal",
) -> dict:
    """Return one explicitly scoped internal or customer incentive snapshot.

    A parent incentive contains the immutable OC snapshot for both concepts,
    but the API never returns those concepts mixed together.  Internal rows
    are also filtered to the signed-in user's permitted recipient scope.
    """
    actor = actor or current_user() or {}
    result = {**row}
    lines = [dict(line) for line in (row.get("incentive_lines") or []) if isinstance(line, dict)]
    customer_lines = [line for line in lines if _customer_incentive_line(line)]
    if view == "customer":
        result["incentive_lines"] = customer_lines if _customer_incentive_visibility(actor) else []
    else:
        internal_lines = [line for line in lines if not _customer_incentive_line(line)]
        role = str(actor.get("role_id") or "")
        actor_id = str(actor.get("_id") or "")
        if role in {"manager", "manager_sales_admin"}:
            allowed_recipients = {str(value) for value in accessible_user_ids(_store(), actor) if value}
            allowed_recipients.add(actor_id)
            internal_lines = [
                line for line in internal_lines
                if str(line.get("recipient_user_id") or "") in allowed_recipients
            ]
        elif role == "user":
            internal_lines = [
                line for line in internal_lines
                if str(line.get("recipient_user_id") or "") == actor_id
            ]
        result["incentive_lines"] = internal_lines
        # Customer bearer details are never part of the internal incentive
        # contract, including for Superadmin.  They remain available through
        # the dedicated customer-incentives endpoint.
        for field in CUSTOMER_INCENTIVE_SNAPSHOT_FIELDS:
            result.pop(field, None)

    visible_total = money(sum(money(line.get("incentive_amount")) for line in result["incentive_lines"]))
    result["gross_incentive_amount"] = visible_total
    result["net_payable_incentive"] = max(0.0, visible_total - money(result.get("credit_note_deduction")))
    result["paid_amount"] = min(money(result.get("paid_amount")), result["net_payable_incentive"])
    return result


def _internal_allocation_views(row: dict, actor: dict | None = None) -> list[dict]:
    """Flatten one OC snapshot into one API record per internal recipient."""
    scoped = _serialize_incentive_for_actor(row, actor, view="internal")
    grouped: dict[tuple[str, str], list[dict]] = {}
    for line in scoped.get("incentive_lines") or []:
        recipient_id = str(line.get("recipient_user_id") or "")
        allocation_type = str(line.get("allocation_type") or "creator")
        if not recipient_id:
            continue
        grouped.setdefault((recipient_id, allocation_type), []).append(line)

    views: list[dict] = []
    for (recipient_id, allocation_type), lines in grouped.items():
        rates = {float(line.get("incentive_rate_snapshot") or 0) for line in lines}
        first = lines[0]
        total = money(sum(money(line.get("incentive_amount")) for line in lines))
        view = {
            **scoped,
            "incentive_lines": lines,
            "allocation_key": f"{scoped.get('_id')}:{recipient_id}:{allocation_type}",
            "recipient_user_id": recipient_id,
            "recipient_role": first.get("recipient_role"),
            "recipient_type": first.get("recipient_type"),
            "recipient_snapshot": first.get("recipient_snapshot") or {},
            "allocation_type": allocation_type,
            "incentive_percentage_snapshot": next(iter(rates)) if len(rates) == 1 else None,
            "gross_incentive_amount": total,
            "net_payable_incentive": total,
            "paid_amount": min(money(scoped.get("paid_amount")), total),
        }
        views.append(view)
    return views


def _allocation_snapshot_views(
    lines: list[dict],
    *,
    status: object = None,
    payment_status: object = None,
) -> list[dict]:
    """Group the already-persisted incentive lines by actual recipient.

    This is a presentation DTO only.  It deliberately reads recipient and
    rate snapshots from each line instead of resolving today's incentive
    rules, so historical Order Confirmations remain immutable.
    """
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        is_customer = _customer_incentive_line(line)
        recipient_id = str(line.get("recipient_user_id") or line.get("customer_id") or "")
        recipient_type = str(line.get("recipient_type") or ("CUSTOMER" if is_customer else "USER"))
        allocation_type = str(line.get("allocation_type") or ("customer" if is_customer else "creator"))
        # A legacy line without an id is still useful in the total view.  Keep
        # it in a stable group rather than dropping the persisted allocation.
        key = (recipient_id or f"legacy:{len(grouped)}", allocation_type, recipient_type)
        grouped.setdefault(key, []).append(line)

    views: list[dict] = []
    for (recipient_id, allocation_type, recipient_type), grouped_lines in grouped.items():
        first = grouped_lines[0]
        snapshot = first.get("recipient_snapshot") if isinstance(first.get("recipient_snapshot"), dict) else {}
        if recipient_type.upper() == "CUSTOMER":
            snapshot = {
                **snapshot,
                "_id": snapshot.get("_id") or first.get("customer_id"),
                "name": snapshot.get("name") or first.get("customer_name_snapshot") or first.get("bearer_name_snapshot"),
            }
        amount = money(sum(money(line.get("incentive_amount")) for line in grouped_lines))
        rates = {float(line.get("incentive_rate_snapshot") or 0) for line in grouped_lines}
        views.append({
            "recipient_id": None if recipient_id.startswith("legacy:") else recipient_id,
            "recipient_user_id": first.get("recipient_user_id"),
            "recipient_type": recipient_type,
            "recipient_role": first.get("recipient_role") or ("customer" if recipient_type.upper() == "CUSTOMER" else "user"),
            "recipient_snapshot": snapshot,
            "recipient_name": snapshot.get("name") or snapshot.get("email") or first.get("customer_name_snapshot") or first.get("bearer_name_snapshot"),
            "allocation_type": allocation_type,
            "rate": next(iter(rates)) if len(rates) == 1 else None,
            "amount": amount,
            "status": status,
            "payment_status": payment_status,
            "lines": grouped_lines,
        })
    return views


def _user_id(user: dict | None = None) -> str:
    return str((user or current_user() or {}).get("_id") or "")


def _bank_detail_permission(actor: dict | None = None) -> bool:
    actor = actor or current_user() or {}
    role = str(actor.get("role_id") or "")
    return role in {"superadmin", "admin", "manager_sales_admin", "manager", "user"} and (
        role in {"superadmin", "admin"} or "bank_details.view" in set(actor.get("permissions") or [])
    )


def _order_customer_id(order: dict) -> str:
    return str(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id") or "")


def _order_owner_ids(order: dict) -> set[str]:
    """Return the explicit people attached to an Order Confirmation.

    ``salesperson_id`` is the primary relationship.  The other fields keep
    older Order Confirmations accessible without guessing from names/emails.
    """
    owner_ids = {
        str(order.get(field) or "")
        for field in ("salesperson_id", "prepared_by_user_id", "created_by_user_id", "user_id")
        if order.get(field)
    }
    for snapshot_key in ("salesperson_snapshot",):
        snapshot = order.get(snapshot_key)
        if isinstance(snapshot, dict) and snapshot.get("_id"):
            owner_ids.add(str(snapshot["_id"]))
    return owner_ids


def _customer_access(actor: dict, customer_id: str) -> bool:
    """Check finance scope without treating Admin as automatically global."""
    if not customer_id:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    if str(actor.get("role_id") or "") != "admin":
        return enforce_customer(customer_id)
    if "customers.view_all" in set(actor.get("permissions") or []):
        return True
    customer = customer_record(customer_id) or {}
    actor_id = _user_id(actor)
    return actor_id in {str(value) for value in (customer.get("assigned_user_ids") or []) if value} or str(customer.get("created_by_user_id") or "") == actor_id


def _can_access_order(order: dict, user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    if not actor:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    customer_id = _order_customer_id(order)
    if not _customer_access(actor, customer_id):
        return False
    # Payment visibility is customer-assignment based. A normal user may work
    # with every eligible OC for an assigned customer, but cannot gain access
    # merely by changing an order/customer identifier in the request.
    return str(actor.get("role_id") or "") in {"admin", "manager_sales_admin", "user"}


def _can_view_payment(payment: dict, user: dict | None = None) -> bool:
    order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if order:
        return _can_access_order(order, user)
    actor = user or current_user() or {}
    return _customer_access(actor, str(payment.get("customer_id") or ""))


def _can_record_payment(order: dict, user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    if not _can_access_order(order, actor):
        return False
    return str(actor.get("role_id") or "") in {"admin", "superadmin", "manager_sales_admin", "user"}


def _can_create_payment(user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    return str(actor.get("role_id") or "") == "superadmin" or bool({"payments.create", "payments.manage"}.intersection(set(actor.get("permissions") or [])))


def _can_view_incentive(row: dict, user: dict | None = None) -> bool:
    actor = user or current_user() or {}
    if not actor:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    actor_id = _user_id(actor)
    recipient_ids = _incentive_recipient_ids(row)
    if str(actor.get("role_id") or "") in {"manager", "manager_sales_admin"}:
        team_ids = set(accessible_user_ids(_store(), actor))
        order = _incentive_order(row)
        owner_ids = _order_owner_ids(order) if order else set()
        return bool(
            recipient_ids.intersection(team_ids | {actor_id})
            or str(row.get("salesperson_id") or "") in team_ids
            or owner_ids.intersection(team_ids)
        )
    if str(actor.get("role_id") or "") == "admin":
        return _customer_access(actor, str(row.get("customer_id") or ""))
    if actor_id in recipient_ids:
        order = _incentive_order(row)
        if order and actor_id in _order_owner_ids(order):
            return _can_access_order(order, actor)
        return bool(order and _customer_access(actor, _order_customer_id(order))) or _customer_access(actor, str(row.get("customer_id") or ""))
    order = _incentive_order(row)
    return bool(order and actor_id in _order_owner_ids(order) and _can_access_order(order, actor))


def _payment_recipients(order: dict, payment: dict | None = None) -> list[str]:
    values: list[str] = []
    customer = order.get("customer_snapshot") or order.get("customer_company_snapshot") or {}
    for value in (customer.get("email"), (order.get("salesperson_snapshot") or {}).get("email")):
        email = _email(value)
        if email and email not in values:
            values.append(email)
    return values


def _configured_payment_cc_bcc(order: dict) -> tuple[list[str], list[str]]:
    def normalise(values):
        result: list[str] = []
        for value in values if isinstance(values, list) else [values]:
            email = _email(value)
            if email and email not in result:
                result.append(email)
        return result
    return normalise(order.get("cc")), normalise(order.get("bcc"))


def _payment_list_row(payment: dict) -> dict:
    """Return the existing payment record with safe OC/quote context.

    Banking is an operational view over the payments collection; this helper
    only adds references already present on the related Order Confirmation and
    never creates or duplicates a financial record.
    """
    row = dict(payment)
    # Payment proof is private evidence.  Banking list/detail responses may
    # expose its metadata, but never return the base64/file body.
    attachment = row.get("attachment")
    if isinstance(attachment, dict):
        row["attachment"] = {
            key: attachment.get(key)
            for key in ("name", "type", "size")
            if attachment.get(key) is not None
        }
    order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if not order:
        return row
    row.setdefault("order_number", order.get("order_number") or order.get("oc_number"))
    row.setdefault("quotation_id", order.get("quotation_id"))
    row.setdefault("quotation_number", order.get("quotation_number") or order.get("quote_number"))
    row.setdefault("customer_snapshot", order.get("customer_snapshot") or order.get("customer_company_snapshot"))
    row.setdefault("order_confirmation_id", order.get("_id"))
    return row


def _invoice_amount(order: dict) -> float:
    totals = order.get("totals") if isinstance(order.get("totals"), dict) else {}
    return money(order.get("order_amount", totals.get("grand_total", 0)))


def _confirmed_payment_total(order_id: str) -> float:
    """Return confirmed receipts for an OC without counting rejected entries."""
    rows, _ = _store().list(
        "payments",
        {"status": "CONFIRMED", "$or": [{"order_id": order_id}, {"oc_id": order_id}]},
        limit=100_000,
    )
    return money(sum(money(row.get("amount", row.get("payment_amount"))) for row in rows))


def _payment_rollup(order: dict, payments: list[dict], current_id: str) -> dict:
    rollup = payment_rollup(order, payments)
    previous_confirmed = money(sum(
        payment_amount(row)
        for row in payments
        if str(row.get("_id") or "") != current_id and is_confirmed_payment(row)
    ))
    return {
        **rollup,
        "previous_paid": previous_confirmed,
        "total_paid": rollup["confirmed_received"],
        # Compatibility field for older clients; it now represents invoice
        # state and never replaces the payment workflow status.
        "derived_status": rollup["invoice_status"],
    }


def _enrich_payment_rows(rows: list[dict]) -> list[dict]:
    store = _store()
    order_cache: dict[str, dict] = {}
    payment_cache: dict[str, list[dict]] = {}
    enriched: list[dict] = []
    for payment in rows:
        order_id = str(payment.get("order_id") or payment.get("oc_id") or "")
        if order_id not in order_cache:
            order_cache[order_id] = _order(order_id) or {}
        order = order_cache[order_id]
        if order_id not in payment_cache:
            all_payments, _ = store.list(
                "payments",
                {"$or": [{"order_id": order_id}, {"oc_id": order_id}]},
                limit=100_000,
            )
            payment_cache[order_id] = sorted(all_payments, key=lambda row: str(row.get("created_at") or ""))
        row = _payment_list_row(payment)
        row.update(_payment_rollup(order, payment_cache[order_id], str(payment.get("_id") or "")))
        row["workflow_status"] = str(payment.get("status") or "")
        enriched.append(row)
    return enriched


def _is_final_invoice(row: dict) -> bool:
    """Recognize current and historical Order Confirmations for finance totals."""
    record_type = str(row.get("record_type") or "").upper()
    order_kind = str(row.get("order_kind") or "").upper()
    if record_type == "ORDER" or order_kind == "WORKING":
        return False
    if record_type == "ORDER_CONFIRMATION":
        return True
    if bool(row.get("finalized")):
        return True
    if str(row.get("document_type") or "").casefold() == "order_confirmation":
        return True
    if str(row.get("lifecycle_state") or "").upper() in {"FINAL", "FINALIZED"}:
        return True
    metadata_fields = ("record_type", "order_kind", "lifecycle_state", "document_type")
    return not any(row.get(field) for field in metadata_fields) and str(row.get("order_number") or "").upper().startswith("MT-OC-")


def _payment_summary(rows: list[dict], actor: dict) -> dict:
    """Return finance aggregates from the actor's authorized immutable OCs."""
    store = _store()
    confirmed = [row for row in rows if str(row.get("status") or "").upper() == PAYMENT_STATUS_CONFIRMED]
    awaiting = [row for row in rows if str(row.get("status") or "").upper() in {PAYMENT_STATUS_AWAITING, "AWAITING BANK CONFIRMATION"}]
    current, _ = store.list("order_confirmations", {"status": {"$ne": "Deleted"}}, limit=100_000)
    legacy, _ = store.list("orders", {"status": {"$ne": "Deleted"}}, limit=100_000)
    invoices_by_id = {
        str(row.get("_id")): row
        for row in [*current, *legacy]
        if row.get("_id") and _is_final_invoice(row) and _can_access_order(row, actor)
    }
    outstanding = 0.0
    customer_credit = 0.0
    for invoice in invoices_by_id.values():
        invoice_id = str(invoice.get("_id") or "")
        linked_rows, _ = store.list(
            "payments",
            {"$or": [{"order_id": invoice_id}, {"oc_id": invoice_id}]},
            limit=100_000,
        )
        rollup = payment_rollup(invoice, linked_rows)
        outstanding += money(rollup.get("remaining_balance"))
        customer_credit += money(rollup.get("customer_credit"))
    return {
        "total_payments": len(rows),
        "confirmed_received": money(sum(money(row.get("amount")) for row in confirmed)),
        "awaiting_confirmation": len(awaiting),
        "outstanding": money(outstanding),
        "customer_credit": money(customer_credit),
        "invoice_count": len(invoices_by_id),
        "currency": "EUR",
    }


def _validate_banking_payload(payload: dict) -> dict[str, str]:
    fields = {
        "customer_id": "Customer",
        "order_id": "Invoice / Order Confirmation",
        "amount": "Payment amount",
        "payment_date": "Payment date",
        "payment_mode": "Payment mode",
        "bank_name": "Bank name",
        "bank_account": "Bank account",
        "utr": "UTR / transaction reference",
        "reference_number": "Payment reference",
        "attachment": "Payment proof",
    }
    errors: dict[str, str] = {}
    for field, label in fields.items():
        value = payload.get(field)
        if field == "amount":
            if money(value) <= 0:
                errors[field] = f"{label} must be greater than zero"
        elif field == "attachment":
            if not isinstance(value, dict) or not str(value.get("data") or "").strip():
                errors[field] = f"{label} is required"
        elif not str(value or "").strip():
            errors[field] = f"{label} is required"
    currency = str(payload.get("currency") or "EUR").upper()
    if currency != "EUR":
        errors["currency"] = "Payments must use the invoice currency (EUR)"
    return errors


def _bank_detail_row_for_user(user_id: str) -> dict:
    row = _store().find_one("user_bank_details", {"user_id": str(user_id)})
    return row or {"user_id": str(user_id)}


def _bank_detail_user(user_id: str) -> dict | None:
    return _store().find_one("users", {"_id": str(user_id), "active": {"$ne": False}})


@bp.get("/bank-details")
@login_required
def list_bank_details():
    """List bank details inside the requester's ownership/team scope."""
    actor = current_user() or {}
    if not _bank_detail_permission(actor):
        return failure("Bank details access is not permitted", status=403, error="bank_details_forbidden")
    actor_id = _user_id(actor)
    if str(actor.get("role_id") or "") in {"superadmin", "admin"}:
        users, _ = _store().list("users", {"active": {"$ne": False}}, limit=100_000, sort="name", direction=1)
    else:
        allowed_ids = set(accessible_user_ids(_store(), actor))
        users, _ = _store().list("users", {"_id": {"$in": list(allowed_ids)}, "active": {"$ne": False}}, limit=100_000, sort="name", direction=1)
    rows: list[dict] = []
    for user in users:
        target_id = str(user.get("_id") or "")
        if not target_id or not can_view_bank_details(_store(), actor, target_id):
            continue
        row = serialize_bank_details(_bank_detail_row_for_user(target_id), include_sensitive=target_id == actor_id)
        row["owner"] = {"_id": target_id, "name": user.get("name"), "email": user.get("email"), "role_id": user.get("role_id"), "manager_id": user.get("manager_id")}
        row["can_edit"] = can_edit_bank_details(_store(), actor, target_id) and (
            str(actor.get("role_id") or "") in {"superadmin", "admin"} or "bank_details.update" in set(actor.get("permissions") or [])
        )
        rows.append(row)
    return success({"items": rows, "total": len(rows)})


@bp.get("/bank-details/<user_id>")
@login_required
def get_bank_details(user_id: str):
    actor = current_user() or {}
    if not _bank_detail_permission(actor) or not can_view_bank_details(_store(), actor, user_id):
        return failure("Bank details access denied", status=403, error="bank_details_forbidden")
    user = _bank_detail_user(user_id)
    if not user:
        return failure("User not found", status=404)
    # Owners may see their own values; delegated manager/admin views remain
    # masked to reduce unnecessary exposure of account identifiers.
    row = serialize_bank_details(_bank_detail_row_for_user(user_id), include_sensitive=str(user_id) == _user_id(actor))
    row["owner"] = {"_id": user.get("_id"), "name": user.get("name"), "email": user.get("email"), "role_id": user.get("role_id"), "manager_id": user.get("manager_id")}
    row["can_edit"] = can_edit_bank_details(_store(), actor, user_id) and (
        str(actor.get("role_id") or "") in {"superadmin", "admin"} or "bank_details.update" in set(actor.get("permissions") or [])
    )
    return success(row)


@bp.patch("/bank-details/<user_id>")
@login_required
def update_bank_details(user_id: str):
    actor = current_user() or {}
    if not _bank_detail_permission(actor) or not can_edit_bank_details(_store(), actor, user_id):
        return failure("Bank details update is not permitted", status=403, error="bank_details_update_forbidden")
    if str(actor.get("role_id") or "") not in {"superadmin", "admin"} and "bank_details.update" not in set(actor.get("permissions") or []):
        return failure("Bank details update is not permitted", status=403, error="bank_details_update_forbidden")
    target = _bank_detail_user(user_id)
    if not target:
        return failure("User not found", status=404)
    payload = request.get_json(silent=True) or {}
    changes = normalize_bank_changes(payload)
    if not changes:
        return failure("No bank-detail fields were supplied", status=422, error="bank_details_empty")
    old = _store().find_one("user_bank_details", {"user_id": str(user_id)}) or {"user_id": str(user_id)}
    row = _store().update_one("user_bank_details", {"user_id": str(user_id)}, changes, upsert=True)
    if not row:
        return failure("Bank details could not be saved", status=500)
    audit("bank_details.update", "user_bank_details", str(user_id), {
        "changed_by": _user_id(actor), "target_user": str(user_id), "manager_id": target.get("manager_id"),
        **audit_bank_diff(old, row, sorted(changes)),
    })
    return success(serialize_bank_details(row, include_sensitive=str(user_id) == _user_id(actor)), "Bank details updated")


@bp.get("/payments")
@login_required
def list_payments():
    store = _store()
    user = current_user() or {}
    order_id = str(request.args.get("order_id") or "").strip()
    global_scope = str(user.get("role_id") or "") == "superadmin"
    has_global_permission = (
        global_scope
        or "payments.view" in user.get("permissions", [])
        or "payments.manage" in user.get("permissions", [])
    )
    if not has_global_permission and not order_id:
        return failure("You do not have permission to view payments", status=403)
    query: dict = {}
    if request.args.get("status"):
        query["status"] = request.args["status"]
    else:
        # Deleted records remain auditable in Mongo/audit logs but are not part
        # of the operational Banking list.
        query["status"] = {"$ne": PAYMENT_STATUS_DELETED}
    if order_id:
        query["$or"] = [{"order_id": order_id}, {"oc_id": order_id}]
        order = _order(order_id)
        if not order:
            return failure("Order not found", status=404)
        if not global_scope and not _can_access_order(order, user):
            return failure("Payment access denied", status=403)
    if not global_scope:
        rows, _ = store.list("payments", query, limit=100_000)
        rows = [row for row in rows if _can_view_payment(row, user)]
        rows = _enrich_payment_rows(rows)
        return success({"items": rows, "total": len(rows), "summary": _payment_summary(rows, user)})
    summary_rows, total = store.list("payments", query, limit=100_000)
    rows = summary_rows[:min(int(request.args.get("limit", 100)), 500)]
    rows = _enrich_payment_rows(rows)
    return success({"items": rows, "total": total, "summary": _payment_summary(summary_rows, user)})


@bp.get("/payments/<payment_id>/proof")
@login_required
def payment_proof(payment_id: str):
    """Return private proof bytes only to an authorized payment viewer."""
    payment = _store().find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    if not _can_view_payment(payment):
        return failure("Payment access denied", status=403)
    attachment = payment.get("attachment")
    if not isinstance(attachment, dict) or not isinstance(attachment.get("data"), str):
        return failure("No payment proof is attached", status=404, error="payment_proof_missing")
    return success({
        "name": str(attachment.get("name") or "Payment proof"),
        "type": str(attachment.get("type") or "application/octet-stream"),
        "size": int(attachment.get("size") or 0),
        "data": attachment["data"],
    })


@bp.post("/payments")
@login_required
def create_payment():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("oc_id") or "").strip()
    order = _order(order_id)
    if not order:
        return failure("Order not found", status=404, error="order_not_found")
    customer_id = _order_customer_id(order)
    user = current_user() or {}
    if not _can_create_payment(user):
        return failure("Payment creation is not permitted", status=403, error="payment_create_forbidden")
    if payload.get("workflow") == "banking":
        errors = _validate_banking_payload({**payload, "customer_id": payload.get("customer_id"), "order_id": order_id})
        if str(payload.get("customer_id") or "") != customer_id:
            errors["customer_id"] = "Selected customer does not own this Order Confirmation"
        if errors:
            return failure("Please correct the payment details", status=422, error="invalid_payment_fields", details={"fields": errors})
    if not _can_record_payment(order, user):
        return failure("Payment access denied", status=403)
    amount = money(payload.get("payment_amount", payload.get("amount")))
    payment_date = _parse_date(payload.get("payment_date"))
    if amount <= 0 or payment_date is None:
        return failure("Payment amount and date are required", status=422, error="invalid_payment")
    attachment = payload.get("attachment")
    if attachment is not None:
        if not isinstance(attachment, dict) or not isinstance(attachment.get("data"), str):
            return failure("Payment proof must be a valid uploaded file", status=422, error="invalid_payment_proof")
        allowed_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
        if payload.get("workflow") == "banking" and str(attachment.get("type") or "").lower() not in allowed_types:
            return failure("Payment proof must be a PDF or image", status=422, error="invalid_payment_proof_type")
        if len(attachment["data"]) > 8_000_000:
            return failure("Payment proof is too large", status=422, error="payment_proof_too_large")
        try:
            attachment_size = max(0, min(int(attachment.get("size") or 0), 5 * 1024 * 1024))
        except (TypeError, ValueError):
            attachment_size = 0
        attachment = {
            "name": str(attachment.get("name") or "proof")[:160],
            "type": str(attachment.get("type") or "application/octet-stream")[:120],
            "size": attachment_size,
            "data": attachment["data"],
        }
    now = utcnow()
    actor_id = (current_user() or {}).get("_id")
    row = _store().insert_one("payments", {
        "payment_number": f"PAY-{_store().next_counter('payment'):05d}",
        "order_id": order_id, "oc_id": order_id, "quotation_id": order.get("quotation_id"),
        "customer_id": customer_id, "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or {},
        "amount": amount, "payment_amount": amount, "currency": "EUR", "payment_date": payment_date,
        "bank_name": str(payload.get("bank_name") or "").strip()[:160], "bank_account": str(payload.get("bank_account") or "").strip()[:160],
        "utr": str(payload.get("utr") or payload.get("transaction_reference") or "").strip()[:160],
        "payment_mode": str(payload.get("payment_mode") or "").strip()[:80], "reference_number": str(payload.get("reference_number") or "").strip()[:160],
        "notes": str(payload.get("notes") or "").strip()[:2000], "attachment": attachment,
        "status": PAYMENT_STATUS_AWAITING, "created_by_user_id": actor_id, "workflow": payload.get("workflow"),
        "submitted_at": now, "submitted_by_user_id": actor_id,
        "audit": [
            {"action": "created", "by": actor_id, "at": now},
            {"action": "submitted", "by": actor_id, "at": now},
        ],
    })
    sync_order_payment_state(_store(), order_id)
    audit("payment.create", "payment", str(row["_id"]), {"order_id": order_id, "amount": amount})
    return success(_enrich_payment_rows([row])[0], "Payment submitted for Superadmin confirmation", 201)


@bp.patch("/payments/<payment_id>")
@login_required
def update_payment(payment_id: str):
    """Update an unconfirmed payment and return it to the submission queue."""
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    payment_order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if not payment_order or not _can_record_payment(payment_order):
        return failure("Payment access denied", status=403)
    if not _can_create_payment():
        return failure("Payment editing is not permitted", status=403, error="payment_edit_forbidden")
    if payment.get("financial_locked") or payment.get("status") == "CONFIRMED":
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    payload = request.get_json(silent=True) or {}
    if payload.get("workflow") == "banking":
        order_customer_id = _order_customer_id(payment_order)
        errors = _validate_banking_payload({**payment, **payload, "customer_id": payload.get("customer_id") or order_customer_id, "order_id": payment_order.get("_id")})
        if payload.get("customer_id") and str(payload.get("customer_id")) != order_customer_id:
            errors["customer_id"] = "Selected customer does not own this Order Confirmation"
        if errors:
            return failure("Please correct the payment details", status=422, error="invalid_payment_fields", details={"fields": errors})
    changes: dict[str, object] = {}
    if "amount" in payload or "payment_amount" in payload:
        amount = money(payload.get("payment_amount", payload.get("amount")))
        if amount <= 0:
            return failure("Payment amount must be greater than zero", status=422, error="invalid_payment")
        changes["amount"] = amount
        changes["payment_amount"] = amount
    if "payment_date" in payload:
        payment_date = _parse_date(payload.get("payment_date"))
        if payment_date is None:
            return failure("Payment date is required", status=422, error="invalid_payment")
        changes["payment_date"] = payment_date
    for field, limit in (("bank_name", 160), ("bank_account", 160), ("utr", 160), ("payment_mode", 80), ("reference_number", 160), ("notes", 2000)):
        if field in payload:
            changes[field] = str(payload.get(field) or "").strip()[:limit]
    if "attachment" in payload:
        attachment = payload.get("attachment")
        if attachment is not None:
            if not isinstance(attachment, dict) or not isinstance(attachment.get("data"), str):
                return failure("Payment proof must be a valid uploaded file", status=422, error="invalid_payment_proof")
            allowed_types = {"application/pdf", "image/jpeg", "image/png", "image/webp"}
            if payload.get("workflow") == "banking" and str(attachment.get("type") or "").lower() not in allowed_types:
                return failure("Payment proof must be a PDF or image", status=422, error="invalid_payment_proof_type")
            if len(attachment["data"]) > 8_000_000:
                return failure("Payment proof is too large", status=422, error="payment_proof_too_large")
            try:
                attachment_size = max(0, min(int(attachment.get("size") or 0), 5 * 1024 * 1024))
            except (TypeError, ValueError):
                attachment_size = 0
            attachment = {"name": str(attachment.get("name") or "proof")[:160], "type": str(attachment.get("type") or "application/octet-stream")[:120], "size": attachment_size, "data": attachment["data"]}
        changes["attachment"] = attachment
    if not changes:
        return failure("No payment fields were supplied", status=422, error="invalid_payment")
    now = utcnow()
    actor_id = (current_user() or {}).get("_id")
    changes.update({
        "status": PAYMENT_STATUS_AWAITING,
        "submitted_at": now,
        "submitted_by_user_id": actor_id,
        "updated_at": now,
        "audit": [
            *payment.get("audit", []),
            {"action": "updated", "by": actor_id, "at": now, "fields": sorted(changes)},
            {"action": "submitted", "by": actor_id, "at": now},
        ],
    })
    row = store.update_one("payments", {"_id": payment_id}, changes)
    sync_order_payment_state(store, str(payment_order.get("_id") or ""))
    audit("payment.update", "payment", payment_id, {"fields": sorted(changes)})
    return success(_enrich_payment_rows([row])[0], "Payment updated and submitted for Superadmin confirmation")


@bp.post("/payments/<payment_id>/submit")
@login_required
def submit_payment(payment_id: str):
    if not _can_create_payment():
        return failure("Payment submission is not permitted", status=403, error="payment_submit_forbidden")
    payment = _store().find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    payment_order = _order(str(payment.get("order_id") or payment.get("oc_id") or ""))
    if not payment_order or not _can_record_payment(payment_order):
        return failure("Payment access denied", status=403)
    if payment.get("financial_locked") and not _superadmin():
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    if payment.get("status") == PAYMENT_STATUS_AWAITING:
        return success(_enrich_payment_rows([payment])[0], "Payment is awaiting Superadmin confirmation")
    if payment.get("status") not in {"PAYMENT RECORDED", PAYMENT_STATUS_REJECTED}:
        return failure("Payment cannot be submitted in its current state", status=409)
    now = utcnow()
    row = _store().update_one("payments", {"_id": payment_id}, {"status": PAYMENT_STATUS_AWAITING, "submitted_at": now, "submitted_by_user_id": (current_user() or {}).get("_id"), "audit": [*payment.get("audit", []), {"action": "submitted", "by": (current_user() or {}).get("_id"), "at": now}]})
    sync_order_payment_state(_store(), str(payment_order.get("_id") or ""))
    audit("payment.submit", "payment", payment_id, {})
    return success(_enrich_payment_rows([row])[0], "Payment submitted for Superadmin confirmation")


@bp.post("/payments/<payment_id>/confirm")
@login_required
def confirm_payment(payment_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can confirm bank receipt", status=403, error="superadmin_required")
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    # Confirmation is a separate privileged step.  A merely entered payment
    # must first be submitted into the Superadmin review queue.
    if payment.get("status") not in {PAYMENT_STATUS_AWAITING, "AWAITING BANK CONFIRMATION"}:
        return failure("Payment is not awaiting confirmation", status=409)
    confirmed_at = utcnow()
    actor = current_user() or {}
    row = store.update_one("payments", {"_id": payment_id}, {"status": PAYMENT_STATUS_CONFIRMED, "confirmed_by_user_id": actor.get("_id"), "confirmed_at": confirmed_at, "financial_locked": True, "audit": [*payment.get("audit", []), {"action": "confirmed", "by": actor.get("_id"), "at": confirmed_at}]})
    incentive = activate_incentive(store, str(payment.get("order_id")), confirmed_at)
    if incentive:
        audit("incentive.activated", "incentive", str(incentive.get("_id")), {"order_id": payment.get("order_id"), "payment_id": payment_id})
    order_id = str(payment.get("order_id") or payment.get("oc_id") or "")
    order = sync_order_payment_state(store, order_id) or _order(order_id) or {}
    row = _enrich_payment_rows([row])[0]
    recipients = _payment_recipients(order, payment)
    email_result = None
    if recipients:
        try:
            email_result = current_app.extensions["email_service"].send(
                purpose="order", to=recipients,
                subject=f"Payment confirmation {order.get('order_number', order.get('_id', ''))} - Moneda Technologies",
                html=f"<p>Payment of <strong>EUR {money(payment.get('amount')):.2f}</strong> has been confirmed for Order Confirmation <strong>{escape(str(order.get('order_number') or order.get('_id') or ''))}</strong>.</p><p>Payment date: {escape(str(payment.get('payment_date') or ''))}<br>UTR/reference: {escape(str(payment.get('utr') or payment.get('reference_number') or 'Not provided'))}</p>",
                cc=_configured_payment_cc_bcc(order)[0], bcc=_configured_payment_cc_bcc(order)[1],
                customer_facing=True, request_id=f"payment-confirmed-{payment_id}",
            )
        except Exception:
            current_app.logger.exception("payment confirmation email failed payment_id=%s", payment_id)
    store.insert_one("email_logs", {"payment_id": payment_id, "order_id": payment.get("order_id"), "message_type": "payment_confirmation", "status": "sent" if email_result else "not_sent", "created_at": utcnow()})
    audit("payment.confirm", "payment", payment_id, {"order_id": payment.get("order_id"), "incentive_id": incentive.get("_id") if incentive else None})
    return success({"payment": row, "order": order, "incentive": incentive}, "Payment confirmed")


@bp.post("/payments/<payment_id>/reject")
@login_required
def reject_payment(payment_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can reject a payment", status=403, error="superadmin_required")
    payment = _store().find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    if payment.get("status") not in {PAYMENT_STATUS_AWAITING, "AWAITING BANK CONFIRMATION", "PAYMENT RECORDED"}:
        return failure("Payment cannot be rejected in its current state", status=409)
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return failure("A rejection reason is required", status=422)
    rejected_at = utcnow()
    row = _store().update_one("payments", {"_id": payment_id}, {"status": PAYMENT_STATUS_REJECTED, "rejected_by_user_id": (current_user() or {}).get("_id"), "rejected_at": rejected_at, "rejection_reason": reason, "financial_locked": False, "audit": [*payment.get("audit", []), {"action": "rejected", "reason": reason, "by": (current_user() or {}).get("_id"), "at": rejected_at}]})
    order_id = str(payment.get("order_id") or payment.get("oc_id") or "")
    sync_order_payment_state(_store(), order_id)
    row = _enrich_payment_rows([row])[0]
    audit("payment.reject", "payment", payment_id, {"reason": reason})
    return success(row, "Payment sent back for correction")


@bp.post("/payments/<payment_id>/void")
@login_required
def void_payment(payment_id: str):
    """Void a payment without destroying its financial/audit history."""
    actor = current_user() or {}
    if not _can_create_payment(actor):
        return failure("Payment voiding is not permitted", status=403, error="payment_void_forbidden")
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    if not _can_view_payment(payment, actor):
        return failure("Payment access denied", status=403)
    status = str(payment.get("status") or "").upper()
    if status == PAYMENT_STATUS_VOIDED:
        return success(_enrich_payment_rows([payment])[0], "Payment already voided")
    if status == PAYMENT_STATUS_DELETED:
        return failure("Deleted payments cannot be voided", status=409, error="payment_deleted")
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()[:500]
    if not reason:
        return failure("A reason is required to void a payment", status=422, error="void_reason_required")
    now = utcnow()
    updated = store.update_one("payments", {"_id": payment_id}, {
        "status": PAYMENT_STATUS_VOIDED, "voided_at": now, "voided_by_user_id": actor.get("_id"),
        "void_reason": reason, "financial_locked": False,
        "audit": [*(payment.get("audit") or []), {"action": "voided", "from": payment.get("status"), "to": PAYMENT_STATUS_VOIDED, "reason": reason, "by": actor.get("_id"), "at": now}],
    }) or payment
    order_id = str(payment.get("order_id") or payment.get("oc_id") or "")
    if order_id:
        sync_order_payment_state(store, order_id)
    audit("payment.void", "payment", payment_id, {"order_id": order_id, "reason": reason, "previous_status": payment.get("status")})
    return success(_enrich_payment_rows([updated])[0], "Payment voided")


@bp.delete("/payments/<payment_id>")
@login_required
def delete_payment(payment_id: str):
    """Soft-delete a payment only after it has been voided."""
    actor = current_user() or {}
    if not _can_create_payment(actor):
        return failure("Payment deletion is not permitted", status=403, error="payment_delete_forbidden")
    store = _store()
    payment = store.find_one("payments", {"_id": payment_id})
    if not payment:
        return failure("Payment not found", status=404)
    if not _can_view_payment(payment, actor):
        return failure("Payment access denied", status=403)
    status = str(payment.get("status") or "").upper()
    if status == PAYMENT_STATUS_DELETED:
        return success({"_id": payment_id, "status": PAYMENT_STATUS_DELETED}, "Payment already deleted")
    if status != PAYMENT_STATUS_VOIDED:
        return failure("Active payments must be voided before deletion", status=409, error="payment_must_be_voided")
    reason = str((request.get_json(silent=True) or {}).get("reason") or "Deleted after payment was voided")[:500].strip()
    now = utcnow()
    updated = store.update_one("payments", {"_id": payment_id}, {
        "status": PAYMENT_STATUS_DELETED, "deleted_at": now, "deleted_by_user_id": actor.get("_id"),
        "deletion_reason": reason, "audit": [*(payment.get("audit") or []), {"action": "deleted", "from": PAYMENT_STATUS_VOIDED, "to": PAYMENT_STATUS_DELETED, "reason": reason, "by": actor.get("_id"), "at": now}],
    }) or payment
    order_id = str(payment.get("order_id") or payment.get("oc_id") or "")
    if order_id:
        sync_order_payment_state(store, order_id)
    audit("payment.delete", "payment", payment_id, {"order_id": order_id, "reason": reason})
    return success({"_id": updated.get("_id"), "status": PAYMENT_STATUS_DELETED}, "Payment deleted")


@bp.get("/incentives")
@login_required
def list_incentives():
    store = _store()
    user = current_user() or {}
    has_permission = "incentives.view" in user.get("permissions", []) or "incentives.manage" in user.get("permissions", [])
    if not has_permission and str(user.get("role_id") or "") not in {"admin", "superadmin", "user", "manager", "manager_sales_admin"}:
        return failure("You do not have permission to view incentives", status=403)
    query: dict = {}
    if str(request.args.get("include_cancelled") or "").lower() not in {"1", "true", "yes"}:
        query["status"] = {"$ne": "CANCELLED"}
    # Superadmin has company-wide visibility. Admins are restricted to their
    # assigned customer scope; sales people and managers remain restricted to
    # their own OC relationships.
    global_scope = str(user.get("role_id") or "") == "superadmin"
    allowed_customer_ids: list[str] | None = None
    if not global_scope:
        allowed_customer_ids = customer_access_ids_for_user(str(user.get("_id") or ""))
        team_ids = accessible_user_ids(store, user) if str(user.get("role_id") or "") in {"manager", "manager_sales_admin"} else [user.get("_id")]
        owner_orders, _ = store.list("orders", {"$or": [
            {"salesperson_id": {"$in": team_ids}}, {"prepared_by_user_id": {"$in": team_ids}},
            {"created_by_user_id": {"$in": team_ids}}, {"user_id": {"$in": team_ids}},
        ]}, limit=100_000)
        owner_order_ids = [row.get("_id") for row in owner_orders if row.get("_id")]
        query["$or"] = [
            {"salesperson_id": {"$in": team_ids}},
            {"order_id": {"$in": owner_order_ids or ["__no_owned_orders__"]}},
            {"oc_id": {"$in": owner_order_ids or ["__no_owned_orders__"]}},
        ]
        if str(user.get("role_id") or "") in {"admin", "manager", "manager_sales_admin"} and allowed_customer_ids:
            query["$or"].append({"customer_id": {"$in": allowed_customer_ids}})
    for key in ("status", "salesperson_id", "customer_id", "order_id"):
        if request.args.get(key):
            if key == "status" and str(request.args[key]).upper() == "CANCELLED" and str(request.args.get("include_cancelled") or "").lower() not in {"1", "true", "yes"}:
                return success({"items": [], "total": 0})
            if key == "salesperson_id" and not global_scope:
                allowed_salespeople = set(accessible_user_ids(store, user)) if str(user.get("role_id") or "") in {"manager", "manager_sales_admin"} else {str(user.get("_id") or "")}
                if request.args[key] not in allowed_salespeople:
                    return success({"items": [], "total": 0})
            if key == "customer_id" and allowed_customer_ids is not None and request.args[key] not in allowed_customer_ids:
                return success({"items": [], "total": 0})
            if key == "order_id":
                # Keep the role/customer scope while supporting legacy
                # incentives that only retained oc_id.
                query.setdefault("$and", []).append({"$or": [{"order_id": request.args[key]}, {"oc_id": request.args[key]}]})
            else:
                query[key] = request.args[key]
    # Search and payment/date filters are applied to the server-authorized
    # result set below.  Fetching the bounded management list first avoids
    # allowing a client-supplied filter to bypass the scope query above.
    rows, _ = store.list("incentives", query, limit=min(int(request.args.get("limit", 500)), 500))
    search = str(request.args.get("search") or "").strip().casefold()
    payment_filter = str(request.args.get("payment_status") or "").strip().casefold()
    product_filter = str(request.args.get("product_id") or "").strip()
    category_filter = str(request.args.get("category_id") or request.args.get("category") or "").strip().casefold()
    from_date = _parse_date(request.args.get("from_date"), required=False) if request.args.get("from_date") else None
    to_date = _parse_date(request.args.get("to_date"), required=False) if request.args.get("to_date") else None
    if to_date:
        # Date inputs are inclusive from the user's perspective.
        from datetime import timedelta
        to_date = to_date + timedelta(days=1)
    filtered: list[dict] = []
    for index, row in enumerate(rows):
        refreshed = recompute_incentive_totals(store, str(row["_id"]))
        if refreshed:
            rows[index] = refreshed
        row = rows[index]
        if not global_scope and not _can_view_incentive(row, user):
            continue
        order = _incentive_order(row) or {}
        if not order or str(order.get("status") or "").casefold() == "deleted":
            # A transaction-linked incentive is not an independent payable
            # record.  Do not expose orphan/deleted-OC rows in any overview.
            continue
        if not row.get("customer_snapshot"):
            row["customer_snapshot"] = order.get("customer_snapshot") or order.get("customer_company_snapshot") or order.get("company_snapshot") or {}
        if not row.get("salesperson_snapshot"):
            row["salesperson_snapshot"] = order.get("salesperson_snapshot") or {}
        row["order_number"] = row.get("order_number") or order.get("order_number")
        transaction_id = incentive_transaction_id(row)
        payments, _ = store.list(
            "payments",
            {"$or": [{"order_id": transaction_id}, {"oc_id": transaction_id}]},
            limit=100,
        )
        payment_status = "Paid" if row.get("payment_confirmation_date") or any(str(payment.get("status") or "").upper() == "CONFIRMED" for payment in payments) else "Pending Payment"
        row["payment_status"] = payment_status
        customer = row.get("customer_snapshot") or {}
        salesperson = row.get("salesperson_snapshot") or {}
        internal_lines = [
            line for line in (row.get("incentive_lines") or [])
            if isinstance(line, dict) and not _customer_incentive_line(line)
        ]
        recipient_text = " ".join(
            str(value or "")
            for line in internal_lines
            for value in (
                line.get("recipient_user_id"),
                (line.get("recipient_snapshot") or {}).get("name"),
                (line.get("recipient_snapshot") or {}).get("email"),
                line.get("recipient_role"),
            )
        )
        haystack = " ".join(str(value or "") for value in (
            row.get("order_number"), row.get("oc_number"), row.get("order_id"),
            row.get("customer_id"), customer.get("name"), customer.get("company_name"),
            salesperson.get("name"), salesperson.get("email"), row.get("salesperson_id"), recipient_text,
        )).casefold()
        if search and search not in haystack:
            continue
        if payment_filter and payment_filter not in {payment_status.casefold(), "confirmed" if payment_status == "Paid" else "pending", "unpaid" if payment_status != "Paid" else ""}:
            continue
        created_at = row.get("created_at")
        if from_date and (not created_at or created_at < from_date):
            continue
        if to_date and (not created_at or created_at >= to_date):
            continue
        allocation_views = _internal_allocation_views(row, user)
        for allocation in allocation_views:
            lines = list(allocation.get("incentive_lines") or [])
            if product_filter:
                lines = [line for line in lines if str(line.get("product_id") or "") == product_filter]
            if category_filter:
                lines = [
                    line for line in lines
                    if str(line.get("category_id") or line.get("category_name") or "").casefold() == category_filter
                ]
            recipient_role_filter = str(request.args.get("recipient_role") or "").strip().casefold()
            if recipient_role_filter and recipient_role_filter not in {
                str(allocation.get("recipient_role") or "").casefold(),
                str(allocation.get("recipient_type") or "").casefold(),
            }:
                continue
            if not lines:
                continue
            allocation["incentive_lines"] = lines
            allocation["gross_incentive_amount"] = money(sum(money(line.get("incentive_amount")) for line in lines))
            allocation["net_payable_incentive"] = allocation["gross_incentive_amount"]
            rates = {float(line.get("incentive_rate_snapshot") or 0) for line in lines}
            allocation["incentive_percentage_snapshot"] = next(iter(rates)) if len(rates) == 1 else None
            filtered.append(allocation)
    return success({"items": filtered, "total": len(filtered)})


@bp.get("/customer-incentives")
@login_required
def list_customer_incentives():
    """List the customer allocation view without exposing hidden snapshots."""
    store = _store()
    actor = current_user() or {}
    if not _customer_incentive_visibility(actor):
        return failure("Customer incentive visibility is disabled", status=403, error="customer_incentive_visibility_denied")
    global_scope = str(actor.get("role_id") or "") == "superadmin"
    allowed_ids = set(customer_access_ids_for_user(str(actor.get("_id") or ""))) if not global_scope else set()
    rows, _ = store.list("incentives", {"status": {"$ne": "CANCELLED"}}, limit=min(int(request.args.get("limit", 500)), 500))
    search = str(request.args.get("search") or "").strip().casefold()
    result: list[dict] = []
    for row in rows:
        lines = [line for line in (row.get("incentive_lines") or []) if isinstance(line, dict) and _customer_incentive_line(line)]
        if not lines:
            continue
        customer_id = str(row.get("customer_id") or lines[0].get("customer_id") or "")
        if not global_scope and customer_id not in allowed_ids:
            continue
        order = _incentive_order(row) or {}
        if not order or str(order.get("status") or "").casefold() == "deleted":
            continue
        customer = row.get("customer_snapshot") or {}
        haystack = " ".join(str(value or "") for value in (
            customer.get("company_name"), customer.get("name"), row.get("oc_number"),
            row.get("order_number"), row.get("customer_id"), row.get("salesperson_id"),
        )).casefold()
        if search and search not in haystack:
            continue
        result.append(_serialize_incentive_for_actor(row, actor, view="customer"))
    return success({"items": result, "total": len(result)})


@bp.get("/customer-incentive-visibility")
@login_required
def customer_incentive_visibility_for_actor():
    """Expose only the effective visibility flag to the signed-in actor.

    Unlike the admin settings endpoint this never returns the global toggle
    values, so it is safe to use for role-aware navigation/bootstrap.
    """
    actor = current_user() or {}
    return success({
        "visible": _customer_incentive_visibility(actor),
        "can_manage": str(actor.get("role_id") or "") == "superadmin",
    })


@bp.get("/customer-incentives/<incentive_id>")
@login_required
def get_customer_incentive(incentive_id: str):
    store = _store()
    actor = current_user() or {}
    if not _customer_incentive_visibility(actor):
        return failure("Customer incentive visibility is disabled", status=403, error="customer_incentive_visibility_denied")
    row = store.find_one("incentives", {"_id": incentive_id})
    if not row or not any(_customer_incentive_line(line) for line in (row.get("incentive_lines") or []) if isinstance(line, dict)):
        return failure("Customer incentive not found", status=404)
    if str(actor.get("role_id") or "") != "superadmin":
        customer_lines = [line for line in (row.get("incentive_lines") or []) if isinstance(line, dict) and _customer_incentive_line(line)]
        customer_id = str(row.get("customer_id") or (customer_lines[0].get("customer_id") if customer_lines else "") or "")
        if customer_id not in set(customer_access_ids_for_user(str(actor.get("_id") or ""))):
            return failure("Customer incentive access denied", status=403)
    order = _incentive_order(row) or {}
    if not order or str(order.get("status") or "").casefold() == "deleted":
        return failure("The originating Order Confirmation no longer exists", status=410, error="INCENTIVE_TRANSACTION_DELETED")
    return success(_serialize_incentive_for_actor(recompute_incentive_totals(store, incentive_id) or row, actor, view="customer"))


@bp.get("/incentives/<incentive_id>")
@login_required
def get_incentive(incentive_id: str):
    store = _store()
    row = store.find_one("incentives", {"_id": incentive_id})
    if not row:
        return failure("Incentive not found", status=404)
    user = current_user() or {}
    if not _can_view_incentive(row, user):
        return failure("Incentive access denied", status=403)
    order = _incentive_order(row)
    if not order or str(order.get("status") or "").casefold() == "deleted":
        return failure("The originating Order Confirmation no longer exists", status=410, error="INCENTIVE_TRANSACTION_DELETED")
    snapshot = recompute_incentive_totals(store, incentive_id) or row
    result = _serialize_incentive_for_actor(snapshot, user, view="internal")
    # Internal and customer allocations are kept separate in the response so
    # the UI can expose only the recipient tabs the actor is authorized to see.
    if _customer_incentive_visibility(user):
        result["customer_incentive_lines"] = [
            line for line in (snapshot.get("incentive_lines") or [])
            if isinstance(line, dict) and _customer_incentive_line(line)
        ]
    else:
        result["customer_incentive_lines"] = []
    visible_lines = [
        line for line in (result.get("incentive_lines") or [])
        if isinstance(line, dict)
    ] + [
        line for line in (result.get("customer_incentive_lines") or [])
        if isinstance(line, dict)
    ]
    payment_status = result.get("payment_status") or order.get("payment_status") or "Pending Payment"
    allocations = _allocation_snapshot_views(
        visible_lines,
        status=result.get("status") or "PENDING PAYMENT",
        payment_status=payment_status,
    )
    result["allocations"] = allocations
    result["total_incentive"] = money(sum(money(allocation.get("amount")) for allocation in allocations))
    requested_recipient = str(request.args.get("recipient_user_id") or "")
    requested_allocation = str(request.args.get("allocation_type") or "")
    if requested_recipient:
        result["incentive_lines"] = [
            line for line in result.get("incentive_lines") or []
            if str(line.get("recipient_user_id") or "") == requested_recipient
            and (not requested_allocation or str(line.get("allocation_type") or "creator") == requested_allocation)
        ]
        if not result["incentive_lines"]:
            return failure("Incentive allocation not found", status=404)
        first = result["incentive_lines"][0]
        result.update({
            "recipient_user_id": first.get("recipient_user_id"),
            "recipient_role": first.get("recipient_role"),
            "recipient_type": first.get("recipient_type"),
            "recipient_snapshot": first.get("recipient_snapshot") or {},
            "allocation_type": first.get("allocation_type") or "creator",
        })
        result["gross_incentive_amount"] = money(sum(money(line.get("incentive_amount")) for line in result["incentive_lines"]))
        result["net_payable_incentive"] = result["gross_incentive_amount"]
    return success(result)


@bp.delete("/incentives/<incentive_id>")
@permission_required("incentives.delete")
def cancel_incentive(incentive_id: str):
    """Soft-cancel an unpaid incentive while retaining its audit snapshot."""
    store = _store()
    row = store.find_one("incentives", {"_id": incentive_id})
    if not row:
        return failure("Incentive not found", status=404)
    actor = current_user() or {}
    if not _can_view_incentive(row, actor):
        return failure("Incentive access denied", status=403)
    status = str(row.get("status") or "").upper()
    paid_amount = money(row.get("paid_amount"))
    allocations, _ = store.list("incentive_allocations", {"incentive_id": incentive_id}, limit=100_000)
    has_paid_allocation = any(
        str(allocation.get("status") or "").upper() == "PAID" or money(allocation.get("paid_amount")) > 0
        for allocation in allocations
    )
    payout_linked = has_payout_link(row) or any(has_payout_link(allocation) for allocation in allocations)
    if status == "PAID" or paid_amount > 0 or has_paid_allocation or payout_linked:
        return failure(
            "Paid or payout-linked incentives cannot be deleted",
            status=409,
            error="INCENTIVE_FINANCIALLY_LOCKED",
        )
    if status == "CANCELLED":
        return success(row, "Incentive already cancelled")
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or "Cancelled from Incentive Overview").strip()[:500]
    now = utcnow()
    audit_entry = {
        "action": "cancelled",
        "from": status or "PENDING PAYMENT",
        "to": "CANCELLED",
        "reason": reason,
        "by": actor.get("_id"),
        "at": now,
    }
    updated = store.update_one("incentives", {"_id": incentive_id}, {
        "status": "CANCELLED",
        "cancelled_at": now,
        "cancelled_by": actor.get("_id"),
        "cancelled_reason": reason,
        "financial_locked": True,
        "audit": [*(row.get("audit") or []), audit_entry],
    }) or row
    for allocation in allocations:
        allocation_status = str(allocation.get("status") or "").upper()
        if allocation_status not in {"PAID", "CANCELLED"} and money(allocation.get("paid_amount")) <= 0 and not has_payout_link(allocation):
            store.update_one("incentive_allocations", {"_id": allocation.get("_id")}, {
                "status": "CANCELLED",
                "cancelled_at": now,
                "cancelled_by": actor.get("_id"),
                "cancelled_reason": reason,
            })
    audit("incentive.cancel", "incentive", incentive_id, {
        "order_id": row.get("order_id"),
        "customer_id": row.get("customer_id"),
        "reason": reason,
    })
    return success(updated, "Incentive cancelled")


@bp.post("/incentives/<incentive_id>/pay")
@login_required
def pay_incentive(incentive_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can mark incentives paid", status=403, error="superadmin_required")
    store = _store()
    incentive = store.find_one("incentives", {"_id": incentive_id})
    if not incentive:
        return failure("Incentive not found", status=404)
    amount = money((request.get_json(silent=True) or {}).get("paid_amount", incentive.get("net_payable_incentive", 0)))
    if amount < 0:
        return failure("Paid amount cannot be negative", status=422)
    row = store.update_one("incentives", {"_id": incentive_id}, {"paid_amount": amount, "paid_at": utcnow(), "paid_by_user_id": (current_user() or {}).get("_id")})
    row = recompute_incentive_totals(store, incentive_id) or row
    audit("incentive.pay", "incentive", incentive_id, {"paid_amount": amount})
    return success(row, "Incentive payment recorded")


@bp.post("/incentives/<incentive_id>/confirm-payment")
@login_required
def confirm_incentive_payment(incentive_id: str):
    """Activate an incentive for an already bank-confirmed payment.

    Customer payment confirmation belongs to ``/payments/<id>/confirm``. This
    compatibility endpoint cannot bypass that workflow.
    """
    if not _superadmin():
        return failure("Only a Superadmin can confirm payment", status=403, error="superadmin_required")
    store = _store()
    incentive = store.find_one("incentives", {"_id": incentive_id})
    if not incentive:
        return failure("Incentive not found", status=404)
    if incentive.get("payment_confirmation_date"):
        return success(recompute_incentive_totals(store, incentive_id) or incentive, "Payment already confirmed")
    payload = request.get_json(silent=True) or {}
    payment_id = str(payload.get("payment_id") or "").strip()
    payment = store.find_one("payments", {"_id": payment_id}) if payment_id else None
    if not payment or str(payment.get("order_id") or "") != str(incentive.get("order_id") or ""):
        return failure("Confirm the related customer payment first", status=409, error="payment_confirmation_required")
    if str(payment.get("status") or "").upper() != "CONFIRMED":
        return failure("Confirm the related customer payment first", status=409, error="payment_confirmation_required")
    confirmed_at = payment.get("confirmed_at") or utcnow()
    activated = activate_incentive(store, str(incentive.get("order_id")), confirmed_at)
    if not activated:
        return failure("Order Confirmation not found", status=404)
    row = store.update_one("incentives", {"_id": incentive_id}, {
        "payment_reference": str(payload.get("payment_reference") or payload.get("reference") or payment.get("utr") or payment.get("reference_number") or "").strip()[:160] or None,
        "payment_confirmed_by_user_id": (current_user() or {}).get("_id"),
        "payment_confirmed_at": confirmed_at,
    }) or activated
    row = recompute_incentive_totals(store, incentive_id) or row
    audit("incentive.activated", "incentive", incentive_id, {"order_id": incentive.get("order_id"), "payment_reference": row.get("payment_reference")})
    audit("incentive.payment_confirmed", "incentive", incentive_id, {"order_id": incentive.get("order_id")})
    return success(row, "Payment confirmed and incentive activated")


@bp.get("/credit-notes")
@permission_required_any("credit_notes.view", "credit_notes.manage")
def list_credit_notes():
    store = _store()
    user = current_user() or {}
    rows, total = store.list("credit_notes", {}, limit=min(int(request.args.get("limit", 100)), 500))
    if str(user.get("role_id") or "") != "superadmin":
        rows = [row for row in rows if (_order(str(row.get("order_id") or row.get("oc_id") or "")) and _can_access_order(_order(str(row.get("order_id") or row.get("oc_id") or "")), user))]
        total = len(rows)
    enriched = []
    for row in rows:
        order = _order(str(row.get("order_id") or row.get("oc_id") or "")) or {}
        customer = order.get("customer_snapshot") or order.get("customer_company_snapshot") or row.get("customer_snapshot") or {}
        enriched.append({
            **row,
            "order_number": order.get("order_number") or order.get("oc_number") or row.get("order_id") or row.get("oc_id"),
            "customer_name": customer.get("company_name") or customer.get("name") or order.get("customer_id"),
            "order_total": _invoice_amount(order),
            "currency": str(order.get("currency") or row.get("currency") or "EUR"),
        })
    return success({"items": enriched, "total": total})


@bp.post("/credit-notes")
@permission_required_any("credit_notes.create", "credit_notes.manage")
def create_credit_note():
    payload = request.get_json(silent=True) or {}
    order_id = str(payload.get("order_id") or payload.get("oc_id") or "").strip()
    order = _order(order_id)
    if not order:
        return failure("Order Confirmation not found", status=404)
    if not _can_access_order(order):
        return failure("Customer access denied", status=403)
    if order.get("financial_locked") and not _superadmin():
        return failure("Confirmed financial records are locked", status=423, error="financial_record_locked")
    amount = money(payload.get("amount", payload.get("credit_note_amount")))
    note_date = _parse_date(payload.get("credit_note_date"))
    if note_date is None or (amount <= 0 and not isinstance(payload.get("lines"), list)):
        return failure("Credit Note amount and date are required", status=422)
    incentive = _store().find_one("incentives", {"order_id": order_id})
    if not incentive:
        return failure("No incentive exists for this Order Confirmation", status=409)
    incentive_lines = incentive.get("incentive_lines") or []
    requested_lines = payload.get("lines")
    note_lines: list[dict] = []
    rate = None
    if requested_lines is not None:
        if not isinstance(requested_lines, list) or not requested_lines:
            return failure("Credit Note product lines are required", status=422, error="invalid_credit_note_lines")
        for requested in requested_lines:
            if not isinstance(requested, dict):
                return failure("Each Credit Note line must be an object", status=422, error="invalid_credit_note_lines")
            requested_line_id = str(requested.get("oc_line_id") or "").strip()
            product_id = str(requested.get("product_id") or "").strip()
            source = next((line for line in incentive_lines if requested_line_id and str(line.get("oc_line_id") or "") == requested_line_id), None)
            if source is None and product_id:
                source = next((line for line in incentive_lines if str(line.get("product_id") or "") == product_id), None)
            if not source:
                return failure("Credit Note line must reference a product on the Order Confirmation", status=422, error="credit_note_product_not_found")
            line_amount = money(requested.get("amount", requested.get("credit_note_amount")))
            if line_amount <= 0:
                return failure("Credit Note line amounts must be positive", status=422, error="invalid_credit_note_lines")
            line_rate = float(source.get("incentive_rate_snapshot") or 0)
            note_lines.append({
                "product_id": source.get("product_id"), "oc_line_id": source.get("oc_line_id"),
                "product_name": source.get("product_name"), "category_id": source.get("category_id"), "category_name": source.get("category_name"), "amount": line_amount,
                "incentive_rate_snapshot": line_rate, "incentive_deduction_amount": money(line_amount * line_rate / 100),
            })
    else:
        # Compatibility for older notes that were entered as one OC total.
        if len({float(line.get("incentive_rate_snapshot") or 0) for line in incentive_lines}) > 1:
            return failure("Product-level Credit Note lines are required for a multi-rate Order Confirmation", status=422, error="credit_note_lines_required")
        rate = float(incentive.get("incentive_percentage_snapshot") or 0)
        note_lines = [{"product_id": None, "oc_line_id": None, "product_name": "Order Confirmation", "category_id": None, "category_name": None, "amount": amount, "incentive_rate_snapshot": rate, "incentive_deduction_amount": money(amount * rate / 100)}]
    if requested_lines is not None:
        amount = money(sum(money(line.get("amount")) for line in note_lines))
    if rate is None:
        rates = {float(line.get("incentive_rate_snapshot") or 0) for line in note_lines}
        rate = next(iter(rates)) if len(rates) == 1 else None
    deduction = money(sum(money(line.get("incentive_deduction_amount")) for line in note_lines))
    now = utcnow()
    sequence = _store().next_counter("credit_note")
    row = _store().insert_one("credit_notes", {
        "credit_note_number": f"CN-{sequence:05d}", "order_id": order_id, "oc_id": order_id, "quotation_id": order.get("quotation_id"),
        "customer_id": order.get("customer_id"), "customer_snapshot": order.get("customer_snapshot") or order.get("customer_company_snapshot") or {},
        "amount": amount, "credit_note_amount": amount, "currency": "EUR", "credit_note_date": note_date,
        "reason": str(payload.get("reason") or "").strip()[:2000], "incentive_percentage_snapshot": rate,
        "incentive_deduction_amount": deduction, "lines": note_lines, "status": "APPROVED", "created_by_user_id": (current_user() or {}).get("_id"),
        "created_at": now, "audit": [{"action": "created", "by": (current_user() or {}).get("_id"), "at": now}],
    })
    for line in note_lines:
        adjustment = _store().insert_one("incentive_adjustments", {
            "credit_note_id": row["_id"], "incentive_id": incentive["_id"], "order_id": order_id,
            "product_id": line.get("product_id"), "oc_line_id": line.get("oc_line_id"),
            "category_id": line.get("category_id"), "category_name": line.get("category_name"),
            "amount": line.get("amount", 0), "incentive_rate_snapshot": line.get("incentive_rate_snapshot", 0),
            "deduction_amount": line.get("incentive_deduction_amount", 0), "created_at": now,
        })
        audit("incentive.adjustment_created", "incentive_adjustment", str(adjustment.get("_id")), {
            "credit_note_id": row["_id"], "incentive_id": incentive["_id"],
            "product_id": line.get("product_id"), "category_id": line.get("category_id"), "deduction_amount": line.get("incentive_deduction_amount", 0),
        })
    notes, _ = _store().list("credit_notes", {"order_id": order_id, "status": {"$ne": "VOID"}}, limit=10000)
    total_deduction = money(sum(money(item.get("incentive_deduction_amount")) for item in notes))
    _store().update_one("incentives", {"_id": incentive["_id"]}, {"credit_note_deduction": total_deduction})
    recompute_incentive_totals(_store(), str(incentive["_id"]))
    audit("credit_note.create", "credit_note", str(row["_id"]), {"order_id": order_id, "deduction": deduction})
    return success(row, "Credit Note created", 201)


@bp.post("/credit-notes/<credit_note_id>/void")
@login_required
def void_credit_note(credit_note_id: str):
    if not _superadmin():
        return failure("Only a Superadmin can void a Credit Note", status=403, error="superadmin_required")
    store = _store()
    note = store.find_one("credit_notes", {"_id": credit_note_id})
    if not note:
        return failure("Credit Note not found", status=404)
    if note.get("status") == "VOID":
        return success(note, "Credit Note already void")
    reason = str((request.get_json(silent=True) or {}).get("reason") or "").strip()
    if not reason:
        return failure("A reason is required", status=422, error="void_reason_required")
    row = store.update_one("credit_notes", {"_id": credit_note_id}, {"status": "VOID", "voided_by_user_id": (current_user() or {}).get("_id"), "voided_at": utcnow(), "void_reason": reason})
    incentive_id = None
    if note.get("order_id"):
        incentive = store.find_one("incentives", {"order_id": note.get("order_id")})
        if incentive:
            incentive_id = incentive.get("_id")
            notes, _ = store.list("credit_notes", {"order_id": note.get("order_id"), "status": {"$ne": "VOID"}}, limit=10_000)
            store.update_one("incentives", {"_id": incentive["_id"]}, {"credit_note_deduction": money(sum(money(item.get("incentive_deduction_amount")) for item in notes))})
            recompute_incentive_totals(store, str(incentive["_id"]))
    audit("credit_note.void", "credit_note", credit_note_id, {"reason": reason, "incentive_id": incentive_id})
    return success(row, "Credit Note voided")
