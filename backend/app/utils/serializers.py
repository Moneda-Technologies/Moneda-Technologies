from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

try:
    from bson import ObjectId
except ImportError:  # pragma: no cover
    ObjectId = str  # type: ignore[misc,assignment]


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if ObjectId is not str and isinstance(value, ObjectId):
        return str(value)
    return value

