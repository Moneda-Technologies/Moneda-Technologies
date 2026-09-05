import { customerApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import type { Customer } from "../types/domain";
import { emptyState, escapeHtml, skeleton } from "../utils/dom";

const customerName = (customer: Customer) => customer.company_name ?? customer.name;

export async function customersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Relationships", "Customers", "Manage the customer businesses Moneda Technologies quotes and sells to.", '<button class="button button-secondary"><i data-lucide="download"></i>Export</button><button class="button button-primary" id="add-customer"><i data-lucide="building-2"></i>Add Customer</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  const load = async () => {
    const result = await customerApi.list();
    body.innerHTML = `<div class="table-toolbar"><div class="field-search"><i data-lucide="search"></i><input placeholder="Search customers" aria-label="Search customers"></div><div class="segmented"><button class="active">All</button><button>Active</button><button>Archived</button></div><span>${result.pagination?.total ?? result.items.length} records</span></div>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Customer company</th><th>Primary contact</th><th>Email / phone</th><th>Currency</th><th>Status</th><th></th></tr></thead><tbody>${result.items.map((customer) => { const name = customerName(customer); const id = customer.customer_id ?? customer._id; return `<tr><td><div class="table-identity"><span>${escapeHtml(name.slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(name)}</strong><small>${escapeHtml(customer.address ?? "")}</small></p></div></td><td>${escapeHtml(customer.contact_name ?? "—")}</td><td><strong>${escapeHtml(customer.email ?? "No email")}</strong><small>${escapeHtml(customer.phone ?? "")}</small></td><td><span class="currency-tag">${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</span></td><td>${statusBadge(customer.status ?? "active")}</td><td><a class="icon-button" href="/customers/${encodeURIComponent(id)}" data-route="/customers/${encodeURIComponent(id)}" aria-label="View customer" title="View customer"><i data-lucide="arrow-up-right"></i></a></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("building-2", "No customers yet", "Add a customer business to prepare a quotation.")}`;
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customers unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector("#add-customer")?.addEventListener("click", () => {
    const content = document.createElement("div");
    content.innerHTML = `<form class="stack-form" id="customer-form"><div class="form-grid"><label>Customer company name<input name="company_name" required placeholder="Registered company"></label><label>Primary contact<input name="contact_name" placeholder="Contact person"></label><label>Email<input name="email" type="email" placeholder="procurement@company.com"></label><label>Phone<input name="phone" placeholder="+91"></label><label>Country<input name="country" placeholder="India"></label><label>Preferred currency<select name="preferred_currency"><option>EUR</option><option>USD</option><option>INR</option></select></label><label>Payment terms<input name="payment_terms" value="30 days"></label><label>Tax number<input name="gst_vat_number" placeholder="Optional"></label><label>Address<textarea name="address" rows="2" placeholder="Customer address"></textarea></label></div><button class="button button-primary button-full" type="submit"><i data-lucide="save"></i>Create Customer</button></form>`;
    const dialog = openModal("Add Customer", content, "wide");
    content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
      event.preventDefault(); const value = Object.fromEntries(new FormData(event.currentTarget as HTMLFormElement).entries());
      try { await customerApi.create(value); dialog.close(); toast("Customer created"); await load(); }
      catch (error) { toast(error instanceof Error ? error.message : "Customer could not be created", "error"); }
    });
    refreshIcons(content);
  });
  refreshIcons(page);
  return page;
}

export async function customerDetailPage(customerId: string): Promise<HTMLElement> {
  const page = pageScaffold("Relationships", "Customer detail", "A customer business record with commercial history and contact context.", '<a class="button button-secondary" href="/customers" data-route="/customers"><i data-lucide="arrow-left"></i>Back to customers</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  try {
    const customer = await customerApi.get(customerId);
    const related = customer.related ?? {};
    const name = customerName(customer);
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar">${escapeHtml(name.slice(0, 2).toUpperCase())}</div><div><span class="eyebrow">Customer business</span><h2>${escapeHtml(name)}</h2><p>${escapeHtml(customer.contact_name ?? "Primary contact not configured")}</p>${statusBadge(customer.status ?? "active")}</div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Contact & billing</span><h2>Commercial profile</h2></div></div><div class="detail-grid"><div><span>Email</span><strong>${escapeHtml(customer.email ?? "—")}</strong></div><div><span>Phone</span><strong>${escapeHtml(customer.phone ?? "—")}</strong></div><div><span>Currency</span><strong>${escapeHtml(customer.default_currency ?? customer.preferred_currency ?? "EUR")}</strong></div><div><span>Payment terms</span><strong>${escapeHtml(customer.payment_terms ?? "—")}</strong></div><div><span>Tax number</span><strong>${escapeHtml(customer.gst_vat_number ?? customer.tax_number ?? "—")}</strong></div><div><span>Address</span><strong>${escapeHtml(customer.address ?? "—")}</strong></div></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Activity</span><h2>Related records</h2></div></div><div class="metric-grid compact-metrics"><article class="metric-card"><span>Quotations</span><strong>${related.quotations?.length ?? 0}</strong></article><article class="metric-card"><span>Orders</span><strong>${related.orders?.length ?? 0}</strong></article><article class="metric-card"><span>Leads</span><strong>${related.leads?.length ?? 0}</strong></article></div></section></div>`;
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customer unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
