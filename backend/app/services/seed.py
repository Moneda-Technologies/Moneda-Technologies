from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from werkzeug.security import generate_password_hash

from app.repositories.store import Store, utcnow
from app.customers.codes import customer_code


PERMISSIONS = [
    "dashboard.view", "calculator.view", "products.view", "products.create", "products.update",
    "products.archive", "products.delete", "pricing.view", "pricing.edit", "pricing.update", "pricing.history",
    "pricing.discount.override", "taxes.view", "taxes.manage", "currency.view", "currency.manage",
    "cart.view", "cart.manage", "customers.view", "customers.create", "customers.update",
    "customers.delete", "customers.archive", "customers.restore", "customers.view_all", "quotations.view", "quotations.view_all", "quotations.create", "quotations.edit",
    "quotations.delete", "quotations.archive", "quotations.restore", "quotations.send", "quotations.download", "orders.view", "orders.create",
    "orders.update", "crm.view", "crm.manage", "leads.view", "leads.manage",
    "reminders.view", "reminders.manage", "reports.view", "users.view", "users.create",
    "users.update", "users.delete", "roles.view", "roles.manage", "companies.view",
    "companies.create", "companies.update", "companies.delete", "settings.view", "settings.currencies.view", "settings.currencies.manage",
    "settings.communication.view", "settings.communication.manage", "settings.security.view", "settings.security.manage",
    "security.devices.delete", "security.devices.reinstate", "security.screenshot_protection.manage", "security.login_notifications.manage",
    "settings.manage", "audit_logs.view",
]


ROLE_PERMISSIONS = {
    "superadmin": PERMISSIONS,
    "admin": [permission for permission in PERMISSIONS if permission not in {"roles.manage"}],
    "manager_sales_admin": [
        permission for permission in PERMISSIONS
        if permission.split(".")[0] in {"dashboard", "calculator", "products", "pricing", "currency", "cart", "companies", "customers", "quotations", "orders", "crm", "leads", "reminders", "reports"}
        and permission not in {"products.delete", "customers.delete", "quotations.delete", "quotations.view_all", "pricing.edit", "pricing.update"}
    ],
    "user": [
        "dashboard.view", "calculator.view", "products.view", "pricing.view", "currency.view",
        "cart.view", "cart.manage", "companies.view", "customers.view", "customers.create",
        "customers.update", "quotations.view", "quotations.create", "quotations.edit",
        "quotations.download", "quotations.send", "orders.view", "crm.view", "reminders.view",
    ],
}


def _load(path: Path) -> Any:
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"Duplicate JSON key '{key}' in {path.name}")
            document[key] = value
        return document

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)


def _discount_rules() -> dict[str, Any]:
    return {
        "enabled": True, "step": 0.5,
        "default_max_percent": 5, "privileged_max_percent": 10,
    }


def _valid_price(value: Any) -> bool:
    return value is None or isinstance(value, (int, float)) and value >= 0


def _catalog_seed(data_directory: Path) -> dict[str, Any]:
    """Resolve the explicitly named, separated JSON sources into runtime rows."""
    names = (
        "product_types.json", "blanket_categories.json", "blanket_options.json", "machines.json",
        "blanket_bars.json", "blankets.json", "mpack_types.json", "mpack_options.json",
        "mpacks.json", "mpack_price_list_2026_h2.json", "chemical_categories.json", "chemical_options.json",
        "chemicals.json", "pricing_eur.json", "tax_rules.json",
    )
    documents = {name: _load(data_directory / name) for name in names}
    product_types = documents["product_types.json"].get("product_types", [])
    family_ids = [row.get("id") for row in product_types]
    if family_ids != ["blankets", "mpacks", "chemicals"] or len(family_ids) != len(set(family_ids)):
        raise ValueError("product_types.json must define exactly blankets, mpacks and chemicals")

    blanket_categories_source = documents["blanket_categories.json"].get("categories", [])
    blanket_category_ids = [row.get("id") for row in blanket_categories_source]
    if not blanket_category_ids or len(blanket_category_ids) != len(set(blanket_category_ids)):
        raise ValueError("Blanket category IDs must be present and unique")

    blanket_options = documents["blanket_options.json"]
    machine_rows = documents["machines.json"].get("machines", [])
    machine_ids = [row.get("id") for row in machine_rows]
    if any(not value for value in machine_ids) or len(machine_ids) != len(set(machine_ids)):
        raise ValueError("Machine IDs must be present and unique")
    # Blanket configuration consumes the dedicated machine catalogue; keeping
    # it in one source prevents UI and API lists from drifting apart.
    blanket_options = {**blanket_options, "machines": machine_rows}
    blanket_bars_source = documents["blanket_bars.json"].get("bars", [])
    bar_ids = [row.get("id") for row in blanket_bars_source]
    if not bar_ids or len(bar_ids) != len(set(bar_ids)):
        raise ValueError("Blanket bar IDs must be present and unique")
    default_bar_ids = blanket_options.get("rules", {}).get("default_bar_ids", [])
    if len(default_bar_ids) != 2 or not set(default_bar_ids).issubset(set(bar_ids)):
        raise ValueError("Blanket defaults must reference exactly two valid bars")
    for bar in blanket_bars_source:
        if bar.get("unit") != "bar" or not _valid_price(bar.get("price_eur")):
            raise ValueError(f"Invalid EUR/bar price for {bar.get('id')}")
        if bar.get("price_eur") is None and bar.get("status") == "configured":
            raise ValueError(f"Configured bar {bar.get('id')} requires a price")

    blankets_document = documents["blankets.json"]
    blankets = blankets_document.get("products", [])
    underlays = blankets_document.get("underlay_products", [])
    mpack_products = documents["mpacks.json"].get("products", [])
    chemical_products = documents["chemicals.json"].get("products", [])
    family_products = {"blankets": blankets + underlays, "mpacks": mpack_products, "chemicals": chemical_products}
    all_product_ids = [row.get("id") for rows in family_products.values() for row in rows]
    if any(not value for value in all_product_ids) or len(all_product_ids) != len(set(all_product_ids)):
        raise ValueError("Active product IDs must be present and globally unique")

    pricing_document = documents["pricing_eur.json"]
    if pricing_document.get("currency") != "EUR":
        raise ValueError("pricing_eur.json must use EUR")
    pricing_groups = pricing_document.get("products", {})
    for family_id, rows in family_products.items():
        pricing = pricing_groups.get(family_id, {})
        row_ids = {row["id"] for row in rows}
        if set(pricing) != row_ids:
            raise ValueError(f"Pricing references for {family_id} must match product IDs exactly")
        for product_id, price in pricing.items():
            if not _valid_price(price.get("price_eur")):
                raise ValueError(f"Invalid EUR price for {product_id}")
            for price_map_name in ("variant_prices_eur", "dimension_prices_eur", "package_prices_eur"):
                if any(not _valid_price(value) for value in (price.get(price_map_name) or {}).values()):
                    raise ValueError(f"Invalid {price_map_name} entry for {product_id}")

    for row in blankets + underlays:
        unknown = set(row.get("category_ids", [])) - set(blanket_category_ids)
        if unknown:
            raise ValueError(f"Product {row['id']} references unknown blanket categories")
        variant_keys = {str(value) for value in pricing_groups["blankets"][row["id"]].get("variant_prices_eur", {})}
        thicknesses = {str(variant.get("thickness_mm")) for variant in row.get("variants", [])}
        if not variant_keys.issubset(thicknesses):
            raise ValueError(f"Product {row['id']} has dangling variant prices")

    mpack_types_source = documents["mpack_types.json"].get("types", [])
    mpack_type_map = {row["id"]: row for row in mpack_types_source}
    if len(mpack_type_map) != len(mpack_types_source):
        raise ValueError("Underpacking type IDs must be unique")
    if any(row.get("type_id") not in mpack_type_map for row in mpack_products):
        raise ValueError("Underpacking product references an unknown type")

    mpack_price_list = documents["mpack_price_list_2026_h2.json"]
    price_list_meta = mpack_price_list.get("price_list", {})
    if price_list_meta.get("currency") != "EUR" or price_list_meta.get("quantity_unit") != "box":
        raise ValueError("MPack machine prices must use EUR per box")
    if (price_list_meta.get("valid_from"), price_list_meta.get("valid_until")) != ("2026-07-01", "2026-12-31"):
        raise ValueError("MPack price-list validity must be 01 Jul through 31 Dec 2026")
    mpack_machine_sizes = mpack_price_list.get("machine_sizes", [])
    if not mpack_machine_sizes:
        raise ValueError("MPack price list requires machine-size rows")
    machine_size_keys: set[tuple[str, str, int, int]] = set()
    expected_thicknesses = {
        Decimal(str(row["thickness_mm"])) for row in mpack_price_list.get("thicknesses", [])
    }
    for row in mpack_machine_sizes:
        key = (
            str(row.get("manufacturer", "")).strip(), str(row.get("machine_model", "")).strip(),
            int(row.get("width_mm", 0)), int(row.get("length_mm", 0)),
        )
        if not all(key) or key in machine_size_keys:
            raise ValueError("MPack machine-size rows must have unique complete keys")
        machine_size_keys.add(key)
        prices = row.get("prices", [])
        if {Decimal(str(price.get("thickness_mm"))) for price in prices} != expected_thicknesses:
            raise ValueError(f"MPack row {key} does not define every supported thickness")
        if any(
            price.get("price_per_sheet_eur") is None
            or not _valid_price(price.get("price_per_sheet_eur"))
            or price.get("price_per_box_eur") is None
            or not _valid_price(price.get("price_per_box_eur"))
            or int(price.get("sheets_per_box", 0)) <= 0
            for price in prices
        ):
            raise ValueError(f"MPack row {key} has invalid pricing")

    chemical_categories_source = documents["chemical_categories.json"].get("categories", [])
    chemical_category_map = {row["id"]: row for row in chemical_categories_source}
    if len(chemical_category_map) != len(chemical_categories_source):
        raise ValueError("Chemical category IDs must be unique")
    if any(row.get("category_id") not in chemical_category_map for row in chemical_products):
        raise ValueError("Chemical product references an unknown category")

    families = [{
        "_id": row["id"], "name": row["name"], "description": row.get("description", ""),
        "icon": row.get("icon", "package"), "active": True, "calculator_enabled": True,
        "sort_order": row.get("sort_order", 99),
    } for row in product_types]
    blanket_categories = [{
        "_id": row["id"], "name": row["name"], "description": row.get("description", ""),
        "sort_order": row.get("sort_order", 99), "active": True,
    } for row in blanket_categories_source]
    blanket_bars = [{
        "_id": row["id"], "article_no": row["article_no"], "name": row["name"],
        "description": row.get("description", ""), "active": row.get("active", True),
        "pricing": {"master_currency": "EUR", "pricing_type": "per_bar", "unit": "bar", "price": row.get("price_eur")},
        "pricing_status": row.get("status", "pending"), "source": "canonical_catalogue",
    } for row in blanket_bars_source]
    bar_options = [{
        "id": row["_id"], "article_no": row["article_no"], "name": row["name"],
        "price": row["pricing"]["price"], "pricing_status": row["pricing_status"],
    } for row in blanket_bars]

    products: list[dict[str, Any]] = []
    format_ids = [row["id"] for row in blanket_options.get("formats", [])]
    for item in blankets + underlays:
        is_underlay = item in underlays
        variants = item.get("variants", [])
        price = pricing_groups["blankets"][item["id"]]
        products.append({
            "_id": item["id"], "article_no": item.get("article_no"), "sku": item.get("sku", item["id"].upper()),
            "name": item["name"], "category_id": "blankets", "description": item.get("description") or item.get("application", ""),
            "commercial_unit": "pc",
            "pricing": {"master_currency": "EUR", "pricing_type": price["pricing_type"], "unit": price["unit"],
                        "price": price.get("price_eur"), "variant_prices": price.get("variant_prices_eur", {})},
            "tax": {"mode": None, "rate": None, "override_enabled": False}, "discount_rules": _discount_rules(),
            "configuration": {
                "configurator": "underlay" if is_underlay else "blanket", "product_type": "underlay" if is_underlay else "blanket",
                "category_ids": item.get("category_ids", []), "application": item.get("application", ""),
                "thicknesses_mm": [variant["thickness_mm"] for variant in variants], "thickness_variants": variants,
                "standard_widths_mm": sorted({width for variant in variants for width in variant.get("standard_widths_mm", [])}),
                "dimension_units": blanket_options.get("dimension_units", []),
                "format_types": ["cut_format"] if is_underlay else format_ids,
                "machine_options": blanket_options.get("machines", []), "bar_options": [] if is_underlay else bar_options,
                "default_bar_ids": default_bar_ids,
                "fingerprint_fields": ["machine", "thickness_mm", "length", "width", "dimension_unit", "format_type", "bar_1_id", "bar_2_id"],
            },
            "pricing_status": price.get("status", "pending"), "active": item.get("active", True), "source": "canonical_catalogue",
        })

    mpack_options = documents["mpack_options.json"]
    thickness_catalog = mpack_options.get("thickness_catalog", [])
    for item in mpack_products:
        type_row = mpack_type_map[item["type_id"]]
        price = pricing_groups["mpacks"][item["id"]]
        uses_machine_price_list = item["id"] == "mtech-mpack"
        products.append({
            "_id": item["id"], "article_no": item.get("article_no"), "sku": item.get("sku", item["id"].upper()),
            "name": type_row["name"], "category_id": "mpacks", "description": item.get("description", ""),
            "commercial_unit": "box",
            "pricing": {"master_currency": "EUR", "pricing_type": price["pricing_type"], "unit": price["unit"],
                        "price": price.get("price_eur"), "dimension_prices": price.get("dimension_prices_eur", {})},
            "tax": {"mode": None, "rate": None, "override_enabled": False}, "discount_rules": _discount_rules(),
            "configuration": {
                "configurator": "mpack", "type_id": item["type_id"], "self_adhesive": type_row.get("self_adhesive"),
                "underpacking_type": type_row["name"], "machine_options": mpack_options.get("machines", []),
                "thicknesses": [{"label": f"{row['micron']} micron", "value": row["micron"]} for row in thickness_catalog],
                "size_presets": thickness_catalog, "dimension_units": mpack_options.get("dimension_units", []),
                "machine_price_list": price_list_meta if uses_machine_price_list else None,
                "machine_sizes": mpack_machine_sizes if uses_machine_price_list else [],
                "complete_containers": mpack_options.get("rules", {}).get("complete_containers", False),
                "fingerprint_fields": [
                    "manufacturer", "machine_model", "width_mm", "length_mm", "thickness_mm", "underpacking_type",
                ] if uses_machine_price_list else ["machine", "thickness_micron", "length", "width", "dimension_unit", "underpacking_type"],
            },
            "pricing_status": price.get("status", "pending"), "active": item.get("active", True), "source": "canonical_catalogue",
        })

    chemical_options = documents["chemical_options.json"]
    for item in chemical_products:
        category = chemical_category_map[item["category_id"]]
        price = pricing_groups["chemicals"][item["id"]]
        products.append({
            "_id": item["id"], "article_no": item.get("article_no"), "sku": item.get("sku", item["id"].upper()),
            "name": item["name"], "category_id": "chemicals", "description": item.get("description", ""),
            "commercial_unit": "litre",
            "pricing": {"master_currency": "EUR", "pricing_type": price["pricing_type"], "unit": price["unit"],
                        "price": price.get("price_eur"), "package_prices": price.get("package_prices_eur", {})},
            "tax": {"mode": None, "rate": None, "override_enabled": False}, "discount_rules": _discount_rules(),
            "configuration": {
                "configurator": "chemical", "sub_category_id": item["category_id"], "sub_category": category["name"],
                "formats": item.get("packages", []),
                "complete_containers": chemical_options.get("rules", {}).get("complete_containers", True),
                "fingerprint_fields": ["format_id", "size_litre"],
            },
            "pricing_status": price.get("status", "pending"), "active": item.get("active", True), "source": "canonical_catalogue",
        })

    return {
        "families": families, "products": products, "blanket_bars": blanket_bars,
        "machines": machine_rows,
        "blanket_categories": blanket_categories,
        "mpack_types": [{"_id": row["id"], **row, "active": True} for row in mpack_types_source],
        "chemical_categories": [{"_id": row["id"], **row, "active": True} for row in chemical_categories_source],
        "catalog_options": [
            {"_id": "blankets", **blanket_options}, {"_id": "mpacks", **mpack_options},
            {"_id": "chemicals", **chemical_options},
        ],
        "tax_rules": documents["tax_rules.json"],
    }


def seed(store: Store, data_directory: Path, *, demo_mode: bool) -> None:
    store.unset_many("users", {"currency_preference": {"$exists": True}}, ["currency_preference"])
    for permission in PERMISSIONS:
        if not store.find_one("permissions", {"_id": permission}):
            store.insert_one("permissions", {"_id": permission, "name": permission})

    display_names = {
        "superadmin": "Superadmin",
        "admin": "Admin",
        "manager_sales_admin": "Manager / Sales Admin",
        "user": "User",
    }
    for role_id, permissions in ROLE_PERMISSIONS.items():
        if not store.find_one("roles", {"_id": role_id}):
            store.insert_one("roles", {
                "_id": role_id, "name": role_id, "display_name": display_names[role_id],
                "permissions": permissions, "system": True,
            })

    # One-time permission migration: price editing is now distinct from price
    # visibility. Managers may inspect master prices and history, but only
    # administrators can change the EUR master.
    permission_migration = "permission-pricing-edit-v1"
    if not store.find_one("system_migrations", {"_id": permission_migration}):
        for role_id in ("superadmin", "admin", "manager_sales_admin", "user"):
            role = store.find_one("roles", {"_id": role_id}) or {}
            permissions = set(role.get("permissions", []))
            if role_id in {"superadmin", "admin"}:
                permissions.add("pricing.edit")
            else:
                permissions.discard("pricing.edit")
                permissions.discard("pricing.update")
            if role:
                store.update_one("roles", {"_id": role_id}, {"permissions": sorted(permissions)})
        store.insert_one("system_migrations", {"_id": permission_migration, "applied_at": utcnow()})

    quotation_send_migration = "quotation-send-user-v1"
    if not store.find_one("system_migrations", {"_id": quotation_send_migration}):
        role = store.find_one("roles", {"_id": "user"}) or {}
        if role:
            permissions = set(role.get("permissions", []))
            permissions.add("quotations.send")
            store.update_one("roles", {"_id": "user"}, {"permissions": sorted(permissions)})
        store.insert_one("system_migrations", {"_id": quotation_send_migration, "applied_at": utcnow()})

    customer_access_migration = "customer-view-all-v1"
    if not store.find_one("system_migrations", {"_id": customer_access_migration}):
        for role_id in ("superadmin", "admin"):
            role = store.find_one("roles", {"_id": role_id}) or {}
            if role:
                permissions = set(role.get("permissions", []))
                permissions.add("customers.view_all")
                store.update_one("roles", {"_id": role_id}, {"permissions": sorted(permissions)})
        store.insert_one("system_migrations", {"_id": customer_access_migration, "applied_at": utcnow()})

    settings_controls_migration = "settings-controls-v1"
    if not store.find_one("system_migrations", {"_id": settings_controls_migration}):
        # Keep the existing role registry authoritative while adding the
        # narrowly-scoped controls introduced by the settings restructure.
        for role_id in ("superadmin", "admin"):
            role = store.find_one("roles", {"_id": role_id}) or {}
            if role:
                permissions = set(role.get("permissions", []))
                permissions.update({
                    "customers.archive", "customers.restore", "quotations.archive", "quotations.restore",
                    "settings.currencies.view", "settings.currencies.manage", "settings.communication.view",
                    "settings.communication.manage", "settings.security.view", "settings.security.manage",
                    "security.devices.delete", "security.devices.reinstate", "security.screenshot_protection.manage",
                    "security.login_notifications.manage",
                })
                store.update_one("roles", {"_id": role_id}, {"permissions": sorted(permissions)})
        store.insert_one("system_migrations", {"_id": settings_controls_migration, "applied_at": utcnow()})

    pricing_policy_migration = "eur-only-no-tax-v1"
    if not store.find_one("system_migrations", {"_id": pricing_policy_migration}):
        # This deliberately does not rewrite historical quotations or customer tax fields.
        store.insert_one("system_migrations", {"_id": pricing_policy_migration, "applied_at": utcnow()})

    catalog = _catalog_seed(data_directory)
    families = catalog["families"]
    canonical_products = catalog["products"]
    blanket_bars = catalog["blanket_bars"]
    blanket_categories = catalog["blanket_categories"]
    family_ids = {item["_id"] for item in families}
    existing_categories, _ = store.list("categories", limit=1000)
    for existing in existing_categories:
        if existing["_id"] not in family_ids:
            store.delete_one("categories", {"_id": existing["_id"]})
    for category in families:
        if store.find_one("categories", {"_id": category["_id"]}):
            store.update_one("categories", {"_id": category["_id"]}, category)
        else:
            store.insert_one("categories", category)

    canonical_ids = {item["_id"] for item in canonical_products}
    existing_products, _ = store.list("products", limit=100_000)
    for existing in existing_products:
        if existing.get("source") in {"moneda_catalogue", "legacy_structure_only", "canonical_catalogue"} and existing["_id"] not in canonical_ids:
            store.update_one("products", {"_id": existing["_id"]}, {"active": False, "calculator_enabled": False})
    for item in canonical_products:
        existing = store.find_one("products", {"_id": item["_id"]})
        if existing:
            # MongoDB is authoritative after an administrator changes pricing.
            if store.count("price_history", {"product_id": item["_id"]}) and not item.get("configuration", {}).get("machine_sizes"):
                item["pricing"] = existing.get("pricing", item["pricing"])
                item["tax"] = existing.get("tax", item["tax"])
                item["pricing_status"] = existing.get("pricing_status", item["pricing_status"])
                item["active"] = existing.get("active", item["active"])
                for field in ("price_updated_at", "price_updated_by", "price_updated_by_name"):
                    if field in existing:
                        item[field] = existing[field]
            store.update_one("products", {"_id": item["_id"]}, item)
        else:
            store.insert_one("products", item)

    canonical_bar_ids = {item["_id"] for item in blanket_bars}
    existing_bars, _ = store.list("blanket_bars", limit=1000)
    for existing in existing_bars:
        if existing["_id"] not in canonical_bar_ids:
            store.delete_one("blanket_bars", {"_id": existing["_id"]})
    for item in blanket_bars:
        existing = store.find_one("blanket_bars", {"_id": item["_id"]})
        if existing:
            if store.count("price_history", {"resource_id": item["_id"], "entity_type": "bar"}):
                item["pricing"] = existing.get("pricing", item["pricing"])
                item["pricing_status"] = existing.get("pricing_status", item["pricing_status"])
                item["active"] = existing.get("active", item["active"])
                for field in ("price_updated_at", "price_updated_by", "price_updated_by_name"):
                    if field in existing:
                        item[field] = existing[field]
            store.update_one("blanket_bars", {"_id": item["_id"]}, item)
        else:
            store.insert_one("blanket_bars", item)

    blanket_category_ids = {item["_id"] for item in blanket_categories}
    for existing in store.list("blanket_categories", limit=1000)[0]:
        if existing["_id"] not in blanket_category_ids:
            store.delete_one("blanket_categories", {"_id": existing["_id"]})
    for item in blanket_categories:
        if store.find_one("blanket_categories", {"_id": item["_id"]}):
            store.update_one("blanket_categories", {"_id": item["_id"]}, item)
        else:
            store.insert_one("blanket_categories", item)

    # Machine names are canonical data, shared by blanket configuration and
    # the /machines API. User-recorded machines are intentionally preserved.
    for item in catalog["machines"]:
        machine = {
            "_id": item["id"], "name": item["name"],
            "manufacturer": item.get("manufacturer"),
            "machine_model": item.get("machine_model"),
            "active": True, "source": "canonical_catalogue",
        }
        if store.find_one("machines", {"_id": machine["_id"]}):
            store.update_one("machines", {"_id": machine["_id"]}, machine)
        else:
            store.insert_one("machines", machine)

    # Older demo seeds contained fabricated FX values. They are not valid
    # exchange-rate history and must never be used as a fallback.
    for legacy_rate in store.list("exchange_rates", {"provider": "demo-bootstrap"}, limit=100)[0]:
        store.delete_one("exchange_rates", {"_id": legacy_rate["_id"]})

    for collection, rows in (
        ("mpack_types", catalog["mpack_types"]),
        ("chemical_categories", catalog["chemical_categories"]),
        ("catalog_options", catalog["catalog_options"]),
    ):
        canonical_row_ids = {item["_id"] for item in rows}
        for existing in store.list(collection, limit=10_000)[0]:
            if existing["_id"] not in canonical_row_ids:
                store.delete_one(collection, {"_id": existing["_id"]})
        for item in rows:
            if store.find_one(collection, {"_id": item["_id"]}):
                store.update_one(collection, {"_id": item["_id"]}, item)
            else:
                store.insert_one(collection, item)

    if not store.find_one("app_settings", {"_id": "system"}):
        store.insert_one("app_settings", {
            "_id": "system",
            "brand_name": "Moneda Technologies",
            "brand_logo_path": "/brand/moneda-logo.svg",
            "watermark_enabled": True,
            "issuer": {
                "name": "Moneda Technologies",
                "email": "business@monedatechnologies.com",
                "phone": None,
                "address": None,
            },
            "master_currency": "EUR",
            "supported_currencies": ["EUR", "USD", "INR"],
            "quotation_pricing_policy": "eur_only_no_tax_v1",
            "quotation_prefix": "MON_Q",
            "quotation_validity_days": 30,
            "commercial_conditions": {
                "payment": "Prepayment against Pro-Forma.",
                "despatch": "Between 1 Week - 8 Weeks.",
                "duties_taxes_bank_charges": "To be borne by the consignee.",
            },
            "discount_rules": {
                "bulk_rolls": {"enabled": False, "minimum_quantity": 10, "discount_percent": 0, "applies_to_categories": ["blankets"]},
            },
            "surcharge_rules": {
                "cut_format": {"enabled": False, "percent": 5, "applies_to_categories": ["blankets"]},
            },
                "payment_terms": ["Advance", "POD", "30 Days from receipt", "60 Days", "Custom"],
            "transport_options": ["by_consignee", "by_moneda_team"],
            "post_order_follow_up_days": [15, 25],
        })
    else:
        settings = store.find_one("app_settings", {"_id": "system"}) or {}
        discount_rules = {**settings.get("discount_rules", {})}
        # There is no approved automatic bulk discount. Keep the legacy key
        # disabled so an older seeded 2.5% value cannot affect new pricing.
        discount_rules["bulk_rolls"] = {
            **discount_rules.get("bulk_rolls", {}), "enabled": False,
            "discount_percent": 0,
        }
        surcharge_rules = {**settings.get("surcharge_rules", {})}
        surcharge_rules["cut_format"] = {
            **surcharge_rules.get("cut_format", {}), "enabled": False,
            "percent": surcharge_rules.get("cut_format", {}).get("percent", 5),
            "applies_to_categories": ["blankets"],
        }
        store.update_one("app_settings", {"_id": "system"}, {
            "watermark_enabled": bool(settings.get("watermark_enabled", True)),
            "issuer": {
                **settings.get("issuer", {}),
                "name": "Moneda Technologies",
                "email": settings.get("issuer", {}).get("email") or "business@monedatechnologies.com",
                "phone": settings.get("issuer", {}).get("phone"),
                "address": settings.get("issuer", {}).get("address"),
            },
            "surcharge_rules": surcharge_rules,
            "discount_rules": discount_rules,
            "quotation_pricing_policy": "eur_only_no_tax_v1",
            "payment_terms": ["Advance", "POD", "30 Days from receipt", "60 Days", "Custom"],
            "transport_options": ["by_consignee", "by_moneda_team"],
        })

    if demo_mode:
        company_id = "company-moneda-demo"
        if not store.find_one("companies", {"_id": company_id}):
            store.insert_one("companies", {
                "_id": company_id,
                "name": "Moneda Technologies",
                "legal_name": "Moneda Technologies",
                "issuer": True, "default_currency": "EUR", "active": True,
            })
        else:
            store.update_one("companies", {"_id": company_id}, {
                "name": "Moneda Technologies", "legal_name": "Moneda Technologies", "issuer": True,
            })
        demo_admin = store.find_one("users", {"email": "demo@moneda.local"})
        demo_admin_fields = {
            "username": "Admin",
            "name": "Superadmin",
            "phone": "+91 00000 00000",
            "password_hash": generate_password_hash("123@Admin"),
            "role_id": "superadmin",
            "company_ids": [company_id],
            "customer_company_ids": [company_id],
            "customer_ids": [company_id, "customer-demo-1", "customer-demo-2"],
            "active": True,
            "demo": True,
            "device_access_mode": "any_authorized_device",
        }
        if not demo_admin:
            store.insert_one("users", {
                "_id": "user-demo-admin", "email": "demo@moneda.local", **demo_admin_fields,
            })
        else:
            # Keep the documented local credentials available after a demo
            # restart, even when a test or a previous seed created the row.
            store.update_one("users", {"_id": demo_admin["_id"]}, demo_admin_fields)
        demo_customers = [
            # Legacy compatibility snapshot; hidden from customer selection.
            (company_id, "Moneda Technologies - Demo", "", "business@monedatechnologies.com"),
            ("customer-demo-1", "Northstar Printworks", "Priya Shah", "procurement@northstar.example"),
            ("customer-demo-2", "Orbit Packaging", "Rahul Iyer", "operations@orbit.example"),
        ]
        for customer_id, name, contact, email in demo_customers:
            preferred_currency = "EUR" if customer_id == company_id else "INR"
            if not store.find_one("customers", {"_id": customer_id}):
                store.insert_one("customers", {
                    "_id": customer_id, "customer_id": customer_id, "name": name, "company_name": name,
                    "contact_name": contact, "email": email, "phone": "+91 00000 00000",
                    "address": "Customer address pending", "country": "India", "preferred_currency": preferred_currency,
                    "continent": "Asia", "country_code": "IN", "country_name": "India",
                    "default_currency": preferred_currency,
                    "payment_terms": "30 days", "status": "active", "active": True,
                    "demo": True, "is_issuer": customer_id == company_id, "customer_code": customer_code(name),
                })
            else:
                store.update_one("customers", {"_id": customer_id}, {
                    "customer_id": customer_id, "name": name, "company_name": name,
                    "contact_name": contact, "email": email, "active": True, "status": "active",
                    "preferred_currency": preferred_currency, "default_currency": preferred_currency,
                    "continent": "Asia", "country_code": "IN", "country_name": "India",
                    "customer_code": (store.find_one("customers", {"_id": customer_id}) or {}).get("customer_code") or customer_code(name),
                    "is_issuer": customer_id == company_id,
                })


def sync_underpacking_catalog(store: Store, data_directory: Path) -> dict[str, int]:
    """Reconcile live Underpacking rows without reseeding the whole app.

    Production MongoDB is normally not fully auto-seeded on every restart.
    Underpacking is nevertheless a canonical, source-owned catalog: stale
    Polipack/Mark3ZET rows must not remain selectable and MPack must carry the
    structured machine price list. This focused, idempotent reconciliation
    keeps historical cart/quotation documents intact while updating only the
    Underpacking collections.
    """
    catalog = _catalog_seed(data_directory)
    canonical_products = [row for row in catalog["products"] if row.get("category_id") == "mpacks"]
    canonical_ids = {row["_id"] for row in canonical_products}
    deactivated = 0

    for existing in store.list("products", {"category_id": "mpacks"}, limit=100_000)[0]:
        if existing.get("_id") not in canonical_ids and (
            existing.get("active", True) or existing.get("calculator_enabled", True)
        ):
            store.update_one("products", {"_id": existing["_id"]}, {
                "active": False, "available": False, "catalog_visible": False,
                "calculator_enabled": False,
            })
            deactivated += 1

    upserted = 0
    for item in canonical_products:
        # The structured MPack matrix is canonical and server-owned. Replacing
        # this product document clears stale generic/pending configuration.
        if store.find_one("products", {"_id": item["_id"]}):
            store.update_one("products", {"_id": item["_id"]}, item)
        else:
            store.insert_one("products", item)
        upserted += 1

    canonical_types = catalog["mpack_types"]
    canonical_type_ids = {row["_id"] for row in canonical_types}
    for existing in store.list("mpack_types", limit=10_000)[0]:
        if existing.get("_id") not in canonical_type_ids:
            store.delete_one("mpack_types", {"_id": existing["_id"]})
    for item in canonical_types:
        if store.find_one("mpack_types", {"_id": item["_id"]}):
            store.update_one("mpack_types", {"_id": item["_id"]}, item)
        else:
            store.insert_one("mpack_types", item)

    options = next((row for row in catalog["catalog_options"] if row.get("_id") == "mpacks"), None)
    if options:
        if store.find_one("catalog_options", {"_id": "mpacks"}):
            store.update_one("catalog_options", {"_id": "mpacks"}, options)
        else:
            store.insert_one("catalog_options", options)

    return {"deactivated": deactivated, "canonical_products": upserted}


def sync_blanket_catalog(store: Store, data_directory: Path) -> dict[str, int]:
    """Reconcile blanket products so Mongo and the JSON catalogue share IDs."""
    catalog = _catalog_seed(data_directory)
    canonical = [row for row in catalog["products"] if row.get("category_id") == "blankets"]
    ids = {row["_id"] for row in canonical}
    deactivated = 0
    for existing in store.list("products", {"category_id": "blankets"}, limit=100_000)[0]:
        if existing.get("_id") not in ids and existing.get("active", True):
            store.update_one("products", {"_id": existing["_id"]}, {"active": False, "available": False, "catalog_visible": False})
            deactivated += 1
    upserted = 0
    for item in canonical:
        if store.find_one("products", {"_id": item["_id"]}):
            store.update_one("products", {"_id": item["_id"]}, item)
        else:
            store.insert_one("products", item)
        upserted += 1
    return {"deactivated": deactivated, "canonical_products": upserted}


def sync_machine_catalog(store: Store, data_directory: Path) -> int:
    """Load the dedicated machine JSON into MongoDB on every startup."""
    document = _load(data_directory / "machines.json")
    rows = document.get("machines", [])
    canonical_ids = {row.get("id") for row in rows}
    if any(not value for value in canonical_ids) or len(canonical_ids) != len(rows):
        raise ValueError("Machine IDs must be present and unique")
    updated = 0
    for row in rows:
        machine = {
            "_id": row["id"], "name": row["name"],
            "manufacturer": row.get("manufacturer"),
            "machine_model": row.get("machine_model"),
            "active": True, "source": "canonical_catalogue",
        }
        if store.find_one("machines", {"_id": machine["_id"]}):
            store.update_one("machines", {"_id": machine["_id"]}, machine)
        else:
            store.insert_one("machines", machine)
        updated += 1
    # Existing production blanket documents may predate machines.json. Keep
    # their configuration in sync so product detail, selectors and pricing all
    # consume the same canonical machine IDs without requiring a full reseed.
    machine_options = [
        {"id": row["_id"], "name": row["name"], "manufacturer": row.get("manufacturer"), "machine_model": row.get("machine_model")}
        for row in store.list("machines", {"active": {"$ne": False}}, limit=2000, sort="name", direction=1)[0]
    ]
    for product in store.list("products", {"category_id": "blankets"}, limit=100_000)[0]:
        configuration = dict(product.get("configuration") or {})
        if configuration.get("machine_options") != machine_options:
            configuration["machine_options"] = machine_options
            store.update_one("products", {"_id": product["_id"]}, {"configuration": configuration})
    return updated


def sync_commercial_units(store: Store) -> int:
    """Backfill the canonical commercial unit on existing product documents."""
    units = {"blankets": "pc", "mpacks": "box", "chemicals": "litre"}
    updated = 0
    for category_id, commercial_unit in units.items():
        for product in store.list("products", {"category_id": category_id}, limit=100_000)[0]:
            if product.get("commercial_unit") != commercial_unit:
                store.update_one("products", {"_id": product["_id"]}, {"commercial_unit": commercial_unit})
                updated += 1
    return updated
