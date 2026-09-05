from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, EmailStr, Field


Currency = Literal["USD", "INR", "EUR"]
TaxMode = Literal["exclusive", "inclusive", "no_tax"]
PricingType = Literal[
    "fixed", "quantity", "per_piece", "per_bar", "per_sqm", "per_meter", "per_litre",
    "per_kg", "per_pack", "per_packet", "per_roll", "formula", "on_request",
]


class PricingConfig(BaseModel):
    pricing_type: PricingType
    price: float | None = Field(default=None, ge=0)
    master_currency: Literal["EUR"] = "EUR"
    unit: str


class TaxConfig(BaseModel):
    mode: TaxMode = "exclusive"
    rate: Literal[0, 5, 12, 18] | None = None
    override_enabled: bool = True


class ProductDocument(BaseModel):
    id: str = Field(alias="_id")
    sku: str
    name: str
    category_id: str
    pricing: PricingConfig
    tax: TaxConfig
    configuration: dict[str, Any] = Field(default_factory=dict)
    pricing_status: Literal["pending", "configured", "on_request"]
    active: bool = True


class CustomerCreate(BaseModel):
    customer_id: str | None = Field(default=None, validation_alias=AliasChoices("customer_id", "customer_company_id", "company_id"))
    company_name: str | None = Field(default=None, min_length=2, max_length=160)
    name: str | None = Field(default=None, min_length=2, max_length=160)
    contact_name: str | None = Field(default=None, max_length=120)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=30)
    preferred_currency: Currency | None = None


class QuotationCreate(BaseModel):
    customer_id: str = Field(min_length=1)
    currency: Currency
    payment_terms: str = Field(default="", max_length=500)
    transport_cost: float = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=4000)
