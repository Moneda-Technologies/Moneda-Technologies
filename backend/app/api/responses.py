from __future__ import annotations

from typing import Any

from flask import jsonify

from app.utils.serializers import json_safe


def success(data: Any = None, message: str | None = None, status: int = 200):
    return jsonify({"success": True, "data": json_safe(data), "message": message, "errors": []}), status


def failure(message: str, errors: list[Any] | None = None, status: int = 400, error: str | None = None, **metadata: Any):
    payload = {"success": False, "data": None, "message": message, "errors": errors or []}
    if error:
        payload["error"] = error
    payload.update({key: value for key, value in metadata.items() if value is not None})
    return jsonify(payload), status
