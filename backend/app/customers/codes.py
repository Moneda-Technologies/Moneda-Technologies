from __future__ import annotations

import re
from typing import Any


def customer_code(name: str) -> str:
    """Return a short, stable identifier suitable for quotation numbers."""
    words = re.findall(r"[A-Za-z0-9]+", str(name or "").upper())
    if not words:
        return "CUSTOMER"
    if len(words) > 1 and words[-1] in {"DEMO", "TEST"}:
        return "-".join(words[:1] + words[-1:])[:24]
    if len(words[0]) <= 3 and len(words) > 1:
        return "-".join(words[:2])[:24]
    return words[0][:24]


def available_customer_code(store: Any, name: str, customer_id: str | None = None) -> str:
    """Allocate a readable code without changing an existing customer's code."""
    base_code = customer_code(name)
    code = base_code
    suffix = 2
    while existing := store.find_one("customers", {"customer_code": code}):
        if customer_id and str(existing.get("_id")) == str(customer_id):
            return code
        code = f"{base_code[:20]}-{suffix}"
        suffix += 1
    return code
