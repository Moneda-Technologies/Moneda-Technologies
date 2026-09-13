from __future__ import annotations

from pathlib import Path

from app.finance.service import create_incentive_for_order
from app.finance.bank_details import can_edit_bank_details, can_view_bank_details
from app.services.seed import ensure_business_logic_schema
from app.services.business_logic import customer_ids_for_user, normalize_client_type, resolve_incentive_rate


def test_client_type_is_canonical_and_customer_api_exposes_it(authenticated):
    response = authenticated.post("/api/v1/customers", json={
        "company_name": "Dealer Profile Ltd", "contact_name": "Buyer", "email": "-", "phone": "-",
        "country_code": "IN", "payment_terms": "Advance", "address": "Mumbai", "client_type": "DEALER",
    })
    assert response.status_code == 201
    row = response.json["data"]
    assert row["client_type"] == "DEALER"
    listed = authenticated.get("/api/v1/customers").json["data"]["items"]
    assert next(item for item in listed if item["_id"] == row["_id"])["client_type"] == "DEALER"
    assert normalize_client_type("unknown") == "WHOLESALER"


def test_manager_scope_includes_direct_team_customers(app):
    store = app.extensions["store"]
    manager = store.insert_one("users", {"_id": "manager-scope", "name": "Manager", "email": "manager2@monedatechnologies.com", "role_id": "manager_sales_admin", "active": True})
    user = store.insert_one("users", {"_id": "team-user", "name": "Team user", "email": "team@monedatechnologies.com", "role_id": "user", "manager_id": manager["_id"], "active": True})
    store.insert_one("customers", {"_id": "team-customer", "name": "Team customer", "active": True, "status": "active", "assigned_user_ids": [user["_id"]]})
    assert "team-customer" in customer_ids_for_user(store, manager)


def test_manager_bank_details_are_limited_to_own_team(app):
    store = app.extensions["store"]
    manager = store.insert_one("users", {"_id": "bank-manager", "role_id": "manager_sales_admin", "active": True})
    team_user = store.insert_one("users", {"_id": "bank-team-user", "role_id": "user", "manager_id": manager["_id"], "active": True})
    other_manager = store.insert_one("users", {"_id": "bank-other-manager", "role_id": "manager_sales_admin", "active": True})
    other_user = store.insert_one("users", {"_id": "bank-other-user", "role_id": "user", "manager_id": other_manager["_id"], "active": True})
    assert can_view_bank_details(store, manager, manager["_id"])
    assert can_edit_bank_details(store, manager, manager["_id"])
    assert can_view_bank_details(store, manager, team_user["_id"])
    assert can_edit_bank_details(store, manager, team_user["_id"])
    assert not can_view_bank_details(store, manager, other_user["_id"])
    assert not can_edit_bank_details(store, manager, other_user["_id"])


def test_incentive_rule_seed_is_idempotent_and_preserves_rate(app):
    store = app.extensions["store"]
    key = {
        "allocation_type": "manager_override",
        "client_type": "WHOLESALER",
        "recipient_role": "manager_sales_admin",
        "category_id": "*",
    }
    rule = store.find_one("incentive_rules", key)
    assert rule is not None
    store.update_one("incentive_rules", {"_id": rule["_id"]}, {"rate": 6.0})
    before = store.count("incentive_rules")
    ensure_business_logic_schema(store, Path(app.config["DATA_DIRECTORY"]))
    ensure_business_logic_schema(store, Path(app.config["DATA_DIRECTORY"]))
    assert store.count("incentive_rules") == before
    assert store.find_one("incentive_rules", key)["rate"] == 6.0
    assert store.find_one("incentive_rules", {
        **key, "allocation_type": "creator", "recipient_role": "manager_sales_admin",
    }) is not None


def test_user_order_allocates_configured_manager_incentive(app):
    store = app.extensions["store"]
    manager = store.insert_one("users", {"_id": "incentive-manager", "name": "Manager", "email": "manager3@monedatechnologies.com", "role_id": "manager_sales_admin", "active": True})
    user = store.insert_one("users", {"_id": "incentive-user", "name": "Sales user", "email": "sales@monedatechnologies.com", "role_id": "user", "manager_id": manager["_id"], "active": True, "incentive_rates": {"blankets": 5}})
    order = {
        "_id": "oc-manager-incentive", "created_by_user_id": user["_id"], "created_by_role": "user",
        "manager_id_at_creation": manager["_id"], "manager_at_creation": {"_id": manager["_id"], "name": "Manager"},
        "client_type_at_creation": "WHOLESALER", "order_amount": 100.0, "customer_id": "customer-1",
        "products_snapshot": [{"product_id": "blanket-1", "product_name": "Blanket", "category_id": "blankets", "line_total": 100.0}],
    }
    created = create_incentive_for_order(store, order, user)
    assert created["gross_incentive_amount"] == 7.5
    assert {line["recipient_user_id"] for line in created["incentive_lines"]} == {user["_id"], manager["_id"]}


def test_manager_created_order_uses_manager_creator_rule(app):
    store = app.extensions["store"]
    manager = store.insert_one("users", {
        "_id": "manager-created", "name": "Manager", "email": "manager-created@monedatechnologies.com",
        "role_id": "manager_sales_admin", "active": True, "incentive_rates": {},
    })
    product = store.find_one("products", {"_id": "mtech-mpack"}) or {}
    order = {
        "_id": "oc-manager-created", "created_by_user_id": manager["_id"], "created_by_role": "manager_sales_admin",
        "customer_id": "customer-demo-1", "client_type_at_creation": "WHOLESALER", "order_amount": 100.0,
        "products_snapshot": [{"product_id": product.get("_id", "mtech-mpack"), "category_id": "mpacks", "line_total": 100.0}],
    }
    created = create_incentive_for_order(store, order, manager)
    assert created["gross_incentive_amount"] == 5.0
    assert created["incentive_lines"][0]["recipient_user_id"] == manager["_id"]
    assert created["incentive_lines"][0]["incentive_rate_snapshot"] == 5.0


def test_incentive_rule_priority_prefers_customer_then_type(app):
    store = app.extensions["store"]
    user = store.insert_one("users", {
        "_id": "priority-user", "name": "Sales", "email": "priority@monedatechnologies.com",
        "role_id": "user", "active": True, "incentive_rates": {},
    })
    store.insert_one("incentive_rules", {
        "_id": "priority-type", "allocation_type": "creator", "client_type": "DEALER",
        "recipient_role": "user", "category_id": "blankets", "rate": 5.5, "active": True,
    })
    store.insert_one("incentive_rules", {
        "_id": "priority-customer", "allocation_type": "creator", "customer_id": "customer-priority",
        "category_id": "blankets", "rate": 6.0, "active": True,
    })
    assert resolve_incentive_rate(store, recipient=user, client_type="DEALER", category_id="blankets", customer_id="customer-priority") == 6.0
    assert resolve_incentive_rate(store, recipient=user, client_type="DEALER", category_id="blankets", customer_id="other") == 5.5


def test_customer_and_product_type_combinations_resolve_independently(app):
    store = app.extensions["store"]
    user = store.insert_one("users", {
        "_id": "matrix-user", "name": "Matrix User",
        "email": "matrix@monedatechnologies.com", "role_id": "user",
        "active": True, "incentive_rates": {},
    })
    expected = {
        ("WHOLESALER", "blankets"): 1.0,
        ("WHOLESALER", "mpacks"): 1.5,
        ("DEALER", "blankets"): 2.0,
        ("DEALER", "mpacks"): 2.5,
        ("CUSTOMER", "blankets"): 3.0,
        ("CUSTOMER", "mpacks"): 3.5,
    }
    for (client_type, category_id), rate in expected.items():
        store.insert_one("incentive_rules", {
            "allocation_type": "creator", "client_type": client_type,
            "recipient_role": "user", "category_id": category_id,
            "rate": rate, "active": True,
        })
    for (client_type, category_id), rate in expected.items():
        assert resolve_incentive_rate(
            store, recipient=user, client_type=client_type,
            category_id=category_id, allocation_type="creator",
        ) == rate


def test_incentive_configurator_uses_catalog_hierarchy_and_resolver(app, authenticated):
    store = app.extensions["store"]
    manager = store.insert_one("users", {
        "_id": "configurator-manager", "name": "Config Manager",
        "email": "config-manager@monedatechnologies.com",
        "role_id": "manager_sales_admin", "active": True, "incentive_rates": {},
    })
    user = store.insert_one("users", {
        "_id": "configurator-user", "name": "Config User",
        "email": "config-user@monedatechnologies.com", "role_id": "user",
        "manager_id": manager["_id"], "active": True,
        "incentive_rates": {"blankets": 4.5, "mpacks": 4.0, "chemicals": 3.5},
    })

    response = authenticated.get("/api/v1/admin/incentive-configurator")
    assert response.status_code == 200
    data = response.json["data"]
    assert {row["id"] for row in data["product_types"]} == {"blankets", "mpacks", "chemicals"}
    assert all(row["name"] for row in data["product_types"])
    projected_user = next(row for row in data["users"] if row["_id"] == user["_id"])
    assert projected_user["manager"]["_id"] == manager["_id"]
    blanket = next(
        row for row in projected_user["configurations"]
        if row["customer_type"] == "WHOLESALER" and row["product_type_id"] == "blankets"
    )
    assert blanket["user_rate"] == 4.5
    assert blanket["manager_team_rate"] == resolve_incentive_rate(
        store, recipient=manager, client_type="WHOLESALER",
        category_id="blankets", allocation_type="manager_override",
    )
    projected_manager = next(row for row in data["managers"] if row["_id"] == manager["_id"])
    assert [row["_id"] for row in projected_manager["connected_users"]] == [user["_id"]]
    assert data["resolution"]["historical_snapshots_preserved"] is True


def test_product_incentive_configuration_upserts_category_rules_without_duplicates(authenticated):
    first = authenticated.post("/api/v1/admin/incentive-rules/configure-products", json={
        "allocation_type": "creator", "recipient_role": "user", "client_type": "DEALER",
        "rates": {"blankets": 5.0, "mpacks": 4.0, "chemicals": 6.0},
    })
    assert first.status_code == 200
    assert first.json["data"]["rates"] == {"blankets": 5.0, "mpacks": 4.0, "chemicals": 6.0}
    store = authenticated.application.extensions["store"]
    before = store.count("incentive_rules", {"allocation_type": "creator", "recipient_role": "user", "client_type": "DEALER", "category_id": {"$in": ["blankets", "mpacks", "chemicals"]}})
    second = authenticated.post("/api/v1/admin/incentive-rules/configure-products", json={
        "allocation_type": "creator", "recipient_role": "user", "client_type": "DEALER",
        "rates": {"blankets": 5.5},
    })
    assert second.status_code == 200
    assert store.count("incentive_rules", {"allocation_type": "creator", "recipient_role": "user", "client_type": "DEALER", "category_id": {"$in": ["blankets", "mpacks", "chemicals"]}}) == before
    assert store.find_one("incentive_rules", {"allocation_type": "creator", "recipient_role": "user", "client_type": "DEALER", "category_id": "blankets"})["rate"] == 5.5


def test_incentive_rule_status_update_preserves_unique_record(app, authenticated):
    store = app.extensions["store"]
    rule = store.find_one("incentive_rules", {
        "allocation_type": "manager_override", "client_type": "WHOLESALER",
        "recipient_role": "manager_sales_admin", "category_id": "*",
    })
    assert rule is not None
    before = store.count("incentive_rules")

    disabled = authenticated.patch(
        f"/api/v1/admin/incentive-rules/{rule['_id']}", json={"active": False},
    )
    assert disabled.status_code == 200
    assert disabled.json["data"]["active"] is False
    assert store.count("incentive_rules") == before
    invalid = authenticated.patch(
        f"/api/v1/admin/incentive-rules/{rule['_id']}", json={"active": "false"},
    )
    assert invalid.status_code == 422
    duplicate = authenticated.post("/api/v1/admin/incentive-rules", json={
        "allocation_type": "manager_override", "client_type": "WHOLESALER",
        "recipient_role": "manager_sales_admin", "category_id": "*", "rate": 3.0,
    })
    assert duplicate.status_code == 409
    assert store.count("incentive_rules") == before


def test_user_cannot_read_or_modify_incentive_configuration(app, client):
    store = app.extensions["store"]
    user = store.insert_one("users", {
        "_id": "restricted-incentive-user", "name": "Restricted User",
        "email": "restricted@monedatechnologies.com", "role_id": "user",
        "active": True, "device_access_mode": "any_authorized_device",
    })
    with client.session_transaction() as user_session:
        user_session["user_id"] = user["_id"]
        user_session["role_id"] = "user"
    assert client.get("/api/v1/admin/incentive-configurator").status_code == 403
    assert client.post("/api/v1/admin/incentive-rules", json={
        "allocation_type": "creator", "client_type": "DEALER",
        "recipient_role": "user", "category_id": "blankets", "rate": 4.0,
    }).status_code == 403


def test_mixed_product_incentive_snapshots_are_not_recalculated(app):
    store = app.extensions["store"]
    user = store.insert_one("users", {
        "_id": "mixed-incentive-user", "name": "Mixed User",
        "email": "mixed@monedatechnologies.com", "role_id": "user", "active": True,
        "incentive_rates": {"blankets": 5.0, "mpacks": 3.0, "chemicals": 1.0},
    })
    order = {
        "_id": "oc-mixed-incentives", "created_by_user_id": user["_id"],
        "created_by_role": "user", "client_type_at_creation": "DEALER",
        "customer_id": "customer-demo-1", "order_amount": 300.0,
        "products_snapshot": [
            {"product_id": "blanket-1", "category_id": "blankets", "line_total": 100.0},
            {"product_id": "mtech-mpack", "category_id": "mpacks", "line_total": 100.0},
            {"product_id": "chemical-1", "category_id": "chemicals", "line_total": 100.0},
        ],
    }
    created = create_incentive_for_order(store, order, user)
    assert [row["incentive_rate_snapshot"] for row in created["incentive_lines"]] == [5.0, 3.0, 1.0]
    assert created["gross_incentive_amount"] == 9.0
    assert created["client_type_snapshot"] == "DEALER"

    store.update_one("users", {"_id": user["_id"]}, {
        "incentive_rates": {"blankets": 1.0, "mpacks": 1.0, "chemicals": 1.0},
    })
    unchanged = create_incentive_for_order(store, order, store.find_one("users", {"_id": user["_id"]}))
    assert unchanged["_id"] == created["_id"]
    assert [row["incentive_rate_snapshot"] for row in unchanged["incentive_lines"]] == [5.0, 3.0, 1.0]
    assert unchanged["gross_incentive_amount"] == 9.0
