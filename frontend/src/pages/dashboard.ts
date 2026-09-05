import Chart from "chart.js/auto";
import { dashboardApi, rateApi } from "../api";
import { refreshIcons } from "../components/icons";
import { pageScaffold, statusBadge } from "../components/page";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";

export async function dashboardPage(): Promise<HTMLElement> {
  const page = pageScaffold("Workspace", "Good morning", "A clear view of sales activity and the work that needs attention.", '<a class="button button-primary" href="/calculator" data-route="/calculator"><i data-lucide="plus"></i>New quotation</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "No customer selected", "Select a customer before viewing its workspace."); return page; }
  try {
    const [data, rates] = await Promise.all([dashboardApi.get(customerCompany._id), rateApi.get()]);
    const metrics = [
      ["Revenue", formatMoney(data.metrics.revenue, appStore.state.currency), "trending-up", "Across active orders"],
      ["Open quotations", String(data.metrics.open_quotations), "file-clock", `${data.metrics.quotations} total quotations`],
      ["Conversion", `${data.metrics.conversion_rate}%`, "gauge", `${data.metrics.accepted_quotations} accepted`],
      ["Follow-ups", String(data.metrics.follow_ups_due), "bell-ring", "Open reminders"],
    ];
    body.innerHTML = `
      ${rates.stale ? `<div class="notice warning"><i data-lucide="triangle-alert"></i><div><strong>Stored exchange rate in use</strong><p>${escapeHtml(rates.warning ?? "Live rate unavailable")}. Last updated ${formatDate(rates.fetched_at)}.</p></div></div>` : ""}
      <div class="metric-grid">${metrics.map(([label, value, icon, copy], index) => `<article class="metric-card"><div class="metric-top"><span>${label}</span><i data-lucide="${icon}"></i></div><strong>${value}</strong><p>${copy}</p><div class="metric-accent accent-${index}"></div></article>`).join("")}</div>
      <div class="dashboard-grid">
        <article class="panel chart-panel"><div class="panel-head"><div><span class="eyebrow">Pipeline signal</span><h2>Quotation status</h2></div><select aria-label="Chart period"><option>All time</option></select></div>${Object.keys(data.quotation_status).length ? '<div class="chart-wrap"><canvas id="quotation-chart"></canvas></div>' : emptyState("chart-no-axes-column-increasing", "No quotation activity yet", "Create the first quotation to begin measuring your pipeline.")}</article>
        <article class="panel rate-panel"><div class="panel-head"><div><span class="eyebrow">Currency engine</span><h2>EUR master rates</h2></div><span class="live-state ${rates.stale ? "stale" : ""}"><span></span>${rates.stale ? "Stored" : "Live"}</span></div>
          <div class="rate-row"><div><span class="currency-flag flag-eu">€</span><p><strong>EUR</strong><small>Master catalogue</small></p></div><strong>1.0000</strong></div>
          <div class="rate-row"><div><span class="currency-flag flag-us">$</span><p><strong>USD</strong><small>US dollar</small></p></div><strong>${rates.rates.USD?.toFixed(4) ?? "—"}</strong></div>
          <div class="rate-row"><div><span class="currency-flag flag-in">₹</span><p><strong>INR</strong><small>Indian rupee</small></p></div><strong>${rates.rates.INR?.toFixed(4) ?? "—"}</strong></div>
          <p class="rate-foot">${escapeHtml(rates.provider)} · ${formatDate(rates.fetched_at)}</p>
        </article>
      </div>
      <div class="dashboard-grid lower-grid">
        <article class="panel"><div class="panel-head"><div><span class="eyebrow">Latest work</span><h2>Recent quotations</h2></div><a href="/quotations" data-route="/quotations">View all <i data-lucide="arrow-right"></i></a></div>
          ${data.recent_quotations.length ? `<div class="table-scroll"><table><thead><tr><th>Quotation</th><th>Customer</th><th>Status</th><th>Total</th></tr></thead><tbody>${data.recent_quotations.map((quote) => `<tr><td><strong>${escapeHtml(quote.quotation_number)}</strong><small>${formatDate(quote.created_at)}</small></td><td>${escapeHtml(quote.customer_snapshot.company_name ?? quote.customer_snapshot.name)}</td><td>${statusBadge(quote.status)}</td><td class="money">${formatMoney(quote.totals.grand_total, quote.currency)}</td></tr>`).join("")}</tbody></table></div>` : emptyState("file-plus-2", "No quotations yet", "Your latest quotations will appear here.")}
        </article>
        <article class="panel activity-panel"><div class="panel-head"><div><span class="eyebrow">Relationships</span><h2>Recent customers</h2></div><a href="/customers" data-route="/customers">Manage</a></div>
          ${data.recent_customers.length ? data.recent_customers.map((customer) => `<div class="customer-row"><span class="customer-mark">${escapeHtml(customer.name.slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(customer.name)}</strong><small>${escapeHtml(customer.contact_name ?? customer.email ?? "Customer")}</small></p><i data-lucide="chevron-right"></i></div>`).join("") : emptyState("users", "No customers yet", "Add customers before preparing quotations.")}
        </article>
      </div>`;
    const chartElement = body.querySelector<HTMLCanvasElement>("#quotation-chart");
    if (chartElement) {
      new Chart(chartElement, { type: "doughnut", data: { labels: Object.keys(data.quotation_status), datasets: [{ data: Object.values(data.quotation_status), backgroundColor: ["#171717", "#e3342f", "#f4c400", "#8a8a8a"], borderWidth: 0, hoverOffset: 4 }] }, options: { responsive: true, maintainAspectRatio: false, cutout: "72%", plugins: { legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8, padding: 20 } } } } });
    }
  } catch (error) {
    body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Dashboard unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
  }
  refreshIcons(page);
  return page;
}
