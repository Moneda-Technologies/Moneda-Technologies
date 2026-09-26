from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any

from app.account.signature import read_signature
from app.repositories.store import utcnow


def capture_signature_snapshot(actor: dict[str, Any], upload_directory: str | Path) -> dict[str, Any] | None:
    """Capture the actor's current local signature as immutable version data."""
    loaded = read_signature(actor, upload_directory)
    if not loaded:
        return None
    metadata, content = loaded
    return {
        "actor_user_id": actor.get("_id"),
        "actor_name": actor.get("name"),
        "actor_email": actor.get("email"),
        "original_filename": metadata.get("filename"),
        "mime_type": metadata.get("mime_type"),
        "width": metadata.get("width"),
        "height": metadata.get("height"),
        "captured_at": utcnow(),
        "content_sha256": sha256(content).hexdigest(),
        "content": content,
    }


def public_signature_snapshot(snapshot: Any) -> dict[str, Any] | None:
    """Return safe signature metadata without exposing image bytes in JSON."""
    if not isinstance(snapshot, dict):
        return None
    return {key: value for key, value in snapshot.items() if key != "content"}
