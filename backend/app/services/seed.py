from __future__ import annotations

import json
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
    "customers.delete", "customers.view_all", "quotations.view", "quotations.view_all", "quotations.create", "quotations.edit",
    "quotations.delete", "quotations.send", "quotations.download", "orders.view", "orders.create",
    "orders.update", "crm.view", "crm.manage", "leads.view", "leads.manage",
    "reminders.view", "reminders.manage", "reports.view", "users.view", "users.create",
    "users.update", "users.delete", "roles.view", "roles.manage", "companies.view",
    "companies.create", "companies.update", "companies.delete", "settings.view",
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
        "product_types.json", "blanket_categories.json", "blanket_options.json",
        "blanket_bars.json", "blankets.json", "mpack_types.json", "mpack_options.json",
        "mpacks.json", "chemical_categories.json", "chemical_options.json",
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
            "name": item["name"], "category_id": "blankets", "description": item.get("application", ""),
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
        products.append({
            "_id": item["id"], "article_no": item.get("article_no"), "sku": item.get("sku", item["id"].upper()),
            "name": type_row["name"], "category_id": "mpacks", "description": item.get("description", ""),
            "pricing": {"master_currency": "EUR", "pricing_type": price["pricing_type"], "unit": price["unit"],
                        "price": price.get("price_eur"), "dimension_prices": price.get("dimension_prices_eur", {})},
            "tax": {"mode": None, "rate": None, "override_enabled": False}, "discount_rules": _discount_rules(),
            "configuration": {
                "configurator": "mpack", "type_id": item["type_id"], "self_adhesive": type_row.get("self_adhesive"),
                "underpacking_type": type_row["name"], "machine_options": mpack_options.get("machines", []),
                "thicknesses": [{"label": f"{row['micron']} micron", "value": row["micron"]} for row in thickness_catalog],
                "size_presets": thickness_catalog, "dimension_units": mpack_options.get("dimension_units", []),
                "complete_containers": mpack_options.get("rules", {}).get("complete_containers", False),
                "fingerprint_fields": ["machine", "thickness_micron", "length", "width", "dimension_unit", "underpacking_type"],
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
            if store.count("price_history", {"product_id": item["_id"]}):
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
                "incoterms": "ICC INCOTERMS 2020: Ex Works unless specified.",
            },
            "discount_rules": {
                "bulk_rolls": {"enabled": False, "minimum_quantity": 10, "discount_percent": 0, "applies_to_categories": ["blankets"]},
            },
            "surcharge_rules": {
                "cut_format": {"enabled": False, "percent": 5, "applies_to_categories": ["blankets"]},
            },
            "payment_terms": ["Advance", "POD", "15 Days", "30 Days"],
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
            "payment_terms": ["Advance", "POD", "15 Days", "30 Days"],
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
