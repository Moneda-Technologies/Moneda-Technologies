from __future__ import annotations

from pathlib import Path

from werkzeug.security import generate_password_hash

from app.services.seed import ensure_business_logic_schema


def _login_as(client, user_id: str) -> None:
    with client.session_transaction() as session:
        session["user_id"] = user_id


def _user(store, user_id: str, role_id: str) -> dict:
    return store.insert_one("users", {
        "_id": user_id,
        "name": user_id,
        "username": user_id,
        "username_normalized": user_id,
        "email": f"{user_id}@monedatechnologies.com",
        "password_hash": generate_password_hash("Secure123"),
        "role_id": role_id,
        "active": True,
        "email_verified": True,
        "device_access_mode": "any_authorized_device",
    })


def test_superadmin_can_delete_oc_and_lower_roles_remain_denied(app, client):
    store = app.extensions["store"]
    order = store.insert_one("orders", {
        "_id": "oc-delete-authz-test",
        "order_number": "MT-OC-DELETE-AUTHZ-001",
        "customer_id": "company-moneda-demo",
        "customer_company_id": "company-moneda-demo",
        "order_amount": 100,
        "totals": {"grand_total": 100},
        "status": "Pending",
    })
    # A voided payment is retained for the audit path and no longer blocks OC
    # deletion. Active payment blocking is covered by the dedicated flow tests.
    payment = store.insert_one("payments", {"_id": "oc-delete-payment", "order_id": order["_id"], "status": "VOIDED", "amount": 100})
    incentive = store.insert_one("incentives", {"_id": "oc-delete-incentive", "order_id": order["_id"], "status": "PENDING PAYMENT"})

    _login_as(client, "user-demo-admin")
    listed = client.get("/api/v1/orders")
    assert listed.status_code == 200
    dashboard_before_delete = client.get("/api/v1/dashboard")
    assert dashboard_before_delete.status_code == 200
    orders_before_delete = dashboard_before_delete.json["data"]["metrics"]["orders"]
    deleted = client.delete(f"/api/v1/orders/{order['_id']}", json={"reason": "Regression test"})
    assert deleted.status_code == 200
    assert deleted.json["data"]["status"] == "Deleted"
    assert store.find_one("orders", {"_id": order["_id"]})["status"] == "Deleted"
    assert store.find_one("payments", {"_id": payment["_id"]}) is not None
    assert store.find_one("incentives", {"_id": incentive["_id"]}) is not None
    assert store.find_one("audit_logs", {"action": "ORDER_CONFIRMATION_DELETED", "entity_id": order["_id"]}) is not None
    assert client.get(f"/api/v1/orders/{order['_id']}").status_code == 404
    assert all(row.get("_id") != order["_id"] for row in client.get("/api/v1/orders").json["data"]["items"])
    dashboard_after_delete = client.get("/api/v1/dashboard")
    assert dashboard_after_delete.status_code == 200
    assert dashboard_after_delete.json["data"]["metrics"]["orders"] == orders_before_delete

    for role_id, user_id in (("manager_sales_admin", "oc-delete-manager"), ("user", "oc-delete-user")):
        _user(store, user_id, role_id)
        _login_as(client, user_id)
        denied = client.delete(f"/api/v1/orders/{order['_id']}", json={})
        assert denied.status_code == 403


def test_order_delete_permission_repair_is_idempotent(app):
    store = app.extensions["store"]
    role = store.find_one("roles", {"_id": "superadmin"})
    assert role is not None
    permissions = sorted(set(role.get("permissions") or []) - {"orders.delete"})
    store.update_one("roles", {"_id": "superadmin"}, {"permissions": permissions})
    store.delete_one("system_migrations", {"_id": "order-confirmation-delete-permission-v3"})
    ensure_business_logic_schema(store, Path(app.config["DATA_DIRECTORY"]))
    assert "orders.delete" in set(store.find_one("roles", {"_id": "superadmin"})["permissions"])
    ensure_business_logic_schema(store, Path(app.config["DATA_DIRECTORY"]))
    assert "orders.delete" in set(store.find_one("roles", {"_id": "superadmin"})["permissions"])
