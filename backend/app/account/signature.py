from __future__ import annotations

import os
import struct
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage


MAX_SIGNATURE_BYTES = 2 * 1024 * 1024
MAX_SIGNATURE_WIDTH = 4000
MAX_SIGNATURE_HEIGHT = 2000
ALLOWED_SIGNATURES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}


class SignatureValidationError(ValueError):
    pass


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 3 < len(data):
        while offset < len(data) and data[offset] != 0xFF:
            offset += 1
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in (0xD8, 0xD9):
            continue
        if offset + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[offset:offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and segment_length >= 7:
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8L" and len(data) >= 25 and data[21] == 0x2F:
        bits = int.from_bytes(data[22:26], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def _validate_image(data: bytes, mimetype: str) -> tuple[int, int]:
    if mimetype not in ALLOWED_SIGNATURES:
        raise SignatureValidationError("Use a PNG, JPG, JPEG or WEBP signature image.")
    if not data:
        raise SignatureValidationError("Choose a signature image.")
    if len(data) > MAX_SIGNATURE_BYTES:
        raise SignatureValidationError("Signature images must be 2 MB or smaller.")
    dimensions = {"image/png": _png_dimensions, "image/jpeg": _jpeg_dimensions, "image/webp": _webp_dimensions}[mimetype](data)
    if not dimensions or dimensions[0] <= 0 or dimensions[1] <= 0:
        raise SignatureValidationError("The signature image is invalid or unreadable.")
    if dimensions[0] > MAX_SIGNATURE_WIDTH or dimensions[1] > MAX_SIGNATURE_HEIGHT:
        raise SignatureValidationError("Signature images must be no larger than 4000 × 2000 pixels.")
    return dimensions


def _signature_dir(upload_directory: str | os.PathLike[str]) -> Path:
    directory = Path(upload_directory).resolve() / "signatures"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def signature_metadata(user: dict[str, Any]) -> dict[str, Any] | None:
    path = user.get("signature_path")
    if not path:
        return None
    return {
        "filename": user.get("signature_filename") or Path(str(path)).name,
        "mime_type": user.get("signature_mime_type") or "image/png",
        "size": int(user.get("signature_size") or 0),
        "width": int(user.get("signature_width") or 0),
        "height": int(user.get("signature_height") or 0),
        "updated_at": user.get("signature_updated_at"),
    }


def signature_path(user: dict[str, Any], upload_directory: str | os.PathLike[str]) -> Path | None:
    stored = str(user.get("signature_path") or "").strip()
    if not stored:
        return None
    root = Path(upload_directory).resolve()
    candidate = (root / stored).resolve() if not Path(stored).is_absolute() else Path(stored).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def read_signature(user: dict[str, Any], upload_directory: str | os.PathLike[str]) -> tuple[dict[str, Any], bytes] | None:
    path = signature_path(user, upload_directory)
    metadata = signature_metadata(user)
    if not path or not metadata:
        return None
    try:
        return metadata, path.read_bytes()
    except OSError:
        return None


def save_signature(user_id: str, upload_directory: str | os.PathLike[str], upload: FileStorage) -> dict[str, Any]:
    mimetype = str(upload.mimetype or "").lower()
    filename = Path(str(upload.filename or "")).name
    extension = Path(filename).suffix.casefold()
    if extension not in set(ALLOWED_SIGNATURES.values()) and not (mimetype == "image/jpeg" and extension == ".jpeg"):
        raise SignatureValidationError("Use a PNG, JPG, JPEG or WEBP signature image with the correct file extension.")
    data = upload.read()
    width, height = _validate_image(data, mimetype)
    extension = ALLOWED_SIGNATURES[mimetype]
    safe_id = "".join(char for char in str(user_id) if char.isalnum() or char in "-_") or "user"
    directory = _signature_dir(upload_directory)
    target = directory / f"{safe_id}{extension}"
    for previous in directory.glob(f"{safe_id}.*"):
        if previous != target:
            previous.unlink(missing_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return {
        "signature_path": str(target.relative_to(Path(upload_directory).resolve())),
        "signature_filename": filename or target.name,
        "signature_mime_type": mimetype,
        "signature_size": len(data),
        "signature_width": width,
        "signature_height": height,
    }


def restore_signature(user_id: str, upload_directory: str | os.PathLike[str], data: bytes,
                      filename: str, mimetype: str) -> dict[str, Any]:
    """Persist a validated WorkDrive-restored signature without changing its bytes."""
    mimetype = str(mimetype or "").lower()
    width, height = _validate_image(data, mimetype)
    extension = ALLOWED_SIGNATURES[mimetype]
    safe_id = "".join(char for char in str(user_id) if char.isalnum() or char in "-_") or "user"
    directory = _signature_dir(upload_directory)
    target = directory / f"{safe_id}{extension}"
    for previous in directory.glob(f"{safe_id}.*"):
        if previous != target:
            previous.unlink(missing_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return {
        "signature_path": str(target.relative_to(Path(upload_directory).resolve())),
        "signature_filename": Path(filename or target.name).name,
        "signature_mime_type": mimetype, "signature_size": len(data),
        "signature_width": width, "signature_height": height,
    }


def remove_signature(user: dict[str, Any], upload_directory: str | os.PathLike[str]) -> bool:
    path = signature_path(user, upload_directory)
    if not path:
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
