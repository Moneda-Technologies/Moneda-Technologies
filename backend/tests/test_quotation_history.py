from datetime import datetime, timezone


def _quotation(number: str, owner: str, customer_id: str, total: float, currency: str = "INR") -> dict:
    return {
        "_id": number.lower(), "quotation_number": number, "created_by_user_id": owner,
        "user_id": owner, "customer_id": customer_id, "currency": currency, "status": "Sent",
        "created_at": datetime(2026, 9, 7, tzinfo=timezone.utc),
        "customer_snapshot": {"company_name": "Northstar Printworks" if customer_id.endswith("1") else "Orbit Packaging", "name": "Customer", "email": "customer@example.com", "country_code": "IN", "country_name": "India", "continent": "Asia"},
        "totals": {"grand_total": total}, "lines": [{"product_name": "Line"}],
    }


def test_quotation_history_is_independent_of_active_customer_and_server_filtered(app, authenticated):
    store = app.extensions["store"]
    store.insert_one("quotations", _quotation("MT-NORTHSTAR-001", "user-demo-admin", "customer-demo-1", 1200))
    store.insert_one("quotations", _quotation("MT-ORBIT-001", "user-demo-admin", "customer-demo-2", 2200))
    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "user"})

    cleared = authenticated.post("/api/v1/companies/clear-customer", json={})
    assert cleared.status_code == 200
    response = authenticated.get("/api/v1/quotations?currency=INR&min_total=2000")
    assert response.status_code == 200
    assert [row["quotation_number"] for row in response.json["data"]["items"]] == ["MT-ORBIT-001"]
    assert response.json["data"]["scope"] == "own"

    no_currency = authenticated.get("/api/v1/quotations?min_total=10")
    assert no_currency.status_code == 422


def test_global_quotation_permission_sees_all_users_without_customer_selection(app, authenticated):
    store = app.extensions["store"]
    store.insert_one("quotations", _quotation("MT-USER-001", "another-user", "customer-demo-1", 100))
    store.insert_one("quotations", _quotation("MT-ADMIN-001", "user-demo-admin", "customer-demo-2", 200))
    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "superadmin"})
    authenticated.post("/api/v1/companies/clear-customer", json={})
    response = authenticated.get("/api/v1/quotations")
    assert response.status_code == 200
    numbers = {row["quotation_number"] for row in response.json["data"]["items"]}
    assert {"MT-USER-001", "MT-ADMIN-001"}.issubset(numbers)
    assert response.json["data"]["scope"] == "all"
