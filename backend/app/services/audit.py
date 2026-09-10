from __future__ import annotations

from typing import Any

from flask import current_app, request, session
from app.repositories.store import utcnow


def audit(action: str, entity: str, entity_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    current_app.extensions["store"].insert_one("audit_logs", {
        "user_id": session.get("user_id"),
        "role": session.get("role_id"),
        "action": action,
        "entity": entity,
        "entity_id": entity_id,
        "timestamp": utcnow(),
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "user_agent": request.user_agent.string[:300],
        "metadata": metadata or {},
    })
