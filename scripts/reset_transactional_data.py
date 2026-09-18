"""Development-only reset of downstream transaction data.

This script intentionally does not import or start the Flask application.  It
backs up the records it may touch, requires an explicit confirmation phrase,
removes order/payment/incentive transaction data, and restores quotations and
CRM leads to their pre-order state.

Usage:
    python scripts/reset_transactional_data.py --dry-run
    python scripts/reset_transactional_data.py --development \
        --confirm "RESET TRANSACTIONAL DATA"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from bson.json_util import dumps as bson_dumps
from bson.json_util import loads as bson_loads
from dotenv import load_dotenv
from pymongo import MongoClient


CONFIRMATION = "RESET TRANSACTIONAL DATA"
REFERENCE_KEYS = {
    "order_id",
    "oc_id",
    "order_confirmation_id",
    "orderconfirmation_id",
    "orderConfirmationId",
}
QUOTE_REFERENCE_KEYS = {"converted_order_id", "converted_oc_id", "order_id", "oc_id"}

# These collections contain only downstream transaction/workflow data in the
# Moneda schema.  Master data, users, customers, pricing, rules and settings
# are deliberately excluded.
TRANSACTION_COLLECTIONS = {
    "orders",
    "payments",
    "payment_allocations",
    "payment_proofs",
    "payment_confirmations",
    "payment_transactions",
    "bank_transactions",
    "invoices",
    "incentives",
    "incentive_allocations",
    "incentive_adjustments",
    "incentive_payouts",
    "credit_notes",
    "customer_credits",
    "customer_credit_ledger",
    "order_lines",
}
BACKUP_COLLECTIONS = TRANSACTION_COLLECTIONS | {
    "order_documents",
    "quotations",
    "leads",
    "reminders",
    "notifications",
    "email_logs",
    "communication_logs",
    "whatsapp_logs",
    "audit_logs",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="inspect and print the reset plan without changing MongoDB")
    parser.add_argument("--development", action="store_true", help="required acknowledgement that this is a development reset")
    parser.add_argument("--confirm", metavar="PHRASE", help="exactly: RESET TRANSACTIONAL DATA")
    parser.add_argument("--backup-dir", type=Path, help="optional backup directory (default: backups/transactional_reset_<timestamp>)")
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def normalize(value: Any) -> str:
    return str(value) if value is not None else ""


def values_match(value: Any, references: set[str]) -> bool:
    if isinstance(value, (list, tuple, set)):
        return any(values_match(item, references) for item in value)
    return normalize(value) in references


def contains_linked_reference(value: Any, references: set[str], path: str = "") -> bool:
    """Find an order reference by field name, including nested metadata."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in REFERENCE_KEYS and values_match(child, references):
                return True
            if contains_linked_reference(child, references, f"{path}.{key_text}"):
                return True
    elif isinstance(value, list):
        return any(contains_linked_reference(item, references, path) for item in value)
    return False


def history_pre_conversion_status(quotation: dict[str, Any]) -> str:
    """Return the last known state before the first conversion event."""
    history = quotation.get("history") or []
    previous: str | None = None
    for event in history:
        if not isinstance(event, dict):
            continue
        status = str(event.get("status") or "").strip()
        if status.casefold() == "converted to order":
            break
        if status:
            previous = status
    if previous and previous.casefold() != "converted to order":
        return previous
    current = str(quotation.get("status") or "").strip()
    if current and current.casefold() != "converted to order":
        return current
    return "Sent"


def safe_name(collection: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in collection)


def backup_collections(db: Any, backup_dir: Path, collections: Iterable[str]) -> dict[str, int]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for collection in sorted(set(collections)):
        if collection not in db.list_collection_names():
            continue
        documents = list(db[collection].find({}))
        target = backup_dir / f"{safe_name(collection)}.json"
        target.write_text(bson_dumps(documents, indent=2), encoding="utf-8")
        # Re-read the BSON-JSON backup before any destructive operation.
        restored = bson_loads(target.read_text(encoding="utf-8"))
        if len(restored) != len(documents):
            raise RuntimeError(f"Backup verification failed for collection {collection}")
        counts[collection] = len(documents)
    return counts


def collection_counts(db: Any, collections: Iterable[str]) -> dict[str, int]:
    existing = set(db.list_collection_names())
    return {collection: (db[collection].count_documents({}) if collection in existing else 0) for collection in sorted(set(collections))}


def write_manifest(backup_dir: Path, *, database: str, before: dict[str, int], order_ids: list[str]) -> None:
    manifest = {
        "created_at": iso_now(),
        "database": database,
        "purpose": "development transactional reset",
        "confirmation_phrase": CONFIRMATION,
        "collections": before,
        "order_ids_removed": order_ids,
        "audit_logs_preserved": True,
    }
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def delete_linked_documents(db: Any, collection: str, references: set[str]) -> int:
    if collection not in db.list_collection_names() or not references:
        return 0
    ids: list[Any] = []
    for document in db[collection].find({}):
        if contains_linked_reference(document, references):
            ids.append(document.get("_id"))
    if not ids:
        return 0
    return int(db[collection].delete_many({"_id": {"$in": ids}}).deleted_count)


def clear_order_files(root: Path, backup_dir: Path, order_documents: list[dict[str, Any]]) -> list[str]:
    """Back up and remove only referenced files inside generated PDF roots."""
    allowed_roots = [
        (root / "generated" / "quotations").resolve(),
        (root / "output" / "pdf").resolve(),
    ]
    backed_up: list[str] = []
    file_backup_root = backup_dir / "files"
    for document in order_documents:
        candidates: list[Path] = []
        for key in ("file_path", "path", "storage_path", "pdf_path", "filename", "oc_pdf_filename"):
            value = document.get(key)
            if not value or not isinstance(value, str):
                continue
            raw = Path(value)
            if raw.is_absolute():
                candidates.append(raw)
            else:
                candidates.extend(root / raw for _ in [0])
                candidates.extend(base / raw.name for base in allowed_roots)
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            if not any(resolved == allowed or allowed in resolved.parents for allowed in allowed_roots):
                continue
            relative = resolved.relative_to(root.resolve())
            destination = file_backup_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, destination)
            resolved.unlink()
            backed_up.append(str(relative))
    return backed_up


def restore_quotation_state(db: Any, order_references: set[str]) -> int:
    if "quotations" not in db.list_collection_names():
        return 0
    changed = 0
    now = utc_now()
    for quotation in db.quotations.find({}):
        status = str(quotation.get("status") or "").strip()
        converted_status = status.casefold() == "converted to order"
        stale_ref = any(values_match(quotation.get(key), order_references) for key in QUOTE_REFERENCE_KEYS)
        if not converted_status and not stale_ref:
            continue
        restored_status = history_pre_conversion_status(quotation)
        update: dict[str, Any] = {
            "status": restored_status,
            "converted_order_id": None,
            "converted_oc_id": None,
            "converted_at": None,
        }
        # Legacy relationship fields are cleared only when this quotation was
        # converted or points at an order being removed.
        if converted_status or stale_ref:
            update["order_id"] = None
            update["oc_id"] = None
        if status != restored_status or stale_ref or converted_status:
            history = list(quotation.get("history") or [])
            history.append({
                "status": restored_status,
                "at": now,
                "by": "development-reset",
                "reason": "Transactional reset restored pre-order quotation state",
            })
            update["history"] = history
        db.quotations.update_one({"_id": quotation.get("_id")}, {"$set": update})
        changed += 1
    return changed


def restore_leads(db: Any, order_references: set[str]) -> int:
    if "leads" not in db.list_collection_names():
        return 0
    changed = 0
    for lead in db.leads.find({}):
        status = str(lead.get("status") or "").strip()
        stale_ref = any(values_match(lead.get(key), order_references) for key in REFERENCE_KEYS)
        if status.casefold() != "order received" and not stale_ref:
            continue
        db.leads.update_one(
            {"_id": lead.get("_id")},
            {"$set": {"status": "Lead", "order_id": None, "oc_id": None}},
        )
        changed += 1
    return changed


def stale_reference_report(db: Any, order_references: set[str]) -> dict[str, int]:
    report: dict[str, int] = {}
    if not order_references:
        return report
    for collection in db.list_collection_names():
        if collection == "audit_logs":
            continue  # audit history intentionally preserves deleted IDs
        matches = 0
        for document in db[collection].find({}):
            if collection == "quotations":
                # Historical quotation history may legitimately retain the
                # original conversion event; live top-level links were reset.
                live = {key: document.get(key) for key in QUOTE_REFERENCE_KEYS}
                if any(values_match(value, order_references) for value in live.values()):
                    matches += 1
                continue
            if contains_linked_reference(document, order_references):
                matches += 1
        if matches:
            report[collection] = matches
    return report


def print_counts(label: str, counts: dict[str, int]) -> None:
    print(label)
    for collection, count in counts.items():
        if count:
            print(f"  {collection}: {count}")


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    database_name = str(os.getenv("MONGODB_DATABASE") or "").strip()
    uri = str(os.getenv("MONGODB_URI") or "").strip()
    environment = str(os.getenv("FLASK_ENV") or "").strip().casefold()
    if not uri or not database_name:
        print("MONGODB_URI and MONGODB_DATABASE must be configured in .env", file=sys.stderr)
        return 2
    if not args.development and not args.dry_run:
        print("Refusing to mutate MongoDB without --development.", file=sys.stderr)
        return 2
    if not args.dry_run and environment not in {"development", "dev", "local", "test"}:
        print("Refusing to mutate a non-development environment.", file=sys.stderr)
        return 2
    if not args.dry_run and args.confirm != CONFIRMATION:
        print(f'Confirmation required: --confirm "{CONFIRMATION}"', file=sys.stderr)
        return 2

    client = MongoClient(uri, serverSelectionTimeoutMS=10_000)
    try:
        client.admin.command("ping")
        db = client[database_name]
        existing = set(db.list_collection_names())
        before = collection_counts(db, BACKUP_COLLECTIONS)
        order_documents = list(db["orders"].find({})) if "orders" in existing else []
        order_document_records = list(db["order_documents"].find({})) if "order_documents" in existing else []
        order_references = {normalize(document.get("_id")) for document in order_documents if document.get("_id") is not None}
        order_numbers = [normalize(document.get("order_number")) for document in order_documents if document.get("order_number")]
        print(f"Database: {database_name}")
        print(f"Orders/OCs identified: {len(order_references)}")
        if order_numbers:
            print(f"Order numbers identified: {len(order_numbers)}")
        print_counts("Records in reset scope before operation:", before)
        if args.dry_run:
            print("Dry run only: no backup or database changes were made.")
            return 0

        timestamp = utc_now().strftime("%Y%m%d_%H%M%S")
        backup_dir = (args.backup_dir or (root / "backups" / f"transactional_reset_{timestamp}")).resolve()
        if backup_dir.exists() and any(backup_dir.iterdir()):
            print(f"Refusing to overwrite non-empty backup directory: {backup_dir}", file=sys.stderr)
            return 2
        backup_counts = backup_collections(db, backup_dir, BACKUP_COLLECTIONS)
        write_manifest(backup_dir, database=database_name, before=backup_counts, order_ids=sorted(order_references))
        print(f"Verified backup: {backup_dir}")

        affected: dict[str, int] = {}
        # Order documents are the authoritative OC records in this schema.
        for collection in sorted(TRANSACTION_COLLECTIONS):
            if collection not in existing:
                continue
            deleted = int(db[collection].delete_many({}).deleted_count)
            if deleted:
                affected[collection] = deleted
        if "order_documents" in existing:
            deleted = delete_linked_documents(db, "order_documents", order_references)
            if deleted:
                affected["order_documents"] = deleted

        # Preserve quotation-related communication, but remove records tied to
        # the deleted order/OC from workflow and delivery collections.
        for collection in ("reminders", "notifications", "email_logs", "communication_logs", "whatsapp_logs"):
            deleted = delete_linked_documents(db, collection, order_references)
            if deleted:
                affected[collection] = deleted

        quotation_changes = restore_quotation_state(db, order_references)
        lead_changes = restore_leads(db, order_references)
        if quotation_changes:
            affected["quotations_restored"] = quotation_changes
        if lead_changes:
            affected["leads_restored"] = lead_changes
        removed_files = clear_order_files(root, backup_dir, [*order_documents, *order_document_records])

        after = collection_counts(db, BACKUP_COLLECTIONS)
        stale = stale_reference_report(db, order_references)
        print_counts("Records in reset scope after operation:", after)
        print("Records removed/restored:")
        for collection, count in sorted(affected.items()):
            print(f"  {collection}: {count}")
        print(f"Generated transaction files removed: {len(removed_files)}")
        print(f"Audit logs preserved: {after.get('audit_logs', 0)}")
        if stale:
            print("ERROR: stale live references remain:")
            for collection, count in sorted(stale.items()):
                print(f"  {collection}: {count}")
            return 1
        print("Stale live order references: 0")
        print("Reset completed without starting Flask.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
