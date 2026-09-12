from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from flask import current_app

from app.pricing.engine import (
    calculate_line, calculate_quote_totals, resolve_product_adjustments,
    validate_blanket_machine_selection, with_display_currency,
)
from app.repositories.store import Store, utcnow
from app.customers.codes import available_customer_code


class QuotationService:
    """Build quotation snapshots with a fixed Moneda issuer and selected customer company."""

    def __init__(self, store: Store, exchange_rate_service: Any) -> None:
        self.store = store
        self.exchange_rate_service = exchange_rate_service

    def preview(self, *, payload: dict[str, Any], user: dict[str, Any], customer_company: dict[str, Any]) -> dict[str, Any]:
        return self._build(payload=payload, user=user, customer_company=customer_company, persist=False)

    def create(self, *, payload: dict[str, Any], user: dict[str, Any], customer_company: dict[str, Any]) -> dict[str, Any]:
        return self._build(payload=payload, user=user, customer_company=customer_company, persist=True)

    def _build(
        self, *, payload: dict[str, Any], user: dict[str, Any], customer_company: dict[str, Any] | None, persist: bool,
    ) -> dict[str, Any]:
        if not customer_company:
            raise LookupError("Customer company not found")

        # The selected customer business is the sole TO party.  Older clients
        # may still send a contact ``customer_id``; that value is ignored when
        # a legacy company scope is supplied by the route.
        customer_id = customer_company.get("customer_id") or customer_company["_id"]

        settings = self.store.find_one("app_settings", {"_id": "system"}) or {}
        payment_terms = str(payload.get("payment_terms") or "Advance").strip()
        legacy_terms = {"30 Days": "30 Days from receipt", "30 days": "30 Days from receipt", "60 days": "60 Days"}
        payment_terms = legacy_terms.get(payment_terms, payment_terms)
        allowed_terms = {"Advance", "POD", "30 Days from receipt", "60 Days", "Custom"}
        if payment_terms not in allowed_terms:
            raise ValueError("Payment terms must be Advance, POD, 30 Days from receipt, 60 Days or Custom")
        if payment_terms == "Custom":
            try:
                custom_days = int(payload.get("custom_payment_days"))
            except (TypeError, ValueError):
                raise ValueError("Custom payment terms require a positive whole number of days") from None
            if custom_days <= 0:
                raise ValueError("Custom payment terms require a positive whole number of days")
            payment_terms = f"Custom: {custom_days} Days"
        validity = int(payload.get("proforma_validity_days", payload.get("validity_days", settings.get("quotation_validity_days", 30))))
        if validity < 1 or validity > 365:
            raise ValueError("Proforma Validity must be between 1 and 365 days")

        transport_mode = str(payload.get("transport_mode") or (payload.get("transport") or {}).get("mode") or "by_consignee")
        if transport_mode not in {"by_consignee", "by_moneda_team"}:
            raise ValueError("Transport must be By Consignee or By Moneda Team")
        raw_transport = payload.get("transport_charges", payload.get("transport_cost"))
        if transport_mode == "by_moneda_team" and raw_transport in (None, ""):
            raise ValueError("Transport Charges are required when transport is By Moneda Team")
        try:
            transport_charges = Decimal(str(raw_transport or 0))
        except InvalidOperation as exc:
            raise ValueError("Transport Charges must be numeric") from exc
        if transport_charges < 0:
            raise ValueError("Transport Charges cannot be negative")
        if transport_mode == "by_consignee":
            transport_charges = Decimal("0")

        # New customer-facing quotations are always authored from EUR master
        # prices. A submitted currency is a display preference only.
        currency = "EUR"
        rate, rate_meta = self.exchange_rate_service.rate_for(currency)
        cart = self.store.find_one("carts", {"user_id": user["_id"], "customer_id": customer_id})
        if not cart:
            cart = self.store.insert_one("carts", {"user_id": user["_id"], "customer_id": customer_id, "active": True})
        cart_items, _ = self.store.list(
            "cart_items", {"user_id": user["_id"], "$or": [{"customer_id": customer_id}, {"customer_company_id": customer_id}, {"company_id": customer_id}]},
            limit=250, sort="created_at", direction=1,
        )
        if not cart_items:
            raise ValueError("The quotation cart is empty")
        for cart_item in cart_items:
            if cart_item.get("cart_id") != cart["_id"]:
                self.store.update_one("cart_items", {"_id": cart_item["_id"]}, {"cart_id": cart["_id"]})

        lines = []
        for cart_item in cart_items:
            product = self.store.find_one("products", {"_id": cart_item["product_id"], "active": True})
            if not product:
                raise LookupError(f"Product {cart_item['product_id']} is unavailable")
            configuration = cart_item.get("configuration", {})
            saved = cart_item.get("pricing_preview") or {}
            saved_quantity = int(saved.get("requested_quantity", saved.get("quantity", 0)) or 0)
            use_saved_eur = (
                saved.get("master_currency") == "EUR"
                and saved.get("product_id") == cart_item.get("product_id")
                and saved.get("master_final_total") is not None
                and saved_quantity == int(cart_item.get("quantity", 1))
                and float(saved.get("discount_percent", 0)) == float(cart_item.get("discount_percent", 0))
            )
            if use_saved_eur:
                # Cart pricing is a trusted server-created snapshot. Preserve
                # its EUR commercial amount even if a product price or the
                # user's display currency changes before quotation creation.
                line = with_display_currency(saved, "EUR", 1)
                pricing_source = "saved_cart_eur"
            else:
                # Compatibility path for historical carts without explicit EUR
                # snapshots. New and edited carts never take this branch.
                if product.get("category_id") == "blankets":
                    machine_rows, _ = self.store.list("machines", {"active": {"$ne": False}}, limit=2000, sort="name", direction=1)
                    validate_blanket_machine_selection(product, configuration, machine_rows)
                line = calculate_line(
                    product, configuration, quantity=int(cart_item.get("quantity", 1)),
                    discount_percent=cart_item.get("discount_percent", 0), currency=currency, exchange_rate=rate,
                    company_tax_rate=0, company_tax_mode="no_tax",
                    privileged_discount="pricing.discount.override" in user.get("permissions", []),
                    adjustments=resolve_product_adjustments(self.store, product, configuration), business_rules=settings,
                    apply_tax=False, tax_mode_override="no_tax",
                )
                pricing_source = "legacy_cart_recalculated_eur"
            line["discount_source"] = "saved_cart_item"
            line["category_id"] = product.get("category_id")
            # Commercial amounts remain the immutable cart snapshot, while
            # descriptive catalogue copy comes from the current canonical
            # product record when the quotation itself is created.
            line["description"] = product.get("description", "")
            line["commercial_unit"] = product.get("commercial_unit") or (
                "box" if product.get("category_id") == "mpacks" else
                "pc" if product.get("category_id") == "blankets" else
                str(product.get("pricing", {}).get("unit", "unit"))
            )
            current_app.logger.info(
                "quotation_discount quotation=%s cart_item_id=%s customer_id=%s product_id=%s master_currency=EUR master_final_total_eur=%s discount_percent=%s discount_source=saved_cart_item pricing_source=%s",
                "create" if persist else "preview", cart_item.get("_id"), customer_id, product["_id"],
                line["master_final_total"], line["discount_percent"], pricing_source,
            )
            line["thickness"] = configuration.get("thickness_mm") or configuration.get("thickness_micron")
            line["dimensions"] = {
                "length": configuration.get("length"), "width": configuration.get("width"),
                "unit": configuration.get("dimension_unit", "mm"),
            }
            line["format"] = configuration.get("format_type")
            line["bars"] = [
                {"name": adjustment.get("label"), "quantity": adjustment.get("quantity", 1)}
                for adjustment in line.get("adjustments", []) if adjustment.get("type") == "barring"
            ]
            lines.append(line)

        totals = calculate_quote_totals(lines, transport_charges)
        created_at = utcnow()
        quotation_number = "PREVIEW"
        if persist:
            stable_code = str(customer_company.get("customer_code") or available_customer_code(
                self.store, customer_company.get("name", "Customer"), str(customer_id),
            ))
            if not customer_company.get("customer_code"):
                self.store.update_one("customers", {"_id": customer_id}, {"customer_code": stable_code})
            sequence = self.store.next_counter(f"quotation:{customer_id}")
            quotation_number = f"MT-{stable_code}-{sequence:03d}"

        issuer = {
            "name": "Moneda Technologies",
            **(settings.get("issuer") or {}),
            "name": "Moneda Technologies",
        }
        issuer["email"] = issuer.get("email") or "business@monedatechnologies.com"
        customer_snapshot = self._snapshot(customer_company)
        creator_snapshot = {
            key: user.get(key)
            for key in ("name", "email", "phone")
            if user.get(key)
        }

        commercial_conditions = {**settings.get("commercial_conditions", {}), **customer_company.get("commercial_conditions", {})}
        transport = {
            "mode": transport_mode,
            "label": "By Moneda Team" if transport_mode == "by_moneda_team" else "By Consignee",
            "description": "Arranged by Moneda Team" if transport_mode == "by_moneda_team" else "To be borne by consignee",
            "charges": float(transport_charges),
        }
        explicit_customer_notes = payload.get("customer_notes")
        legacy_notes = payload.get("notes", "")
        customer_notes = str(explicit_customer_notes if explicit_customer_notes is not None else legacy_notes).strip()
        commercial_notes = str(legacy_notes)[:4000] if explicit_customer_notes is not None else ""
        document = {
            "quotation_number": quotation_number,
            "issuer": issuer, "issuer_snapshot": issuer,
            "cart_id": cart["_id"],
            "customer_id": customer_id, "customer_snapshot": customer_snapshot,
            # Deprecated aliases retained as read-only compatibility bridges.
            "customer_company_id": customer_id, "customer_company_snapshot": customer_snapshot,
            "company_id": customer_id, "company_snapshot": customer_snapshot,
            "created_by_user_id": user["_id"], "creator_snapshot": creator_snapshot,
            "prepared_by_user_id": user["_id"], "salesperson_id": user["_id"],
            "salesperson_snapshot": self._snapshot(user), "user_id": user["_id"],
            "master_currency": "EUR", "base_currency": "EUR", "quotation_currency": currency,
            "pricing_policy": "eur_only_no_tax_v1",
            "currency": currency, "exchange_rate": rate, "exchange_rate_meta": rate_meta,
            "exchange_rate_provider": rate_meta.get("provider"), "exchange_rate_timestamp": rate_meta.get("fetched_at"),
            "exchange_rate_provider_source": rate_meta.get("provider_source"),
            "exchange_rate_provider_date": rate_meta.get("provider_date") or (rate_meta.get("provider_dates") or {}).get(currency),
            "exchange_rate_expires_at": rate_meta.get("expires_at"),
            "exchange_rate_source": rate_meta.get("source", "live"),
            "master_price_eur": float(sum(Decimal(str(line.get("master_price_eur", 0))) * int(line.get("quantity", 0)) for line in lines)),
            "converted_price": float(sum(Decimal(str(line.get("converted_price", 0))) * int(line.get("quantity", 0)) for line in lines)),
            "lines": lines, "totals": totals, "payment_terms": payment_terms,
            "transport": transport, "notes": commercial_notes,
            "customer_notes": customer_notes,
            "terms": payload.get("terms", []), "commercial_conditions": commercial_conditions,
            "created_at": created_at, "validity_days": validity, "proforma_validity_days": validity,
            "expiry_date": created_at + timedelta(days=validity), "status": "Draft",
            "history": [] if not persist else [{"status": "Draft", "at": created_at, "by": user["_id"]}],
            "preview": not persist,
        }
        idempotency_key = str(payload.get("idempotency_key", ""))[:160]
        if idempotency_key:
            document["idempotency_key"] = idempotency_key
        return self.store.insert_one("quotations", document) if persist else document

    @staticmethod
    def _snapshot(document: dict[str, Any]) -> dict[str, Any]:
        excluded = {"password_hash", "permissions", "created_at", "updated_at"}
        return {key: value for key, value in document.items() if key not in excluded}
