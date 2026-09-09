export type Currency = "USD" | "INR" | "EUR";

export interface ApiEnvelope<T> {
  success: boolean;
  data: T;
  message: string | null;
  errors: unknown[];
  error?: string;
  request_id?: string;
  diagnostic_id?: string;
  stage?: string;
}

export interface Company {
  _id: string;
  name: string;
  legal_name?: string;
  default_currency: Currency;
  default_tax_rate: number;
  default_tax_mode: "exclusive" | "inclusive" | "no_tax";
  tax_enabled?: boolean;
  tax_jurisdiction?: string;
  transport_taxable?: boolean;
  country?: string;
  continent?: string;
  country_code?: string;
  country_name?: string;
  region?: { continent?: string; country_code?: string; country_name?: string };
  address?: string;
  email?: string;
  active: boolean;
}

export interface User {
  _id: string;
  username?: string;
  name: string;
  email: string;
  phone?: string;
  role_id: string;
  role_display_name: string;
  company_ids: string[];
  customer_ids?: string[];
  customer_company_ids?: string[];
  assigned_customer_ids?: string[];
  customer_access_count?: number | null;
  customer_access_global?: boolean;
  permissions: string[];
  active?: boolean;
  email_verified?: boolean;
  pending_email?: string;
  pending_email_verification_expires_at?: string;
  created_at?: string;
  demo?: boolean;
}

export interface SessionPayload {
  user: User;
  customers?: Customer[];
  companies: Company[];
  customer_companies?: Company[];
  selected_customer_company_id?: string | null;
  active_company_id?: string | null;
  active_customer_id?: string | null;
  selected_customer_id?: string | null;
  issuer?: Issuer;
}

export interface Issuer {
  name: string;
  email?: string;
  phone?: string;
  address?: string;
  country?: string;
  legal_name?: string;
  logo?: string;
}

export interface Category {
  _id: string;
  name: string;
  description: string;
  icon?: string;
  calculator_enabled?: boolean;
  sort_order: number;
}

export interface CatalogOption { id: string; name: string }

export interface Product {
  _id: string;
  article_no?: string;
  sku: string;
  name: string;
  category_id: string;
  description: string;
  commercial_unit?: "pc" | "box" | "litre" | string;
  pricing: { pricing_type: string; price: number | null; variant_prices?: Record<string, number | null>; master_currency: "EUR"; unit: string };
  tax: { mode: "exclusive" | "inclusive" | "no_tax" | null; rate: number | null; override_enabled: boolean };
  discount_rules: { enabled: boolean; step: number; default_max_percent: number; privileged_max_percent: number; restricted?: boolean };
  configuration: Record<string, unknown>;
  pricing_status: "pending" | "configured" | "on_request" | "inactive";
  active: boolean;
}

export interface PricingResource {
  id: string;
  entity_type: "product" | "bar";
  name: string;
  article_no: string;
  sku: string;
  family_id: "blankets" | "mpacks" | "chemicals" | "bars";
  family_name: string;
  category_name: string;
  active: boolean;
  pricing_status: "pending" | "configured" | "on_request" | "inactive";
  pricing: {
    master_currency: "EUR";
    pricing_type: string;
    unit: string;
    price_eur: number | null;
    variant_prices?: Record<string, number | null>;
    dimension_prices?: Record<string, number | null>;
    package_prices?: Record<string, number | null>;
  };
  price_updated_at?: string;
  price_updated_by?: string;
}

export interface PriceHistoryEntry {
  _id: string;
  resource_id?: string;
  product_id?: string;
  entity_type?: string;
  price_map?: string | null;
  price_key?: string | null;
  old_price_eur?: number | null;
  new_price_eur?: number | null;
  old_price?: number | null;
  new_price?: number | null;
  currency: "EUR";
  changed_by_name?: string;
  changed_by?: string;
  changed_at?: string;
  created_at?: string;
  reason?: string;
}

export interface Customer {
  _id: string;
  customer_id?: string;
  company_id?: string;
  company_name?: string;
  name: string;
  contact_name?: string;
  email?: string;
  phone?: string;
  alternate_phone?: string;
  legal_name?: string;
  address?: string;
  country?: string;
  continent?: string;
  country_code?: string;
  country_name?: string;
  region?: { continent?: string; country_code?: string; country_name?: string };
  billing_address?: string;
  shipping_address?: string;
  gst_vat_number?: string;
  tax_number?: string;
  preferred_currency?: Currency;
  default_currency?: Currency;
  default_tax_rate?: number;
  default_tax_mode?: "exclusive" | "inclusive" | "no_tax";
  tax_enabled?: boolean;
  payment_terms?: string;
  custom_payment_days?: number;
  payment_terms_display?: string;
  tax_profile?: { gst_applicable?: boolean; tax_number?: string };
  access?: {
    created_by?: { name: string; email?: string } | null;
    assigned_users?: Array<{ name: string; email?: string }>;
  };
  status: string;
  active?: boolean;
}

export interface PriceLine {
  product_id: string;
  article_no?: string;
  product_name: string;
  description?: string;
  configuration?: Record<string, unknown>;
  commercial_unit?: "pc" | "box" | "litre" | string;
  pricing_unit: string;
  requested_quantity?: number;
  quantity: number;
  currency: Currency;
  display_currency?: Currency;
  quotation_currency?: "EUR";
  master_currency: "EUR";
  exchange_rate: number;
  master_price_eur?: number;
  converted_price?: number;
  master_subtotal?: number;
  master_discount_amount?: number;
  master_total?: number;
  master_final_total?: number;
  master_discounted_unit_price?: number;
  display_unit_price?: number;
  display_subtotal?: number;
  display_discount_amount?: number;
  display_total?: number;
  display_final_total?: number;
  master_unit_price: number;
  base_unit_price_master: number;
  adjustments: Array<{ type: string; label: string; amount_master: number; quantity?: number }>;
  area_sqm?: number;
  price_per_sheet_eur?: number;
  price_per_box_eur?: number;
  discounted_price_per_sheet_eur?: number;
  discounted_price_per_box_eur?: number;
  sheets_per_box?: number;
  price_list?: { id?: string; valid_from?: string; valid_until?: string };
  unit_price: number;
  subtotal: number;
  discount_percent: number;
  discount_amount: number;
  taxable_amount?: number;
  tax_rate?: number;
  tax_mode?: string;
  tax_amount?: number;
  taxable_subtotal?: number;
  gst_applicable?: boolean;
  gst_rate?: number;
  gst_amount?: number;
  is_gst_inclusive?: boolean;
  total?: number;
  line_total: number;
}

export interface CartItem {
  _id: string;
  product_id: string;
  customer_id?: string;
  company_id?: string;
  customer_company_id?: string;
  configuration: Record<string, unknown>;
  quantity: number;
  discount_percent: number;
  currency: Currency;
  master_currency?: "EUR";
  display_currency?: Currency;
  master_unit_price_eur?: number;
  master_subtotal?: number;
  master_discount_amount?: number;
  master_final_total?: number;
  tax_enabled?: boolean;
  tax_mode?: "exclusive" | "inclusive" | "no_tax";
  pricing_preview: PriceLine;
}

export interface Quotation {
  _id: string;
  quotation_number: string;
  customer_id: string;
  company_id?: string;
  customer_company_id?: string;
  issuer_snapshot?: Issuer;
  customer_company_snapshot?: Company;
  prepared_by_user_id?: string;
  created_by_user_id?: string;
  creator_snapshot?: Pick<User, "name" | "email" | "phone">;
  salesperson_snapshot?: User;
  customer_snapshot: Customer;
  company_snapshot?: Company;
  currency: Currency;
  master_currency: "EUR";
  exchange_rate: number;
  master_price_eur?: number;
  converted_price?: number;
  exchange_rate_provider?: string;
  exchange_rate_provider_source?: string;
  exchange_rate_provider_date?: string | null;
  exchange_rate_timestamp?: string;
  exchange_rate_expires_at?: string | null;
  exchange_rate_source?: "live" | "cached" | "master";
  status: string;
  lines: PriceLine[];
  totals: { subtotal: number; discount_amount: number; taxable_amount?: number; product_tax_amount?: number; tax_amount?: number; transport_cost: number; transport_tax_amount?: number; transport_total?: number; grand_total: number };
  payment_terms?: string;
  validity_days?: number;
  proforma_validity_days?: number;
  transport?: { mode: string; label: string; description: string; charges: number; taxable?: boolean; tax_rate?: number; tax_mode?: string };
  notes?: string;
  customer_notes?: string;
  preview?: boolean;
  preview_pdf_base64?: string;
  created_at: string;
  expiry_date: string;
}

export interface PageResult<T> {
  items: T[];
  pagination?: { page: number; limit: number; total: number; pages?: number };
  total?: number;
  scope?: "own" | "all";
}

export interface DashboardData {
  metrics: {
    quotations: number;
    open_quotations: number;
    accepted_quotations: number;
    orders: number;
    revenue: number;
    conversion_rate: number;
    leads: number;
    follow_ups_due: number;
  };
  quotation_status: Record<string, number>;
  recent_quotations: Quotation[];
  recent_customers: Customer[];
}
