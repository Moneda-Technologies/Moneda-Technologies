import { api } from "../api/client";
import { refreshIcons } from "../components/icons";
import { pageScaffold } from "../components/page";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, skeleton } from "../utils/dom";

export async function reportsPage(): Promise<HTMLElement> {
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  const exportUrl = customerCompany ? `/api/v1/reports/quotations.csv?customer_company_id=${encodeURIComponent(customerCompany._id)}` : "#";
  const page = pageScaffold("Intelligence", "Reports", "Filter, inspect and export operational data without changing source records.", `<a class="button button-secondary" href="${exportUrl}"><i data-lucide="file-down"></i>Export quotations</a>`);
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "No customer selected", "Choose a customer to view reports."); return page; }
  try {
    const data = await api<Record<string, number>>(`/reports/summary?customer_company_id=${encodeURIComponent(customerCompany._id)}`);
    const reports = [["Sales performance", "Revenue, conversion and salesperson outcomes", "chart-spline"], ["Quotation analysis", "Status, currency, tax and discount views", "file-chart-column"], ["Customer growth", "Acquisition and commercial activity", "users-round"], ["Product demand", "Configured catalog lines and category mix", "package-search"], ["Tax summary", "Product tax collected by rate and mode", "landmark"], ["Currency exposure", "EUR master conversion into USD and INR", "euro"]];
    body.innerHTML = `<div class="report-stats"><span><strong>${data.customers}</strong> customers</span><span><strong>${data.quotations}</strong> quotations</span><span><strong>${data.orders}</strong> orders</span><span><strong>${data.leads}</strong> leads</span></div><div class="report-grid">${reports.map(([title, copy, icon]) => `<article class="report-card"><span><i data-lucide="${icon}"></i></span><div><h3>${title}</h3><p>${copy}</p></div><button class="icon-button" aria-label="Open ${escapeHtml(title)} report" title="Open report"><i data-lucide="arrow-up-right"></i></button></article>`).join("")}</div><div class="notice compact"><i data-lucide="info"></i><div><strong>Reports reflect live business records</strong><p>No illustrative revenue or conversion values are injected into production reporting.</p></div></div>`;
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Reports unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
