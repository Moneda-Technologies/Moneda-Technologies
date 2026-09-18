from __future__ import annotations

from app.middleware.access import permitted_quotation_query, quotation_is_authorized
from app.reports.routes import _is_valid_conversion


def _user(store, user_id: str, role_id: str, manager_id: str | None = None):
    payload = {"_id": user_id, "name": user_id, "role_id": role_id, "active": True}
    if manager_id:
        payload["manager_id"] = manager_id
    return store.insert_one("users", payload)


def test_manager_quotation_scope_includes_team_but_not_other_manager(app):
    with app.app_context():
        store = app.extensions["store"]
        manager = _user(store, "quote-manager", "manager_sales_admin")
        team_user = _user(store, "quote-team-user", "user", manager["_id"])
        other_manager = _user(store, "quote-other-manager", "manager_sales_admin")
        other_user = _user(store, "quote-other-user", "user", other_manager["_id"])
        for customer_id, assigned in (
            ("quote-team-customer", [team_user["_id"]]),
            ("quote-other-customer", [other_user["_id"]]),
        ):
            store.insert_one("customers", {
                "_id": customer_id, "company_name": customer_id, "active": True,
                "status": "active", "assigned_user_ids": assigned,
            })
        rows = [
            ("quote-own", "quote-team-customer", manager["_id"]),
            ("quote-team", "quote-team-customer", team_user["_id"]),
            ("quote-other", "quote-other-customer", other_user["_id"]),
        ]
        for quotation_id, customer_id, creator_id in rows:
            store.insert_one("quotations", {
                "_id": quotation_id, "quotation_number": quotation_id,
                "customer_id": customer_id, "created_by_user_id": creator_id,
                "status": "Sent", "totals": {"grand_total": 100},
            })

        visible, _ = store.list("quotations", permitted_quotation_query(manager), limit=100)
        assert {row["_id"] for row in visible} >= {"quote-own", "quote-team"}
        assert "quote-other" not in {row["_id"] for row in visible}
        assert quotation_is_authorized(store.find_one("quotations", {"_id": "quote-team"}), manager)
        assert not quotation_is_authorized(store.find_one("quotations", {"_id": "quote-other"}), manager)


def test_user_quotation_scope_is_own_only_and_global_roles_remain_global(app):
    with app.app_context():
        store = app.extensions["store"]
        user = _user(store, "quote-user-only", "user")
        other = _user(store, "quote-user-other", "user")
        customer_id = "quote-user-customer"
        store.insert_one("customers", {
            "_id": customer_id, "company_name": customer_id, "active": True,
            "status": "active", "assigned_user_ids": [user["_id"], other["_id"]],
        })
        for quotation_id, creator_id in (("quote-user-own", user["_id"]), ("quote-user-other", other["_id"])):
            store.insert_one("quotations", {
                "_id": quotation_id, "quotation_number": quotation_id,
                "customer_id": customer_id, "created_by_user_id": creator_id, "status": "Sent",
            })
        visible, _ = store.list("quotations", permitted_quotation_query(user), limit=100)
        assert {row["_id"] for row in visible} == {"quote-user-own"}
        superadmin = _user(store, "quote-superadmin", "superadmin")
        visible_all, _ = store.list("quotations", permitted_quotation_query(superadmin), limit=100)
        assert {row["_id"] for row in visible_all} >= {"quote-user-own", "quote-user-other"}


def test_converted_quotation_requires_live_order_confirmation(app):
    with app.app_context():
        store = app.extensions["store"]
        quotation = {"_id": "quote-stale-conversion", "status": "Converted to Order"}
        assert not _is_valid_conversion(store, quotation)
        store.insert_one("orders", {"_id": "quote-stale-conversion-order", "quotation_id": quotation["_id"], "status": "Deleted"})
        assert not _is_valid_conversion(store, quotation)
        store.insert_one("orders", {"_id": "quote-live-conversion-order", "quotation_id": quotation["_id"], "status": "Pending"})
        assert _is_valid_conversion(store, quotation)
