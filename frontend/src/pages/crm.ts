import { crmApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatMoney, skeleton } from "../utils/dom";

const stages = ["Lead", "Follow Up", "Order Received", "Won", "Lost", "Closed"];

const customerId = (customer: Record<string, unknown>) => String(customer.customer_id ?? customer._id ?? "");
const customerLabel = (customer: Record<string, unknown>) => String(customer.company_name ?? customer.name ?? "Customer");

function leadValue(lead: Record<string, unknown>): number {
  return Number(lead.value_eur ?? lead.estimated_value ?? lead.value ?? 0) || 0;
}

function leadCard(lead: Record<string, unknown>): string {
  const id = String(lead.customer_id ?? "");
  const customer = lead.customer_name
    ? (id ? `<a href="/customers/${encodeURIComponent(id)}" data-route="/customers/${encodeURIComponent(id)}">${escapeHtml(String(lead.customer_name))}</a>` : escapeHtml(String(lead.customer_name)))
    : "Customer not linked";
  const ownerName = String(lead.owner_name ?? "Unassigned");
  return `<article class="lead-card"><span class="eyebrow">${escapeHtml(String(lead.source ?? "Direct"))}</span><h3>${escapeHtml(String(lead.title ?? lead.notes ?? "Sales opportunity"))}</h3><p class="lead-customer">${customer}</p><div class="lead-meta"><span><b class="avatar mini">${escapeHtml(String(lead.owner_initials ?? "—"))}</b><span>${escapeHtml(ownerName)}</span></span><strong>${formatMoney(leadValue(lead), "EUR")}</strong></div>${lead.next_action || lead.next_action_date ? `<small class="lead-next-action">${escapeHtml(String(lead.next_action ?? "Next action"))}${lead.next_action_date ? ` · ${escapeHtml(String(lead.next_action_date))}` : ""}</small>` : ""}</article>`;
}

function customerOptions(selected: string): string {
  return `<option value="">All Customers</option>${appStore.state.customers.map((customer) => `<option value="${escapeHtml(customerId(customer as unknown as Record<string, unknown>))}" ${customerId(customer as unknown as Record<string, unknown>) === selected ? "selected" : ""}>${escapeHtml(customerLabel(customer as unknown as Record<string, unknown>))}</option>`).join("")}`;
}

function openLeadEditor(preselectedCustomerId: string, reload: () => Promise<void>): void {
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form crm-lead-form"><div class="form-grid"><label>Customer<select name="customer_id"><option value="">No customer linked yet</option>${appStore.state.customers.map((customer) => { const row = customer as unknown as Record<string, unknown>; const id = customerId(row); return `<option value="${escapeHtml(id)}" ${id === preselectedCustomerId ? "selected" : ""}>${escapeHtml(customerLabel(row))}</option>`; }).join("")}</select></label><label>Owner<input value="${escapeHtml(appStore.state.user?.name ?? "Current user")}" disabled></label><label>Lead / opportunity title *<input name="title" required placeholder="Company opportunity"></label><label>Value (EUR)<input name="value_eur" type="number" min="0" step="0.01" value="0"></label><label>Contact name<input name="contact_name"></label><label>Email<input name="email" type="email"></label><label>Phone<input name="phone" type="tel" inputmode="tel"></label><label>Stage<select name="status">${stages.map((stage) => `<option>${stage}</option>`).join("")}</select></label><label>Next action<input name="next_action" placeholder="Follow up with procurement"></label><label>Next action date<input name="next_action_date" type="date"></label></div><label>Notes<textarea name="notes" rows="3"></textarea><small class="field-error" data-lead-error></small><button class="button button-primary button-full" type="submit"><i data-lucide="save"></i>Create lead</button></form>`;
  const dialog = openModal("New lead", content, "wide");
  content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    const title = String(data.get("title") ?? "").trim();
    const error = content.querySelector<HTMLElement>("[data-lead-error]");
    if (!title) { if (error) error.textContent = "A lead title is required."; return; }
    try {
      await crmApi.createLead({ customer_id: data.get("customer_id") || null, title, value_eur: Number(data.get("value_eur") || 0), status: data.get("status"), contact_name: data.get("contact_name"), email: data.get("email"), phone: data.get("phone"), next_action: data.get("next_action"), next_action_date: data.get("next_action_date"), notes: data.get("notes") });
      dialog.close(); toast("Lead created"); await reload();
    } catch (saveError) { if (error) error.textContent = saveError instanceof Error ? saveError.message : "Lead could not be created"; }
  });
  refreshIcons(content);
}

export async function crmPage(): Promise<HTMLElement> {
  const url = new URLSearchParams(window.location.search);
  // CRM is company-wide by default; only an explicit URL/filter selection
  // scopes it to one customer.
  const initialCustomer = url.get("customer_id") ?? "";
  const page = pageScaffold("Sales", "CRM Pipeline", "Company-wide sales activity with customer relationships, ownership, value and next actions.", '<button class="button button-primary" id="new-lead"><i data-lucide="plus"></i>New lead</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  const load = async () => {
    const filters = body.querySelector<HTMLFormElement>("[data-crm-filters]");
    const query = new URLSearchParams();
    const values = filters ? new FormData(filters) : new FormData();
    const selectedFilter = filters ? String(values.get("customer_id") ?? "") : initialCustomer;
    ["customer_id", "owner_id", "status", "country", "region", "date_from", "date_to"].forEach((key) => { const value = String(values.get(key) ?? ""); if (value) query.set(key, value); });
    const valueRange = String(values.get("value_range") ?? "");
    if (valueRange) { const [minimum, maximum] = valueRange.split(":"); if (minimum) query.set("min_value", minimum); if (maximum) query.set("max_value", maximum); }
    if (!filters && initialCustomer) query.set("customer_id", initialCustomer);
    const data = await crmApi.leads(query.toString());
    const selectedId = selectedFilter;
    const selectedName = selectedId ? customerLabel((appStore.state.customers.find((item) => customerId(item as unknown as Record<string, unknown>) === selectedId) ?? {}) as unknown as Record<string, unknown>) : "All Customers";
    const owners = [...new Map(data.items.map((row) => [String(row.owner_user_id ?? ""), String(row.owner_name ?? "Unassigned")])).entries()].filter(([id]) => id);
    const countries = [...new Set(appStore.state.customers.map((item) => item.country_name ?? item.country).filter(Boolean))] as string[];
    const regions = [...new Set(appStore.state.customers.map((item) => item.continent ?? item.region?.continent).filter(Boolean))] as string[];
    const status = String(values.get("status") ?? "");
    const group = (stage: string) => data.items.filter((lead) => String(lead.status ?? lead.stage ?? "Lead") === stage);
    body.innerHTML = `<form class="crm-filters panel" data-crm-filters><div class="crm-filter-heading"><div><span class="eyebrow">CRM Pipeline</span><h2>${escapeHtml(selectedName)}</h2></div><span class="result-count">${data.total} records</span></div><div class="crm-filter-grid"><label>Customer<select name="customer_id">${customerOptions(selectedId)}</select></label><label>Owner<select name="owner_id"><option value="">All Owners</option>${owners.map(([id, name]) => `<option value="${escapeHtml(id)}" ${String(values.get("owner_id") ?? "") === id ? "selected" : ""}>${escapeHtml(name)}</option>`).join("")}</select></label><label>Stage<select name="status"><option value="">All Stages</option>${stages.map((item) => `<option value="${item}" ${status === item ? "selected" : ""}>${item}</option>`).join("")}</select></label><label>Country<select name="country"><option value="">All Countries</option>${countries.map((item) => `<option ${String(values.get("country") ?? "") === item ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select></label><label>Region<select name="region"><option value="">All Regions</option>${regions.map((item) => `<option ${String(values.get("region") ?? "") === item ? "selected" : ""}>${escapeHtml(item)}</option>`).join("")}</select></label><label>Value<select name="value_range"><option value="">Any value</option><option value="0:1000" ${valueRange === "0:1000" ? "selected" : ""}>Under €1,000</option><option value="1000:5000" ${valueRange === "1000:5000" ? "selected" : ""}>€1,000–€5,000</option><option value="5000:10000" ${valueRange === "5000:10000" ? "selected" : ""}>€5,000–€10,000</option><option value="10000:" ${valueRange === "10000:" ? "selected" : ""}>Over €10,000</option></select></label><label>From<input name="date_from" type="date" value="${escapeHtml(String(values.get("date_from") ?? ""))}"></label><label>To<input name="date_to" type="date" value="${escapeHtml(String(values.get("date_to") ?? ""))}"></label></div></form><div class="pipeline-metrics"><div><span>Open pipeline</span><strong>${formatMoney(data.items.filter((lead) => !["Won", "Lost", "Closed"].includes(String(lead.status))).reduce((sum, lead) => sum + leadValue(lead), 0), "EUR")}</strong></div><div><span>Active leads</span><strong>${data.items.filter((lead) => !["Won", "Lost", "Closed"].includes(String(lead.status))).length}</strong></div><div><span>Needs follow-up</span><strong>${data.items.filter((lead) => String(lead.status) === "Follow Up").length}</strong></div></div><div class="kanban">${stages.slice(0, 4).map((stage) => { const rows = group(stage); return `<section class="kanban-column"><header><div>${statusBadge(stage)}<strong>${rows.length}</strong></div></header><div class="kanban-body">${rows.length ? rows.map(leadCard).join("") : '<div class="kanban-empty"><i data-lucide="circle-dashed"></i><span>No opportunities</span></div>'}</div></section>`; }).join("")}</div>${data.items.length ? "" : emptyState("chart-no-axes-combined", "No CRM activity yet", "Create a lead to start the company-wide pipeline.")}`;
    body.querySelector<HTMLFormElement>("[data-crm-filters]")?.addEventListener("change", () => { void load(); });
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Pipeline unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector<HTMLButtonElement>("#new-lead")?.addEventListener("click", () => openLeadEditor(initialCustomer, load));
  refreshIcons(page);
  return page;
}

export async function remindersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Reminders", "Keep follow-ups visible and close every commercial loop.", '<button class="button button-primary"><i data-lucide="plus"></i>New reminder</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  try { const data = await crmApi.reminders(); body.innerHTML = data.items.length ? `<div class="data-table panel"><table><thead><tr><th>Reminder</th><th>Related to</th><th>Due</th><th>Priority</th><th>Status</th></tr></thead><tbody>${data.items.map((item) => `<tr><td><strong>${escapeHtml(String(item.notes ?? "Follow up"))}</strong></td><td>${escapeHtml(String(item.customer_id ?? item.lead_id ?? "—"))}</td><td>${escapeHtml(String(item.due_date ?? "—"))}</td><td>${statusBadge(String(item.priority ?? "normal"))}</td><td>${statusBadge(String(item.status ?? "open"))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("bell-ring", "All caught up", "Open and recurring reminders will appear here."); }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Reminders unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function remindersWorkspacePage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Reminders", "Keep follow-ups visible and close every commercial loop.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  try { const data = await crmApi.reminders(); body.innerHTML = data.items.length ? `<div class="data-table panel"><table><thead><tr><th>Reminder</th><th>Related to</th><th>Due</th><th>Priority</th><th>Status</th><th></th></tr></thead><tbody>${data.items.map((item) => { const state = String(item.status ?? "Pending"); const open = !["completed", "cancelled"].includes(state.toLowerCase()); return `<tr><td><strong>${escapeHtml(String(item.notes ?? "Follow up"))}</strong></td><td>${escapeHtml(String(item.customer_id ?? item.lead_id ?? "—"))}</td><td>${escapeHtml(String(item.due_date ?? "—"))}</td><td>${statusBadge(String(item.priority ?? "normal"))}</td><td>${statusBadge(state)}</td><td>${open ? `<button class="button button-quiet complete-reminder" data-id="${escapeHtml(String(item._id))}"><i data-lucide="check"></i>Complete</button>` : ""}</td></tr>`; }).join("")}</tbody></table></div>` : emptyState("bell-ring", "All caught up", "Open and recurring reminders will appear here."); body.querySelectorAll<HTMLButtonElement>(".complete-reminder").forEach((button) => button.addEventListener("click", async () => { button.disabled = true; try { const result = await crmApi.completeReminder(button.dataset.id!); toast(result.next_reminder ? "Reminder completed and next follow-up scheduled" : "Reminder completed"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/reminders" })); } catch (error) { toast(error instanceof Error ? error.message : "Reminder could not be completed", "error"); button.disabled = false; } })); }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Reminders unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
