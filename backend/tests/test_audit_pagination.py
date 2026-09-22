from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_audit_logs_filters_before_server_side_pagination(app, authenticated):
    store = app.extensions["store"]
    base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    for index in range(130):
        store.insert_one("audit_logs", {
            "_id": f"pagination-audit-{index:03d}",
            "action": "pagination_test_action",
            "entity_type": "pagination_test_resource",
            "entity_id": f"resource-{index:03d}",
            "user_id": "user-demo-admin",
            "status": "RECORDED",
            "ip_address": "127.0.0.1",
            "created_at": base + timedelta(minutes=index),
        })

    response = authenticated.get(
        "/api/admin/audit-logs?action=pagination_test_action&page=2&page_size=50"
    )

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["total"] == 130
    assert payload["page"] == 2
    assert payload["page_size"] == 50
    assert payload["pages"] == 3
    assert payload["has_next"] is True
    assert len(payload["items"]) == 50
    assert payload["items"][0]["entity_id"] == "resource-079"
    assert payload["items"][-1]["entity_id"] == "resource-030"

    filtered = authenticated.get(
        "/api/admin/audit-logs?search=RESOURCE-129&from_date=2026-09-01&to_date=2026-09-01"
    )
    assert filtered.status_code == 200
    filtered_payload = filtered.get_json()["data"]
    assert filtered_payload["total"] == 1
    assert filtered_payload["items"][0]["entity_id"] == "resource-129"


def test_audit_logs_enforces_page_size_and_validates_numbers(authenticated):
    response = authenticated.get("/api/admin/audit-logs?page=1&page_size=500")
    assert response.status_code == 200
    assert response.get_json()["data"]["page_size"] == 100

    invalid = authenticated.get("/api/admin/audit-logs?page=invalid&page_size=25")
    assert invalid.status_code == 422


def test_audit_logs_requires_authorization(client):
    assert client.get("/api/admin/audit-logs").status_code == 401
