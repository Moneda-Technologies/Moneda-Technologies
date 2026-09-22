from __future__ import annotations


def test_superadmin_custom_role_lifecycle_and_assigned_role_guard(app, authenticated):
    store = app.extensions["store"]
    created = authenticated.post("/api/admin/roles", json={
        "key": "support_ops",
        "display_name": "Support Operations",
        "description": "Support workflow",
        "permissions": ["customers.view"],
    })
    assert created.status_code == 201

    updated = authenticated.patch("/api/admin/roles/support_ops", json={
        "permissions": ["customers.view", "reports.view"],
    })
    assert updated.status_code == 200
    assert set(updated.json["data"]["permissions"]) == {"customers.view", "reports.view"}

    store.insert_one("users", {
        "_id": "support-role-user", "name": "Support User", "email": "support@example.com",
        "role_id": "support_ops", "active": True,
    })
    blocked = authenticated.delete("/api/admin/roles/support_ops")
    assert blocked.status_code == 409
    assert blocked.json["error"] == "role_in_use"

    store.update_one("users", {"_id": "support-role-user"}, {"role_id": "user"})
    deleted = authenticated.delete("/api/admin/roles/support_ops")
    assert deleted.status_code == 200


def test_non_superadmin_cannot_manage_roles_or_customer_assignments(app, authenticated):
    store = app.extensions["store"]
    store.update_one("users", {"_id": "user-demo-admin"}, {"role_id": "user"})

    assert authenticated.post("/api/admin/roles", json={
        "key": "forbidden_role", "display_name": "Forbidden", "permissions": [],
    }).status_code == 403
    assert authenticated.patch("/api/admin/roles/admin", json={"permissions": []}).status_code == 403
    assert authenticated.delete("/api/admin/roles/admin").status_code == 403
    assert authenticated.patch(
        "/api/admin/users/user-demo-manager",
        json={"customer_ids": ["customer-demo-1"]},
    ).status_code == 403
