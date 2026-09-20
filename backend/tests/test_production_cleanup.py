from __future__ import annotations

from werkzeug.security import generate_password_hash


def _login(client, user_id: str) -> None:
    with client.session_transaction() as session:
        session["user_id"] = user_id


def _user(store, user_id: str, role_id: str, *, customers: list[str] | None = None) -> dict:
    return store.insert_one("users", {
        "_id": user_id,
        "name": user_id,
        "username": user_id,
        "username_normalized": user_id,
        "email": f"{user_id}@example.com",
        "password_hash": generate_password_hash("Secure123"),
        "role_id": role_id,
        "active": True,
        "email_verified": True,
        "assigned_customer_ids": customers or [],
        "device_access_mode": "any_authorized_device",
    })


def test_roles_endpoints_are_explicitly_superadmin_only(app, client):
    store = app.extensions["store"]
    for role in ("admin", "manager_sales_admin", "user"):
        user_id = f"roles-denied-{role}"
        _user(store, user_id, role)
        _login(client, user_id)
        assert client.get("/api/v1/admin/roles").status_code == 403
        assert client.patch("/api/v1/admin/roles/user", json={"display_name": "No"}).status_code == 403

    _login(client, "user-demo-admin")
    roles = client.get("/api/v1/admin/roles")
    assert roles.status_code == 200
    assert roles.json["data"]["items"]


def test_user_role_options_do_not_expose_permissions(app, client):
    store = app.extensions["store"]
    _user(store, "role-options-admin", "admin")
    _login(client, "role-options-admin")
    response = client.get("/api/v1/admin/user-role-options")
    assert response.status_code == 200
    assert all("permissions" not in row for row in response.json["data"]["items"])
    assert "superadmin" not in {row["_id"] for row in response.json["data"]["items"]}


def test_working_order_delete_is_superadmin_only_and_dependency_safe(app, client):
    store = app.extensions["store"]
    safe = store.insert_one("orders", {
        "_id": "safe-working-order-delete",
        "order_number": "MT-ORD-SAFE-001",
        "record_type": "ORDER",
        "order_kind": "WORKING",
        "lifecycle_state": "WORKING",
        "customer_id": "company-moneda-demo",
        "status": "Working",
    })
    _user(store, "order-delete-admin", "admin")
    _login(client, "order-delete-admin")
    assert client.delete(f"/api/v1/orders/{safe['_id']}", json={"reason": "Denied"}).status_code == 403

    _login(client, "user-demo-admin")
    deleted = client.delete(f"/api/v1/orders/{safe['_id']}", json={"reason": "Obsolete draft"})
    assert deleted.status_code == 200
    assert deleted.json["data"]["record_type"] == "working_order"
    assert store.find_one("audit_logs", {"action": "WORKING_ORDER_DELETED", "entity_id": safe["_id"]})

    linked = store.insert_one("orders", {
        "_id": "linked-working-order-delete",
        "order_number": "MT-ORD-LINKED-001",
        "record_type": "ORDER",
        "order_kind": "WORKING",
        "lifecycle_state": "FINALIZED",
        "customer_id": "company-moneda-demo",
        "status": "Finalized",
    })
    store.insert_one("order_confirmations", {
        "_id": "linked-final-oc-delete",
        "order_number": "MT-OC-LINKED-001",
        "record_type": "ORDER_CONFIRMATION",
        "source_order_id": linked["_id"],
        "customer_id": "company-moneda-demo",
        "status": "Finalized",
    })
    blocked = client.delete(f"/api/v1/orders/{linked['_id']}", json={"reason": "Must fail"})
    assert blocked.status_code == 409
    assert blocked.json["error"] == "ORDER_HAS_FINAL_CONFIRMATION"


def test_payment_summary_uses_authorized_final_ocs(app, client):
    store = app.extensions["store"]
    customer_id = "finance-summary-customer"
    user_id = "finance-summary-user"
    store.insert_one("customers", {"_id": customer_id, "name": "Finance Summary", "assigned_user_ids": [user_id], "active": True})
    _user(store, user_id, "user", customers=[customer_id])
    current = store.insert_one("order_confirmations", {
        "_id": "finance-summary-current-oc",
        "order_number": "MT-OC-SUMMARY-001",
        "record_type": "ORDER_CONFIRMATION",
        "source_order_id": "finance-summary-source-order",
        "customer_id": customer_id,
        "order_amount": 200,
        "totals": {"grand_total": 200},
        "status": "Finalized",
    })
    legacy = store.insert_one("orders", {
        "_id": "finance-summary-legacy-oc",
        "order_number": "MT-OC-SUMMARY-LEGACY",
        "customer_id": customer_id,
        "order_amount": 100,
        "totals": {"grand_total": 100},
        "status": "Pending",
    })
    store.insert_one("payments", {"_id": "finance-summary-confirmed", "order_id": current["_id"], "customer_id": customer_id, "amount": 50, "status": "CONFIRMED"})
    store.insert_one("payments", {"_id": "finance-summary-awaiting", "order_id": legacy["_id"], "customer_id": customer_id, "amount": 25, "status": "AWAITING SUPERADMIN CONFIRMATION"})
    _login(client, user_id)
    response = client.get("/api/v1/payments?limit=500")
    assert response.status_code == 200
    summary = response.json["data"]["summary"]
    assert summary["total_payments"] == 2
    assert summary["confirmed_received"] == 50
    assert summary["awaiting_confirmation"] == 1
    assert summary["outstanding"] == 250
    assert summary["customer_credit"] == 0
    assert summary["invoice_count"] == 2


def test_confirmation_origin_contract_distinguishes_linked_and_historical(app, authenticated):
    store = app.extensions["store"]
    working = store.insert_one("orders", {
        "_id": "origin-working-order",
        "order_number": "MT-ORD-ORIGIN-001",
        "record_type": "ORDER",
        "order_kind": "WORKING",
        "lifecycle_state": "FINALIZED",
        "customer_id": "company-moneda-demo",
        "status": "Finalized",
    })
    linked = store.insert_one("order_confirmations", {
        "_id": "origin-linked-oc",
        "order_number": "MT-OC-ORIGIN-001",
        "record_type": "ORDER_CONFIRMATION",
        "source_order_id": working["_id"],
        "customer_id": "company-moneda-demo",
        "status": "Finalized",
    })
    historical = store.insert_one("orders", {
        "_id": "origin-historical-oc",
        "order_number": "MT-OC-ORIGIN-LEGACY",
        "customer_id": "company-moneda-demo",
        "status": "Pending",
    })
    items = authenticated.get("/api/v1/order-confirmations?limit=100").json["data"]["items"]
    by_id = {row["_id"]: row for row in items}
    assert by_id[linked["_id"]]["confirmation_type"] == "LINKED_FINAL_OC"
    assert by_id[linked["_id"]]["is_legacy_confirmation"] is False
    assert by_id[historical["_id"]]["confirmation_type"] == "HISTORICAL_OC"
    assert by_id[historical["_id"]]["is_legacy_confirmation"] is True
