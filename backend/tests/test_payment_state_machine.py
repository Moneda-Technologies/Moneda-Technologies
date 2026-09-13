from __future__ import annotations

from werkzeug.security import generate_password_hash

from app.finance.service import repair_payment_states


def _order(store, order_id: str, customer_id: str, amount: float = 458.70, **extra):
    return store.insert_one("orders", {
        "_id": order_id,
        "order_number": f"MT-OC-{order_id}",
        "customer_id": customer_id,
        "customer_company_id": customer_id,
        "company_id": customer_id,
        "order_amount": amount,
        "totals": {"grand_total": amount},
        "currency": "EUR",
        "payment_terms": "Advance",
        "oc_date": "2026-09-12",
        "status": "Pending",
        **extra,
    })


def _banking_payload(order: dict, customer_id: str, amount: float, *, payment_date: str = "2026-09-12"):
    return {
        "workflow": "banking",
        "customer_id": customer_id,
        "order_id": order["_id"],
        "amount": amount,
        "currency": "EUR",
        "payment_date": payment_date,
        "payment_mode": "Bank Transfer",
        "bank_name": "Test Bank",
        "bank_account": "Account 001",
        "utr": f"UTR-{order['_id']}-{amount}",
        "reference_number": f"REF-{order['_id']}-{amount}",
        "attachment": {
            "name": "proof.png",
            "type": "image/png",
            "size": 1,
            "data": "data:image/png;base64,AA==",
        },
    }


def _login_as(client, user_id: str):
    with client.session_transaction() as session:
        session["user_id"] = user_id


def test_full_payment_does_not_pay_oc_until_superadmin_confirms(app, authenticated):
    store = app.extensions["store"]
    order = _order(store, "payment-full", "company-moneda-demo")

    created = authenticated.post("/api/v1/payments", json=_banking_payload(order, "company-moneda-demo", 458.70))
    assert created.status_code == 201
    payment = created.json["data"]
    assert payment["status"] == "AWAITING SUPERADMIN CONFIRMATION"
    assert payment["confirmed_received"] == 0
    assert payment["remaining_balance"] == 458.70
    assert payment["customer_credit"] == 0
    pending_order = store.find_one("orders", {"_id": order["_id"]})
    assert pending_order["payment_status"] == "PENDING"
    assert pending_order["confirmed_received"] == 0
    assert pending_order["customer_credit"] == 0

    confirmed = authenticated.post(f"/api/v1/payments/{payment['_id']}/confirm", json={})
    assert confirmed.status_code == 200
    confirmed_payment = confirmed.json["data"]["payment"]
    assert confirmed_payment["status"] == "CONFIRMED"
    assert confirmed_payment["remaining_balance"] == 0
    paid_order = store.find_one("orders", {"_id": order["_id"]})
    assert paid_order["payment_status"] == "PAID"
    assert paid_order["confirmed_received"] == 458.70
    assert paid_order["remaining_balance"] == 0
    assert paid_order["customer_credit"] == 0


def test_partial_overpayment_and_rejection_use_confirmed_receipts_only(app, authenticated):
    store = app.extensions["store"]

    partial_order = _order(store, "payment-partial", "company-moneda-demo")
    partial = authenticated.post("/api/v1/payments", json=_banking_payload(partial_order, "company-moneda-demo", 300)).json["data"]
    assert store.find_one("orders", {"_id": partial_order["_id"]})["remaining_balance"] == 458.70
    assert authenticated.post(f"/api/v1/payments/{partial['_id']}/confirm", json={}).status_code == 200
    partial_state = store.find_one("orders", {"_id": partial_order["_id"]})
    assert partial_state["payment_status"] == "PARTIALLY_PAID"
    assert partial_state["remaining_balance"] == 158.70
    assert partial_state["customer_credit"] == 0

    overpaid_order = _order(store, "payment-overpaid", "company-moneda-demo")
    overpaid = authenticated.post("/api/v1/payments", json=_banking_payload(overpaid_order, "company-moneda-demo", 500)).json["data"]
    assert store.find_one("orders", {"_id": overpaid_order["_id"]})["customer_credit"] == 0
    assert authenticated.post(f"/api/v1/payments/{overpaid['_id']}/confirm", json={}).status_code == 200
    overpaid_state = store.find_one("orders", {"_id": overpaid_order["_id"]})
    assert overpaid_state["payment_status"] == "PAID"
    assert overpaid_state["remaining_balance"] == 0
    assert overpaid_state["customer_credit"] == 41.30

    rejected_order = _order(store, "payment-rejected", "company-moneda-demo")
    rejected = authenticated.post("/api/v1/payments", json=_banking_payload(rejected_order, "company-moneda-demo", 458.70)).json["data"]
    response = authenticated.post(f"/api/v1/payments/{rejected['_id']}/reject", json={"reason": "Bank receipt not found"})
    assert response.status_code == 200
    rejected_state = store.find_one("orders", {"_id": rejected_order["_id"]})
    assert rejected_state["payment_status"] == "PENDING"
    assert rejected_state["confirmed_received"] == 0
    assert rejected_state["remaining_balance"] == 458.70
    assert rejected_state["customer_credit"] == 0


def test_invalid_payment_date_is_rejected(app, authenticated):
    store = app.extensions["store"]
    order = _order(store, "payment-invalid-date", "company-moneda-demo")
    response = authenticated.post(
        "/api/v1/payments",
        json=_banking_payload(order, "company-moneda-demo", 10, payment_date="not-a-date"),
    )
    assert response.status_code == 422
    assert store.count("payments", {"order_id": order["_id"]}) == 0


def test_normal_user_cannot_list_or_create_for_an_unassigned_customer(app, client):
    store = app.extensions["store"]
    user = store.insert_one("users", {
        "_id": "payment-scoped-user",
        "name": "Payment Scoped User",
        "email": "payment.scoped@monedatechnologies.com",
        "username": "payment-scoped-user",
        "username_normalized": "payment-scoped-user",
        "password_hash": generate_password_hash("Secure123"),
        "role_id": "user",
        "active": True,
        "device_access_mode": "any_authorized_device",
    })
    assigned_customer = store.insert_one("customers", {
        "_id": "payment-assigned-customer", "name": "Assigned Customer",
        "assigned_user_ids": [user["_id"]], "active": True, "status": "active",
    })
    foreign_customer = store.insert_one("customers", {
        "_id": "payment-foreign-customer", "name": "Foreign Customer",
        "assigned_user_ids": [], "active": True, "status": "active",
    })
    assigned_order = _order(store, "payment-assigned-order", assigned_customer["_id"], salesperson_id=user["_id"])
    foreign_order = _order(store, "payment-foreign-order", foreign_customer["_id"])
    foreign_payment = store.insert_one("payments", {
        "_id": "payment-foreign-record", "order_id": foreign_order["_id"],
        "customer_id": foreign_customer["_id"], "amount": 10,
        "status": "AWAITING SUPERADMIN CONFIRMATION",
        "attachment": {"name": "proof.png", "type": "image/png", "size": 1, "data": "data:image/png;base64,AA=="},
    })
    _login_as(client, user["_id"])

    allowed = client.post("/api/v1/payments", json=_banking_payload(assigned_order, assigned_customer["_id"], 10))
    assert allowed.status_code == 201
    denied = client.post("/api/v1/payments", json=_banking_payload(foreign_order, foreign_customer["_id"], 10))
    assert denied.status_code == 403
    mismatched = client.post("/api/v1/payments", json=_banking_payload(assigned_order, foreign_customer["_id"], 10))
    assert mismatched.status_code == 422
    assert client.get(f"/api/v1/payments?order_id={foreign_order['_id']}").status_code == 403
    assert client.get(f"/api/v1/payments/{foreign_payment['_id']}/proof").status_code == 403
    listed = client.get("/api/v1/payments?limit=500")
    assert listed.status_code == 200
    assert {row["customer_id"] for row in listed.json["data"]["items"]} == {assigned_customer["_id"]}


def test_legacy_paid_without_confirmation_evidence_is_repaired_to_review_queue(app):
    store = app.extensions["store"]
    order = _order(store, "payment-legacy-client-paid", "company-moneda-demo", 100)
    payment = store.insert_one("payments", {
        "_id": "payment-legacy-client-paid-row",
        "order_id": order["_id"],
        "customer_id": "company-moneda-demo",
        "amount": 100,
        "status": "PAID",
    })
    result = repair_payment_states(store)
    assert result["payments"] == 1
    assert store.find_one("payments", {"_id": payment["_id"]})["status"] == "AWAITING SUPERADMIN CONFIRMATION"
    repaired_order = store.find_one("orders", {"_id": order["_id"]})
    assert repaired_order["payment_status"] == "PENDING"
    assert repaired_order["confirmed_received"] == 0
    assert repaired_order["remaining_balance"] == 100
