from __future__ import annotations

import mimetypes
import os
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4
import fitz
from PIL import Image

from werkzeug.datastructures import FileStorage

from app.account.signature import SignatureValidationError, _validate_image

MAX_COMPANY_ASSET_BYTES = 10 * 1024 * 1024
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp"}
DOCUMENT_TYPES = {"application/pdf", *IMAGE_TYPES}


def _safe_filename(value: str) -> str:
    name = Path(value or "asset").name.strip() or "asset"
    return "".join(char for char in name if char.isalnum() or char in " ._-()")[:180] or "asset"


def _asset_root(upload_directory: str | os.PathLike[str], customer_id: str) -> Path:
    root = (Path(upload_directory).resolve() / "company-assets" / "".join(c for c in customer_id if c.isalnum() or c in "-_"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_company_upload(upload: FileStorage, category: str) -> tuple[bytes, str, str]:
    filename = _safe_filename(str(upload.filename or "asset"))
    data = upload.read(MAX_COMPANY_ASSET_BYTES + 1)
    if not data or len(data) > MAX_COMPANY_ASSET_BYTES:
        raise SignatureValidationError("Company assets must be non-empty and no larger than 10 MB.")
    mime = str(upload.mimetype or mimetypes.guess_type(filename)[0] or "application/octet-stream").lower()
    if mime not in DOCUMENT_TYPES:
        raise SignatureValidationError("Company assets must be PNG, JPG, WEBP, or PDF files.")
    if mime == "application/pdf" and not data.startswith(b"%PDF-"):
        raise SignatureValidationError("The uploaded PDF is invalid.")
    if mime in IMAGE_TYPES:
        _validate_image(data, mime)
    if category == "logo" and mime == "application/pdf":
        raise SignatureValidationError("A company logo must be an image.")
    return data, mime, filename


def persist_company_asset(upload_directory: str | os.PathLike[str], customer_id: str,
                          upload: FileStorage, category: str, uploader_id: str,
                          store: Any, workdrive_enabled: bool = False) -> dict[str, Any]:
    data, mime, filename = validate_company_upload(upload, category)
    asset_id = str(uuid4())
    suffix = Path(filename).suffix.lower() or (".pdf" if mime == "application/pdf" else ".bin")
    relative = Path("company-assets") / "".join(c for c in customer_id if c.isalnum() or c in "-_") / f"{asset_id}{suffix}"
    target = (Path(upload_directory).resolve() / relative).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    thumbnail_relative = None
    if category == "logo" and mime in IMAGE_TYPES:
        # Keep the original upload untouched.  Avatar rendering uses a
        # normalized, tightly framed preview so transparent padding or an
        # oversized source canvas cannot make the logo appear clipped/tiny.
        try:
            with Image.open(BytesIO(data)) as source:
                image = source.convert("RGBA")
                alpha = image.getchannel("A")
                bbox = alpha.getbbox()
                if bbox:
                    image = image.crop(bbox)
                elif image.getbbox():
                    image = image.crop(image.getbbox())
                image.thumbnail((512, 512), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (512, 512), (255, 255, 255, 255))
                offset = ((512 - image.width) // 2, (512 - image.height) // 2)
                canvas.alpha_composite(image, offset)
                thumbnail_relative = relative.with_name(f"{asset_id}-avatar.png")
                thumbnail_target = (Path(upload_directory).resolve() / thumbnail_relative)
                canvas.convert("RGB").save(thumbnail_target, format="PNG", optimize=True)
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise SignatureValidationError("The company logo could not be prepared for display.") from exc
    if mime == "application/pdf":
        try:
            document = fitz.open(stream=data, filetype="pdf")
            if document.page_count < 1:
                raise ValueError("PDF has no pages")
            pixmap = document.load_page(0).get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            thumbnail_relative = relative.with_name(f"{asset_id}-thumbnail.png")
            thumbnail_target = (Path(upload_directory).resolve() / thumbnail_relative).resolve()
            pixmap.save(str(thumbnail_target))
            document.close()
        except Exception as exc:
            target.unlink(missing_ok=True)
            raise SignatureValidationError("The uploaded PDF could not be rendered.") from exc
    now = __import__("app.repositories.store", fromlist=["utcnow"]).utcnow()
    row = store.insert_one("company_assets", {
        "_id": asset_id, "customer_id": customer_id, "category": category,
        "filename": filename, "mime_type": mime, "size": len(data),
        "storage_path": str(relative), "uploaded_by_user_id": uploader_id,
        "thumbnail_path": str(thumbnail_relative) if thumbnail_relative else None,
        "created_at": now, "updated_at": now, "sync_status": "PENDING" if workdrive_enabled else "LOCAL_ONLY",
        "workdrive_resource_id": None, "workdrive_folder_id": None,
    })
    return row
