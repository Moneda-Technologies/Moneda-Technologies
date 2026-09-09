def test_crm_is_company_wide_without_selected_customer_and_resolves_identity(app, authenticated):
    store = app.extensions["store"]
    store.insert_one("leads", {
        "_id": "crm-linked", "customer_id": "customer-demo-1", "owner_user_id": "user-demo-admin",
        "title": "Linked opportunity", "status": "Lead", "value_eur": 1250,
    })
    store.insert_one("leads", {
        "_id": "crm-unlinked", "owner_user_id": "user-demo-admin",
        "title": "Unlinked prospect", "status": "Follow Up", "value_eur": 400,
    })
    assert authenticated.post("/api/companies/clear-customer", json={}).status_code == 200
    response = authenticated.get("/api/v1/leads")
    assert response.status_code == 200
    rows = {row["_id"]: row for row in response.json["data"]["items"]}
    assert rows["crm-linked"]["customer_name"]
    assert rows["crm-linked"]["owner_name"] == "Superadmin"
    assert rows["crm-linked"]["owner_initials"] == "SU"
    assert rows["crm-unlinked"]["customer_name"] is None
    assert rows["crm-unlinked"]["owner_initials"] == "SU"
    assert all(row["owner_initials"] != "E7" for row in rows.values())


def test_crm_customer_filter_and_lead_creation_do_not_require_header_context(app, authenticated):
    store = app.extensions["store"]
    store.insert_one("leads", {
        "_id": "crm-filtered", "customer_id": "customer-demo-1", "owner_user_id": "user-demo-admin",
        "title": "Filtered opportunity", "status": "Won", "value_eur": 5000,
    })
    assert authenticated.post("/api/companies/clear-customer", json={}).status_code == 200
    response = authenticated.get("/api/v1/leads?customer_id=customer-demo-1&min_value=1000")
    assert response.status_code == 200
    assert [row["_id"] for row in response.json["data"]["items"]] == ["crm-filtered"]
    created = authenticated.post("/api/v1/leads", json={"title": "Company-wide prospect", "value_eur": 850})
    assert created.status_code == 201
    assert created.json["data"]["customer_id"] is None
    assert created.json["data"]["owner_user_id"] == "user-demo-admin"


def test_user_cannot_change_own_role(app, authenticated):
    response = authenticated.patch("/api/v1/admin/users/user-demo-admin", json={"role_id": "user"})
    assert response.status_code == 403


def test_dashboard_returns_company_metrics_without_customer_context(app, authenticated):
    assert authenticated.post("/api/companies/clear-customer", json={}).status_code == 200
    response = authenticated.get("/api/v1/dashboard")
    assert response.status_code == 200
    assert "metrics" in response.json["data"]
