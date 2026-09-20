import { customerApi, customerCompanyApi, priceListApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { PAYMENT_TERMS } from "../config/customer-metadata";
import { accountTypeLabel, accountTypeOptions, PRICE_LIST_ACCOUNT_TYPES } from "../config/businessConfig";
import type { CountryMeta } from "../config/customer-metadata";
import type { Company, Customer, PriceListDefinition } from "../types/domain";
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

function customerForm(countries: CountryMeta[], existing?: Customer, priceLists: PriceListDefinition[] = []): HTMLDivElement {
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
  const accountType = existing?.account_type ?? (existing?.client_type === "DEALER" ? "DEALER" : existing?.client_type === "WHOLESALER" || existing?.client_type === "CUSTOMER" ? "DISTRIBUTOR" : "");
  const isSuperadmin = appStore.state.user?.role_id === "superadmin";
  const incentiveEnabled = existing?.have_to_give_incentive === true;
  const incentivePercentages = Array.from({ length: 40 }, (_, index) => (index + 1) / 2);
  const incentiveVisibleToManagers = existing?.incentive_visible_to_managers === true;
  const incentiveVisibleToSalespersons = existing?.incentive_visible_to_salespersons === true;
  const billing = existing?.billing_address_record ?? {};
  const shippingRows = existing?.shipping_addresses?.length ? existing.shipping_addresses : [{ id: "", label: "Primary shipping", address_line_1: "", city: "", state: "", postal_code: "", country_code: countryCode, country_name: countryName, active: true, is_default: true }];
  const shippingPriceListOptions = (selectedId?: string | null) => `<option value="">Use customer default</option>${priceLists.map((list) => `<option value="${escapeHtml(list._id)}" ${selectedId === list._id ? "selected" : ""}>${escapeHtml(list.display_name)}</option>`).join("")}`;
  const shippingRowsMarkup = shippingRows.map((item, index) => `<div class="shipping-address-row" data-shipping-row data-address-id="${escapeHtml(String(item.id ?? ""))}" data-active="${item.active === false ? "false" : "true"}"><div class="shipping-address-row-head"><strong>Shipping address ${index + 1}</strong><button type="button" class="button button-quiet" data-remove-shipping>Remove</button></div><div class="customer-form-fields"><label>Label<input name="shipping_label_${index}" value="${escapeHtml(String(item.label ?? `Shipping address ${index + 1}`))}" placeholder="e.g. Mumbai warehouse"></label><label>Recipient / company<input name="shipping_recipient_${index}" value="${escapeHtml(String(item.recipient_name ?? item.company_name ?? ""))}" placeholder="Recipient or company"></label><label class="customer-form-field-wide">Address line 1<input name="shipping_line_1_${index}" value="${escapeHtml(String(item.address_line_1 ?? ""))}" placeholder="Address line 1"></label><label>Address line 2<input name="shipping_line_2_${index}" value="${escapeHtml(String(item.address_line_2 ?? ""))}" placeholder="Apartment, suite, etc."></label><label>City<input name="shipping_city_${index}" value="${escapeHtml(String(item.city ?? ""))}" placeholder="City"></label><label>State / region<input name="shipping_state_${index}" value="${escapeHtml(String(item.state ?? ""))}" placeholder="State"></label><label>Postal code<input name="shipping_postal_${index}" value="${escapeHtml(String(item.postal_code ?? ""))}" placeholder="Postal code"></label>${appStore.can("customer.pricing.update") ? `<label>Default price list override<select name="shipping_default_price_list_${index}">${shippingPriceListOptions(item.default_price_list_id)}</select></label><label>Blankets override<select name="shipping_blankets_price_list_${index}"><option value="">Use customer default</option>${priceLists.filter((list) => list.category === "blankets" || list._id === "blankets").map((list) => `<option value="${escapeHtml(list._id)}" ${item.category_price_list_ids?.blankets === list._id ? "selected" : ""}>${escapeHtml(list.display_name)}</option>`).join("")}</select></label><label>Underpacking override<select name="shipping_underpacking_price_list_${index}"><option value="">Use customer default</option>${priceLists.filter((list) => list.category === "mpacks" || list.category === "underpacking" || list._id.includes("underpacking")).map((list) => `<option value="${escapeHtml(list._id)}" ${item.category_price_list_ids?.underpacking === list._id ? "selected" : ""}>${escapeHtml(list.display_name)}</option>`).join("")}</select></label>` : ""}<label class="check-row"><input name="shipping_default" type="radio" value="${index}" ${item.is_default !== false && item.active !== false ? "checked" : ""}><span>Use as default shipping address</span></label></div></div>`).join("");
  const countryResultsId = `customer-country-results-${crypto.randomUUID()}`;
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form customer-form customer-editor-form" id="customer-form" autocomplete="off">
    <div class="customer-editor-grid">
      <div class="customer-editor-column">
        <section class="customer-form-card">
          <header class="customer-form-card-head"><span class="customer-form-card-icon"><i data-lucide="user-round"></i></span><div><h3>Basic Information</h3><p>Essential customer details.</p></div></header>
          <div class="customer-form-card-body customer-form-fields">
            <label>Customer company name *<input name="company_name" required placeholder="Enter customer name" value="${escapeHtml(existing?.company_name ?? existing?.name ?? "")}"><small class="field-error" data-error-for="company_name"></small></label>
            <label>Primary contact *<input name="contact_name" required autocomplete="off" placeholder="Contact person" value="${escapeHtml(existing?.contact_name ?? "")}"><small class="field-error" data-error-for="contact_name"></small></label>
            <label class="customer-form-field-wide">Account Type *<select name="account_type" required>${accountTypeOptions(accountType)}</select><small>Determines the Distributor or Dealer price list.</small><small class="field-error" data-error-for="account_type"></small></label>
            <label>Email *<input name="email" type="text" inputmode="email" required placeholder="customer@company.com or -" value="${escapeHtml(existing?.email ?? "")}"><small>Use - if the email is not known.</small><small class="field-error" data-error-for="email"></small></label>
            <label>Phone *<input name="phone" type="tel" required inputmode="tel" placeholder="Phone number or -" value="${escapeHtml(existing?.phone ?? "")}"><small>Use - if the phone number is not known.</small><small class="field-error" data-error-for="phone"></small></label>
          </div>
        </section>
        <section class="customer-form-card">
          <header class="customer-form-card-head"><span class="customer-form-card-icon"><i data-lucide="globe-2"></i></span><div><h3>Location</h3><p>Region and country information.</p></div></header>
          <div class="customer-form-card-body customer-form-fields">
            <label class="customer-form-field-wide">Region / Continent *<select name="continent" required><option value="">Select a region</option>${regions.map((item) => `<option value="${escapeHtml(item)}" ${item === continent ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select><small>Select a region to filter countries, or search for a country and the region will be selected automatically.</small><small class="field-error" data-error-for="continent"></small></label>
            <label class="customer-form-field-wide">Country *<span class="customer-country-combobox"><input name="country_search" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="${countryResultsId}" autocomplete="off" required placeholder="Search or select country" value="${escapeHtml(initialCountry?.name ?? countryName)}"><input name="country_code" type="hidden" value="${escapeHtml(initialCountry?.code ?? countryCode)}"><div class="customer-country-results" id="${countryResultsId}" role="listbox" hidden></div></span><small class="field-error" data-error-for="country_code"></small></label>
          </div>
        </section>
        <section class="customer-form-card">
          <header class="customer-form-card-head"><span class="customer-form-card-icon"><i data-lucide="map-pin"></i></span><div><h3>Address</h3><p>Customer location and delivery details.</p></div></header>
          <div class="customer-form-card-body customer-form-fields">
            <label class="customer-form-field-wide">Address *<textarea name="address" required rows="3" placeholder="Enter customer address">${escapeHtml(existing?.address ?? billing.address_line_1 ?? "")}</textarea><small class="field-error" data-error-for="address"></small></label>
            <div class="customer-address-subgrid customer-form-field-wide"><label>Billing line 2<input name="billing_address_line_2" value="${escapeHtml(String(billing.address_line_2 ?? ""))}" placeholder="Apartment, suite, etc."></label><label>City<input name="billing_city" value="${escapeHtml(String(billing.city ?? ""))}" placeholder="City"></label><label>State / region<input name="billing_state" value="${escapeHtml(String(billing.state ?? ""))}" placeholder="State"></label><label>Postal code<input name="billing_postal_code" value="${escapeHtml(String(billing.postal_code ?? ""))}" placeholder="Postal code"></label></div>
          </div>
        </section>
        <section class="customer-form-card">
          <header class="customer-form-card-head"><span class="customer-form-card-icon"><i data-lucide="truck"></i></span><div><h3>Shipping Addresses</h3><p>Add delivery destinations and choose the default address for new quotations.</p></div></header>
          <div class="customer-form-card-body shipping-address-list" data-shipping-address-list>${shippingRowsMarkup}<button type="button" class="button button-secondary" data-add-shipping><i data-lucide="plus"></i>Add shipping address</button></div>
        </section>
      </div>
      <div class="customer-editor-column">
        <section class="customer-form-card">
          <header class="customer-form-card-head"><span class="customer-form-card-icon"><i data-lucide="settings-2"></i></span><div><h3>Commercial Settings</h3><p>Pricing and payment configuration.</p></div></header>
          <div class="customer-form-card-body customer-form-fields">
            <label>Display currency *<select name="preferred_currency" required><option value="">Select display currency</option><option ${currency === "EUR" ? "selected" : ""}>EUR</option><option ${currency === "USD" ? "selected" : ""}>USD</option><option ${currency === "INR" ? "selected" : ""}>INR</option></select><small>Reference display only; quotations are always EUR.</small><small class="field-error" data-error-for="preferred_currency"></small></label>
            <label>Payment terms *<select name="payment_terms" required><option value="">Select payment terms</option>${PAYMENT_TERMS.map((item) => `<option value="${item}" ${item === payment ? "selected" : ""}>${item}</option>`).join("")}</select><small>Determines the Distributor or Dealer price list.</small><small class="field-error" data-error-for="payment_terms"></small></label>
            <label class="custom-payment-days customer-form-field-wide" ${payment === "Custom" ? "" : "hidden"}>Custom days *<input name="custom_payment_days" type="number" min="1" step="1" inputmode="numeric" placeholder="Days" value="${customDays}"><small class="field-error" data-error-for="custom_payment_days"></small></label>
            ${appStore.can("customer.pricing.update") ? `<label class="customer-form-field-wide">Default price list<select name="default_price_list_id"><option value="">Use customer type default</option>${priceLists.map((list) => `<option value="${escapeHtml(list._id)}" ${existing?.default_price_list_id === list._id ? "selected" : ""}>${escapeHtml(list.display_name)}</option>`).join("")}</select><small>Overrides the default list for this customer only.</small></label><label class="customer-form-field-wide">Blankets price list<select name="category_price_list_blankets"><option value="">Use default</option>${priceLists.filter((list) => list.category === "blankets" || list._id === "blankets").map((list) => `<option value="${escapeHtml(list._id)}" ${existing?.category_price_list_ids?.blankets === list._id ? "selected" : ""}>${escapeHtml(list.display_name)}</option>`).join("")}</select></label><label class="customer-form-field-wide">Underpacking price list<select name="category_price_list_underpacking"><option value="">Use default</option>${priceLists.filter((list) => list.category === "underpacking" || list._id.includes("underpacking")).map((list) => `<option value="${escapeHtml(list._id)}" ${existing?.category_price_list_ids?.underpacking === list._id ? "selected" : ""}>${escapeHtml(list.display_name)}</option>`).join("")}</select></label>` : ""}
          </div>
        </section>
        ${isSuperadmin ? `<section class="customer-form-card customer-incentive-card">
          <header class="customer-form-card-head"><span class="customer-form-card-icon"><i data-lucide="gift"></i></span><div><h3>Customer Incentive <span class="customer-superadmin-badge">Superadmin only</span></h3><p>Configure incentive for this customer.</p></div></header>
          <div class="customer-form-card-body customer-incentive-body">
            <div class="customer-incentive-notice"><i data-lucide="info"></i><span>This incentive configuration is managed by Superadmins only.</span></div>
            <label>Have to give incentive<select name="have_to_give_incentive"><option value="false" ${!incentiveEnabled ? "selected" : ""}>No</option><option value="true" ${incentiveEnabled ? "selected" : ""}>Yes</option></select></label>
            <div class="customer-incentive-fields" ${incentiveEnabled ? "" : "hidden"}>
              <div class="customer-form-fields"><label>Incentive Bearer Name *<input name="incentive_bearer_name" value="${escapeHtml(existing?.incentive_bearer_name ?? "")}" placeholder="Purchase Manager"><small class="field-error" data-error-for="incentive_bearer_name"></small></label><label>Designation *<input name="incentive_designation" value="${escapeHtml(existing?.incentive_designation ?? "")}" placeholder="Procurement Head"><small class="field-error" data-error-for="incentive_designation"></small></label><label class="customer-form-field-wide">Incentive Percentage *<select name="customer_incentive_percentage"><option value="">Select percentage</option>${incentivePercentages.map((value) => `<option value="${value}" ${Number(existing?.customer_incentive_percentage) === value ? "selected" : ""}>${value.toFixed(1)}%</option>`).join("")}</select><small>Available options: 0.5% to 20% (in 0.5% increments).</small><small class="field-error" data-error-for="customer_incentive_percentage"></small></label></div>
              <fieldset class="customer-incentive-visibility"><legend><i data-lucide="eye"></i>Incentive Visibility</legend><p>Control who can view this customer's incentive information.</p><label class="customer-switch"><input type="checkbox" name="incentive_visible_to_managers" ${incentiveVisibleToManagers ? "checked" : ""}><span class="customer-switch-track"></span><span><strong>Show to Managers</strong><small>Allow managers to view this customer's incentive.</small></span></label><label class="customer-switch"><input type="checkbox" name="incentive_visible_to_salespersons" ${incentiveVisibleToSalespersons ? "checked" : ""}><span class="customer-switch-track"></span><span><strong>Show to Sales Persons</strong><small>Allow sales persons to view this customer's incentive.</small></span></label></fieldset>
            </div>
          </div>
        </section>` : ""}
      </div>
    </div>
    <div class="modal-actions customer-form-footer"><button class="button button-secondary" type="button" data-customer-cancel>Cancel</button><button class="button button-primary" type="submit"><i data-lucide="save"></i>${existing ? "Save Customer" : "Create Customer"}</button></div>
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
  const incentiveToggle = form.elements.namedItem("have_to_give_incentive") as HTMLSelectElement | null;
  const incentiveFields = content.querySelector<HTMLElement>(".customer-incentive-fields");
  const shippingList = content.querySelector<HTMLElement>("[data-shipping-address-list]");
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
  incentiveToggle?.addEventListener("change", () => {
    const disablingExisting = existing?.have_to_give_incentive === true && incentiveToggle.value !== "true";
    if (disablingExisting && !window.confirm("Disable customer incentives for future Order Confirmations? Historical incentive snapshots will remain unchanged.")) {
      incentiveToggle.value = "true";
      return;
    }
    if (incentiveFields) incentiveFields.hidden = incentiveToggle.value !== "true";
  });
  shippingList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement;
    const removeButton = target.closest<HTMLButtonElement>("[data-remove-shipping]");
    if (removeButton) {
      event.preventDefault();
      const row = removeButton.closest<HTMLElement>("[data-shipping-row]");
      if (!row) return;
      if (row.dataset.addressId) {
        row.dataset.active = "false";
        row.hidden = true;
      } else {
        row.remove();
      }
      const activeDefault = shippingList.querySelector<HTMLInputElement>('[data-shipping-row]:not([hidden]) input[name="shipping_default"]:checked');
      if (!activeDefault) shippingList.querySelector<HTMLInputElement>('[data-shipping-row]:not([hidden]) input[name="shipping_default"]')?.click();
      return;
    }
    const addButton = target.closest<HTMLButtonElement>("[data-add-shipping]");
    if (!addButton) return;
    event.preventDefault();
    const source = shippingList.querySelector<HTMLElement>('[data-shipping-row]:not([hidden])');
    if (!source) return;
    const row = source.cloneNode(true) as HTMLElement;
    const nextIndex = shippingList.querySelectorAll("[data-shipping-row]").length;
    row.dataset.addressId = "";
    row.dataset.active = "true";
    row.hidden = false;
    row.querySelector("strong")!.textContent = `Shipping address ${nextIndex + 1}`;
    row.querySelectorAll<HTMLInputElement | HTMLSelectElement>("input, select").forEach((field) => {
      if (field instanceof HTMLInputElement && field.type === "radio") {
        field.checked = false;
        field.value = String(nextIndex);
        field.name = "shipping_default";
      } else if (field instanceof HTMLInputElement) {
        field.value = field.name.startsWith("shipping_label_") ? `Shipping address ${nextIndex + 1}` : "";
        field.name = field.name.replace(/_\d+$/, `_${nextIndex}`);
      } else {
        field.value = "";
        field.name = field.name.replace(/_\d+$/, `_${nextIndex}`);
      }
    });
    shippingList.insertBefore(row, addButton);
  });
  return content;
}

export interface CustomerEditorOptions {
  onCreated?: (customer: Customer) => Promise<void> | void;
  suppressSuccessDialog?: boolean;
  title?: string;
}

export async function openCustomerEditor(
  existing: Customer | undefined,
  onSaved: () => Promise<void> | void,
  options: CustomerEditorOptions = {},
): Promise<void> {
  let countries: CountryMeta[];
  try { countries = await loadCountryCatalogue(); }
  catch (error) { toast(error instanceof Error ? error.message : "Country list could not be loaded", "error"); return; }
  let priceLists: PriceListDefinition[] = [];
  if (appStore.can("customer.pricing.update")) {
    try { priceLists = (await priceListApi.list()).items ?? []; } catch { priceLists = []; }
  }
  const content = customerForm(countries, existing, priceLists);
  const isSuperadmin = appStore.state.user?.role_id === "superadmin";
  const dialog = openModal(options.title ?? (existing ? "Edit Customer" : "Add Customer"), content, "wide");
  dialog.classList.add("customer-editor-dialog");
  const modalHead = dialog.querySelector<HTMLElement>(".modal-head > div");
  if (modalHead) modalHead.insertAdjacentHTML("beforeend", '<p class="customer-editor-subtitle">Create a new customer to manage their quotations and pricing.</p>');
  content.querySelector<HTMLButtonElement>("[data-customer-cancel]")?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => content.dispatchEvent(new Event("moneda:dispose")), { once: true });
  content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const value = Object.fromEntries(new FormData(form).entries()) as Record<string, any>;
    delete value.country_search;
    if (isSuperadmin) {
      value.incentive_visible_to_managers = (form.elements.namedItem("incentive_visible_to_managers") as HTMLInputElement | null)?.checked ? "true" : "false";
      value.incentive_visible_to_salespersons = (form.elements.namedItem("incentive_visible_to_salespersons") as HTMLInputElement | null)?.checked ? "true" : "false";
    }
    if (value.payment_terms !== "Custom") { delete value.custom_payment_days; }
    const errors: Record<string, string> = {};
    const required = [["company_name", "Company name is required"], ["contact_name", "Primary contact is required"], ["account_type", "Account Type is required"], ["email", "Email is required; use - if unknown"], ["phone", "Phone is required; use - if unknown"], ["continent", "Region / Continent is required"], ["country_code", "Country is required"], ["preferred_currency", "Display currency is required"], ["payment_terms", "Payment terms are required"], ["address", "Address is required"]] as const;
    if (value.account_type && !PRICE_LIST_ACCOUNT_TYPES.some((type) => type.code === String(value.account_type))) errors.account_type = "Select Distributor or Dealer";
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
    if (isSuperadmin && String(value.have_to_give_incentive) === "true") {
      if (!String(value.incentive_bearer_name ?? "").trim()) errors.incentive_bearer_name = "Incentive Bearer Name is required";
      if (!String(value.incentive_designation ?? "").trim()) errors.incentive_designation = "Designation is required";
      const percentage = Number(value.customer_incentive_percentage);
      if (!Number.isFinite(percentage) || percentage < 0.5 || percentage > 20 || Math.round(percentage * 2) !== percentage * 2) errors.customer_incentive_percentage = "Select a percentage from 0.5% to 20.0% in 0.5% steps";
    }
    if (isSuperadmin && String(value.have_to_give_incentive) !== "true") { value.incentive_bearer_name = ""; value.incentive_designation = ""; value.customer_incentive_percentage = ""; value.incentive_visible_to_managers = "false"; value.incentive_visible_to_salespersons = "false"; }
    content.querySelectorAll<HTMLElement>("[data-error-for]").forEach((node) => { node.textContent = errors[node.dataset.errorFor ?? ""] ?? ""; });
    if (Object.keys(errors).length) return;
    const billingLine1 = String(value.address ?? "").trim();
    value.billing_address_record = {
      id: existing?.billing_address_record?.id ?? "billing",
      label: "Billing",
      recipient_name: String(value.contact_name ?? "").trim(),
      company_name: String(value.company_name ?? "").trim(),
      address_line_1: billingLine1,
      address_line_2: String(value.billing_address_line_2 ?? "").trim(),
      city: String(value.billing_city ?? "").trim(),
      state: String(value.billing_state ?? "").trim(),
      postal_code: String(value.billing_postal_code ?? "").trim(),
      country_code: String(value.country_code ?? "").trim(),
      country_name: String(value.country_name ?? "").trim(),
      active: true,
      is_default: true,
    };
    const defaultShippingIndex = Number(content.querySelector<HTMLInputElement>('input[name="shipping_default"]:checked')?.value ?? -1);
    const shippingRowsPayload = [...content.querySelectorAll<HTMLElement>("[data-shipping-row]")].map((row, index) => {
      const field = (prefix: string) => row.querySelector<HTMLInputElement | HTMLSelectElement>(`[name^="${prefix}_"]`)?.value?.trim() ?? "";
      const addressLine1 = field("shipping_line_1");
      const existingId = row.dataset.addressId ?? "";
      const active = row.dataset.active !== "false";
      const isExisting = Boolean(existingId);
      if (!addressLine1 && !isExisting) return null;
      const categoryPriceListIds: Record<string, string> = {};
      const blanketsPriceList = field("shipping_blankets_price_list");
      const underpackingPriceList = field("shipping_underpacking_price_list");
      if (blanketsPriceList) categoryPriceListIds.blankets = blanketsPriceList;
      if (underpackingPriceList) categoryPriceListIds.underpacking = underpackingPriceList;
      return {
        id: existingId || crypto.randomUUID(),
        label: field("shipping_label") || `Shipping address ${index + 1}`,
        recipient_name: field("shipping_recipient"),
        company_name: String(value.company_name ?? "").trim(),
        address_line_1: addressLine1,
        address_line_2: field("shipping_line_2"),
        city: field("shipping_city"),
        state: field("shipping_state"),
        postal_code: field("shipping_postal"),
        country_code: String(value.country_code ?? "").trim(),
        country_name: String(value.country_name ?? "").trim(),
        active,
        is_default: active && index === defaultShippingIndex,
        ...(appStore.can("customer.pricing.update") ? {
          default_price_list_id: field("shipping_default_price_list") || null,
          category_price_list_ids: categoryPriceListIds,
        } : {}),
      };
    }).filter((row) => row !== null) as Array<Record<string, any>>;
    value.shipping_addresses = shippingRowsPayload;
    value.default_shipping_address_id = shippingRowsPayload.find((row) => row.active && row.is_default)?.id
      ?? shippingRowsPayload.find((row) => row.active)?.id
      ?? null;
    delete value.billing_address_line_2; delete value.billing_city; delete value.billing_state; delete value.billing_postal_code;
    if (appStore.can("customer.pricing.update")) {
      const categoryPriceLists: Record<string, string> = { ...(existing?.category_price_list_ids ?? {}) };
      if (String(value.category_price_list_blankets ?? "").trim()) categoryPriceLists.blankets = String(value.category_price_list_blankets);
      else delete categoryPriceLists.blankets;
      if (String(value.category_price_list_underpacking ?? "").trim()) categoryPriceLists.underpacking = String(value.category_price_list_underpacking);
      else delete categoryPriceLists.underpacking;
      value.category_price_list_ids = categoryPriceLists;
      if (!String(value.default_price_list_id ?? "").trim()) value.default_price_list_id = null;
    }
    delete value.category_price_list_blankets; delete value.category_price_list_underpacking;
    try {
      if (existing) { await customerApi.update(existing._id, value); dialog.close(); toast("Customer updated"); await onSaved(); return; }
      const created = await customerApi.create(value);
      dialog.close(); await onSaved();
      toast("Customer created successfully");
      if (options.onCreated) await options.onCreated(created);
      if (options.suppressSuccessDialog) return;
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
  let clientTypeFilter = "";
  const load = async () => {
    const result = await customerApi.list(undefined, statusFilter, clientTypeFilter);
    body.innerHTML = `<div class="table-toolbar"><div class="field-search"><i data-lucide="search"></i><input placeholder="Search customers" aria-label="Search customers"></div><div class="segmented"><button class="active">All</button><button>Active</button><button>Archived</button></div><span>${result.pagination?.total ?? result.items.length} records</span></div>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Customer company</th><th>Primary contact</th><th>Email / phone</th><th>Currency</th><th>Status</th><th></th></tr></thead><tbody>${result.items.map((customer) => { const name = customerName(customer); const id = customer.customer_id ?? customer._id; return `<tr><td><div class="table-identity"><span>${escapeHtml(name.slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(name)}</strong><small>${escapeHtml(customer.address ?? "")}</small></p></div></td><td>${escapeHtml(customer.contact_name ?? "—")}</td><td><strong>${escapeHtml(customer.email ?? "No email")}</strong><small>${escapeHtml(customer.phone ?? "")}</small></td><td><span class="currency-tag">${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</span></td><td>${statusBadge(customer.status ?? "active")}</td><td><a class="icon-button" href="/customers/${encodeURIComponent(id)}" data-route="/customers/${encodeURIComponent(id)}" aria-label="View customer" title="View customer"><i data-lucide="arrow-up-right"></i></a></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("building-2", "No customers yet", "Add a customer business to prepare a quotation.")}`;
    const customerTable = body.querySelector<HTMLTableElement>("table");
    if (customerTable) {
      const headerRow = customerTable.tHead?.rows[0];
      const typeHeader = document.createElement("th");
      typeHeader.textContent = "Account Type";
      headerRow?.insertBefore(typeHeader, headerRow.cells[1] ?? null);
      customerTable.tBodies[0]?.querySelectorAll("tr").forEach((row, index) => {
        const typeCell = document.createElement("td");
        typeCell.innerHTML = `<span class="customer-type-badge">${escapeHtml(accountTypeLabel(result.items[index]?.account_type))}</span>`;
        row.insertBefore(typeCell, row.cells[1] ?? null);
      });
    }
    const currencyHeading = [...body.querySelectorAll("th")].find((node) => node.textContent === "Currency");
    if (currencyHeading) currencyHeading.textContent = "Display";
    const actionHeading = body.querySelector("th:last-child");
    if (actionHeading) actionHeading.textContent = "Actions";
    const toolbar = body.querySelector<HTMLElement>(".table-toolbar");
    if (toolbar && !toolbar.querySelector("[data-client-type-filter]")) {
      const typeSelect = document.createElement("select");
      typeSelect.className = "client-type-filter";
      typeSelect.dataset.clientTypeFilter = "true";
      typeSelect.setAttribute("aria-label", "Filter by account type");
      typeSelect.innerHTML = `<option value="">All Account Types</option>${PRICE_LIST_ACCOUNT_TYPES.map((type) => `<option value="${type.code}" ${type.code === clientTypeFilter ? "selected" : ""}>${type.label}</option>`).join("")}`;
      typeSelect.value = clientTypeFilter;
      typeSelect.addEventListener("change", () => { clientTypeFilter = typeSelect.value; void load(); });
      toolbar.append(typeSelect);
    }
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
    const profileGrid = body.querySelector<HTMLElement>(".customer-profile-panel .detail-grid");
    if (profileGrid) {
      const typeField = document.createElement("div");
      typeField.innerHTML = `<span>Account Type</span><strong>${escapeHtml(accountTypeLabel(customer.account_type))}</strong>`;
      profileGrid.insertBefore(typeField, profileGrid.children[1] ?? null);
      if (appStore.state.user?.role_id === "superadmin") {
        const incentiveField = document.createElement("div");
        incentiveField.innerHTML = `<span>Have to give incentive</span><strong>${customer.have_to_give_incentive ? `Yes · ${Number(customer.customer_incentive_percentage ?? 0).toFixed(1)}%` : "No"}</strong>`;
        profileGrid.append(incentiveField);
      }
    }
    const accessGrid = body.querySelector<HTMLElement>(".customer-access-panel .detail-grid");
    if (accessGrid) {
      const managers = customer.access?.assigned_managers ?? [];
      const managerField = document.createElement("div");
      managerField.innerHTML = `<span>Manager</span><strong>${escapeHtml(managers.length ? managers.map((manager) => manager.name).join(", ") : "No manager assigned")}</strong>`;
      accessGrid.append(managerField);
    }
    body.querySelector("#edit-customer")?.addEventListener("click", () => { void openCustomerEditor(customer, load); });
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customer unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
