from __future__ import annotations


def test_active_payment_must_be_voided_before_oc_delete_and_unpaid_incentive_is_cancelled(app, authenticated):
    store = app.extensions["store"]
    quotation = store.insert_one("quotations", {
        "_id": "quotation-for-delete-flow",
        "quotation_number": "MT-MONEDA-DELETE-FLOW",
        "status": "Converted to Order",
    })
    order = store.insert_one("orders", {
        "_id": "order-for-delete-flow",
        "order_number": "MT-OC-DELETE-FLOW",
        "quotation_id": quotation["_id"],
        "customer_id": "company-moneda-demo",
        "customer_company_id": "company-moneda-demo",
        "company_id": "company-moneda-demo",
        "order_amount": 100,
        "totals": {"grand_total": 100},
        "status": "Pending",
    })
    incentive = store.insert_one("incentives", {
        "_id": "incentive-for-delete-flow",
        "order_id": order["_id"],
        "status": "PENDING PAYMENT",
        "paid_amount": 0,
    })
    allocation = store.insert_one("incentive_allocations", {
        "_id": "allocation-for-delete-flow",
        "incentive_id": incentive["_id"],
        "status": "ELIGIBLE",
    })
    payment = store.insert_one("payments", {
        "_id": "payment-for-delete-flow",
        "order_id": order["_id"],
        "customer_id": "company-moneda-demo",
        "amount": 100,
        "status": "AWAITING SUPERADMIN CONFIRMATION",
    })

    blocked = authenticated.delete(f"/api/v1/orders/{order['_id']}", json={"reason": "remove test OC"})
    assert blocked.status_code == 409
    assert blocked.json["error"] == "ORDER_HAS_ACTIVE_PAYMENT"

    direct_delete = authenticated.delete(f"/api/v1/payments/{payment['_id']}", json={})
    assert direct_delete.status_code == 409
    assert direct_delete.json["error"] == "payment_must_be_voided"

    voided = authenticated.post(f"/api/v1/payments/{payment['_id']}/void", json={"reason": "Duplicate receipt"})
    assert voided.status_code == 200
    assert voided.json["data"]["status"] == "VOIDED"
    persisted = store.find_one("payments", {"_id": payment["_id"]})
    assert persisted["voided_by_user_id"]
    assert persisted["void_reason"] == "Duplicate receipt"
    assert any(entry.get("action") == "voided" for entry in persisted.get("audit", []))

    deleted = authenticated.delete(f"/api/v1/orders/{order['_id']}", json={"reason": "remove test OC"})
    assert deleted.status_code == 200
    assert deleted.json["data"]["quotation_restored"] is False
    assert store.find_one("quotations", {"_id": quotation["_id"]})["status"] == "Converted to Order"
    assert store.find_one("incentives", {"_id": incentive["_id"]})["status"] == "CANCELLED"
    assert store.find_one("incentive_allocations", {"_id": allocation["_id"]})["status"] == "CANCELLED"

    payment_deleted = authenticated.delete(f"/api/v1/payments/{payment['_id']}", json={"reason": "remove voided receipt"})
    assert payment_deleted.status_code == 200
    assert store.find_one("payments", {"_id": payment["_id"]})["status"] == "DELETED"


def test_privileged_user_can_soft_cancel_unpaid_incentive_but_not_paid_one(app, authenticated):
    store = app.extensions["store"]
    pending = store.insert_one("incentives", {
        "_id": "incentive-manual-cancel",
        "order_id": "order-manual-cancel",
        "customer_id": "company-moneda-demo",
        "status": "PENDING PAYMENT",
        "gross_incentive_amount": 25,
        "net_payable_incentive": 25,
        "paid_amount": 0,
    })
    allocation = store.insert_one("incentive_allocations", {
        "_id": "allocation-manual-cancel",
        "incentive_id": pending["_id"],
        "status": "ELIGIBLE",
    })

    cancelled = authenticated.delete(
        f"/api/v1/incentives/{pending['_id']}",
        json={"reason": "Duplicate incentive snapshot"},
    )
    assert cancelled.status_code == 200
    persisted = store.find_one("incentives", {"_id": pending["_id"]})
    assert persisted["status"] == "CANCELLED"
    assert persisted["cancelled_reason"] == "Duplicate incentive snapshot"
    assert persisted["cancelled_by"] == "user-demo-admin"
    assert store.find_one("incentive_allocations", {"_id": allocation["_id"]})["status"] == "CANCELLED"
    visible = authenticated.get("/api/v1/incentives").json["data"]["items"]
    assert pending["_id"] not in {item["_id"] for item in visible}

    paid = store.insert_one("incentives", {
        "_id": "incentive-paid-lock",
        "customer_id": "company-moneda-demo",
        "status": "PAID",
        "paid_amount": 10,
    })
    blocked = authenticated.delete(f"/api/v1/incentives/{paid['_id']}", json={"reason": "should fail"})
    assert blocked.status_code == 409
    assert blocked.json["error"] == "INCENTIVE_FINANCIALLY_LOCKED"
    assert store.find_one("incentives", {"_id": paid["_id"]})["status"] == "PAID"


def test_standard_user_cannot_cancel_incentive(app, authenticated):
    store = app.extensions["store"]
    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "user"})
    row = store.insert_one("incentives", {
        "_id": "incentive-user-delete-denied",
        "customer_id": "company-moneda-demo",
        "status": "PENDING PAYMENT",
        "paid_amount": 0,
    })
    denied = authenticated.delete(f"/api/v1/incentives/{row['_id']}", json={"reason": "not allowed"})
    assert denied.status_code == 403
    assert store.find_one("incentives", {"_id": row["_id"]})["status"] == "PENDING PAYMENT"


def test_oc_id_only_incentives_and_allocations_are_cancelled_and_hidden(app, authenticated):
    store = app.extensions["store"]
    order = store.insert_one("orders", {
        "_id": "oc-only-delete-flow",
        "order_number": "MT-OC-OC-ID-ONLY",
        "customer_id": "company-moneda-demo",
        "order_amount": 75,
        "status": "Pending",
    })
    incentive = store.insert_one("incentives", {
        "_id": "oc-only-incentive",
        "oc_id": order["_id"],
        "customer_id": "company-moneda-demo",
        "status": "PENDING PAYMENT",
        "gross_incentive_amount": 7.5,
        "paid_amount": 0,
    })
    allocation = store.insert_one("incentive_allocations", {
        "_id": "oc-only-allocation",
        "oc_id": order["_id"],
        "incentive_id": incentive["_id"],
        "status": "ELIGIBLE",
    })

    deleted = authenticated.delete(f"/api/v1/orders/{order['_id']}", json={"reason": "OC id compatibility"})
    assert deleted.status_code == 200
    assert store.find_one("incentives", {"_id": incentive["_id"]})["status"] == "CANCELLED"
    assert store.find_one("incentive_allocations", {"_id": allocation["_id"]})["status"] == "CANCELLED"
    assert incentive["_id"] not in {row["_id"] for row in authenticated.get("/api/v1/incentives").json["data"]["items"]}
    detail = authenticated.get(f"/api/v1/incentives/{incentive['_id']}")
    assert detail.status_code == 410
    assert detail.json["error"] == "INCENTIVE_TRANSACTION_DELETED"
