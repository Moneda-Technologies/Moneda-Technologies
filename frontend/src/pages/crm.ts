import { crmApi } from "../api";
import { refreshIcons } from "../components/icons";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatMoney, skeleton } from "../utils/dom";

const activeCustomer = () => appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;

export async function crmPage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "CRM pipeline", "Move opportunities forward with clear ownership, value and next actions.", '<button class="button button-secondary"><i data-lucide="list-filter"></i>Filter</button><button class="button button-primary"><i data-lucide="plus"></i>New lead</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  const customerCompany = activeCustomer();
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "No customer selected", "Choose a customer to view its sales pipeline."); return page; }
  try {
    const data = await crmApi.leads(customerCompany._id);
    const statuses = ["Lead", "Follow Up", "Order Received", "Won", "Lost", "Closed"];
    body.innerHTML = `<div class="pipeline-metrics"><div><span>Open pipeline</span><strong>${formatMoney(data.items.filter((lead) => !["Won", "Lost", "Closed"].includes(String(lead.status))).reduce((sum, lead) => sum + Number(lead.estimated_value ?? 0), 0), appStore.state.currency)}</strong></div><div><span>Active leads</span><strong>${data.items.length}</strong></div><div><span>Needs follow-up</span><strong>${data.items.filter((lead) => lead.status === "Follow Up").length}</strong></div></div><div class="kanban">${statuses.slice(0, 4).map((status) => { const rows = data.items.filter((lead) => lead.status === status); return `<section class="kanban-column"><header><div>${statusBadge(status)}<strong>${rows.length}</strong></div><button class="icon-button" aria-label="Add opportunity to ${escapeHtml(status)}" title="Add opportunity"><i data-lucide="plus"></i></button></header><div class="kanban-body">${rows.length ? rows.map((lead) => `<article class="lead-card"><span class="eyebrow">${escapeHtml(lead.source ?? "Direct")}</span><h3>${escapeHtml(lead.title ?? lead.notes ?? "Sales opportunity")}</h3><p>${escapeHtml(lead.customer_name ?? "Customer not linked")}</p><div><strong>${formatMoney(Number(lead.estimated_value ?? 0), String(lead.currency ?? appStore.state.currency))}</strong><span class="avatar mini">${escapeHtml(String(lead.assigned_to ?? "MT").slice(0, 2).toUpperCase())}</span></div></article>`).join("") : '<div class="kanban-empty"><i data-lucide="circle-dashed"></i><span>No opportunities</span></div>'}</div></section>`; }).join("")}</div>`;
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Pipeline unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function remindersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Reminders", "Keep follow-ups visible and close every commercial loop.", '<button class="button button-primary"><i data-lucide="plus"></i>New reminder</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  const customerCompany = activeCustomer();
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "No customer selected", "Choose a customer to view reminders."); return page; }
  try { const data = await crmApi.reminders(customerCompany._id); body.innerHTML = data.items.length ? `<div class="data-table panel"><table><thead><tr><th>Reminder</th><th>Related to</th><th>Due</th><th>Priority</th><th>Status</th></tr></thead><tbody>${data.items.map((item) => `<tr><td><strong>${escapeHtml(item.notes ?? "Follow up")}</strong></td><td>${escapeHtml(item.customer_id ?? item.lead_id ?? "—")}</td><td>${escapeHtml(item.due_date ?? "—")}</td><td>${statusBadge(String(item.priority ?? "normal"))}</td><td>${statusBadge(String(item.status ?? "open"))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("bell-ring", "All caught up", "Open and recurring reminders will appear here."); }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Reminders unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function remindersWorkspacePage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Reminders", "Keep follow-ups visible and close every commercial loop.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  const customerCompany = activeCustomer();
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "No customer selected", "Choose a customer to view reminders."); return page; }
  try {
    const data = await crmApi.reminders(customerCompany._id);
    body.innerHTML = data.items.length ? `<div class="data-table panel"><table><thead><tr><th>Reminder</th><th>Related to</th><th>Due</th><th>Priority</th><th>Status</th><th></th></tr></thead><tbody>${data.items.map((item) => { const status = String(item.status ?? "Pending"); const open = !["completed", "cancelled"].includes(status.toLowerCase()); return `<tr><td><strong>${escapeHtml(String(item.notes ?? "Follow up"))}</strong></td><td>${escapeHtml(String(item.customer_id ?? item.lead_id ?? "—"))}</td><td>${escapeHtml(String(item.due_date ?? "—"))}</td><td>${statusBadge(String(item.priority ?? "normal"))}</td><td>${statusBadge(status)}</td><td>${open ? `<button class="button button-quiet complete-reminder" data-id="${escapeHtml(String(item._id))}"><i data-lucide="check"></i>Complete</button>` : ""}</td></tr>`; }).join("")}</tbody></table></div>` : emptyState("bell-ring", "All caught up", "Open and recurring reminders will appear here.");
    body.querySelectorAll<HTMLButtonElement>(".complete-reminder").forEach((button) => button.addEventListener("click", async () => { button.disabled = true; try { const result = await crmApi.completeReminder(button.dataset.id!); toast(result.next_reminder ? "Reminder completed and next follow-up scheduled" : "Reminder completed"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/reminders" })); } catch (error) { toast(error instanceof Error ? error.message : "Reminder could not be completed", "error"); button.disabled = false; } }));
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Reminders unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
