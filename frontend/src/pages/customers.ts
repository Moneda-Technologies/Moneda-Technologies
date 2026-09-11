import { customerApi, customerCompanyApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { PAYMENT_TERMS } from "../config/customer-metadata";
import type { CountryMeta } from "../config/customer-metadata";
import type { Company, Customer } from "../types/domain";
import { emptyState, escapeHtml, skeleton } from "../utils/dom";
import { appStore } from "../state/store";
import { beginCustomerContextChange, customerContextSignal, isCurrentCustomerContextRevision } from "../state/customer-context";

const customerName = (customer: Customer) => customer.company_name ?? customer.name;
let countryCataloguePromise: Promise<CountryMeta[]> | null = null;

async function loadCountryCatalogue(): Promise<CountryMeta[]> {
  if (!countryCataloguePromise) {
    countryCataloguePromise = customerApi.countries().then((result) => result.countries);
    countryCataloguePromise.catch(() => { countryCataloguePromise = null; });
  }
  return countryCataloguePromise;
}

function customerForm(countries: CountryMeta[], existing?: Customer): HTMLDivElement {
  const region = existing?.region ?? {};
  const countryCode = existing?.country_code ?? region.country_code ?? "";
  const countryName = existing?.country_name ?? region.country_name ?? existing?.country ?? "";
  const initialCountry = countries.find((country) => country.code === countryCode)
    ?? countries.find((country) => country.name.toLocaleLowerCase() === countryName.toLocaleLowerCase())
    ?? null;
  const continent = initialCountry?.region ?? existing?.continent ?? region.continent ?? "";
  const regions = [...new Set(countries.map((country) => country.region))].sort((a, b) => a.localeCompare(b));
  const payment = existing?.payment_terms ?? "";
  const customDays = existing?.custom_payment_days ?? "";
  const currency = existing?.preferred_currency ?? existing?.default_currency ?? initialCountry?.default_display_currency ?? "EUR";
  const countryResultsId = `customer-country-results-${crypto.randomUUID()}`;
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form customer-form" id="customer-form" autocomplete="off">
    <div class="form-grid">
      <label>Customer company name *<input name="company_name" required placeholder="Registered company" value="${escapeHtml(existing?.company_name ?? existing?.name ?? "")}"><small class="field-error" data-error-for="company_name"></small></label>
      <label>Primary contact *<input name="contact_name" required autocomplete="off" placeholder="Contact person" value="${escapeHtml(existing?.contact_name ?? "")}"><small class="field-error" data-error-for="contact_name"></small></label>
      <label>Email *<input name="email" type="text" inputmode="email" required placeholder="procurement@company.com or -" value="${escapeHtml(existing?.email ?? "")}"><small>Use - if the email is not known.</small><small class="field-error" data-error-for="email"></small></label>
      <label>Phone *<input name="phone" type="tel" required inputmode="tel" placeholder="International phone number or -" value="${escapeHtml(existing?.phone ?? "")}"><small>Use - if the phone number is not known.</small><small class="field-error" data-error-for="phone"></small></label>
      <label>Region / Continent *<select name="continent" required><option value="">Select a region</option>${regions.map((item) => `<option value="${escapeHtml(item)}" ${item === continent ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select><small>Select a region to filter countries, or search for a country and the region will be selected automatically.</small><small class="field-error" data-error-for="continent"></small></label>
      <label>Country *<span class="customer-country-combobox"><input name="country_search" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${countryResultsId}" autocomplete="off" required placeholder="Search or select country" value="${escapeHtml(initialCountry?.name ?? countryName)}"><input name="country_code" type="hidden" value="${escapeHtml(initialCountry?.code ?? countryCode)}"><div class="customer-country-results" id="${countryResultsId}" role="listbox" hidden></div></span><small class="field-error" data-error-for="country_code"></small></label>
      <label>Display currency *<select name="preferred_currency" required><option value="">Select display currency</option><option ${currency === "EUR" ? "selected" : ""}>EUR</option><option ${currency === "USD" ? "selected" : ""}>USD</option><option ${currency === "INR" ? "selected" : ""}>INR</option></select><small>Reference display only; quotations are always EUR.</small><small class="field-error" data-error-for="preferred_currency"></small></label>
      <label>Payment terms *<select name="payment_terms" required><option value="">Select payment terms</option>${PAYMENT_TERMS.map((item) => `<option value="${item}" ${item === payment ? "selected" : ""}>${item}</option>`).join("")}</select><small class="field-error" data-error-for="payment_terms"></small></label>
      <label class="customer-address">Address *<textarea name="address" required rows="2" placeholder="Customer address">${escapeHtml(existing?.address ?? "")}</textarea><small class="field-error" data-error-for="address"></small></label>
      <label class="custom-payment-days" ${payment === "Custom" ? "" : "hidden"}>Custom days *<input name="custom_payment_days" type="number" min="1" step="1" inputmode="numeric" placeholder="Days" value="${customDays}"><small class="field-error" data-error-for="custom_payment_days"></small></label>
    </div>
    <button class="button button-primary button-full" type="submit"><i data-lucide="save"></i>${existing ? "Save Customer" : "Create Customer"}</button>
  </form>`;
  const form = content.querySelector<HTMLFormElement>("form")!;
  const countryField = form.elements.namedItem("country_search") as HTMLInputElement;
  const countryCodeField = form.elements.namedItem("country_code") as HTMLInputElement;
  const continentField = form.elements.namedItem("continent") as HTMLSelectElement;
  const phoneField = form.elements.namedItem("phone") as HTMLInputElement;
  const currencyField = form.elements.namedItem("preferred_currency") as HTMLSelectElement;
  const countryResults = content.querySelector<HTMLElement>(".customer-country-results")!;
  const countryCombobox = content.querySelector<HTMLElement>(".customer-country-combobox")!;
  const paymentSelect = form.elements.namedItem("payment_terms") as HTMLSelectElement;
  const customField = content.querySelector<HTMLElement>(".custom-payment-days")!;
  let visibleCountries: CountryMeta[] = [];
  let highlightedIndex = -1;
  const selectedCountry = () => countries.find((country) => country.code === countryCodeField.value) ?? null;
  const closeCountries = () => {
    countryResults.hidden = true;
    countryField.setAttribute("aria-expanded", "false");
    countryField.removeAttribute("aria-activedescendant");
    highlightedIndex = -1;
  };
  const highlightCountry = (index: number) => {
    const options = [...countryResults.querySelectorAll<HTMLElement>("[data-country-code]")];
    if (!options.length) return;
    highlightedIndex = (index + options.length) % options.length;
    options.forEach((option, optionIndex) => {
      const active = optionIndex === highlightedIndex;
      option.classList.toggle("is-highlighted", active);
      option.setAttribute("aria-selected", String(active));
    });
    const current = options[highlightedIndex];
    countryField.setAttribute("aria-activedescendant", current.id);
    current.scrollIntoView({ block: "nearest" });
  };
  const renderCountries = (query = "") => {
    const term = query.trim().toLocaleLowerCase();
    visibleCountries = countries.filter((country) => !term
      || country.name.toLocaleLowerCase().includes(term)
      || country.code.toLocaleLowerCase().startsWith(term)).filter((country) => !continentField.value || country.region === continentField.value);
    highlightedIndex = -1;
    countryResults.innerHTML = visibleCountries.length
      ? visibleCountries.map((country, index) => `<div class="customer-country-option" id="${countryResultsId}-option-${index}" role="option" aria-selected="false" data-country-code="${country.code}"><strong>${escapeHtml(country.name)}</strong><small>${escapeHtml(country.code)} · ${escapeHtml(country.region)}${country.phone_country_code ? ` · ${escapeHtml(country.phone_country_code)}` : ""}</small></div>`).join("")
      : '<p class="customer-country-empty">No matching country</p>';
    countryResults.hidden = false;
    countryField.setAttribute("aria-expanded", "true");
  };
  const selectCountry = (country: CountryMeta) => {
    countryField.value = country.name;
    countryCodeField.value = country.code;
    continentField.value = country.region;
    currencyField.value = country.default_display_currency;
    if (!phoneField.value.trim() && country.phone_country_code) phoneField.value = country.phone_country_code;
    closeCountries();
  };
  continentField.addEventListener("change", () => {
    const selected = selectedCountry();
    if (!selected || selected.region !== continentField.value) {
      countryField.value = "";
      countryCodeField.value = "";
    }
    renderCountries(countryField.value);
  });
  countryField.addEventListener("focus", () => renderCountries(countryField.value));
  countryField.addEventListener("input", () => {
    const selected = selectedCountry();
    if (!selected || selected.name !== countryField.value) {
      countryCodeField.value = "";
      if (selected) continentField.value = "";
    }
    renderCountries(countryField.value);
  });
  countryField.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { event.preventDefault(); closeCountries(); return; }
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (countryResults.hidden) renderCountries(countryField.value);
      highlightCountry(highlightedIndex + (event.key === "ArrowDown" ? 1 : -1));
      return;
    }
    if (event.key === "Enter" && !countryResults.hidden && highlightedIndex >= 0) {
      event.preventDefault();
      selectCountry(visibleCountries[highlightedIndex]);
    }
  });
  countryResults.addEventListener("click", (event) => {
    const option = (event.target as HTMLElement).closest<HTMLElement>("[data-country-code]");
    const selected = countries.find((country) => country.code === option?.dataset.countryCode);
    if (selected) selectCountry(selected);
  });
  const closeOnOutsideClick = (event: PointerEvent) => {
    if (!countryCombobox.contains(event.target as Node)) closeCountries();
  };
  document.addEventListener("pointerdown", closeOnOutsideClick);
  content.addEventListener("moneda:dispose", () => document.removeEventListener("pointerdown", closeOnOutsideClick), { once: true });
  paymentSelect.addEventListener("change", () => { customField.hidden = paymentSelect.value !== "Custom"; if (paymentSelect.value !== "Custom") (form.elements.namedItem("custom_payment_days") as HTMLInputElement).value = ""; });
  return content;
}

async function openCustomerEditor(existing: Customer | undefined, onSaved: () => Promise<void> | void): Promise<void> {
  let countries: CountryMeta[];
  try { countries = await loadCountryCatalogue(); }
  catch (error) { toast(error instanceof Error ? error.message : "Country list could not be loaded", "error"); return; }
  const content = customerForm(countries, existing);
  const dialog = openModal(existing ? "Edit Customer" : "Add Customer", content, "wide");
  dialog.addEventListener("close", () => content.dispatchEvent(new Event("moneda:dispose")), { once: true });
  content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const value = Object.fromEntries(new FormData(form).entries());
    delete value.country_search;
    if (value.payment_terms !== "Custom") { delete value.custom_payment_days; }
    const errors: Record<string, string> = {};
    const required = [["company_name", "Company name is required"], ["contact_name", "Primary contact is required"], ["email", "Email is required; use - if unknown"], ["phone", "Phone is required; use - if unknown"], ["continent", "Region / Continent is required"], ["country_code", "Country is required"], ["preferred_currency", "Display currency is required"], ["payment_terms", "Payment terms are required"], ["address", "Address is required"]] as const;
    required.forEach(([field, message]) => { if (!String(value[field] ?? "").trim()) errors[field] = message; });
    if (value.email && value.email !== "-" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(value.email))) errors.email = "Enter a valid email address or - if unknown";
    const phone = String(value.phone ?? "").trim();
    const phoneDigits = phone.replace(/\D/g, "");
    if (phone && phone !== "-" && (!/^\+?[0-9().\-\s]{2,29}$/.test(phone) || phoneDigits.length < 3 || phoneDigits.length > 15)) errors.phone = "Enter a valid international phone number or - if unknown";
    const selected = countries.find((country) => country.code === String(value.country_code));
    if (value.country_code && !selected) errors.country_code = "Select a valid country from the country list";
    if (selected && value.continent !== selected.region) errors.continent = "Select the region that contains this country";
    if (selected) value.country_name = selected.name;
    if (value.payment_terms === "Custom" && !/^[1-9]\d*$/.test(String(value.custom_payment_days ?? ""))) errors.custom_payment_days = "Enter a positive whole number of days";
    content.querySelectorAll<HTMLElement>("[data-error-for]").forEach((node) => { node.textContent = errors[node.dataset.errorFor ?? ""] ?? ""; });
    if (Object.keys(errors).length) return;
    try {
      if (existing) { await customerApi.update(existing._id, value); dialog.close(); toast("Customer updated"); await onSaved(); return; }
      const created = await customerApi.create(value);
      dialog.close(); await onSaved();
      toast("Customer created successfully");
      if (created._id) {
        const next = document.createElement("div");
        next.innerHTML = '<p>Customer created successfully.</p><div class="modal-actions"><button type="button" class="button button-quiet" data-close-success>Stay here</button><button type="button" class="button button-primary" data-select-success>Select this customer</button></div>';
        const successDialog = openModal("Customer created", next);
        next.querySelector("[data-close-success]")?.addEventListener("click", () => successDialog.close());
        next.querySelector("[data-select-success]")?.addEventListener("click", async () => { const transition = beginCustomerContextChange(); const signal = customerContextSignal(); try { await customerCompanyApi.select(created._id, signal); if (!isCurrentCustomerContextRevision(transition)) return; appStore.set({ customer: created, activeCustomerId: created._id, customerCompany: created as unknown as Company, company: created as unknown as Company, currency: created.preferred_currency ?? "EUR", cartCount: 0 }); localStorage.setItem("moneda-active-customer-id", created._id); successDialog.close(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/calculator" })); } catch (error) { if (error instanceof DOMException && error.name === "AbortError") return; toast(error instanceof Error ? error.message : "Customer could not be selected", "error"); } });
      }
    }
    catch (error) { toast(error instanceof Error ? error.message : "Customer could not be saved", "error"); }
  });
  refreshIcons(content);
}

export async function customersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Relationships", "Customers", "Manage the customer businesses Moneda Technologies quotes and sells to.", '<button class="button button-secondary"><i data-lucide="download"></i>Export</button><button class="button button-primary" id="add-customer"><i data-lucide="building-2"></i>Add Customer</button>');
  page.classList.add("customer-list-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  let statusFilter = "active";
  const load = async () => {
    const result = await customerApi.list(undefined, statusFilter);
    body.innerHTML = `<div class="table-toolbar"><div class="field-search"><i data-lucide="search"></i><input placeholder="Search customers" aria-label="Search customers"></div><div class="segmented"><button class="active">All</button><button>Active</button><button>Archived</button></div><span>${result.pagination?.total ?? result.items.length} records</span></div>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Customer company</th><th>Primary contact</th><th>Email / phone</th><th>Currency</th><th>Status</th><th></th></tr></thead><tbody>${result.items.map((customer) => { const name = customerName(customer); const id = customer.customer_id ?? customer._id; return `<tr><td><div class="table-identity"><span>${escapeHtml(name.slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(name)}</strong><small>${escapeHtml(customer.address ?? "")}</small></p></div></td><td>${escapeHtml(customer.contact_name ?? "—")}</td><td><strong>${escapeHtml(customer.email ?? "No email")}</strong><small>${escapeHtml(customer.phone ?? "")}</small></td><td><span class="currency-tag">${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</span></td><td>${statusBadge(customer.status ?? "active")}</td><td><a class="icon-button" href="/customers/${encodeURIComponent(id)}" data-route="/customers/${encodeURIComponent(id)}" aria-label="View customer" title="View customer"><i data-lucide="arrow-up-right"></i></a></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("building-2", "No customers yet", "Add a customer business to prepare a quotation.")}`;
    const currencyHeading = [...body.querySelectorAll("th")].find((node) => node.textContent === "Currency");
    if (currencyHeading) currencyHeading.textContent = "Display";
    const actionHeading = body.querySelector("th:last-child");
    if (actionHeading) actionHeading.textContent = "Actions";
    refreshIcons(body);
    body.querySelectorAll<HTMLButtonElement>(".segmented button").forEach((button) => {
      const label = button.textContent?.trim().toLowerCase();
      const nextFilter = label === "all" ? "" : label;
      if (label === "all" || label === "active" || label === "archived") { button.dataset.customerStatus = nextFilter; button.classList.toggle("active", nextFilter === statusFilter); button.addEventListener("click", () => { statusFilter = nextFilter; void load().catch((error) => { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customers unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; refreshIcons(page); }); }); }
    });
    body.querySelectorAll<HTMLTableRowElement>("tbody tr").forEach((row, index) => {
      const customer = result.items[index]; const id = customer?._id ?? customer?.customer_id; if (!id) return;
      const actions = row.lastElementChild; if (!actions) return;
      const openLink = actions.querySelector<HTMLAnchorElement>("a.icon-button");
      const actionGroup = document.createElement("div"); actionGroup.className = "customer-row-actions";
      if (openLink) { openLink.className = "icon-button customer-icon-action customer-open-button"; openLink.setAttribute("aria-label", "Open customer"); openLink.title = "Open customer"; openLink.innerHTML = '<i data-lucide="arrow-up-right"></i>'; actionGroup.append(openLink); }
      actions.replaceChildren(actionGroup);
      const archived = String(customer.status ?? "").toLowerCase() === "archived" || statusFilter === "archived";
      const button = document.createElement("button"); button.className = "icon-button customer-icon-action customer-status-action"; button.type = "button"; button.setAttribute("aria-label", archived ? "Restore customer" : "Archive customer"); button.title = archived ? "Restore customer" : "Archive customer"; button.innerHTML = `<i data-lucide="${archived ? "rotate-ccw" : "archive"}"></i>`;
      button.addEventListener("click", async () => { try { if (archived) { await customerApi.restore(String(id)); toast("Customer restored"); } else { if (!window.confirm("Archive customer? Historical records remain intact.")) return; await customerApi.remove(String(id), "Archived from customer directory"); toast("Customer archived"); } await load(); } catch (error) { toast(error instanceof Error ? error.message : "Customer action failed", "error"); } });
      actionGroup.append(button);
      if (archived && appStore.state.user?.role_id === "superadmin") {
        const remove = document.createElement("button"); remove.className = "icon-button customer-icon-action customer-icon-danger customer-status-action"; remove.type = "button"; remove.setAttribute("aria-label", "Delete customer"); remove.title = "Delete customer"; remove.innerHTML = '<i data-lucide="trash-2"></i>';
        remove.addEventListener("click", async () => { if (!window.confirm("Delete customer permanently? This is allowed only when no business records reference it.")) return; const reason = window.prompt("Deletion reason (required):", "")?.trim() ?? ""; if (!reason) { toast("A reason is required", "error"); return; } try { await customerApi.remove(String(id), reason, true); toast("Customer deleted"); await load(); } catch (error) { toast(error instanceof Error ? error.message : "Customer could not be deleted", "error"); } });
        actionGroup.append(remove);
      }
      refreshIcons(actionGroup);
    });
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customers unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector("#add-customer")?.addEventListener("click", () => { void openCustomerEditor(undefined, load); });
  if (new URLSearchParams(window.location.search).get("action") === "add") { history.replaceState({}, "", "/customers"); window.setTimeout(() => { void openCustomerEditor(undefined, load); }, 0); }
  refreshIcons(page);
  return page;
}

export async function customerDetailPage(customerId: string): Promise<HTMLElement> {
  const page = pageScaffold("Relationships", "Customer detail", "A customer business record with commercial history and contact context.", '<a class="button button-secondary" href="/customers" data-route="/customers"><i data-lucide="arrow-left"></i>Back to customers</a>');
  page.classList.add("customer-detail-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  const load = async () => {
    const customer = await customerApi.get(customerId);
    const related = customer.related ?? {};
    const name = customerName(customer);
    const leads = (related.leads ?? []) as Record<string, unknown>[];
    const crmHref = `/crm?customer_id=${encodeURIComponent(customerId)}`;
    const accessMarkup = customer.access ? `<section class="panel customer-access-panel"><div class="section-title"><div><span class="eyebrow">Access & ownership</span><h2>Customer access</h2></div></div><div class="detail-grid"><div><span>Created by</span><strong>${escapeHtml(customer.access.created_by?.name ?? "Unknown")}</strong></div><div><span>Assigned users</span><strong>${customer.access.assigned_users?.length ?? 0}</strong></div></div>${customer.access.assigned_users?.length ? `<div class="customer-activity-list">${customer.access.assigned_users.map((member) => `<div><div><strong>${escapeHtml(member.name)}</strong><small>${escapeHtml(member.email ?? "")}</small></div></div>`).join("")}</div>` : '<p class="muted">No assigned users.</p>'}</section>` : "";
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar">${escapeHtml(name.slice(0, 2).toUpperCase())}</div><div><span class="eyebrow">Customer</span><h2>${escapeHtml(name)}</h2><p>${escapeHtml(customer.contact_name ?? "Primary contact not configured")}</p>${statusBadge(customer.status ?? "active")}</div><div class="detail-hero-actions"><a class="button button-secondary" href="${crmHref}" data-route="${crmHref}"><i data-lucide="chart-no-axes-combined"></i>View CRM</a><button class="button button-primary" id="edit-customer"><i data-lucide="pencil"></i>Edit customer</button></div></section><section class="panel customer-profile-panel"><div class="section-title"><div><span class="eyebrow">Contact & business information</span><h2>Customer profile</h2></div></div><div class="detail-grid"><div><span>Company name</span><strong>${escapeHtml(name)}</strong></div><div><span>Primary contact</span><strong>${escapeHtml(customer.contact_name ?? "—")}</strong></div><div><span>Email</span><strong>${escapeHtml(customer.email ?? "—")}</strong></div><div><span>Phone</span><strong>${escapeHtml(customer.phone ?? "—")}</strong></div><div><span>Region</span><strong>${escapeHtml(customer.region?.continent ?? customer.continent ?? "—")}</strong></div><div><span>Country</span><strong>${escapeHtml(customer.region?.country_name ?? customer.country_name ?? customer.country ?? "—")}</strong></div><div><span>Display currency</span><strong>${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</strong></div><div><span>Payment terms</span><strong>${escapeHtml(customer.payment_terms_display ?? customer.payment_terms ?? "—")}</strong></div><div class="detail-grid-wide"><span>Address</span><strong>${escapeHtml(customer.address ?? "—")}</strong></div></div></section>${accessMarkup}<section class="panel customer-history-panel"><div class="section-title"><div><span class="eyebrow">Related records</span><h2>Customer history</h2></div></div><div class="metric-grid compact-metrics"><a class="metric-card" href="/quotations?customer_id=${encodeURIComponent(customerId)}" data-route="/quotations?customer_id=${encodeURIComponent(customerId)}"><span>Quotations</span><strong>${related.quotations?.length ?? 0}</strong></a><a class="metric-card" href="/orders?customer_id=${encodeURIComponent(customerId)}" data-route="/orders?customer_id=${encodeURIComponent(customerId)}"><span>Orders</span><strong>${related.orders?.length ?? 0}</strong></a><a class="metric-card" href="${crmHref}" data-route="${crmHref}"><span>Leads</span><strong>${related.leads?.length ?? 0}</strong></a><a class="metric-card" href="${crmHref}" data-route="${crmHref}"><span>Opportunities</span><strong>${related.opportunities?.length ?? 0}</strong></a></div></section><section class="panel customer-activity-panel"><div class="section-title"><div><span class="eyebrow">CRM</span><h2>Sales activity</h2></div><a class="button button-quiet" href="${crmHref}" data-route="${crmHref}">Open pipeline<i data-lucide="arrow-up-right"></i></a></div>${leads.length ? `<div class="customer-activity-list">${leads.slice(0, 5).map((lead) => `<div><div><strong>${escapeHtml(String(lead.title ?? lead.notes ?? "Sales lead"))}</strong><small>${escapeHtml(String(lead.next_action ?? lead.follow_up_date ?? "No next action"))}</small></div>${statusBadge(String(lead.status ?? "Lead"))}</div>`).join("")}</div>` : '<p class="muted">No CRM activity yet.</p>'}</section></div>`;
    body.querySelector("#edit-customer")?.addEventListener("click", () => { void openCustomerEditor(customer, load); });
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customer unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
