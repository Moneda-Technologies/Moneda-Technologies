from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
import shutil
from uuid import uuid4

import pytest
from PIL import Image as PillowImage
from pypdf import PdfReader

from app.orders.routes import _ensure_order_pdf, _record_order_version
from app.quotations.routes import _record_quotation_version
from app.repositories.store import utcnow
from app.services.document_archive import archive_version
from app.services.workdrive import WorkDriveError


@pytest.fixture()
def version_upload_dir():
    path = Path.cwd() / ".test-version-signatures" / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def image_bytes(color: tuple[int, int, int]) -> bytes:
    stream = BytesIO()
    PillowImage.new("RGB", (120, 40), color).save(stream, format="PNG")
    return stream.getvalue()


def actor_with_signature(tmp_path, user_id: str, name: str, color: tuple[int, int, int]) -> tuple[dict, bytes]:
    content = image_bytes(color)
    relative = f"signatures/{user_id}.png"
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return ({
        "_id": user_id, "name": name, "email": f"{user_id}@monedatechnologies.com",
        "signature_path": relative, "signature_filename": f"{user_id}.png",
        "signature_mime_type": "image/png", "signature_size": len(content),
        "signature_width": 120, "signature_height": 40, "signature_updated_at": utcnow(),
    }, content)


def document(document_id: str, number: str, version: int = 1) -> dict:
    return {
        "_id": document_id, "quotation_number": number, "order_number": number,
        "version": version, "currency": "EUR", "created_at": utcnow(),
        "customer_id": "company-moneda-demo",
        "customer_snapshot": {"name": "Customer", "email": "customer@example.com"},
        "customer_company_snapshot": {"name": "Customer"},
        "creator_snapshot": {"name": "Creator"}, "payment_terms": "Advance",
        "lines": [{"product_name": "MTech Active Prime", "description": "Product", "quantity": 1,
                   "unit_price": 10, "line_total": 10, "discount_percent": 0}],
        "totals": {"subtotal": 10, "discount_amount": 0, "taxable_amount": 10,
                   "tax_amount": 0, "transport_cost": 0, "transport_tax_amount": 0, "grand_total": 10},
    }


def pdf_text(content: bytes) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(content)).pages)


def pdf_image_count(content: bytes) -> int:
    count = 0
    for page in PdfReader(BytesIO(content)).pages:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject") or {}
        for value in xobjects.values():
            image = value.get_object()
            if image.get("/Subtype") == "/Image":
                count += 1
    return count


def test_quotation_versions_capture_each_actor_signature_immutably(app, version_upload_dir):
    tmp_path = version_upload_dir
    app.config["UPLOAD_DIRECTORY"] = tmp_path
    store = app.extensions["store"]
    actor_a, signature_a = actor_with_signature(tmp_path, "actor-a", "User A", (220, 20, 20))
    actor_b, signature_b = actor_with_signature(tmp_path, "actor-b", "Admin B", (20, 20, 220))
    quote_v1 = document("signed-quote", "QT-SIGNED", 1)
    quote_v2 = {**quote_v1, "version": 2, "notes": "Revision"}

    with app.app_context():
        first = _record_quotation_version(store, quote_v1, actor=actor_a, reason="Original quotation")
        second = _record_quotation_version(
            store, quote_v2, actor=actor_b, reason="Admin revision",
            previous_version_id="signed-quote-v01",
        )

    assert first["signature_snapshot"]["content"] == signature_a
    assert second["signature_snapshot"]["content"] == signature_b
    assert first["signature_snapshot"]["actor_user_id"] == "actor-a"
    assert second["signature_snapshot"]["actor_user_id"] == "actor-b"
    assert "User A" in pdf_text(first["pdf"])
    assert "Admin B" in pdf_text(second["pdf"])
    assert pdf_image_count(first["pdf"]) >= 1
    assert pdf_image_count(second["pdf"]) >= 1
    original_first_pdf = first["pdf"]

    (tmp_path / actor_a["signature_path"]).write_bytes(image_bytes((20, 220, 20)))
    stored_first = store.find_one("quotation_versions", {"_id": "signed-quote-v01"})
    assert stored_first["pdf"] == original_first_pdf
    assert stored_first["signature_snapshot"]["content"] == signature_a


def test_order_versions_capture_signatures_and_unsigned_versions_remain_explicit(app, version_upload_dir):
    tmp_path = version_upload_dir
    app.config["UPLOAD_DIRECTORY"] = tmp_path
    store = app.extensions["store"]
    actor, signature = actor_with_signature(tmp_path, "order-editor", "Order Editor", (50, 100, 150))
    order_v1 = document("signed-order", "ORD-SIGNED", 1)
    order_v2 = {**order_v1, "version": 2, "notes": "Changed"}

    with app.app_context():
        first = _record_order_version(store, order_v1, actor=actor, reason="Original working order")
        unsigned = _record_order_version(
            store, order_v2, actor={"_id": "unsigned", "name": "Unsigned"},
            reason="Unsigned revision", previous_version_id="signed-order-v01",
        )

    assert first["signature_snapshot"]["content"] == signature
    assert "Order Editor" in pdf_text(first["pdf"])
    assert unsigned["signature_snapshot"] is None
    assert first["pdf"] != unsigned["pdf"]


def test_webp_signature_snapshot_is_embedded_as_a_pdf_image(app, version_upload_dir):
    stream = BytesIO()
    PillowImage.new("RGB", (140, 45), (20, 140, 90)).save(stream, format="WEBP")
    content = stream.getvalue()
    relative = "signatures/webp-actor.webp"
    target = version_upload_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    actor = {
        "_id": "webp-actor", "name": "WebP Actor", "email": "webp@example.com",
        "signature_path": relative, "signature_filename": "webp-actor.webp",
        "signature_mime_type": "image/webp", "signature_size": len(content),
        "signature_width": 140, "signature_height": 45, "signature_updated_at": utcnow(),
    }
    app.config["UPLOAD_DIRECTORY"] = version_upload_dir

    with app.app_context():
        version = _record_quotation_version(app.extensions["store"], document("webp-quote", "QT-WEBP"), actor=actor, reason="Original quotation")

    assert version["signature_snapshot"]["content"] == content
    assert pdf_image_count(version["pdf"]) >= 1


def test_signature_file_endpoint_serves_existing_signature(app, authenticated, version_upload_dir):
    actor, content = actor_with_signature(version_upload_dir, "user-demo-admin", "Admin", (10, 80, 180))
    app.config["UPLOAD_DIRECTORY"] = version_upload_dir
    app.extensions["store"].update_one("users", {"_id": "user-demo-admin"}, actor)

    response = authenticated.get("/api/v1/profile/signature/file")

    assert response.status_code == 200
    assert response.headers["Content-Type"].startswith("image/png")
    assert response.data == content


def test_history_and_send_use_exact_saved_quotation_pdf(app, authenticated, monkeypatch):
    store = app.extensions["store"]
    store.update_one("users", {"_id": "user-demo-admin"}, {"email": "sender@monedatechnologies.com"})
    quote = store.insert_one("quotations", {
        **document("immutable-send-quote", "QT-IMMUTABLE", 1),
        "status": "Draft", "created_by_user_id": "user-demo-admin",
    })
    immutable_pdf = b"%PDF-immutable-version-bytes"
    store.insert_one("quotation_versions", {
        "_id": "immutable-send-quote-v01", "quotation_id": quote["_id"], "version": 1,
        "pdf": immutable_pdf, "filename": "QT-IMMUTABLE-V01.pdf", "signature_snapshot": None,
    })
    sent: dict = {}

    def capture_send(**kwargs):
        sent.update(kwargs)
        return {"id": "provider-id", "diagnostic_id": "diagnostic", "stage": "message_submission"}

    monkeypatch.setattr(app.extensions["email_service"], "send_quotation", capture_send)
    monkeypatch.setattr(
        app.extensions["email_service"].recipients, "resolved_for_quotation",
        lambda **_kwargs: {"to": ["customer@example.com"], "cc": [], "bcc": []},
    )
    historical = authenticated.get("/api/quotations/immutable-send-quote/history/immutable-send-quote-v01/pdf")
    response = authenticated.post("/api/quotations/immutable-send-quote/send", json={})

    assert historical.status_code == 200 and historical.data == immutable_pdf
    assert response.status_code == 200, response.json
    assert base64.b64decode(sent["attachments"][0]["content"]) == immutable_pdf


def test_order_pdf_cache_uses_the_saved_version_pdf(app):
    store = app.extensions["store"]
    order = store.insert_one("order_confirmations", {
        **document("immutable-order", "OC-IMMUTABLE", 1),
        "record_type": "ORDER_CONFIRMATION", "finalized": True,
    })
    immutable_pdf = b"%PDF-immutable-order-version"
    store.insert_one("order_versions", {
        "_id": "immutable-order-v01", "order_id": order["_id"], "version": 1,
        "pdf": immutable_pdf, "filename": "OC-IMMUTABLE-V01.pdf",
    })

    with app.test_request_context():
        content, _ = _ensure_order_pdf(order)

    assert content == immutable_pdf
    cached = store.find_one("order_documents", {"order_id": order["_id"]})
    assert base64.b64decode(cached["content_base64"]) == immutable_pdf


def test_workdrive_retry_uploads_the_original_version_pdf_without_regeneration(app):
    store = app.extensions["store"]
    immutable_pdf = b"%PDF-signed-at-version-creation"
    version = store.insert_one("quotation_versions", {
        "_id": "retry-version", "quotation_id": "retry-quote", "version": 1,
        "pdf": immutable_pdf, "filename": "QT-RETRY-V01.pdf",
    })

    class RetryArchive:
        def __init__(self):
            self.calls = 0
            self.received = None

        def archive_document_version(self, _user, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise WorkDriveError("SERVICE_ERROR", "temporary failure", retryable=True)
            self.received = kwargs["pdf"]
            return {"workdrive_file_id": "file", "workdrive_folder_id": "folder", "workdrive_sync_status": "SYNCED"}

    service = RetryArchive()
    first = archive_version(
        store, service, collection="quotation_versions", version=version, owner_user={"_id": "owner"},
        document_type="quote", document_id="retry-quote", document_number="QT-RETRY",
    )
    assert first["workdrive_sync_status"] == "FAILED"
    stored = store.find_one("quotation_versions", {"_id": "retry-version"})
    second = archive_version(
        store, service, collection="quotation_versions", version=stored, owner_user={"_id": "owner"},
        document_type="quote", document_id="retry-quote", document_number="QT-RETRY",
    )
    assert second["workdrive_sync_status"] == "SYNCED"
    assert service.received == immutable_pdf


def test_quote_workdrive_retry_returns_partial_failure_then_reuses_immutable_version(app, authenticated, monkeypatch):
    store = app.extensions["store"]
    quotation = store.insert_one("quotations", {
        "_id": "retry-route-quote", "quotation_number": "MT-RETRY-001",
        "customer_id": "company-moneda-demo", "created_by_user_id": "user-demo-admin",
    })
    version = store.insert_one("quotation_versions", {
        "_id": "retry-route-quote-v01", "quotation_id": quotation["_id"], "version": 1,
        "pdf": b"%PDF-immutable", "filename": "MT-RETRY-001-V01.pdf",
    })
    calls = []

    def archive(_store, _quotation, supplied_version):
        calls.append(supplied_version["pdf"])
        if len(calls) == 1:
            return {"workdrive_sync_status": "FAILED", "workdrive_sync_error": "DUAL_DESTINATION_SYNC_INCOMPLETE",
                    "workdrive_file_id": "user-file", "workdrive_customer_sync_status": "FAILED",
                    "workdrive_customer_sync_error": "AUTHENTICATION_ERROR"}
        return {"workdrive_sync_status": "SYNCED", "workdrive_file_id": "user-file",
                "workdrive_customer_sync_status": "SYNCED", "workdrive_customer_file_id": "company-file"}

    monkeypatch.setattr("app.quotations.routes._archive_quotation_version", archive)
    path = f"/api/v1/quotations/{quotation['_id']}/versions/{version['_id']}/workdrive/retry"
    first = authenticated.post(path)
    assert first.status_code == 503
    assert first.json["error"] == "WORKDRIVE_ARCHIVE_INCOMPLETE"
    assert first.json["archive"]["workdrive_file_id"] == "user-file"
    second = authenticated.post(path)
    assert second.status_code == 200
    assert second.json["data"]["workdrive_customer_file_id"] == "company-file"
    assert calls == [b"%PDF-immutable", b"%PDF-immutable"]
