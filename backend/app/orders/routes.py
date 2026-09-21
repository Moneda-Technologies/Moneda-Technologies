from __future__ import annotations

import base64
from copy import deepcopy
from datetime import timedelta
from html import escape
import re
from threading import Lock
from uuid import uuid4

from email_validator import EmailNotValidError, validate_email
from flask import Blueprint, Response, current_app, request

from app.api.responses import failure, success
from app.communication.email import EmailDeliveryError, email_diagnostic_id
from app.customers.codes import available_customer_code
from app.finance.service import IncentiveConfigurationError, cancel_unpaid_incentives_for_order, create_incentive_for_order, has_payout_link, is_voided_or_deleted_payment, linked_records_for_order, money, payment_rollup, validate_incentive_configuration
from app.middleware.access import current_user, customer_access_ids_for_user, customer_record, enforce_customer, login_required, permission_required, superadmin_required
from app.repositories.store import utcnow
from app.services.audit import audit
from app.services.business_logic import customer_client_type, manager_snapshot
from app.quotations.pdf import render_order_confirmation_pdf
from app.pricing.engine import PricingUnavailable


bp = Blueprint("orders", __name__, url_prefix="/api")
STATUSES = {"Pending", "Confirmed", "Processing", "Completed", "Cancelled"}
_CONVERSION_LOCK = Lock()
PAYMENT_TERMS = ("Advance", "POD", "30 Days from receipt", "60 Days", "Custom")
WORKING_STATES = {"WORKING", "AWAITING_PAYMENT", "PAYMENT_RECORDED", "PAYMENT_CONFIRMED", "READY_FOR_DISPATCH", "READY_TO_FINALIZE", "CANCELLED"}


def _working_order_lines(order: dict) -> list[dict]:
    """Return editable working-order lines with stable item identifiers."""
    source = order.get("lines") or order.get("products_snapshot") or []
    lines: list[dict] = []
    for index, raw in enumerate(source if isinstance(source, list) else []):
        if not isinstance(raw, dict):
            continue
        line = deepcopy(raw)
        # Historical quotation/order rows predate item IDs. Use a stable,
        # document-local identity so the same source/current line compares as
        # the same product across requests without rewriting old snapshots.
        legacy_id = f"legacy-{index}-{line.get('product_id') or line.get('article_no') or 'item'}"
        line["item_id"] = str(line.get("item_id") or line.get("line_id") or line.get("_id") or legacy_id)
        lines.append(line)
    return lines


def _recalculate_working_order(store, order: dict, lines: list[dict]) -> dict:
    """Persist totals after an order line mutation using server pricing data."""
    from app.pricing.engine import calculate_quote_totals

    totals = calculate_quote_totals(lines)
    eur_lines = [
        {
            "subtotal": line.get("master_subtotal", line.get("subtotal", 0)),
            "discount_amount": line.get("master_discount_amount", line.get("discount_amount", 0)),
            "line_total": line.get("master_final_total", line.get("line_total", 0)),
        }
        for line in lines
    ]
    eur_totals = calculate_quote_totals(eur_lines)
    changes = {
        "lines": lines,
        "products_snapshot": lines,
        "totals": totals,
        "final_totals": totals,
        "eur_totals": eur_totals,
        "order_amount": totals.get("grand_total", 0),
        # Any line mutation invalidates a previously recorded materials gate.
        # Payment is then re-evaluated below, so the working Order can never
        # remain finalization-ready on stale totals.
        "materials_ready_for_dispatch": False,
        "materials_ready_at": None,
        "materials_ready_by": None,
        "lifecycle_state": "WORKING",
        "order_status": "WORKING",
    }
    updated = store.update_one("orders", {"_id": order.get("_id")}, changes) or {**order, **changes}
    from app.finance.service import sync_order_payment_state
    return sync_order_payment_state(store, str(order.get("_id"))) or updated


def _reprice_working_order(store, order: dict) -> dict:
    """Re-resolve every line from the current server catalogue and prices."""
    from app.pricing.routes import _calculate

    repriced: list[dict] = []
    for source in _working_order_lines(order):
        payload = {
            "product_id": source.get("product_id"),
            "configuration": source.get("configuration") or {},
            "quantity": source.get("requested_quantity", source.get("quantity", 1)),
            "discount_percent": source.get("requested_discount_percent", source.get("discount_percent", 0)),
            "display_currency": source.get("display_currency") or source.get("currency") or order.get("currency") or "EUR",
            "shipping_address_id": source.get("shipping_address_id") or order.get("shipping_address_id"),
        }
        _, _, line, rate_meta = _calculate(
            payload, customer_id_override=_order_customer_id(order), require_active_context=False,
        )
        item_id = str(source.get("item_id") or source.get("line_id") or uuid4().hex)
        repriced.append({**line, "item_id": item_id, "line_id": item_id, "exchange_rate_meta": rate_meta})
    if not repriced:
        raise ValueError("A working Order must contain at least one item")
    return _recalculate_totals_without_reset(store, order, repriced)


def _recalculate_totals_without_reset(store, order: dict, lines: list[dict]) -> dict:
    """Persist server totals during finalization without reopening a gate."""
    from app.pricing.engine import calculate_quote_totals

    totals = calculate_quote_totals(lines)
    eur_lines = [{
        "subtotal": line.get("master_subtotal", line.get("subtotal", 0)),
        "discount_amount": line.get("master_discount_amount", line.get("discount_amount", 0)),
        "line_total": line.get("master_final_total", line.get("line_total", 0)),
    } for line in lines]
    changes = {
        "lines": lines, "products_snapshot": lines, "totals": totals,
        "final_totals": totals, "eur_totals": calculate_quote_totals(eur_lines),
        "order_amount": totals.get("grand_total", 0),
    }
    return store.update_one("orders", {"_id": order.get("_id")}, changes) or {**order, **changes}


def _normalise_recipients(values) -> list[str]:
    """Return valid, lower-cased, de-duplicated addresses from mixed input."""
    if values is None:
        return []
    raw_values = values if isinstance(values, (list, tuple, set)) else [values]
    result: list[str] = []
    for raw in raw_values:
        if isinstance(raw, (list, tuple, set)):
            for nested in _normalise_recipients(raw):
                if nested not in result:
                    result.append(nested)
            continue
        for value in str(raw or "").replace(";", ",").split(","):
            value = value.strip()
            if not value:
                continue
            try:
                address = validate_email(value, check_deliverability=False).normalized
            except EmailNotValidError:
                continue
            if address not in result:
                result.append(address)
    return result


def _order_customer_id(order: dict) -> str:
    return str(order.get("customer_id") or order.get("customer_company_id") or order.get("company_id") or "")


def _customer_access(actor: dict, customer_id: str) -> bool:
    if not customer_id:
        return False
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    if str(actor.get("role_id") or "") != "admin":
        return enforce_customer(customer_id)
    customer = customer_record(customer_id) or {}
    actor_id = str(actor.get("_id") or "")
    return actor_id in {str(value) for value in (customer.get("assigned_user_ids") or []) if value} or str(customer.get("created_by_user_id") or "") == actor_id


def _can_access_order_record(order: dict, user: dict | None = None) -> bool:
    """Authorize an OC by global scope, explicit owner, or customer scope."""
    actor = user or current_user() or {}
    if str(actor.get("role_id") or "") == "superadmin":
        return True
    actor_id = str(actor.get("_id") or "")
    owner_ids = {
        str(order.get(field) or "")
        for field in ("salesperson_id", "prepared_by_user_id", "created_by_user_id", "user_id")
        if order.get(field)
    }
    snapshot = order.get("salesperson_snapshot")
    if isinstance(snapshot, dict) and snapshot.get("_id"):
        owner_ids.add(str(snapshot["_id"]))
    if actor_id in owner_ids and str(actor.get("role_id") or "") in {"user", "manager_sales_admin"}:
        return bool(_order_customer_id(order))
    return _customer_access(actor, _order_customer_id(order))


def _quotation_recipients(store, quotation: dict) -> tuple[list[str], list[str], list[str]]:
    """Load recipient snapshots from the quotation and its send record.

    Older quotations do not have recipient fields; their latest quotation
    email log is the authoritative snapshot for CC/BCC in that case.
    """
    quotation_id = quotation.get("_id")
    customer_email = (quotation.get("customer_snapshot") or {}).get("email")
    to = _normalise_recipients([quotation.get("to"), quotation.get("recipient"), customer_email])
    cc = _normalise_recipients(quotation.get("cc"))
    bcc = _normalise_recipients(quotation.get("bcc"))
    if quotation_id:
        logs, _ = store.list("email_logs", {"quotation_id": quotation_id, "purpose": "quotation"}, limit=100, sort="created_at", direction=-1)
        if logs:
            latest = logs[0]
            to = _normalise_recipients([*to, latest.get("recipient"), latest.get("to")])
            cc = _normalise_recipients([*cc, latest.get("cc")])
            bcc = _normalise_recipients([*bcc, latest.get("bcc")])
    # An address must have one delivery role only; To takes precedence.
    to_set = set(to)
    cc = [value for value in cc if value not in to_set]
    cc_set = set(cc)
    bcc = [value for value in bcc if value not in to_set and value not in cc_set]
    return to, cc, bcc


def _additional_recipients(payload: dict) -> list[str]:
    values = payload.get("additional_recipients", [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ValueError("Additional recipients must be an array")
    if len(values) > 5:
        raise ValueError("A maximum of 5 additional recipients is allowed")
    result: list[str] = []
    for value in values:
        address = _normalise_recipients(value)
        if not address:
            raise ValueError("Every additional recipient must be a valid email address")
        result.extend(address)
    return list(dict.fromkeys(result))[:5]


def _validated_payment_terms(value, fallback: str | None) -> str:
    terms = str(value or fallback or "Advance").strip()
    if terms in PAYMENT_TERMS:
        return terms
    if re.fullmatch(r"Custom:\s*\d+\s+Days", terms):
        return terms
    raise ValueError("Invalid payment terms")


def _order_recipient(order: dict) -> str:
    for snapshot_name in ("customer_snapshot", "customer_company_snapshot", "company_snapshot"):
        value = (order.get(snapshot_name) or {}).get("email")
        if value:
            try:
                return validate_email(str(value).strip(), check_deliverability=False).normalized
            except EmailNotValidError:
                continue
    return ""


def _order_recipients(order: dict) -> list[str]:
    values: list[str] = _normalise_recipients([
        order.get("to"), order.get("additional_recipients"), order.get("cc"), order.get("bcc"),
    ])
    for snapshot_name in ("customer_snapshot", "customer_company_snapshot", "company_snapshot"):
        value = (order.get(snapshot_name) or {}).get("email")
        if value:
            values.extend(_normalise_recipients(value))
    sales_email = (order.get("salesperson_snapshot") or {}).get("email")
    if sales_email:
        values.extend(_normalise_recipients(sales_email))
    return list(dict.fromkeys(values))


def _order_to_recipients(order: dict) -> list[str]:
    explicit = _normalise_recipients(order.get("to"))
    if not explicit:
        explicit = _normalise_recipients([
            (order.get(snapshot_name) or {}).get("email")
            for snapshot_name in ("customer_snapshot", "customer_company_snapshot", "company_snapshot")
        ])
    explicit.extend(_normalise_recipients(order.get("additional_recipients")))
    # The assigned Sales Person / Manager is always a direct recipient of the
    # OC, alongside the customer.  CC/BCC remain independently configurable.
    explicit.extend(_normalise_recipients((order.get("salesperson_snapshot") or {}).get("email")))
    return list(dict.fromkeys(explicit))


def _can_convert_quotation(user: dict, quotation: dict) -> bool:
    """Authorize conversion without broadening a user's order permissions.

    Users with the existing orders.create permission keep the normal managed
    conversion path.  A user without that global permission may convert only
    a quotation they own, and only when the customer is in their server-side
    customer scope.
    """
    customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    if not enforce_customer(customer_id):
        return False
    if "orders.create" in user.get("permissions", []):
        return True
    user_id = str(user.get("_id") or "")
    owner_ids = {
        str(quotation.get(field) or "")
        for field in ("created_by_user_id", "user_id", "prepared_by_user_id", "salesperson_id")
        if quotation.get(field)
    }
    return bool(user_id and user_id in owner_ids)


def _working_order_for_quotation(store, quotation_id: str) -> dict | None:
    """Return the one current working Order linked to a quotation.

    Historical deployments stored final MT-OC records in ``orders`` with a
    ``quotation_id``.  Those records are deliberately not treated as working
    Orders and must never be reopened or returned as an idempotent conversion.
    """
    records, _ = store.list(
        "orders",
        {"quotation_id": quotation_id, "status": {"$ne": "Deleted"}},
        limit=100,
        sort="created_at",
        direction=-1,
    )
    return next((record for record in records if not _is_final_confirmation_record(record)), None)


def _link_quotation_to_working_order(store, quotation: dict, order: dict) -> dict | None:
    """Persist the forward conversion link only after the Order exists."""
    persisted = store.find_one("orders", {"_id": order.get("_id"), "quotation_id": quotation.get("_id")})
    if not persisted or _is_final_confirmation_record(persisted):
        return None
    now = utcnow()
    history = list(quotation.get("history") or [])
    already_linked = str(quotation.get("converted_order_id") or "") == str(persisted.get("_id") or "")
    if not already_linked:
        history.append({
            "status": "Converted to Order", "at": now,
            "by": (current_user() or {}).get("_id"),
            "order_id": persisted.get("_id"),
            "order_number": persisted.get("order_number"),
        })
    return store.update_one("quotations", {"_id": quotation.get("_id")}, {
        "status": "Converted to Order",
        "converted_order_id": persisted.get("_id"),
        "converted_order_number": persisted.get("order_number"),
        "converted_oc_id": None,
        "converted_oc_number": None,
        "converted_at": quotation.get("converted_at") or now,
        "source_status_before_conversion": quotation.get("status") if str(quotation.get("status") or "").casefold() != "converted to order" else quotation.get("source_status_before_conversion", "Sent"),
        "history": history,
    })


def _customer_oc_code(store, customer_id: str) -> str:
    customer = store.find_one("customers", {"_id": customer_id}) or store.find_one("companies", {"_id": customer_id}) or {}
    code = str(customer.get("customer_code") or "").strip().upper()
    if not code:
        code = available_customer_code(store, str(customer.get("name") or customer.get("company_name") or "Customer"), customer_id)
        if store.find_one("customers", {"_id": customer_id}):
            store.update_one("customers", {"_id": customer_id}, {"customer_code": code})
    return re.sub(r"[^A-Z0-9-]", "-", code).strip("-") or "CUSTOMER"


def _oc_sequence_state(store, code: str) -> tuple[str, int]:
    pattern = rf"^MT-OC-{re.escape(code)}-(\d+)$"
    # Final confirmations live in their own collection, while older records
    # may still be in ``orders``.  Scan both so a migration or a restart can
    # never allocate an OC number that already exists in the final archive.
    rows = []
    for collection in ("orders", "order_confirmations"):
        collection_rows, _ = store.list(collection, {"order_number": {"$regex": pattern}}, limit=100_000)
        rows.extend(collection_rows)
    highest_existing = max((int(match.group(1)) for row in rows if (match := re.match(pattern, str(row.get("order_number") or "")))), default=0)
    counter_name = f"order_oc:{code}"
    counter = store.find_one("quotation_counters", {"_id": counter_name}) or {}
    return counter_name, max(highest_existing, int(counter.get("sequence") or 0))


def _order_sequence_state(store, code: str) -> tuple[str, int]:
    pattern = rf"^MT-ORD-{re.escape(code)}-(\d+)$"
    rows, _ = store.list("orders", {"order_number": {"$regex": pattern}}, limit=100_000)
    confirmations, _ = store.list("order_confirmations", {"source_order_number": {"$regex": pattern}}, limit=100_000)
    rows.extend(confirmations)
    highest_existing = max(
        (int(match.group(1)) for row in rows if (match := re.match(pattern, str(row.get("order_number") or row.get("source_order_number") or "")))),
        default=0,
    )
    counter_name = f"order:{code}"
    counter = store.find_one("quotation_counters", {"_id": counter_name}) or {}
    return counter_name, max(highest_existing, int(counter.get("sequence") or 0))


def _next_order_number(store, customer_id: str) -> str:
    code = _customer_oc_code(store, customer_id)
    counter_name, current = _order_sequence_state(store, code)
    store.ensure_counter_at_least(counter_name, current)
    return f"MT-ORD-{code}-{store.next_counter(counter_name):03d}"


def _preview_order_number(store, customer_id: str) -> str:
    code = _customer_oc_code(store, customer_id)
    _, current = _order_sequence_state(store, code)
    return f"MT-ORD-{code}-{current + 1:03d}"


def _next_oc_number(store, customer_id: str) -> str:
    code = _customer_oc_code(store, customer_id)
    counter_name, current = _oc_sequence_state(store, code)
    store.ensure_counter_at_least(counter_name, current)
    return f"MT-OC-{code}-{store.next_counter(counter_name):03d}"


def _preview_oc_number(store, customer_id: str) -> str:
    code = _customer_oc_code(store, customer_id)
    _, current = _oc_sequence_state(store, code)
    return f"MT-OC-{code}-{current + 1:03d}"


def _order_email_failure(exc: EmailDeliveryError):
    message = (
        "Email service is not connected. Please contact the administrator."
        if exc.error_code == "OAUTH_NOT_CONNECTED" else "Order email could not be delivered."
    )
    if exc.error_code == "ORDER_SENDER_ALIAS_UNAVAILABLE":
        message = "Order email is not ready because its Zoho sender alias is unavailable."
    status = 429 if exc.error_code == "ZOHO_MAIL_API_RATE_LIMIT" else 422 if exc.error_code == "SENDER_INVALID" else 503
    return failure(
        message, status=status, error=exc.error_code,
        diagnostic_id=exc.diagnostic_id, stage=exc.stage,
    )


def _order_pdf_id(order_id: str) -> str:
    return f"order-confirmation-pdf-{order_id}"


def _record_collection(record: dict) -> str:
    """Return the authoritative collection for a working order or final OC."""
    storage_collection = record.get("_storage_collection")
    if storage_collection in {"orders", "order_confirmations"}:
        return storage_collection
    return "order_confirmations" if _is_final_confirmation_record(record) else "orders"


def _is_final_confirmation_record(record: dict) -> bool:
    """Recognize final OCs, including pre-lifecycle legacy records.

    New records always carry explicit lifecycle metadata.  Older deployments
    stored final confirmations in ``orders`` with only an MT-OC number, so a
    metadata-free legacy row is treated as final for authorization/listing
    purposes.  Working orders created by the current workflow always include
    ``record_type=ORDER`` and ``order_kind=WORKING`` and therefore remain
    editable.
    """
    record_type = str(record.get("record_type") or "").upper()
    order_kind = str(record.get("order_kind") or "").upper()
    # Explicit entity identity is authoritative. A working Order remains the
    # source record after finalization and must never be reclassified as its OC.
    if record_type == "ORDER" or order_kind == "WORKING":
        return False
    if record_type == "ORDER_CONFIRMATION":
        return True
    if bool(record.get("finalized")):
        return True
    if str(record.get("document_type") or "").casefold() == "order_confirmation":
        return True
    if str(record.get("lifecycle_state") or "").upper() in {"FINAL", "FINALIZED"}:
        return True
    metadata_fields = ("record_type", "order_kind", "lifecycle_state", "document_type")
    return (
        not any(record.get(field) for field in metadata_fields)
        and str(record.get("order_number") or "").upper().startswith("MT-OC-")
    )


def _is_order_locked(record: dict) -> bool:
    """Keep finalized source Orders immutable without reclassifying them as OCs."""
    return (
        _is_final_confirmation_record(record)
        or bool(record.get("locked"))
        or bool(record.get("finalized"))
        or bool(record.get("final_oc_id"))
        or str(record.get("lifecycle_state") or "").upper() in {"FINAL", "FINALIZED"}
    )


def _find_order_record(store, record_id: str) -> dict | None:
    """Read either a working Order or its immutable Order Confirmation."""
    order = store.find_one("orders", {"_id": record_id})
    if order:
        order["_storage_collection"] = "orders"
        return order
    confirmation = store.find_one("order_confirmations", {"_id": record_id})
    if confirmation:
        confirmation["_storage_collection"] = "order_confirmations"
    return confirmation


def _payment_rows_for_record(store, record_id: str) -> list[dict]:
    record = _find_order_record(store, record_id) or {}
    ids = {str(record_id)}
    source_order_id = str(record.get("source_order_id") or "")
    if source_order_id:
        ids.add(source_order_id)
    rows, _ = store.list(
        "payments",
        {"$or": [{"order_id": value} for value in ids] + [{"oc_id": value} for value in ids]},
        limit=100_000,
    )
    return rows


def _order_scope_query(user: dict, customer_id: str | None = None) -> dict:
    """Build the shared customer-scope query for working Orders and final OCs."""
    not_deleted = {"status": {"$ne": "Deleted"}}
    if customer_id:
        if str(user.get("role_id") or "") != "superadmin" and not _customer_access(user, str(customer_id)):
            raise PermissionError("Customer access denied")
        return {**not_deleted, "$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]}
    if str(user.get("role_id") or "") == "superadmin":
        return not_deleted
    customer_ids = customer_access_ids_for_user(str(user.get("_id") or ""))
    return ({**not_deleted, "$or": [{"customer_id": {"$in": customer_ids}}, {"customer_company_id": {"$in": customer_ids}}, {"company_id": {"$in": customer_ids}}]} if customer_ids else {"_id": "__no_customer_access__"})


def _enrich_order_rows(store, rows: list[dict]) -> list[dict]:
    """Attach payment/incentive summaries without exposing payment proof data."""
    enriched = []
    for row in rows:
        payments = _payment_rows_for_record(store, str(row.get("_id") or ""))
        latest_payment = payments[-1] if payments else None
        rollup = payment_rollup(row, payments)
        if latest_payment:
            row["payment_snapshot"] = {key: value for key, value in latest_payment.items() if key != "attachment"}
        row.update({
            "payment_status": rollup["payment_status"],
            "confirmed_received": rollup["confirmed_received"],
            "remaining_balance": rollup["remaining_balance"],
            "customer_credit": rollup["customer_credit"],
            "pending_payment_count": rollup["pending_payment_count"],
        })
        incentive = store.find_one("incentives", {"order_id": row.get("_id")})
        if not incentive and row.get("source_order_id"):
            incentive = store.find_one("incentives", {"order_id": row.get("source_order_id")})
        if incentive:
            row["incentive_id"] = incentive.get("_id")
            row["incentive_status"] = incentive.get("status", "PENDING PAYMENT")
            row["incentive_amount"] = incentive.get("gross_incentive_amount", row.get("incentive_amount", 0))
        enriched.append(row)
    return enriched


def _confirmation_origin(store, confirmation: dict) -> dict:
    """Describe whether a final OC came from the current working-order flow."""
    source_order_id = str(confirmation.get("source_order_id") or "").strip()
    source_order = store.find_one("orders", {"_id": source_order_id}) if source_order_id else None
    linked = bool(source_order_id and source_order)
    return {
        "confirmation_type": "LINKED_FINAL_OC" if linked else "HISTORICAL_OC",
        "is_legacy_confirmation": not linked,
        "source_order_id": source_order_id or None,
        "source_order_number": (
            confirmation.get("source_order_number")
            or (source_order or {}).get("order_number")
            or None
        ),
        "confirmation_origin_explanation": (
            "Final Order Confirmation created from a linked working order."
            if linked
            else "Historical Order Confirmation created before the current Working Order to Final OC workflow, or without a source working-order link."
        ),
    }


def _ensure_order_pdf(order: dict) -> tuple[bytes, dict]:
    """Generate and persist the canonical OC PDF, reusing an existing copy."""
    store = current_app.extensions["store"]
    order_id = str(order.get("_id") or "")
    document_id = str(order.get("oc_pdf_document_id") or _order_pdf_id(order_id))
    existing = store.find_one("order_documents", {"_id": document_id})
    if existing and existing.get("content_base64"):
        try:
            content = base64.b64decode(str(existing["content_base64"]))
            if order.get("oc_pdf_status") != "Generated" or order.get("oc_pdf_document_id") != document_id:
                order = store.update_one(_record_collection(order), {"_id": order_id}, {
                    "oc_pdf_document_id": document_id, "oc_pdf_filename": existing.get("filename") or f"{order.get('order_number') or order_id}.pdf",
                    "oc_pdf_status": "Generated", "document_status": "Generated",
                }) or order
            return content, order
        except (ValueError, TypeError):
            pass
    content = render_order_confirmation_pdf(order)
    now = utcnow()
    filename = f"{order.get('order_number') or order_id}.pdf"
    document = {
        "_id": document_id, "order_id": order_id, "document_type": "order_confirmation",
        "filename": filename, "content_type": "application/pdf",
        "content_base64": base64.b64encode(content).decode("ascii"), "created_at": now,
        "updated_at": now,
    }
    if existing:
        document = store.update_one("order_documents", {"_id": document_id}, document) or document
    else:
        document = store.insert_one("order_documents", document)
    updated = store.update_one(_record_collection(order), {"_id": order_id}, {
        "oc_pdf_document_id": document_id, "oc_pdf_filename": filename,
        "oc_pdf_status": "Generated", "document_status": "Generated", "oc_pdf_generated_at": now,
    }) or order
    audit("order.pdf_generated", "order", order_id, {"document_id": document_id})
    return content, updated


def _send_order_email(order: dict, message_type: str, attachment: bytes | None = None) -> dict:
    recipients = _order_to_recipients(order)
    if not recipients:
        raise ValueError("CUSTOMER_EMAIL_REQUIRED")
    cc = _normalise_recipients(order.get("cc"))
    bcc = _normalise_recipients(order.get("bcc"))
    number = escape(str(order.get("order_number") or order.get("_id") or "order"))
    status = escape(str(order.get("status") or "Pending"))
    if message_type == "order_confirmation":
        subject = f"Order confirmation {order.get('order_number')} - Moneda Technologies"
        html = (
            "<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
            f"<p>Your Order Confirmation <strong>{number}</strong> has been received.</p>"
            f"<p>Current status: <strong>{status}</strong>.</p></div>"
        )
        method = current_app.extensions["email_service"].send_order_confirmation
    else:
        subject = f"Order status {order.get('order_number')} - {order.get('status')}"
        html = (
            "<div style='font-family:Arial,sans-serif'><h2>Moneda Technologies</h2>"
            f"<p>Order <strong>{number}</strong> is now <strong>{status}</strong>.</p></div>"
        )
        method = current_app.extensions["email_service"].send_order_status
    result = method(
        to=recipients, subject=subject, html=html,
        attachments=([{"filename": str(order.get("oc_pdf_filename") or f"{order.get('order_number') or 'order-confirmation'}.pdf"), "content": base64.b64encode(attachment).decode("ascii")}]
                     if attachment is not None and message_type == "order_confirmation" else None),
        cc=cc, bcc=bcc,
        request_id=f"{message_type}-{order.get('_id')}",
    )
    current_app.extensions["store"].insert_one("email_logs", {
        "order_id": order.get("_id"), "recipient": recipients, "subject": subject,
        "message_type": message_type, "sent_by": (current_user() or {}).get("_id"),
        "status": "sent", "provider_id": result.get("id"), "cc": result.get("cc", []), "bcc": result.get("bcc", []),
        "diagnostic_id": result.get("diagnostic_id"), "stage": result.get("stage", "message_submission"),
        "attachments": [str(order.get("oc_pdf_filename"))] if attachment is not None else [],
        "channel": "email", "created_at": utcnow(),
    })
    return result


def _log_order_email_failure(order: dict, message_type: str, exc: EmailDeliveryError) -> None:
    current_app.extensions["store"].insert_one("email_logs", {
        "order_id": order.get("_id"), "recipient": _order_recipient(order),
        "message_type": message_type, "sent_by": (current_user() or {}).get("_id"),
        "status": "failed", "error_code": exc.error_code,
        "diagnostic_id": exc.diagnostic_id, "stage": exc.stage,
        "channel": "email", "created_at": utcnow(),
    })


def _mark_order_email_failed(order: dict, exc: Exception) -> None:
    error_code = str(getattr(exc, "error_code", "MESSAGE_SUBMISSION_FAILED"))
    details = {
        "email_status": "Failed", "email_error": error_code,
        "email_failed_at": utcnow(),
    }
    current_app.extensions["store"].update_one(_record_collection(order), {"_id": order.get("_id")}, details)
    if isinstance(exc, EmailDeliveryError):
        _log_order_email_failure(order, "order_confirmation", exc)
        audit("order.email_failed", "order", str(order.get("_id")), {"error_code": exc.error_code, "diagnostic_id": exc.diagnostic_id})
    else:
        current_app.extensions["store"].insert_one("email_logs", {
            "order_id": order.get("_id"), "recipient": _order_recipient(order),
            "message_type": "order_confirmation", "status": "failed",
            "error_code": error_code, "channel": "email", "created_at": utcnow(),
        })
        audit("order.email_failed", "order", str(order.get("_id")), {"error_code": "MESSAGE_SUBMISSION_FAILED"})


@bp.get("/orders")
@permission_required("orders.view")
def list_orders():
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    store = current_app.extensions["store"]
    user = current_user() or {}
    try:
        query = _order_scope_query(user, customer_id)
    except PermissionError:
        return failure("Customer access denied", status=403)
    if request.args.get("status"):
        query["status"] = request.args["status"]
    all_rows, _ = store.list("orders", query, limit=100_000)
    # New records in this collection are working Orders. Legacy rows without
    # lifecycle metadata are retained here for backwards-compatible access,
    # while finalized records are exposed through /order-confirmations.
    rows = [
        row for row in all_rows
        if not _is_final_confirmation_record(row)
        and not row.get("final_oc_id")
        and not row.get("finalized")
        and str(row.get("lifecycle_state") or "").upper() != "FINALIZED"
    ]
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(int(request.args.get("limit", 25)), 100)
    start = (page - 1) * limit
    return success({"items": _enrich_order_rows(store, rows[start:start + limit]), "total": len(rows)})


@bp.get("/order-confirmations")
@permission_required("orders.view")
def list_order_confirmations():
    """List immutable final OCs, while retaining visibility of legacy rows."""
    customer_id = request.args.get("customer_id") or request.args.get("customer_company_id") or request.args.get("company_id")
    store = current_app.extensions["store"]
    try:
        query = _order_scope_query(current_user() or {}, customer_id)
    except PermissionError:
        return failure("Customer access denied", status=403)
    if request.args.get("status"):
        query["status"] = request.args["status"]
    final_rows, _ = store.list("order_confirmations", query, limit=100_000)
    legacy_rows, _ = store.list("orders", query, limit=100_000)
    legacy_rows = [
        row for row in legacy_rows
        if _is_final_confirmation_record(row)
        and not (str(row.get("record_type") or "").upper() == "ORDER" and row.get("final_oc_id"))
    ]
    rows_by_id = {str(row.get("_id")): row for row in [*legacy_rows, *final_rows] if row.get("_id")}
    rows = sorted(rows_by_id.values(), key=lambda row: str(row.get("created_at") or ""), reverse=True)
    rows = [{**row, **_confirmation_origin(store, row)} for row in rows]
    page = max(int(request.args.get("page", 1)), 1)
    limit = min(int(request.args.get("limit", 25)), 100)
    start = (page - 1) * limit
    return success({"items": _enrich_order_rows(store, rows[start:start + limit]), "total": len(rows)})


@bp.get("/orders/<order_id>")
@permission_required("orders.view")
def get_order(order_id: str):
    store = current_app.extensions["store"]
    order = _find_order_record(store, order_id)
    if not order or order.get("status") == "Deleted":
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    payments = _payment_rows_for_record(store, order_id)
    rollup = payment_rollup(order, payments)
    order.update({
        "payment_status": rollup["payment_status"],
        "confirmed_received": rollup["confirmed_received"],
        "remaining_balance": rollup["remaining_balance"],
        "customer_credit": rollup["customer_credit"],
        "pending_payment_count": rollup["pending_payment_count"],
    })
    if _is_final_confirmation_record(order):
        order.update(_confirmation_origin(store, order))
    elif order.get("quotation_snapshot"):
        source_lines = _working_order_lines({"lines": (order.get("quotation_snapshot") or {}).get("lines") or []})
        current_lines = _working_order_lines(order)
        changes = []
        matched_source: set[int] = set()
        def comparison_key(line: dict) -> tuple:
            return str(line.get("product_id") or line.get("article_no") or ""), repr(sorted((line.get("configuration") or {}).items()))
        for line in current_lines:
            item_id = str(line.get("item_id"))
            source_index = next((index for index, source in enumerate(source_lines) if index not in matched_source and str(source.get("item_id")) == item_id), None)
            if source_index is None:
                source_index = next((index for index, source in enumerate(source_lines) if index not in matched_source and comparison_key(source) == comparison_key(line)), None)
            source = source_lines[source_index] if source_index is not None else None
            if not source:
                changes.append({"type": "ITEM_ADDED", "item_id": item_id, "product_id": line.get("product_id")})
                continue
            matched_source.add(source_index)
            source_quantity = int(source.get("requested_quantity", source.get("quantity", 1)) or 1)
            current_quantity = int(line.get("requested_quantity", line.get("quantity", 1)) or 1)
            if current_quantity != source_quantity:
                changes.append({"type": "QUANTITY_CHANGED", "item_id": item_id, "product_id": line.get("product_id"), "from": source_quantity, "to": current_quantity})
            source_discount = float(source.get("requested_discount_percent", source.get("discount_percent", 0)) or 0)
            current_discount = float(line.get("requested_discount_percent", line.get("discount_percent", 0)) or 0)
            if current_discount != source_discount:
                changes.append({"type": "DISCOUNT_CHANGED", "item_id": item_id, "product_id": line.get("product_id"), "from": source_discount, "to": current_discount})
        for index, line in enumerate(source_lines):
            if index not in matched_source:
                changes.append({"type": "ITEM_REMOVED", "item_id": str(line.get("item_id")), "product_id": line.get("product_id")})
        order["changes_from_quotation"] = changes
    # Internal storage routing is only needed while persisting legacy rows;
    # never expose it as part of the public order contract.
    order.pop("_storage_collection", None)
    return success(order)


@bp.delete("/orders/<order_id>")
@superadmin_required
def delete_order(order_id: str):
    """Safely archive a working Order or OC without destroying history."""
    store = current_app.extensions["store"]
    order = _find_order_record(store, order_id)
    if not order or order.get("status") == "Deleted":
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    is_final_confirmation = _is_final_confirmation_record(order)
    if not is_final_confirmation:
        linked_confirmation = (
            store.find_one("order_confirmations", {"source_order_id": order_id})
            or (store.find_one("order_confirmations", {"_id": order.get("final_oc_id")}) if order.get("final_oc_id") else None)
        )
        if linked_confirmation:
            return failure(
                "This working Order has a finalized Order Confirmation and cannot be deleted.",
                status=409,
                error="ORDER_HAS_FINAL_CONFIRMATION",
                details={"order_confirmation_id": str(linked_confirmation.get("_id") or "")},
            )
    linked_payments, _ = store.list("payments", {"$or": [{"order_id": order_id}, {"oc_id": order_id}]}, limit=100_000)
    active_payments = [payment for payment in linked_payments if not is_voided_or_deleted_payment(payment)]
    if active_payments:
        return failure(
            "This Order Confirmation cannot be deleted while an active payment is linked to it. Void the payment first.",
            status=409, error="ORDER_HAS_ACTIVE_PAYMENT",
            details={"payment_ids": [str(payment.get("_id")) for payment in active_payments]},
        )
    linked_incentives = linked_records_for_order(store, "incentives", order_id)
    paid_incentives = [row for row in linked_incentives if str(row.get("status") or "").upper() == "PAID" or money(row.get("paid_amount")) > 0 or has_payout_link(row)]
    linked_allocations = linked_records_for_order(store, "incentive_allocations", order_id)
    paid_allocations = [row for row in linked_allocations if str(row.get("status") or "").upper() == "PAID" or money(row.get("paid_amount")) > 0 or has_payout_link(row)]
    if paid_incentives or paid_allocations:
        return failure("This Order Confirmation has a paid incentive and cannot be deleted.", status=409, error="ORDER_HAS_PAID_INCENTIVE")
    linked_credit_notes, _ = store.list(
        "credit_notes",
        {"$or": [{"order_id": order_id}, {"oc_id": order_id}], "status": {"$ne": "VOID"}},
        limit=100_000,
    )
    if linked_credit_notes:
        return failure(
            "This record has an active Credit Note and cannot be deleted.",
            status=409,
            error="ORDER_HAS_CREDIT_NOTE",
            details={"credit_note_ids": [str(row.get("_id") or "") for row in linked_credit_notes]},
        )
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or request.args.get("reason") or "Deleted from Order Confirmations")[:500].strip()
    now = utcnow()
    linked_counts = {}
    for collection, query in (
        ("payments", {"$or": [{"order_id": order_id}, {"oc_id": order_id}]}),
        ("incentives", {"$or": [{"order_id": order_id}, {"oc_id": order_id}]}),
        ("incentive_allocations", {"$or": [{"order_id": order_id}, {"oc_id": order_id}]}),
        ("order_documents", {"order_id": order_id}),
    ):
        linked_counts[collection] = store.list(collection, query, limit=100_000)[1]
    now = utcnow()
    actor_id = (current_user() or {}).get("_id")
    incentive_result = cancel_unpaid_incentives_for_order(store, order_id, actor_id=actor_id, reason=reason)
    source_quotation_id = str(order.get("quotation_id") or order.get("source_quotation_id") or "") or None
    unset_deleted_relationship = []
    if not is_final_confirmation:
        # The source quotation is retained in the audit event below. Clearing
        # the active relationship on the archived document prevents the
        # unique quotation link from blocking a later, valid reconversion.
        unset_deleted_relationship = [
            field for field in ("quotation_id", "source_quotation_id", "quote_id") if order.get(field)
        ]
    updated = store.update_one(_record_collection(order), {"_id": order_id}, {
        "status": "Deleted",
        "document_status": "Deleted",
        "deleted_at": now,
        "deleted_by": (current_user() or {}).get("_id"),
        "deletion_reason": reason,
        "incentive_cancellation": incentive_result,
    }, unset_fields=unset_deleted_relationship) or {**order, "status": "Deleted"}
    audit("ORDER_CONFIRMATION_DELETED" if is_final_confirmation else "WORKING_ORDER_DELETED", "order", order_id, {
        "reason": reason,
        "previous_state": order.get("status", "Pending"),
        "quotation_id": source_quotation_id,
        "linked_records": linked_counts, "incentives": incentive_result,
    })
    # Deleting a working Order releases its source quotation. Final/legacy OCs
    # remain historical and never trigger quotation restoration.
    quotation_id = source_quotation_id or ""
    quotation_restored = False
    if quotation_id and not is_final_confirmation:
        quotation = store.find_one("quotations", {"_id": quotation_id})
        linked_order_id = str(quotation.get("converted_order_id") or "") if quotation else ""
        if quotation and linked_order_id == order_id and str(quotation.get("status") or "").casefold() == "converted to order":
            restored_status = str(order.get("source_status_before_conversion") or quotation.get("source_status_before_conversion") or "Sent")
            if restored_status.casefold() in {"converted to order", "converted", "converted_to_order", ""}:
                restored_status = "Sent"
            quotation_history = [*(quotation.get("history") or []), {"status": restored_status, "at": now, "by": actor_id, "reason": "Working Order deleted", "order_id": order_id}]
            relationship_reset = {
                "status": restored_status, "history": quotation_history,
                "converted_order_id": None, "converted_order_number": None,
                "converted_oc_id": None, "converted_oc_number": None,
                "converted_at": None,
            }
            # These legacy aliases are only cleared when they point to the
            # order being deleted; unrelated quotation metadata is preserved.
            if str(quotation.get("order_id") or "") == order_id:
                relationship_reset["order_id"] = None
            if str(quotation.get("oc_id") or "") == order_id:
                relationship_reset["oc_id"] = None
            store.update_one("quotations", {"_id": quotation_id}, relationship_reset)
            quotation_restored = True
            audit("quotation.restored_after_order_delete", "quotation", quotation_id, {"order_id": order_id, "status": restored_status})
    record_label = "Order Confirmation" if is_final_confirmation else "Working Order"
    return success({"_id": updated.get("_id"), "status": "Deleted", "record_type": "order_confirmation" if is_final_confirmation else "working_order", "incentives": incentive_result, "quotation_restored": quotation_restored}, f"{record_label} archived")


@bp.post("/orders/<order_id>/send-confirmation")
@permission_required("orders.update")
def send_order_confirmation(order_id: str):
    store = current_app.extensions["store"]
    order = _find_order_record(store, order_id)
    if not order:
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    if not _is_final_confirmation_record(order):
        return failure("A final Order Confirmation PDF is created only after Superadmin finalization", status=409, error="ORDER_CONFIRMATION_NOT_FINAL")
    try:
        pdf, order = _ensure_order_pdf(order)
        result = _send_order_email(order, "order_confirmation", pdf)
        store.update_one(_record_collection(order), {"_id": order_id}, {
            "email_status": "Sent", "email_last_sent_at": utcnow(),
            "email_diagnostic_id": result.get("diagnostic_id"),
        })
    except ValueError as exc:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    except EmailDeliveryError as exc:
        _mark_order_email_failed(order, exc)
        return _order_email_failure(exc)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order confirmation email failed diagnostic_id=%s stage=email_service", diagnostic_id)
        _mark_order_email_failed(order, RuntimeError("MESSAGE_SUBMISSION_FAILED"))
        return failure("Order email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    audit("order.email_confirmation", "order", order_id, {"diagnostic_id": result.get("diagnostic_id")})
    return success({"sent": True, "diagnostic_id": result.get("diagnostic_id")}, "Order confirmation sent")


@bp.get("/orders/<order_id>/pdf")
@permission_required("orders.view")
def order_confirmation_pdf(order_id: str):
    store = current_app.extensions["store"]
    order = _find_order_record(store, order_id)
    if not order:
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    if not _is_final_confirmation_record(order):
        return failure("Only a finalized Order Confirmation can be sent with its PDF", status=409, error="ORDER_CONFIRMATION_NOT_FINAL")
    try:
        content, order = _ensure_order_pdf(order)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order confirmation PDF failed diagnostic_id=%s order_id=%s", diagnostic_id, order_id)
        return failure("Order Confirmation PDF could not be generated.", status=503, error="OC_PDF_GENERATION_FAILED", diagnostic_id=diagnostic_id)
    disposition = "inline" if request.args.get("preview") == "true" else "attachment"
    filename = str(order.get("oc_pdf_filename") or f"{order.get('order_number') or order_id}.pdf")
    return Response(content, mimetype="application/pdf", headers={"Content-Disposition": f'{disposition}; filename="{filename}"'})


@bp.post("/orders/<order_id>/resend-confirmation")
@permission_required("orders.update")
def resend_order_confirmation(order_id: str):
    store = current_app.extensions["store"]
    order = _find_order_record(store, order_id)
    if not order:
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    if not _is_final_confirmation_record(order):
        return failure("Only a finalized Order Confirmation can be sent with its PDF", status=409, error="ORDER_CONFIRMATION_NOT_FINAL")
    try:
        pdf, order = _ensure_order_pdf(order)
        result = _send_order_email(order, "order_confirmation", pdf)
        store.update_one(_record_collection(order), {"_id": order_id}, {
            "email_status": "Sent", "email_last_sent_at": utcnow(),
            "email_diagnostic_id": result.get("diagnostic_id"),
        })
    except ValueError:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    except EmailDeliveryError as exc:
        _mark_order_email_failed(order, exc)
        return _order_email_failure(exc)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order confirmation resend failed diagnostic_id=%s order_id=%s", diagnostic_id, order_id)
        _mark_order_email_failed(order, RuntimeError("MESSAGE_SUBMISSION_FAILED"))
        return failure("Order email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    audit("order.email_resent", "order", order_id, {"diagnostic_id": result.get("diagnostic_id")})
    return success({"sent": True, "diagnostic_id": result.get("diagnostic_id")}, "Order confirmation resent")


@bp.post("/orders/<order_id>/send-status")
@permission_required("orders.update")
def send_order_status(order_id: str):
    order = _find_order_record(current_app.extensions["store"], order_id)
    if not order:
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    try:
        result = _send_order_email(order, "order_status")
    except ValueError as exc:
        return failure("A valid customer email is required before sending", status=422, error="CUSTOMER_EMAIL_REQUIRED")
    except EmailDeliveryError as exc:
        _log_order_email_failure(order, "order_status", exc)
        return _order_email_failure(exc)
    except Exception:
        diagnostic_id = email_diagnostic_id()
        current_app.logger.exception("order status email failed diagnostic_id=%s stage=email_service", diagnostic_id)
        return failure("Order email could not be delivered.", status=503, error="MESSAGE_SUBMISSION_FAILED", diagnostic_id=diagnostic_id, stage="email_service")
    audit("order.email_status", "order", order_id, {"diagnostic_id": result.get("diagnostic_id")})
    return success({"sent": True, "diagnostic_id": result.get("diagnostic_id")}, "Order status email sent")


@bp.get("/quotations/<quotation_id>/order-configuration")
@login_required
def order_configuration(quotation_id: str):
    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    if not quotation:
        return failure("Quotation not found", status=404)
    if not _can_convert_quotation(current_user() or {}, quotation):
        return failure("Customer access denied", status=403)
    user = store.find_one("users", {"_id": quotation.get("salesperson_id") or quotation.get("created_by_user_id") or quotation.get("user_id")}) or {}
    return success({
        "quote": quotation,
        "defaults": {"order_number": _preview_order_number(store, str(quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id"))), "payment_terms": quotation.get("payment_terms"), "order_amount": (quotation.get("totals") or {}).get("grand_total"), "salesperson": {"_id": user.get("_id"), "name": user.get("name"), "email": user.get("email")}},
    })


@bp.post("/quotations/<quotation_id>/convert-to-order")
@login_required
def convert_quotation(quotation_id: str):
    store = current_app.extensions["store"]
    quotation = store.find_one("quotations", {"_id": quotation_id})
    if not quotation:
        return failure("Quotation not found", status=404)
    quotation_customer_id = quotation.get("customer_id") or quotation.get("customer_company_id") or quotation.get("company_id")
    user = current_user() or {}
    if not _can_convert_quotation(user, quotation):
        return failure("You do not have permission to convert this quotation", status=403)
    payload = request.get_json(silent=True) or {}
    idempotency_key = str(request.headers.get("Idempotency-Key") or payload.get("idempotency_key") or "").strip()[:160]
    if idempotency_key:
        existing_by_key = store.find_one("orders", {"customer_id": quotation_customer_id, "idempotency_key": idempotency_key})
        if existing_by_key:
            if _is_final_confirmation_record(existing_by_key):
                return failure("The idempotency key is linked to a final Order Confirmation", status=409, error="IDEMPOTENCY_CONFLICT")
            if not _link_quotation_to_working_order(store, quotation, existing_by_key):
                return failure("The existing Working Order could not be linked to the quotation", status=503, error="QUOTATION_LINK_FAILED")
            existing_by_key["idempotent_replay"] = True
            return success(existing_by_key, "Working Order already created")
    existing_record = store.find_one("orders", {"quotation_id": quotation_id})
    if existing_record and _is_final_confirmation_record(existing_record):
        return failure(
            "This historical quotation is linked to a legacy final Order Confirmation and cannot be converted again.",
            status=409, error="LEGACY_ORDER_CONFIRMATION_EXISTS",
        )
    existing = _working_order_for_quotation(store, quotation_id)
    if existing:
        if not _link_quotation_to_working_order(store, quotation, existing):
            return failure("The existing Working Order could not be linked to the quotation", status=503, error="QUOTATION_LINK_FAILED")
        existing["idempotent_replay"] = True
        return success(existing, "Working Order already created")
    try:
        additional_recipients = _additional_recipients(payload)
        payment_terms = _validated_payment_terms(payload.get("payment_terms"), quotation.get("payment_terms"))
    except ValueError as exc:
        return failure(str(exc), status=422, error="INVALID_ORDER_CONFIGURATION")
    quotation_to, quotation_cc, quotation_bcc = _quotation_recipients(store, quotation)
    quotation_to = list(dict.fromkeys([*quotation_to, *additional_recipients]))
    now = utcnow()
    salesperson_id = quotation.get("salesperson_id") or quotation.get("created_by_user_id") or quotation.get("user_id") or user.get("_id")
    salesperson = store.find_one("users", {"_id": salesperson_id}) or {}
    salesperson_snapshot = quotation.get("salesperson_snapshot") or {"name": salesperson.get("name"), "email": salesperson.get("email"), "_id": salesperson_id}
    lead = store.find_one("leads", {"quotation_id": quotation_id, "$or": [{"customer_id": quotation_customer_id}, {"customer_company_id": quotation_customer_id}, {"company_id": quotation_customer_id}]})
    settings = store.find_one("app_settings", {"_id": "system"}) or {}
    issuer = {**(settings.get("issuer") or {}), **(quotation.get("issuer_snapshot") or {})}
    issuer["name"] = "Moneda Technologies"
    issuer["email"] = issuer.get("email") or "business@monedatechnologies.com"
    working_lines = _working_order_lines({"lines": quotation.get("lines") or []})
    initial_state = "AWAITING_PAYMENT" if payment_terms == "Advance" else "WORKING"
    initial_label = "Awaiting Payment" if payment_terms == "Advance" else "Working"
    order_document = {
        "order_number": "", "quotation_id": quotation_id,
        "source_quotation_id": quotation_id,
        "source_quotation_number": quotation.get("quotation_number"),
        "source_quotation_version": int(quotation.get("version") or 1),
        "version": 1,
        "quotation_number": quotation.get("quotation_number"),
        "lead_id": lead.get("_id") if lead else None,
        "customer_id": quotation_customer_id, "customer_company_id": quotation_customer_id, "company_id": quotation_customer_id,
        "issuer_name": "Moneda Technologies", "issuer_snapshot": issuer,
        "customer_company_snapshot": quotation.get("customer_company_snapshot") or quotation.get("company_snapshot"),
        "company_snapshot": quotation.get("company_snapshot"), "customer_snapshot": quotation.get("customer_snapshot"),
        "salesperson_id": salesperson_id, "prepared_by_user_id": quotation.get("prepared_by_user_id") or salesperson_id, "created_by_user_id": user.get("_id"), "salesperson_snapshot": salesperson_snapshot,
        "created_by_role": user.get("role_id"),
        "manager_id_at_creation": user.get("manager_id"),
        "manager_at_creation": manager_snapshot(store, user),
        "client_type_at_creation": quotation.get("client_type_at_creation") or customer_client_type(quotation.get("customer_snapshot") or {}),
        "account_type_at_creation": quotation.get("account_type_at_creation") or ("DEALER" if quotation.get("client_type_at_creation") == "DEALER" else "DISTRIBUTOR"),
        "quotation_snapshot": deepcopy(quotation), "products_snapshot": deepcopy(working_lines), "lines": deepcopy(working_lines),
        "master_currency": quotation.get("master_currency", "EUR"), "currency": quotation["currency"],
        "exchange_rate": quotation.get("exchange_rate"), "exchange_rate_meta": quotation.get("exchange_rate_meta"),
        "billing_address_snapshot": quotation.get("billing_address_snapshot"),
        "shipping_address_id": quotation.get("shipping_address_id"),
        "shipping_address_snapshot": quotation.get("shipping_address_snapshot"),
        "effective_price_lists": quotation.get("effective_price_lists") or [],
        "eur_totals": quotation.get("eur_totals") or quotation.get("totals"),
        "final_totals": quotation.get("final_totals") or quotation.get("totals"),
        "final_quote": quotation.get("final_quote") or {
            "enabled": False,
            "currency": quotation.get("currency", "EUR"),
            "exchange_rate": quotation.get("exchange_rate", 1),
            "eur_totals": quotation.get("totals"),
            "converted_totals": quotation.get("totals"),
        },
        "quotation_currency": quotation.get("quotation_currency") or quotation.get("currency", "EUR"),
        # The quotation's server-calculated amount is the starting snapshot;
        # working-order edits and finalization recalculate it server-side.
        "totals": quotation["totals"], "order_amount": float((quotation.get("totals") or {}).get("grand_total") or 0),
        "original_quote_payment_terms": quotation.get("payment_terms"), "payment_terms": payment_terms,
        "order_date": payload.get("order_date") or payload.get("oc_date") or now.date().isoformat(),
        # A quotation conversion creates a working Order only.  The final OC
        # is a separate immutable snapshot created by the Superadmin finalize
        # endpoint below.
        "record_type": "ORDER", "order_kind": "WORKING", "lifecycle_state": initial_state,
        "order_status": initial_state, "finalized": False, "locked": False,
        "document_type": "order", "status": initial_label,
        "payment_status": "PENDING", "payment_workflow_state": "AWAITING_PAYMENT" if payment_terms == "Advance" else None, "confirmed_received": 0.0,
        "remaining_balance": float((quotation.get("totals") or {}).get("grand_total") or 0),
        "customer_credit": 0.0, "pending_payment_count": 0,
        "created_at": now, "document_status": "Pending", "email_status": "Pending",
        "notes": quotation.get("notes", ""), "to": quotation_to, "cc": quotation_cc, "bcc": quotation_bcc,
        "additional_recipients": additional_recipients,
        "conversion_state": "PROCESSING",
        "history": [{"status": initial_state, "event": "CREATED_FROM_QUOTATION", "at": now, "by": (current_user() or {}).get("_id"), "quotation_id": quotation_id}],
    }
    if idempotency_key:
        order_document["idempotency_key"] = idempotency_key
    # Incentive rates are snapshotted when the final OC is created, after the
    # working Order has been edited and its final totals are known.
    # Keep the existing preflight check for a friendly 409, then repeat it
    # while holding a process-local lock so two rapid conversion requests in
    # this worker cannot both create a Working Order.
    with _CONVERSION_LOCK:
        existing_record = store.find_one("orders", {"quotation_id": quotation_id})
        if existing_record and _is_final_confirmation_record(existing_record):
            return failure(
                "This historical quotation is linked to a legacy final Order Confirmation and cannot be converted again.",
                status=409, error="LEGACY_ORDER_CONFIRMATION_EXISTS",
            )
        existing = _working_order_for_quotation(store, quotation_id)
        if existing:
            if not _link_quotation_to_working_order(store, quotation, existing):
                return failure("The existing Working Order could not be linked to the quotation", status=503, error="QUOTATION_LINK_FAILED")
            existing["idempotent_replay"] = True
            return success(existing, "Working Order already created")
        try:
            # Allocate the customer-scoped Order sequence only after the
            # idempotency check is held under the conversion lock.
            order_document["order_number"] = _next_order_number(store, str(quotation_customer_id))
            order_document["order_id"] = order_document["order_number"]
            order = store.insert_one("orders", order_document)
            persisted = store.find_one("orders", {"_id": order.get("_id"), "quotation_id": quotation_id})
            if not persisted:
                raise RuntimeError("Working Order persistence verification failed")
            quotation_link = _link_quotation_to_working_order(store, quotation, persisted)
            if not quotation_link:
                store.delete_one("orders", {"_id": persisted.get("_id")})
                return failure(
                    "The Working Order was not linked to the quotation; the partial Order was rolled back.",
                    status=503, error="QUOTATION_LINK_FAILED",
                )
            order = store.update_one("orders", {"_id": persisted.get("_id")}, {
                "conversion_state": "COMPLETE", "conversion_completed_at": utcnow(),
            }) or persisted
        except Exception as exc:
            # Mongo's unique/index errors (or a concurrent worker) should be
            # reported as an idempotent conversion conflict, never retried as
            # a second order creation.
            if exc.__class__.__name__ == "DuplicateKeyError":
                existing = _working_order_for_quotation(store, quotation_id)
                if existing:
                    _link_quotation_to_working_order(store, quotation, existing)
                    existing["idempotent_replay"] = True
                    return success(existing, "Working Order already created")
            raise
    try:
        open_reminders, _ = store.list("reminders", {"quotation_id": quotation_id, "$or": [{"customer_id": quotation_customer_id}, {"customer_company_id": quotation_customer_id}, {"company_id": quotation_customer_id}], "status": {"$in": ["Pending", "Due", "Overdue", "open"]}}, limit=100)
        for reminder in open_reminders:
            store.update_one("reminders", {"_id": reminder["_id"]}, {"status": "Cancelled", "cancelled_at": now, "cancelled_reason": "Quotation converted to working order"})
        for interval in settings.get("post_order_follow_up_days", [15, 25]):
            store.insert_one("reminders", {"customer_id": quotation_customer_id, "customer_company_id": quotation_customer_id, "company_id": quotation_customer_id, "order_id": order["_id"], "lead_id": lead.get("_id") if lead else None, "assigned_to": user.get("_id"), "due_date": (now + timedelta(days=int(interval))).date().isoformat(), "status": "Pending", "priority": "normal", "notes": f"Post-order follow-up ({interval} days)", "frequency": "none", "repeat_frequency": "none", "repeat_enabled": False})
        if lead:
            store.update_one("leads", {"_id": lead["_id"]}, {"status": "Order Received", "order_id": order["_id"], "follow_up_date": None})
        store.insert_one("notifications", {
            "user_id": (current_user() or {}).get("_id"), "customer_id": quotation_customer_id,
            "type": "order_created", "title": f"Working order {order['order_number']} created",
            "order_id": order["_id"], "read": False,
        })
    except Exception:
        current_app.logger.exception(
            "working Order created but post-conversion follow-up failed quotation_id=%s order_id=%s",
            quotation_id, order.get("_id"),
        )
        order["conversion_warnings"] = ["POST_CONVERSION_FOLLOW_UP_FAILED"]
    audit("order.create", "order", str(order["_id"]), {"quotation_id": quotation_id, "lifecycle_state": "WORKING"})
    return success(order, "Working Order created", 201)


@bp.post("/orders/<order_id>/materials-ready")
@superadmin_required
def mark_materials_ready(order_id: str):
    """Record the Superadmin dispatch-readiness gate for non-Advance orders."""
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Working Order not found", status=404)
    if _is_order_locked(order):
        return failure("Final Order Confirmations are immutable", status=423, error="order_locked")
    if str(order.get("payment_terms") or "Advance") == "Advance":
        return failure("Advance orders are released after confirmed payment; materials-ready is for non-advance orders", status=409, error="PAYMENT_GATE_REQUIRED")
    now = utcnow()
    updated = store.update_one("orders", {"_id": order_id}, {
        "materials_ready_for_dispatch": True,
        "materials_ready_at": now,
        "materials_ready_by": (current_user() or {}).get("_id"),
        "lifecycle_state": "READY_FOR_DISPATCH",
        "order_status": "READY_FOR_DISPATCH",
        "status": "Ready for Dispatch",
        "history": [*order.get("history", []), {"status": "READY_FOR_DISPATCH", "at": now, "by": (current_user() or {}).get("_id")}],
    }) or order
    audit("order.materials_ready", "order", order_id, {})
    return success(updated, "Materials marked ready for dispatch")


@bp.post("/orders/<order_id>/finalize")
@superadmin_required
def finalize_order(order_id: str):
    """Create exactly one immutable Order Confirmation from a working Order."""
    store = current_app.extensions["store"]
    with _CONVERSION_LOCK:
        existing = store.find_one("order_confirmations", {"source_order_id": order_id})
        if existing:
            return success(existing, "Order Confirmation already finalized")
        order = store.find_one("orders", {"_id": order_id})
        if not order:
            existing = store.find_one("order_confirmations", {"source_order_id": order_id})
            if existing:
                return success(existing, "Order Confirmation already finalized")
            return failure("Working Order not found", status=404)
        if order.get("final_oc_id"):
            existing = store.find_one("order_confirmations", {"_id": order.get("final_oc_id")})
            if existing:
                return success(existing, "Order Confirmation already finalized")
        if order.get("finalized") or str(order.get("lifecycle_state") or "").upper() == "FINALIZED":
            existing = store.find_one("order_confirmations", {"source_order_id": order_id})
            if existing:
                return success(existing, "Order Confirmation already finalized")
            return failure("Order is already finalized", status=409, error="order_already_finalized")

        # The working Order is already server-priced whenever an item changes.
        # Finalization must copy that exact commercial state; repricing here
        # would make the immutable OC differ from the approved working Order.
        if not _working_order_lines(order):
            return failure("A working Order must contain at least one item", status=422, error="ORDER_REQUIRES_ITEM")

        payments = _payment_rows_for_record(store, order_id)
        rollup = payment_rollup(order, payments)
        terms = str(order.get("payment_terms") or "Advance").strip()
        if terms == "Advance" and rollup.get("invoice_status") != "PAID":
            return failure("Advance orders require a confirmed payment before finalization", status=409, error="PAYMENT_CONFIRMATION_REQUIRED", details=rollup)
        if terms != "Advance" and not order.get("materials_ready_for_dispatch"):
            return failure("Materials must be marked ready for dispatch before finalization", status=409, error="MATERIALS_READY_REQUIRED")

        salesperson_id = order.get("salesperson_id") or order.get("created_by_user_id")
        salesperson = store.find_one("users", {"_id": salesperson_id}) or {}
        final_record = deepcopy(order)
        final_record.pop("_id", None)
        final_record.pop("_storage_collection", None)
        now = utcnow()
        oc_number = _next_oc_number(store, _order_customer_id(order))
        final_record.update({
            "order_number": oc_number, "oc_number": oc_number,
            "order_id": order.get("order_number"), "source_order_number": order.get("order_number"),
            "record_type": "ORDER_CONFIRMATION", "order_kind": "FINAL", "lifecycle_state": "FINAL",
            "order_status": "FINALIZED", "document_type": "order_confirmation", "status": "Finalized",
            "finalized": True, "locked": True, "financial_locked": True,
            "source_order_id": order_id, "source_quote_id": order.get("quotation_id"),
            "finalization_state": "FINALIZED",
            "finalized_at": now, "finalized_by": (current_user() or {}).get("_id"),
            "payment_status": rollup["payment_status"], "confirmed_received": rollup["confirmed_received"],
            "total_confirmed_payments": rollup["total_confirmed_payments"], "remaining_balance": rollup["remaining_balance"],
            "customer_credit": rollup["customer_credit"], "pending_payment_count": rollup["pending_payment_count"],
            "history": [*order.get("history", []), {"status": "FINAL", "at": now, "by": (current_user() or {}).get("_id")}],
        })
        try:
            validate_incentive_configuration(store, final_record, salesperson)
        except IncentiveConfigurationError as exc:
            return failure(str(exc), status=422, error="INCENTIVE_CONFIGURATION_REQUIRED", category_id=exc.category_id)
        try:
            final = store.insert_one("order_confirmations", final_record)
        except Exception as exc:
            # The unique source_order_id index is the cross-worker idempotency
            # boundary. If another worker won the race, return its immutable
            # snapshot rather than exposing a duplicate/500 to the caller.
            if exc.__class__.__name__ == "DuplicateKeyError":
                existing = store.find_one("order_confirmations", {"source_order_id": order_id})
                if existing:
                    return success(existing, "Order Confirmation already finalized")
            raise
        try:
            incentive = create_incentive_for_order(store, final, salesperson)
        except Exception:
            store.delete_one("order_confirmations", {"_id": final.get("_id")})
            current_app.logger.exception("final Order Confirmation incentive creation failed order_id=%s", order_id)
            return failure("Order Confirmation could not be finalized because its incentive record failed.", status=503, error="INCENTIVE_CREATION_FAILED")
        final = store.update_one("order_confirmations", {"_id": final.get("_id")}, {
            "incentive_id": incentive.get("_id"), "incentive_status": incentive.get("status", "PENDING PAYMENT"),
            "incentive_amount": incentive.get("gross_incentive_amount", 0),
        }) or final
        try:
            _, final = _ensure_order_pdf(final)
        except Exception:
            current_app.logger.exception("final Order Confirmation PDF generation failed order_id=%s", order_id)
        updated_order = store.update_one("orders", {"_id": order_id}, {
            "final_oc_id": final.get("_id"), "finalized": True, "locked": True,
            "lifecycle_state": "FINALIZED", "order_status": "FINALIZED", "finalized_at": now,
            "finalized_by": (current_user() or {}).get("_id"), "finalization_id": final.get("_id"),
        }) or order
        quotation_id = str(order.get("quotation_id") or order.get("source_quotation_id") or "")
        if quotation_id:
            store.update_one("quotations", {"_id": quotation_id}, {
                "converted_oc_id": final.get("_id"), "converted_oc_number": final.get("oc_number"),
                "final_oc_id": final.get("_id"), "finalized_at": now,
            })
        final["source_order_id"] = updated_order.get("_id")
        audit("order.finalized", "order_confirmation", str(final.get("_id")), {"source_order_id": order_id})
        return success(final, "Order Confirmation finalized", 201)


@bp.patch("/orders/<order_id>")
@permission_required("orders.update")
def update_order(order_id: str):
    store = current_app.extensions["store"]
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return failure("Order not found", status=404)
    if not _can_access_order_record(order):
        return failure("Customer access denied", status=403)
    if _is_order_locked(order):
        return failure("Final Order Confirmations are immutable", status=423, error="order_locked")
    payload = request.get_json(silent=True) or {}
    if "items" in payload:
        try:
            updated = _replace_working_order_items(store, order, payload.get("items"))
        except PermissionError as exc:
            return failure(str(exc), status=403)
        except LookupError as exc:
            return failure(str(exc), status=404)
        except PricingUnavailable as exc:
            return failure(str(exc), status=409)
        except (ValueError, TypeError) as exc:
            return failure(str(exc), status=422)
        audit("order.items.replace", "order", order_id, {"item_count": len(payload.get("items") or [])})
        return success(updated, "Working Order items saved")
    changes: dict = {}
    # Pricing and line values are intentionally not accepted through this
    # generic metadata endpoint.  They must go through the item endpoints,
    # which resolve the product and price server-side before recalculating the
    # order totals.  This prevents a caller from marking an order paid or
    # changing its amount by submitting client-calculated totals.
    pricing_fields = {
        "lines", "products_snapshot", "totals", "eur_totals", "final_totals", "final_quote",
        "order_amount", "currency", "master_currency", "exchange_rate", "exchange_rate_meta",
    }
    if pricing_fields.intersection(payload):
        return failure(
            "Order pricing must be changed through the working-order item endpoints.",
            status=422, error="ORDER_PRICING_UPDATE_REQUIRED",
        )
    editable_fields = {
        "payment_terms", "oc_date", "billing_address_snapshot", "shipping_address_id",
        "shipping_address_snapshot", "notes", "to", "cc", "bcc", "additional_recipients",
    }
    for field in editable_fields:
        if field in payload:
            changes[field] = payload[field]
    if "payment_terms" in changes:
        try:
            changes["payment_terms"] = _validated_payment_terms(changes["payment_terms"], order.get("payment_terms"))
        except ValueError as exc:
            return failure(str(exc), status=422, error="INVALID_ORDER_CONFIGURATION")
    if "status" in payload:
        requested_status = str(payload.get("status") or "").strip().upper().replace(" ", "_")
        current_state = str(order.get("lifecycle_state") or "WORKING").upper().replace(" ", "_")
        if requested_status == "CANCELLED":
            if current_state in {"FINAL", "FINALIZED"}:
                return failure("Final Order Confirmations are immutable", status=423, error="order_locked")
            changes.update({"status": "Cancelled", "order_status": "CANCELLED", "lifecycle_state": "CANCELLED"})
        elif requested_status == current_state:
            return failure("The Order is already in that state", status=409, error="INVALID_ORDER_TRANSITION")
        else:
            return failure(
                "Payment and fulfilment states are advanced by their dedicated server workflows.",
                status=422, error="INVALID_ORDER_TRANSITION",
                details={"current": current_state, "requested": requested_status, "allowed": sorted(WORKING_STATES)},
            )
    if not changes:
        return failure("No editable order fields were supplied", status=422, error="NO_ORDER_CHANGES")
    history = list(order.get("history", []))
    if "status" in changes:
        history.append({"status": changes["status"], "at": utcnow(), "by": (current_user() or {}).get("_id")})
        changes["history"] = history
    updated = store.update_one("orders", {"_id": order_id}, changes)
    # Payment rollup remains authoritative after an order amount or line edit.
    if updated:
        from app.finance.service import sync_order_payment_state
        updated = sync_order_payment_state(store, order_id) or updated
    audit("order.update", "order", order_id, {"fields": sorted(changes)})
    return success(updated, "Order updated")


def _load_editable_order(store, order_id: str):
    order = store.find_one("orders", {"_id": order_id})
    if not order:
        return None, failure("Working Order not found", status=404)
    if not _can_access_order_record(order):
        return None, failure("Customer access denied", status=403)
    if _is_order_locked(order):
        return None, failure("Final Order Confirmations are immutable", status=423, error="order_locked")
    return order, None


def _calculate_order_line(payload: dict, order: dict):
    """Use the central calculator resolver for every working-order line."""
    from app.pricing.routes import _calculate

    request_payload = dict(payload)
    return _calculate(request_payload, customer_id_override=_order_customer_id(order), require_active_context=False)


def _line_edit_signature(value: dict) -> tuple:
    return (
        str(value.get("product_id") or ""),
        repr(sorted((value.get("configuration") or {}).items())),
        int(value.get("requested_quantity", value.get("quantity", 1)) or 1),
        float(value.get("requested_discount_percent", value.get("discount_percent", 0)) or 0),
    )


def _replace_working_order_items(store, order: dict, submitted: object) -> dict:
    if not isinstance(submitted, list) or not submitted:
        raise ValueError("A working Order must contain at least one item")
    existing = {str(line.get("item_id")): line for line in _working_order_lines(order)}
    lines: list[dict] = []
    for raw in submitted:
        if not isinstance(raw, dict):
            raise ValueError("Each order item must be an object")
        item_id = str(raw.get("item_id") or raw.get("line_id") or uuid4().hex)
        previous = existing.get(item_id)
        if previous and _line_edit_signature(previous) == _line_edit_signature(raw):
            line = deepcopy(previous)
        else:
            _customer, _product, priced, rate_meta = _calculate_order_line(raw, order)
            line = {**priced, "exchange_rate_meta": rate_meta}
        line.update({"item_id": item_id, "line_id": item_id})
        lines.append(line)
    before = float((order.get("totals") or {}).get("grand_total") or 0)
    updated = _recalculate_working_order(store, order, lines)
    now = utcnow()
    history = [*(updated.get("history") or []), {
        "status": updated.get("lifecycle_state") or "WORKING", "event": "ITEMS_UPDATED",
        "at": now, "by": (current_user() or {}).get("_id"), "before_total": before,
        "after_total": float((updated.get("totals") or {}).get("grand_total") or 0),
    }]
    return store.update_one("orders", {"_id": order.get("_id")}, {
        "history": history, "version": int(order.get("version") or 1) + 1,
    }) or {**updated, "history": history}


@bp.post("/orders/<order_id>/items")
@permission_required("orders.update")
def add_order_item(order_id: str):
    store = current_app.extensions["store"]
    order, error_response = _load_editable_order(store, order_id)
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    try:
        _customer, _product, line, rate_meta = _calculate_order_line(payload, order)
    except PermissionError as exc:
        return failure(str(exc), status=403)
    except LookupError as exc:
        return failure(str(exc), status=404)
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except (ValueError, TypeError) as exc:
        return failure(str(exc), status=422)
    item_id = str(payload.get("item_id") or uuid4().hex)
    line = {**line, "item_id": item_id, "line_id": item_id, "exchange_rate_meta": rate_meta}
    lines = [*_working_order_lines(order), line]
    updated = _recalculate_working_order(store, order, lines)
    audit("order.item.create", "order", order_id, {"item_id": item_id, "product_id": line.get("product_id")})
    return success({"item": line, "order": updated}, "Item added to working Order", 201)


@bp.patch("/orders/<order_id>/items/<item_id>")
@permission_required("orders.update")
def update_order_item(order_id: str, item_id: str):
    store = current_app.extensions["store"]
    order, error_response = _load_editable_order(store, order_id)
    if error_response:
        return error_response
    payload = request.get_json(silent=True) or {}
    lines = _working_order_lines(order)
    index = next((position for position, line in enumerate(lines) if str(line.get("item_id")) == str(item_id)), None)
    if index is None:
        return failure("Order item not found", status=404)
    merged = {**lines[index], **payload, "item_id": str(item_id), "line_id": str(item_id)}
    try:
        _customer, _product, line, rate_meta = _calculate_order_line(merged, order)
    except PermissionError as exc:
        return failure(str(exc), status=403)
    except LookupError as exc:
        return failure(str(exc), status=404)
    except PricingUnavailable as exc:
        return failure(str(exc), status=409)
    except (ValueError, TypeError) as exc:
        return failure(str(exc), status=422)
    lines[index] = {**line, "item_id": str(item_id), "line_id": str(item_id), "exchange_rate_meta": rate_meta}
    updated = _recalculate_working_order(store, order, lines)
    audit("order.item.update", "order", order_id, {"item_id": str(item_id), "product_id": line.get("product_id")})
    return success({"item": lines[index], "order": updated}, "Working Order item updated")


@bp.delete("/orders/<order_id>/items/<item_id>")
@permission_required("orders.update")
def delete_order_item(order_id: str, item_id: str):
    store = current_app.extensions["store"]
    order, error_response = _load_editable_order(store, order_id)
    if error_response:
        return error_response
    lines = _working_order_lines(order)
    remaining = [line for line in lines if str(line.get("item_id")) != str(item_id)]
    if len(remaining) == len(lines):
        return failure("Order item not found", status=404)
    if not remaining:
        return failure("A working Order must contain at least one item", status=422, error="ORDER_REQUIRES_ITEM")
    updated = _recalculate_working_order(store, order, remaining)
    audit("order.item.delete", "order", order_id, {"item_id": str(item_id)})
    return success(updated, "Working Order item removed")
