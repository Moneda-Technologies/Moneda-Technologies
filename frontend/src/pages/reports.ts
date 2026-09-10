import { api } from "../api/client";
import { refreshIcons } from "../components/icons";
import { pageScaffold } from "../components/page";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatMoney, skeleton } from "../utils/dom";

type ReportCustomerOption = { _id: string; name: string };
type ReportSummary = { customers: number; quotations: number; orders: number; leads: number; customer_options?: ReportCustomerOption[] };
type ReportMetric = { label: string; value: string | number; format?: "money" | "percent" };
type ReportDetail = { report: string; title: string; description: string; scope: { customer_count: number; customer_filter?: string | null }; customer_options?: ReportCustomerOption[]; metrics: ReportMetric[]; rows: Record<string, unknown>[]; rates?: { rates?: Record<string, number>; source?: string; stale?: boolean } | null; empty_message?: string };

const reportCards = [
  ["sales-performance", "Sales performance", "Revenue, conversion and salesperson outcomes", "chart-spline"],
  ["quotation-analysis", "Quotation analysis", "Status, currency, tax and discount views", "file-chart-column"],
  ["customer-growth", "Customer growth", "Acquisition and commercial activity", "users-round"],
  ["product-demand", "Product demand", "Configured catalog lines and category mix", "package-search"],
  ["tax-summary", "Tax summary", "Product tax collected by rate and mode", "landmark"],
  ["currency-exposure", "Currency exposure", "EUR master conversion into USD and INR", "euro"],
] as const;

const reportBySlug = new Map<string, (typeof reportCards)[number]>(reportCards.map((card) => [card[0], card]));
const queryFor = (customerId: string): string => customerId ? `?customer_id=${encodeURIComponent(customerId)}` : "";

function metricValue(metric: ReportMetric): string {
  if (metric.format === "money") return formatMoney(Number(metric.value) || 0, "EUR");
  if (metric.format === "percent") return `${Number(metric.value) || 0}%`;
  return escapeHtml(metric.value);
}

function detailColumns(slug: string): Array<[string, string, (value: unknown, row?: Record<string, unknown>) => string]> {
  const money = (value: unknown, row?: Record<string, unknown>) => formatMoney(Number(value) || 0, String(row?.currency || "EUR"));
  const number = (value: unknown) => Number(value || 0).toLocaleString("en-IN");
  if (slug === "sales-performance") return [["salesperson", "Salesperson", String], ["currency", "Currency", String], ["orders", "Orders", number], ["revenue", "Revenue", money]];
  if (slug === "quotation-analysis") return [["status", "Status", String], ["currency", "Currency", String], ["count", "Quotations", number], ["value", "Value", money]];
  if (slug === "customer-growth") return [["period", "Period", String], ["new_customers", "New customers", number], ["active", "Active", number], ["archived", "Archived", number]];
  if (slug === "product-demand") return [["product", "Product", String], ["category", "Category", String], ["quantity", "Quantity", number], ["quotations", "Quotations", number]];
  if (slug === "tax-summary") return [["rate_mode", "Rate / mode", String], ["currency", "Currency", String], ["taxable", "Taxable amount", money], ["tax", "Tax recorded", money], ["documents", "Documents", number]];
  return [["currency", "Currency", String], ["amount", "Recorded amount", money]];
}

export async function reportsPage(): Promise<HTMLElement> {
  const role = String(appStore.state.user?.role_id ?? "user").toLowerCase();
  const globalScope = role === "admin" || role === "superadmin";
  const subtitle = globalScope ? "View and analyze sales activity across all customers." : "View and analyze sales activity across your assigned customers.";
  const page = pageScaffold("Intelligence", "Reports", subtitle, '<a class="button button-secondary" id="reports-export" href="/api/v1/reports/quotations.csv"><i data-lucide="file-down"></i>Export quotations</a>');
  page.classList.add("reports-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  let customerFilter = new URLSearchParams(location.search).get("customer_id") || "";
  let customerOptions: ReportCustomerOption[] = [];

  const renderError = (error: unknown) => {
    body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Reports unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
    refreshIcons(page);
  };

  const bindCardNavigation = () => {
    body.querySelectorAll<HTMLElement>(".report-card[data-report-href]").forEach((card) => {
      const href = card.dataset.reportHref;
      if (!href) return;
      card.addEventListener("click", (event) => {
        if ((event.target as HTMLElement).closest("a,button")) return;
        window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: href }));
      });
      card.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        if ((event.target as HTMLElement).closest("a,button")) return;
        event.preventDefault();
        window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: href }));
      });
    });
  };

  const load = async () => {
    const query = queryFor(customerFilter);
    const data = await api<ReportSummary>(`/reports/summary${query}`);
    if (!customerOptions.length) customerOptions = data.customer_options ?? [];
    const exportLink = page.querySelector<HTMLAnchorElement>("#reports-export");
    if (exportLink) exportLink.href = `/api/v1/reports/quotations.csv${query}`;
    if (!customerOptions.length) {
      body.innerHTML = emptyState("building-2", "No customers assigned", "You don't have any customers assigned yet.");
      refreshIcons(page);
      return;
    }
    const noReportData = !data.quotations && !data.orders && !data.leads;
    body.innerHTML = `<div class="report-filters panel"><label>Customer<select id="report-customer-filter"><option value="">${globalScope ? "All Customers" : "All Assigned Customers"}</option>${customerOptions.map((customer) => `<option value="${escapeHtml(customer._id)}" ${customer._id === customerFilter ? "selected" : ""}>${escapeHtml(customer.name)}</option>`).join("")}</select></label></div><div class="report-stats"><span><strong>${data.customers}</strong> customers</span><span><strong>${data.quotations}</strong> quotations</span><span><strong>${data.orders}</strong> orders</span><span><strong>${data.leads}</strong> leads</span></div><div class="report-grid">${reportCards.map(([slug, title, copy, icon]) => { const href = `/reports/${slug}${query}`; return `<article class="report-card" tabindex="0" data-report-href="${escapeHtml(href)}"><a class="report-card-main" href="${href}" data-route="${href}"><span><i data-lucide="${icon}"></i></span><div><h3>${title}</h3><p>${copy}</p></div></a><a class="icon-button report-card-action" href="${href}" data-route="${href}" aria-label="Open ${escapeHtml(title)} report" title="Open report"><i data-lucide="arrow-up-right"></i></a></article>`; }).join("")}</div>${noReportData ? '<div class="notice compact"><i data-lucide="info"></i><div><strong>No report data available</strong><p>Authorized customers are available, but there are no matching business records yet.</p></div></div>' : '<div class="notice compact"><i data-lucide="info"></i><div><strong>Reports reflect live business records</strong><p>No illustrative revenue or conversion values are injected into production reporting.</p></div></div>'}`;
    body.querySelector<HTMLSelectElement>("#report-customer-filter")?.addEventListener("change", (event) => {
      customerFilter = (event.target as HTMLSelectElement).value;
      history.replaceState({}, "", `/reports${queryFor(customerFilter)}`);
      void load().catch(renderError);
    });
    bindCardNavigation();
    refreshIcons(page);
  };
  try { await load(); } catch (error) { renderError(error); }
  refreshIcons(page);
  return page;
}

export async function reportDetailPage(slug: string): Promise<HTMLElement> {
  const card = reportBySlug.get(slug);
  if (!card) return pageScaffold("Intelligence", "Report not found", "The requested report does not exist.");
  const [, fallbackTitle, fallbackCopy] = card;
  const customerFilter = new URLSearchParams(location.search).get("customer_id") || "";
  const backHref = `/reports${queryFor(customerFilter)}`;
  const page = pageScaffold("Reports", fallbackTitle, fallbackCopy, `<a class="button button-secondary" href="${backHref}" data-route="${backHref}"><i data-lucide="arrow-left"></i>Back to reports</a>`);
  page.classList.add("report-detail-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  try {
    const data = await api<ReportDetail>(`/reports/detail/${encodeURIComponent(slug)}${queryFor(customerFilter)}`);
    const columns = detailColumns(slug);
    const rows = data.rows || [];
    const rateMarkup = slug === "currency-exposure" && data.rates?.rates ? `<div class="panel report-rates"><strong>Configured EUR reference rates</strong><span>${Object.entries(data.rates.rates).map(([currency, rate]) => `1 EUR = ${escapeHtml(Number(rate).toFixed(4))} ${escapeHtml(currency)}`).join(" · ")}</span><small>${data.rates.stale ? "Stored fallback rate" : "Latest configured rate"}${data.rates.source ? ` · ${escapeHtml(data.rates.source)}` : ""}</small></div>` : slug === "currency-exposure" ? '<div class="notice compact"><i data-lucide="circle-alert"></i><div><strong>Exchange rates unavailable</strong><p>No stored/configured EUR reference rates were available; amounts above are shown without conversion.</p></div></div>' : "";
    body.innerHTML = `<div class="report-filters panel"><label>Customer<select id="report-detail-customer-filter"><option value="">All customers in scope</option>${(data.customer_options || []).map((customer) => `<option value="${escapeHtml(customer._id)}" ${customer._id === customerFilter ? "selected" : ""}>${escapeHtml(customer.name)}</option>`).join("")}</select></label><span class="report-scope-note">${data.scope.customer_filter ? "Filtered customer" : `${data.scope.customer_count} customers in scope`}</span></div><div class="report-detail-metrics">${data.metrics.map((metric) => `<div class="panel"><span>${escapeHtml(metric.label)}</span><strong>${metricValue(metric)}</strong></div>`).join("")}</div>${rateMarkup}${rows.length ? `<div class="panel report-table-wrap"><table class="report-data-table"><thead><tr>${columns.map(([, label]) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map(([key, , render]) => `<td>${escapeHtml(render(row[key], row))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : emptyState("chart-no-axes-column", "No report data", data.empty_message || "No matching records are available for this report.")}`;
    body.querySelector<HTMLSelectElement>("#report-detail-customer-filter")?.addEventListener("change", (event) => {
      const next = (event.target as HTMLSelectElement).value;
      history.replaceState({}, "", `/reports/${slug}${queryFor(next)}`);
      window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/reports/${slug}${queryFor(next)}` }));
    });
  } catch (error) {
    body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Report unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
  }
  refreshIcons(page);
  return page;
}
