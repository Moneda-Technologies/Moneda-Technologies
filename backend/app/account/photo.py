from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from werkzeug.datastructures import FileStorage

from app.account.signature import ALLOWED_SIGNATURES, SignatureValidationError, _validate_image


def _photo_dir(upload_directory: str | os.PathLike[str]) -> Path:
    directory = Path(upload_directory).resolve() / "profile-photos"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def photo_metadata(user: dict[str, Any]) -> dict[str, Any] | None:
    path = user.get("photo_path")
    if not path:
        return None
    return {
        "filename": user.get("photo_filename") or Path(str(path)).name,
        "mime_type": user.get("photo_mime_type") or "image/png",
        "size": int(user.get("photo_size") or 0),
        "width": int(user.get("photo_width") or 0),
        "height": int(user.get("photo_height") or 0),
        "updated_at": user.get("photo_updated_at"),
    }


def photo_path(user: dict[str, Any], upload_directory: str | os.PathLike[str]) -> Path | None:
    stored = str(user.get("photo_path") or "").strip()
    if not stored:
        return None
    root = Path(upload_directory).resolve()
    candidate = (root / stored).resolve() if not Path(stored).is_absolute() else Path(stored).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def read_photo(user: dict[str, Any], upload_directory: str | os.PathLike[str]) -> tuple[dict[str, Any], bytes] | None:
    path = photo_path(user, upload_directory)
    metadata = photo_metadata(user)
    if not path or not metadata:
        return None
    try:
        return metadata, path.read_bytes()
    except OSError:
        return None


def save_photo(user_id: str, upload_directory: str | os.PathLike[str], upload: FileStorage) -> dict[str, Any]:
    mimetype = str(upload.mimetype or "").lower()
    filename = Path(str(upload.filename or "")).name
    extension = Path(filename).suffix.casefold()
    if extension not in set(ALLOWED_SIGNATURES.values()) and not (mimetype == "image/jpeg" and extension == ".jpeg"):
        raise SignatureValidationError("Use a PNG, JPG, JPEG or WEBP profile image with the correct file extension.")
    data = upload.read()
    width, height = _validate_image(data, mimetype)
    extension = ALLOWED_SIGNATURES[mimetype]
    safe_id = "".join(char for char in str(user_id) if char.isalnum() or char in "-_") or "user"
    directory = _photo_dir(upload_directory)
    target = directory / f"{safe_id}{extension}"
    for previous in directory.glob(f"{safe_id}.*"):
        if previous != target:
            previous.unlink(missing_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    return {
        "photo_path": str(target.relative_to(Path(upload_directory).resolve())),
        "photo_filename": filename or target.name,
        "photo_mime_type": mimetype,
        "photo_size": len(data),
        "photo_width": width,
        "photo_height": height,
    }


def remove_photo(user: dict[str, Any], upload_directory: str | os.PathLike[str]) -> bool:
    path = photo_path(user, upload_directory)
    if not path:
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False
