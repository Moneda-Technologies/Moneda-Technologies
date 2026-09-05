import { orderApi } from "../api";
import { refreshIcons } from "../components/icons";
import { pageScaffold, statusBadge } from "../components/page";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatMoney, skeleton } from "../utils/dom";

export async function ordersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Commercial", "Orders", "Track accepted business from confirmation through completion.", '<button class="button button-secondary"><i data-lucide="download"></i>Export</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(6);
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "No customer selected", "Select a customer to view orders."); return page; }
  try { const data = await orderApi.list(customerCompany._id); body.innerHTML = `<div class="metric-grid compact-metrics"><article class="metric-card"><div class="metric-top"><span>All orders</span><i data-lucide="shopping-bag"></i></div><strong>${data.total}</strong><p>For this customer</p></article><article class="metric-card"><div class="metric-top"><span>In progress</span><i data-lucide="loader-circle"></i></div><strong>${data.items.filter((item) => ["Confirmed", "Processing"].includes(String(item.status))).length}</strong><p>Requires fulfilment</p></article><article class="metric-card"><div class="metric-top"><span>Completed</span><i data-lucide="circle-check"></i></div><strong>${data.items.filter((item) => item.status === "Completed").length}</strong><p>Closed successfully</p></article></div>${data.items.length ? `<div class="data-table panel"><table><thead><tr><th>Order</th><th>Quotation</th><th>Status</th><th>Currency</th><th>Total</th></tr></thead><tbody>${data.items.map((item) => `<tr><td><a href="/orders/${item._id}" data-route="/orders/${item._id}"><strong>${escapeHtml(item.order_number)}</strong></a></td><td>${escapeHtml(item.quotation_id)}</td><td>${statusBadge(String(item.status))}</td><td><span class="currency-tag">${escapeHtml(item.currency)}</span></td><td class="money">${formatMoney(Number((item.totals as Record<string, number>)?.grand_total ?? 0), String(item.currency))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("shopping-bag", "No orders yet", "Accepted quotations can be converted into immutable order snapshots.")}`; }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Orders unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function orderDetailPage(orderId: string): Promise<HTMLElement> {
  const page = pageScaffold("Commercial", "Order detail", "Snapshot-backed order record with fulfilment status and timeline.", '<a class="button button-secondary" href="/orders" data-route="/orders"><i data-lucide="arrow-left"></i>Back to orders</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  try {
    const order = await orderApi.get(orderId);
    const totals = (order.totals ?? {}) as Record<string, unknown>;
    const history = Array.isArray(order.history) ? order.history as Record<string, unknown>[] : [];
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar"><i data-lucide="shopping-bag"></i></div><div><span class="eyebrow">${escapeHtml(String(order.order_number ?? order._id))}</span><h2>${escapeHtml(String((order.customer_snapshot as Record<string, unknown> | undefined)?.name ?? order.customer_id ?? "Customer"))}</h2><p>${statusBadge(String(order.status ?? "Pending"))} · ${escapeHtml(String(order.currency ?? "EUR"))}</p></div><div class="detail-hero-total"><span>Order total</span><strong>${formatMoney(Number(totals.grand_total ?? 0), String(order.currency ?? "EUR"))}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Products snapshot</span><h2>${Array.isArray(order.products_snapshot) ? order.products_snapshot.length : 0} line items</h2></div></div><div class="detail-lines">${(Array.isArray(order.products_snapshot) ? order.products_snapshot as Record<string, unknown>[] : []).map((line) => `<div class="detail-line"><div><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))}</strong><small>Qty ${escapeHtml(String(line.quantity ?? 0))}</small></div><strong>${formatMoney(Number(line.line_total ?? 0), String(order.currency ?? "EUR"))}</strong></div>`).join("")}</div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Order timeline</span><h2>Status history</h2></div></div>${history.length ? `<div class="timeline-list">${history.map((item) => `<div class="timeline-item"><i data-lucide="circle-check"></i><div><strong>${escapeHtml(String(item.status ?? "Updated"))}</strong><small>${escapeHtml(String(item.at ?? ""))}</small></div></div>`).join("")}</div>` : '<p class="muted">No status events recorded.</p>'}</section></div>`;
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Order unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
