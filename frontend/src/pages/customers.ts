import { customerApi, customerCompanyApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { CONTINENTS, COUNTRIES_BY_CONTINENT, PAYMENT_TERMS, countryMeta } from "../config/customer-metadata";
import type { Customer } from "../types/domain";
import { emptyState, escapeHtml, skeleton } from "../utils/dom";

const customerName = (customer: Customer) => customer.company_name ?? customer.name;

function customerForm(existing?: Customer): HTMLDivElement {
  const region = existing?.region ?? {};
  const continent = existing?.continent ?? region.continent ?? "";
  const countryCode = existing?.country_code ?? region.country_code ?? "";
  const payment = existing?.payment_terms ?? "";
  const customDays = existing?.custom_payment_days ?? "";
  const currency = existing?.preferred_currency ?? existing?.default_currency ?? "EUR";
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form" id="customer-form" autocomplete="off">
    <div class="form-grid">
      <label>Customer company name *<input name="company_name" required placeholder="Registered company" value="${escapeHtml(existing?.company_name ?? existing?.name ?? "")}"><small class="field-error" data-error-for="company_name"></small></label>
      <label>Primary contact *<input name="contact_name" required autocomplete="off" placeholder="Contact person" value="${escapeHtml(existing?.contact_name ?? "")}"><small class="field-error" data-error-for="contact_name"></small></label>
      <label>Email *<input name="email" type="email" required placeholder="procurement@company.com" value="${escapeHtml(existing?.email ?? "")}"><small class="field-error" data-error-for="email"></small></label>
      <label>Phone *<input name="phone" type="tel" required inputmode="tel" placeholder="Enter international phone number" value="${escapeHtml(existing?.phone ?? "")}"><small class="field-error" data-error-for="phone"></small></label>
      <label>Continent / Region *<select name="continent" required><option value="">Select continent / region</option>${CONTINENTS.map((item) => `<option value="${escapeHtml(item)}" ${item === continent ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select><small class="field-error" data-error-for="continent"></small></label>
      <label>Country *<select name="country_code" required ${continent ? "" : "disabled"}><option value="">${continent ? "Select country" : "Select continent first"}</option>${(COUNTRIES_BY_CONTINENT[continent as keyof typeof COUNTRIES_BY_CONTINENT] ?? []).map((item) => `<option value="${item.code}" ${item.code === countryCode ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("")}</select><small class="field-error" data-error-for="country_code"></small></label>
      <label>Display currency *<select name="preferred_currency" required><option value="">Select display currency</option><option ${currency === "EUR" ? "selected" : ""}>EUR</option><option ${currency === "USD" ? "selected" : ""}>USD</option><option ${currency === "INR" ? "selected" : ""}>INR</option></select><small>Reference display only; quotations are always EUR.</small><small class="field-error" data-error-for="preferred_currency"></small></label>
      <label>Payment terms *<select name="payment_terms" required><option value="">Select payment terms</option>${PAYMENT_TERMS.map((item) => `<option value="${item}" ${item === payment ? "selected" : ""}>${item}</option>`).join("")}</select><small class="field-error" data-error-for="payment_terms"></small></label>
      <label class="custom-payment-days" ${payment === "Custom" ? "" : "hidden"}>Custom days *<input name="custom_payment_days" type="number" min="1" step="1" inputmode="numeric" placeholder="Days" value="${customDays}"><small class="field-error" data-error-for="custom_payment_days"></small></label>
      <label>Address *<textarea name="address" required rows="2" placeholder="Customer address">${escapeHtml(existing?.address ?? "")}</textarea><small class="field-error" data-error-for="address"></small></label>
    </div>
    <button class="button button-primary button-full" type="submit"><i data-lucide="save"></i>${existing ? "Save Customer" : "Create Customer"}</button>
  </form>`;
  const form = content.querySelector<HTMLFormElement>("form")!;
  const continentSelect = form.elements.namedItem("continent") as HTMLSelectElement;
  const countrySelect = form.elements.namedItem("country_code") as HTMLSelectElement;
  const paymentSelect = form.elements.namedItem("payment_terms") as HTMLSelectElement;
  const customField = content.querySelector<HTMLElement>(".custom-payment-days")!;
  const renderCountries = (reset = true) => {
    const countries = COUNTRIES_BY_CONTINENT[continentSelect.value as keyof typeof COUNTRIES_BY_CONTINENT] ?? [];
    if (reset) countrySelect.value = "";
    countrySelect.disabled = !continentSelect.value;
    countrySelect.innerHTML = `<option value="">${continentSelect.value ? "Select country" : "Select continent first"}</option>${countries.map((item) => `<option value="${item.code}">${escapeHtml(item.name)}</option>`).join("")}`;
    if (!reset && countryCode) countrySelect.value = countryCode;
  };
  continentSelect.addEventListener("change", () => { renderCountries(true); });
  paymentSelect.addEventListener("change", () => { customField.hidden = paymentSelect.value !== "Custom"; if (paymentSelect.value !== "Custom") (form.elements.namedItem("custom_payment_days") as HTMLInputElement).value = ""; });
  return content;
}

function openCustomerEditor(existing: Customer | undefined, onSaved: () => Promise<void> | void): void {
  const content = customerForm(existing);
  const dialog = openModal(existing ? "Edit Customer" : "Add Customer", content, "wide");
  content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const value = Object.fromEntries(new FormData(form).entries());
    if (value.payment_terms !== "Custom") { delete value.custom_payment_days; }
    const errors: Record<string, string> = {};
    const required = [["company_name", "Company name is required"], ["contact_name", "Primary contact is required"], ["email", "Email is required"], ["phone", "Phone is required"], ["continent", "Continent / region is required"], ["country_code", "Country is required"], ["preferred_currency", "Display currency is required"], ["payment_terms", "Payment terms are required"], ["address", "Address is required"]] as const;
    required.forEach(([field, message]) => { if (!String(value[field] ?? "").trim()) errors[field] = message; });
    if (value.email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value.email))) errors.email = "Enter a valid email address";
    const phone = String(value.phone ?? "").trim();
    const phoneDigits = phone.replace(/\D/g, "");
    if (phone && (!/^\+?[0-9().\-\s]{2,29}$/.test(phone) || phoneDigits.length < 3 || phoneDigits.length > 15)) errors.phone = "Enter a valid international phone number";
    const selected = countryMeta(String(value.continent), String(value.country_code));
    if (value.continent && value.country_code && !selected) errors.country_code = "Country must belong to the selected continent";
    if (value.payment_terms === "Custom" && !/^[1-9]\d*$/.test(String(value.custom_payment_days ?? ""))) errors.custom_payment_days = "Enter a positive whole number of days";
    content.querySelectorAll<HTMLElement>("[data-error-for]").forEach((node) => { node.textContent = errors[node.dataset.errorFor ?? ""] ?? ""; });
    if (Object.keys(errors).length) return;
    try { if (existing) await customerApi.update(existing._id, value); else { const created = await customerApi.create(value); if (created._id) await customerCompanyApi.select(created._id); } dialog.close(); toast(existing ? "Customer updated" : "Customer created"); await onSaved(); }
    catch (error) { toast(error instanceof Error ? error.message : "Customer could not be saved", "error"); }
  });
  refreshIcons(content);
}

export async function customersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Relationships", "Customers", "Manage the customer businesses Moneda Technologies quotes and sells to.", '<button class="button button-secondary"><i data-lucide="download"></i>Export</button><button class="button button-primary" id="add-customer"><i data-lucide="building-2"></i>Add Customer</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  const load = async () => {
    const result = await customerApi.list();
    body.innerHTML = `<div class="table-toolbar"><div class="field-search"><i data-lucide="search"></i><input placeholder="Search customers" aria-label="Search customers"></div><div class="segmented"><button class="active">All</button><button>Active</button><button>Archived</button></div><span>${result.pagination?.total ?? result.items.length} records</span></div>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Customer company</th><th>Primary contact</th><th>Email / phone</th><th>Currency</th><th>Status</th><th></th></tr></thead><tbody>${result.items.map((customer) => { const name = customerName(customer); const id = customer.customer_id ?? customer._id; return `<tr><td><div class="table-identity"><span>${escapeHtml(name.slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(name)}</strong><small>${escapeHtml(customer.address ?? "")}</small></p></div></td><td>${escapeHtml(customer.contact_name ?? "—")}</td><td><strong>${escapeHtml(customer.email ?? "No email")}</strong><small>${escapeHtml(customer.phone ?? "")}</small></td><td><span class="currency-tag">${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</span></td><td>${statusBadge(customer.status ?? "active")}</td><td><a class="icon-button" href="/customers/${encodeURIComponent(id)}" data-route="/customers/${encodeURIComponent(id)}" aria-label="View customer" title="View customer"><i data-lucide="arrow-up-right"></i></a></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("building-2", "No customers yet", "Add a customer business to prepare a quotation.")}`;
    const currencyHeading = [...body.querySelectorAll("th")].find((node) => node.textContent === "Currency");
    if (currencyHeading) currencyHeading.textContent = "Display";
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customers unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector("#add-customer")?.addEventListener("click", () => openCustomerEditor(undefined, load));
  refreshIcons(page);
  return page;
}

export async function customerDetailPage(customerId: string): Promise<HTMLElement> {
  const page = pageScaffold("Relationships", "Customer detail", "A customer business record with commercial history and contact context.", '<a class="button button-secondary" href="/customers" data-route="/customers"><i data-lucide="arrow-left"></i>Back to customers</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  const load = async () => {
    const customer = await customerApi.get(customerId);
    const related = customer.related ?? {};
    const name = customerName(customer);
    const leads = (related.leads ?? []) as Record<string, unknown>[];
    const crmHref = `/crm?customer_id=${encodeURIComponent(customerId)}`;
    const accessMarkup = customer.access ? `<section class="panel"><div class="section-title"><div><span class="eyebrow">Access & ownership</span><h2>Customer access</h2></div></div><div class="detail-grid"><div><span>Created by</span><strong>${escapeHtml(customer.access.created_by?.name ?? "Unknown")}</strong></div><div><span>Assigned users</span><strong>${customer.access.assigned_users?.length ?? 0}</strong></div></div>${customer.access.assigned_users?.length ? `<div class="customer-activity-list">${customer.access.assigned_users.map((member) => `<div><div><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml(member.email ?? "")}</small></div></div>`).join("")}</div>` : '<p class="muted">No assigned users.</p>'}</section>` : "";
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar">${escapeHtml(name.slice(0, 2).toUpperCase())}</div><div><span class="eyebrow">Customer</span><h2>${escapeHtml(name)}</h2><p>${escapeHtml(customer.contact_name ?? "Primary contact not configured")}</p>${statusBadge(customer.status ?? "active")}</div><div class="detail-hero-actions"><a class="button button-secondary" href="${crmHref}" data-route="${crmHref}"><i data-lucide="chart-no-axes-combined"></i>View CRM</a><button class="button button-primary" id="edit-customer"><i data-lucide="pencil"></i>Edit customer</button></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Contact & billing</span><h2>Customer profile</h2></div></div><div class="detail-grid"><div><span>Company name</span><strong>${escapeHtml(name)}</strong></div><div><span>Primary contact</span><strong>${escapeHtml(customer.contact_name ?? "—")}</strong></div><div><span>Email</span><strong>${escapeHtml(customer.email ?? "—")}</strong></div><div><span>Phone</span><strong>${escapeHtml(customer.phone ?? "—")}</strong></div><div><span>Region</span><strong>${escapeHtml(customer.region?.continent ?? customer.continent ?? "—")}</strong></div><div><span>Country</span><strong>${escapeHtml(customer.region?.country_name ?? customer.country_name ?? customer.country ?? "—")}</strong></div><div><span>Display currency</span><strong>${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</strong></div><div><span>Payment terms</span><strong>${escapeHtml(customer.payment_terms_display ?? customer.payment_terms ?? "—")}</strong></div><div class="detail-grid-wide"><span>Address</span><strong>${escapeHtml(customer.address ?? "—")}</strong></div></div></section>${accessMarkup}<section class="panel"><div class="section-title"><div><span class="eyebrow">Related records</span><h2>Customer history</h2></div></div><div class="metric-grid compact-metrics"><a class="metric-card" href="/quotations?customer_id=${encodeURIComponent(customerId)}" data-route="/quotations?customer_id=${encodeURIComponent(customerId)}"><span>Quotations</span><strong>${related.quotations?.length ?? 0}</strong></a><a class="metric-card" href="/orders?customer_id=${encodeURIComponent(customerId)}" data-route="/orders?customer_id=${encodeURIComponent(customerId)}"><span>Orders</span><strong>${related.orders?.length ?? 0}</strong></a><a class="metric-card" href="${crmHref}" data-route="${crmHref}"><span>Leads</span><strong>${related.leads?.length ?? 0}</strong></a><a class="metric-card" href="${crmHref}" data-route="${crmHref}"><span>Opportunities</span><strong>${related.opportunities?.length ?? 0}</strong></a></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">CRM</span><h2>Sales activity</h2></div><a class="button button-quiet" href="${crmHref}" data-route="${crmHref}">Open pipeline<i data-lucide="arrow-up-right"></i></a></div>${leads.length ? `<div class="customer-activity-list">${leads.slice(0, 5).map((lead) => `<div><div><strong>${escapeHtml(String(lead.title ?? lead.notes ?? "Sales lead"))}</strong><small>${escapeHtml(String(lead.next_action ?? lead.follow_up_date ?? "No next action"))}</small></div>${statusBadge(String(lead.status ?? "Lead"))}</div>`).join("")}</div>` : '<p class="muted">No CRM activity yet.</p>'}</section></div>`;
    body.querySelector("#edit-customer")?.addEventListener("click", () => openCustomerEditor(customer, load));
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customer unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
