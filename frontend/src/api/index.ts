import { api, jsonBody, patchBody } from "./client";
import type { CartItem, CatalogOption, Category, Currency, Customer, DashboardData, PageResult, PriceHistoryEntry, PricingResource, Product, Quotation, SessionPayload } from "../types/domain";
import type { CountryMeta } from "../config/customer-metadata";

export const authApi = {
  me: () => api<SessionPayload>("/me"),
  bootstrapSession: () => api<SessionPayload>("/me", {}, { on401: "anonymous" }),
  deviceAccess: () => api<NonNullable<SessionPayload["device_access"]>>("/me/device-access"),
  login: (identifier: string, password: string) => api<{ next_step: string; device_status?: string; application_access?: boolean }>("/auth/login", jsonBody({ identifier, password })),
  demo: () => api<{ next_step: string }>("/auth/demo", jsonBody({})),
  requestOtp: (email: string, purpose: "login" | "signup" | "reset" = "login") => api<null>("/auth/request-otp", jsonBody({ email, purpose })),
  verifyOtp: (email: string, code: string, purpose: "login" | "signup" | "reset" = "login") => api<{ next_step: string; device_status?: string; application_access?: boolean }>("/auth/verify-otp", jsonBody({ email, code, purpose })),
  signupStart: (value: { name: string; username: string; email: string; previous_pending_signup_id?: string }) => api<{ pending_signup_id: string; masked_email: string; otp_sent: boolean }>("/auth/signup/start", jsonBody(value)),
  signupVerifyEmail: (pending_signup_id: string, otp: string) => api<{ pending_signup_id: string; email_verified: boolean }>("/auth/signup/verify-email", jsonBody({ pending_signup_id, otp })),
  signupComplete: (value: { pending_signup_id: string; password: string; confirm_password: string }) => api<{ next_step: string; selection_context?: string; authenticated: boolean; user_id: string }>("/auth/signup/complete", jsonBody(value)),
  register: (value: { name: string; phone: string; password: string }) => api<{ next_step: string }>("/auth/register", jsonBody(value)),
  resetPassword: (password: string) => api<null>("/auth/reset-password", jsonBody({ password })),
  changePassword: (current_password: string, new_password: string) => api<null>("/auth/change-password", jsonBody({ current_password, new_password })),
  logout: () => api<null>("/auth/logout", jsonBody({})),
};
export const profileApi = {
  update: (value: unknown) => api<SessionPayload["user"]>("/me", patchBody(value)),
  requestEmailChange: (email: string) => api<{ pending_email: string; expires_at: string }>("/profile/email-change/request", jsonBody({ email })),
  resendEmailChange: () => api<{ pending_email: string; expires_at: string }>("/profile/email-change/resend", jsonBody({})),
  verifyEmailChange: (code: string) => api<SessionPayload["user"]>("/profile/email-change/verify", jsonBody({ code })),
  cancelEmailChange: () => api<SessionPayload["user"]>("/profile/email-change/cancel", jsonBody({})),
  notifications: () => api<{ items: Record<string, unknown>[]; unread: number; total: number }>("/notifications"),
  markNotificationRead: (id: string) => api<null>(`/notifications/${encodeURIComponent(id)}`, patchBody({ read: true })),
};

export const catalogApi = {
  categories: () => api<Category[]>("/categories"),
  families: () => api<CatalogOption[]>("/catalog/families"),
  blanketCategories: () => api<CatalogOption[]>("/catalog/blankets/categories"),
  blanketBars: () => api<{ items: Array<{ _id: string; article_no?: string; name: string; sku?: string; material?: string; pricing?: { price?: number | null; unit?: string }; pricing_status?: string }>; total: number }>("/catalog/blankets/bars"),
  blanketProducts: (category = "") => api<{ items: Product[]; total: number }>(`/catalog/blankets/products${category ? `?category=${encodeURIComponent(category)}` : ""}`),
  blanketProduct: (id: string) => api<Product>(`/catalog/blankets/products/${encodeURIComponent(id)}`),
  products: (query = "") => api<PageResult<Product>>(`/products?limit=100&${query}`),
  product: (id: string) => api<Product>(`/products/${encodeURIComponent(id)}`),
  preview: (id: string, value: unknown) => api<{ line: import("../types/domain").PriceLine; rate: { base: string; provider: string; provider_source?: string; source?: string; fetched_at: string; expires_at?: string; stale: boolean; warning?: string } }>(`/products/${encodeURIComponent(id)}/price-preview`, jsonBody(value)),
  updatePricing: (id: string, value: unknown) => api<Product>(`/products/${encodeURIComponent(id)}/pricing`, patchBody(value)),
};

export const machineApi = {
  list: () => api<{ items: Array<{ _id: string; name: string; manufacturer?: string; machine_model?: string }>; total: number }>("/machines"),
  create: (manufacturer: string, machineModel: string) => api<{ _id: string; name: string; manufacturer: string; machine_model: string }>("/machines", jsonBody({ manufacturer, machine_model: machineModel })),
};

export const customerCompanyApi = {
  list: () => api<{ items: Customer[]; total: number }>("/customers?limit=500"),
  search: (term: string) => api<{ items: Customer[]; total: number }>(`/customers?search=${encodeURIComponent(term)}&limit=500`),
  select: (customerId: string) => api<{ customer_id: string }>("/companies/select-customer", jsonBody({ customer_id: customerId })),
  clearSelection: () => api<{ customer_id: null }>("/companies/clear-customer", jsonBody({})),
};
/** @deprecated compatibility alias for integrations that still import companyApi. */
export const companyApi = customerCompanyApi;

export const customerApi = {
  countries: () => api<{ countries: CountryMeta[]; total: number }>("/countries"),
  list: (customerId?: string, status?: string) => api<PageResult<Customer>>(`/customers?${customerId ? `customer_id=${encodeURIComponent(customerId)}&` : ""}${status ? `status=${encodeURIComponent(status)}&` : ""}limit=100`),
  get: (id: string) => api<Customer & { related?: Record<string, unknown[]> }>(`/customers/${encodeURIComponent(id)}`),
  create: (value: unknown) => api<Customer>("/customers", jsonBody(value)),
  update: (id: string, value: unknown) => api<Customer>(`/customers/${encodeURIComponent(id)}`, patchBody(value)),
  remove: (id: string, reason: string, permanent = false) => api<null>(`/customers/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ reason, permanent }), headers: { "Content-Type": "application/json" } }),
  restore: (id: string) => api<Customer>(`/customers/${encodeURIComponent(id)}/restore`, jsonBody({})),
};

export const quotationApi = {
  list: (params: string | URLSearchParams = "") => {
    const query = typeof params === "string"
      ? (params.includes("=") || !params ? params : `customer_id=${encodeURIComponent(params)}`)
      : params.toString();
    return api<PageResult<Quotation>>(`/quotations${query ? `?${query}` : ""}`);
  },
  get: (id: string) => api<Quotation>(`/quotations/${encodeURIComponent(id)}`),
  communications: (id: string) => api<{ items: Record<string, unknown>[]; total: number }>(`/quotations/${encodeURIComponent(id)}/communications`),
  create: (value: unknown) => api<Quotation>("/quotations", jsonBody(value)),
  preview: (value: unknown) => api<Quotation>("/quotations/preview", jsonBody(value)),
  send: (id: string, value: { subject?: string; message?: string } = {}) => api<Quotation>(`/quotations/${encodeURIComponent(id)}/send`, jsonBody(value)),
  whatsapp: (id: string) => api<{ delivery: { status: string }; log_id: string }>(`/quotations/${encodeURIComponent(id)}/whatsapp`, jsonBody({})),
  convert: (id: string) => api<unknown>(`/quotations/${encodeURIComponent(id)}/convert-to-order`, jsonBody({})),
  remove: (id: string, reason: string, permanent = true) => api<null>(`/quotations/${encodeURIComponent(id)}`, { method: "DELETE", body: JSON.stringify({ reason, permanent }), headers: { "Content-Type": "application/json" } }),
  restore: (id: string) => api<Quotation>(`/quotations/${encodeURIComponent(id)}/restore`, jsonBody({})),
};

export const cartApi = {
  get: (customerId: string, currency: Currency = "EUR") => api<{ customer: { id: string; name: string }; customer_id: string; customer_name: string; customer_company_id?: string; customer_company_name?: string; company_id?: string; company_name?: string; item_count: number; items: CartItem[]; master_currency?: "EUR"; display_currency?: Currency; totals: Record<string, number>; master_totals?: Record<string, number> }>(`/cart?customer_id=${encodeURIComponent(customerId)}&currency=${currency}`),
  add: (value: unknown) => api<CartItem>("/cart/items", jsonBody(value)),
  update: (id: string, value: unknown) => api<CartItem>(`/cart/items/${encodeURIComponent(id)}`, patchBody(value)),
  remove: (id: string) => api<null>(`/cart/items/${encodeURIComponent(id)}`, { method: "DELETE" }),
  clear: (customerId: string) => api<{ removed: number }>(`/cart?customer_id=${encodeURIComponent(customerId)}`, { method: "DELETE" }),
};

export const dashboardApi = { get: (customerId?: string) => api<DashboardData>(`/dashboard${customerId ? `?customer_id=${encodeURIComponent(customerId)}` : ""}`) };
export const rateApi = { get: (refresh = false) => api<{ base: string; rates: Record<Currency, number>; provider: string; provider_source?: string; source?: string; status?: "latest" | "stored_fallback" | "unavailable" | "live" | "cached"; rate_date?: string | null; provider_dates?: Record<string, string | null>; provider_fetched_at?: string; last_successful_refresh_at?: string; fetched_at: string; expires_at?: string; stale: boolean; warning?: string }>(`/exchange-rates${refresh ? "?refresh=true" : ""}`) };
export const crmApi = {
  leads: (query = "") => api<{ items: Record<string, unknown>[]; total: number; pagination?: { page: number; limit: number; total: number } }>(`/leads${query ? `?${query}` : ""}`),
  createLead: (value: unknown) => api<Record<string, unknown>>("/leads", jsonBody(value)),
  reminders: (customerId?: string) => api<{ items: Record<string, unknown>[]; total: number }>(`/reminders${customerId ? `?customer_id=${encodeURIComponent(customerId)}` : ""}`),
  completeReminder: (id: string) => api<{ reminder: Record<string, unknown>; next_reminder?: Record<string, unknown> | null }>(`/reminders/${encodeURIComponent(id)}/complete`, jsonBody({})),
};
export const orderApi = {
  list: (customerId?: string) => api<{ items: Record<string, unknown>[]; total: number }>(customerId ? `/orders?customer_id=${encodeURIComponent(customerId)}` : "/orders"),
  get: (id: string) => api<Record<string, unknown>>(`/orders/${encodeURIComponent(id)}`),
  sendConfirmation: (id: string) => api<{ sent: boolean; diagnostic_id?: string }>(`/orders/${encodeURIComponent(id)}/send-confirmation`, jsonBody({})),
  sendStatus: (id: string) => api<{ sent: boolean; diagnostic_id?: string }>(`/orders/${encodeURIComponent(id)}/send-status`, jsonBody({})),
};
export const adminApi = {
  users: () => api<{ items: Record<string, unknown>[]; total: number }>("/admin/users"),
  createUser: (value: unknown) => api<{ user: Record<string, unknown>; invitation: { email_sent: boolean; status: "sent" | "failed"; diagnostic_id: string; error_code?: string } }>("/admin/users", jsonBody(value)),
  userDevices: (userId: string) => api<{ items: Record<string, unknown>[]; total: number }>(`/admin/users/${encodeURIComponent(userId)}/devices`),
  approveDevice: (userId: string, deviceId: string) => api<Record<string, unknown>>(`/admin/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/approve`, jsonBody({})),
  rejectDevice: (userId: string, deviceId: string, reason: string) => api<Record<string, unknown>>(`/admin/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/reject`, jsonBody({ reason })),
  reinstateDevice: (userId: string, deviceId: string, reason: string) => api<Record<string, unknown>>(`/admin/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/reinstate`, jsonBody({ reason })),
  revokeDevice: (userId: string, deviceId: string, reason: string) => api<Record<string, unknown>>(`/admin/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}/revoke`, jsonBody({ reason })),
  deleteDevice: (userId: string, deviceId: string, reason: string) => api<Record<string, unknown>>(`/admin/users/${encodeURIComponent(userId)}/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE", body: JSON.stringify({ reason }), headers: { "Content-Type": "application/json" } }),
  updateUser: (id: string, value: unknown) => api<Record<string, unknown>>(`/admin/users/${encodeURIComponent(id)}`, patchBody(value)),
  roles: () => api<{ items: Record<string, unknown>[]; total: number }>("/admin/roles"),
  settings: () => api<Record<string, unknown>>("/settings"),
  auditLogs: () => api<{ items: Record<string, unknown>[]; total: number; page: number }>("/admin/audit-logs"),
  updateSettings: (value: unknown) => api<Record<string, unknown>>("/settings", patchBody(value)),
  routing: () => api<{ cc: Array<Record<string, unknown>>; bcc: Array<Record<string, unknown>> }>("/integrations/zoho/routing"),
  addRouting: (value: unknown) => api<Record<string, unknown>>("/integrations/zoho/routing", jsonBody(value)),
  updateRouting: (value: unknown) => api<Record<string, unknown>>("/integrations/zoho/routing", patchBody(value)),
  removeRouting: (value: unknown) => api<Record<string, unknown>>("/integrations/zoho/routing", { method: "DELETE", body: JSON.stringify(value), headers: { "Content-Type": "application/json" } }),
  zohoStatus: () => api<{
    provider: string; status: "connected" | "not_connected" | "error"; configured: boolean;
    connected: boolean; oauth: "connected" | "not_connected"; account_email: string;
    account_id?: string | null; account_id_configured: boolean; account_id_status: "configured" | "missing";
    api_domain: string; api_domain_status: "configured" | "derived" | "missing";
    scopes: string[]; connected_at?: string; updated_at?: string;
    email_senders: Array<{ purpose: "otp" | "quotation" | "order" | "general"; from_name: string; address: string; available: boolean | null }>;
    customer_recipient_policy: { cc: Array<{ address: string; enabled: boolean }>; bcc: Array<{ address: string; enabled: boolean }> };
    sender_validation_error?: { code: string; stage: string; diagnostic_id: string } | null;
    last_error?: { code: string; stage: string; diagnostic_id: string } | null;
  }>("/integrations/zoho/status"),
  zohoTest: (to: string) => api<{ sent: boolean; diagnostic_id?: string; stage?: string; checks: Array<{ stage: string; result: string; source?: string }> }>("/integrations/zoho/test", jsonBody({ to })),
  zohoDisconnect: () => api<{ connected: boolean; revoked: boolean; diagnostic_id: string }>("/integrations/zoho/disconnect", jsonBody({})),
  pricingProducts: (query = "") => api<{ items: PricingResource[]; total: number }>(`/admin/pricing/products${query ? `?${query}` : ""}`),
  pricingProduct: (id: string) => api<PricingResource & { history: PriceHistoryEntry[]; history_total: number }>(`/admin/pricing/products/${encodeURIComponent(id)}`),
  updatePrice: (id: string, value: unknown) => api<PricingResource>(`/admin/pricing/products/${encodeURIComponent(id)}`, patchBody(value)),
  priceHistory: (id: string) => api<{ items: PriceHistoryEntry[]; total: number }>(`/admin/pricing/history/${encodeURIComponent(id)}`),
  validateImport: (file: File) => { const body = new FormData(); body.append("file", file); return api<{ valid_rows: unknown[]; invalid_rows: unknown[]; warnings: unknown[]; summary: Record<string, number> }>("/admin/import/products/validate", { method: "POST", body }); },
  commitImport: (file: File) => { const body = new FormData(); body.append("file", file); body.append("confirm", "true"); return api<unknown>("/admin/import/products", { method: "POST", body }); },
};
