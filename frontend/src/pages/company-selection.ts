import { cartApi, customerCompanyApi } from "../api";
import { refreshIcons } from "../components/icons";
import { pageScaffold } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { Company, Customer } from "../types/domain";
import { emptyState, escapeHtml, skeleton } from "../utils/dom";

const RECENT_KEY = "moneda-recent-customer-companies";
const SELECTED_KEY = "moneda-active-customer-id";

function recentIds(): string[] {
  try { return JSON.parse(localStorage.getItem(RECENT_KEY) ?? "[]") as string[]; }
  catch { return []; }
}

async function selectCustomerCompany(customerCompany: Customer): Promise<void> {
  const current = appStore.state.customer;
  const customerId = customerCompany.customer_id ?? customerCompany._id;
  if (current && current._id !== customerCompany._id) {
    const cart = await cartApi.get(current.customer_id ?? current._id, appStore.state.currency).catch(() => null);
    if (cart?.items?.length) {
      const confirmed = window.confirm(`Switch customer?\n\nYour current cart belongs to ${current.name}. ${customerCompany.name} has a separate cart.\n\nCancel to stay, or OK to switch without deleting either cart.`);
      if (!confirmed) return;
    }
  }
  await customerCompanyApi.select(customerId);
  const recent = [customerId, ...recentIds().filter((id) => id !== customerId)].slice(0, 4);
  localStorage.setItem(RECENT_KEY, JSON.stringify(recent));
  localStorage.setItem(SELECTED_KEY, customerId);
  appStore.set({ customer: customerCompany, activeCustomerId: customerId, customerCompany: customerCompany as unknown as Company, company: customerCompany as unknown as Company, currency: customerCompany.preferred_currency ?? customerCompany.default_currency ?? "EUR", cartCount: 0 });
  window.dispatchEvent(new CustomEvent("moneda:customer-selected", { detail: customerId }));
  window.dispatchEvent(new CustomEvent("moneda:customer-company-selected", { detail: customerId }));
  window.dispatchEvent(new CustomEvent("moneda:company-selected", { detail: customerId }));
  window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/calculator" }));
}

function customerCompanyCard(customerCompany: Customer, recent: boolean, selected: boolean): string {
  const id = customerCompany.customer_id ?? customerCompany._id;
  const name = customerCompany.company_name ?? customerCompany.name;
  return `<button class="company-select-card ${selected ? "selected" : ""}" data-company="${escapeHtml(id)}" role="option" aria-selected="${selected}"><span class="company-logo-mini">${escapeHtml(name.slice(0, 2).toUpperCase())}</span><span class="company-select-copy"><small>${recent ? "Recently used customer" : "Available customer"}</small><strong>${escapeHtml(name)}</strong><span>${escapeHtml(customerCompany.contact_name ?? customerCompany.email ?? customerCompany.country ?? "Customer details pending")}</span></span><span class="company-currency">${escapeHtml(customerCompany.preferred_currency ?? customerCompany.default_currency ?? "EUR")}</span><i data-lucide="arrow-right"></i></button>`;
}

export async function companySelectionPage(): Promise<HTMLElement> {
  const actions = appStore.can("customers.create") ? '<a class="button button-secondary" href="/customers" data-route="/customers"><i data-lucide="plus"></i>Add Customer</a>' : "";
  appStore.set({ customer: null, activeCustomerId: null, customerCompany: null, company: null, cartCount: 0 });
  localStorage.removeItem(SELECTED_KEY);
  localStorage.removeItem("moneda-selected-customer-company");
  localStorage.removeItem("moneda-selected-company");
  const page = pageScaffold("Quotation workspace", "Select Customer", "Choose who this quotation is for before configuring Moneda Technologies products.", actions);
  page.classList.add("company-selection-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);

  try {
    let customerCompanies = (await customerCompanyApi.list()).items;
    const recent = recentIds();
    customerCompanies = [...customerCompanies].sort((a, b) => {
      const ai = recent.indexOf(a._id); const bi = recent.indexOf(b._id);
      if (ai >= 0 && bi >= 0) return ai - bi;
      if (ai >= 0) return -1;
      if (bi >= 0) return 1;
      return (a.company_name ?? a.name).localeCompare(b.company_name ?? b.name);
    });
    body.innerHTML = `<section class="company-picker panel"><div class="workspace-steps"><span class="active"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span><b>2</b>Products</span><i data-lucide="chevron-right"></i><span><b>3</b>Configure</span><i data-lucide="chevron-right"></i><span><b>4</b>Quotation</span></div><div class="company-search"><i data-lucide="search"></i><input id="company-search" aria-label="Search customers" placeholder="Search customers..." autocomplete="off"><kbd>Arrow keys</kbd><kbd>Enter</kbd></div><div class="company-picker-meta"><span><strong id="company-result-count">${customerCompanies.length}</strong> available customers</span><span>Customer access is enforced by the API</span></div><div id="company-options" class="company-options" role="listbox"></div></section>`;
    const options = body.querySelector<HTMLElement>("#company-options")!;
    const input = body.querySelector<HTMLInputElement>("#company-search")!;
    let visible = customerCompanies;
    let activeIndex = -1;

    const render = () => {
      options.innerHTML = visible.length ? visible.map((customerCompany, index) => customerCompanyCard(customerCompany, recent.includes(customerCompany.customer_id ?? customerCompany._id), index === activeIndex)).join("") : emptyState("building-2", "No customers found", "Try a different customer name.");
      body.querySelector<HTMLElement>("#company-result-count")!.textContent = String(visible.length);
      options.querySelectorAll<HTMLButtonElement>("[data-company]").forEach((button) => button.addEventListener("click", () => {
        const customerCompany = visible.find((item) => (item.customer_id ?? item._id) === button.dataset.company);
        if (customerCompany) void selectCustomerCompany(customerCompany).catch((error) => toast(error instanceof Error ? error.message : "Customer could not be selected", "error"));
      }));
      refreshIcons(options);
    };
    render();
    input.addEventListener("input", () => {
      const term = input.value.trim().toLowerCase();
      visible = customerCompanies.filter((customerCompany) => `${customerCompany.company_name ?? customerCompany.name} ${customerCompany.contact_name ?? ""} ${customerCompany.email ?? ""}`.toLowerCase().includes(term));
      activeIndex = 0; render();
    });
    input.addEventListener("keydown", (event) => {
      if (!visible.length) return;
      if (event.key === "ArrowDown") { event.preventDefault(); activeIndex = (activeIndex + 1) % visible.length; render(); }
      if (event.key === "ArrowUp") { event.preventDefault(); activeIndex = (activeIndex - 1 + visible.length) % visible.length; render(); }
      if (event.key === "Enter" && activeIndex >= 0) { event.preventDefault(); void selectCustomerCompany(visible[activeIndex]).catch((error) => toast(error instanceof Error ? error.message : "Customer could not be selected", "error")); }
    });
  } catch (error) {
    body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customers unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
  }
  refreshIcons(page);
  return page;
}
