import { orderApi } from "../api";
import { financeApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { customerTypeLabel } from "../config/businessConfig";
import { emptyState, escapeHtml, formatDate, formatDateInput, formatMoney, skeleton } from "../utils/dom";

function snapshotValue(value: unknown, key: string): string {
  return escapeHtml(String((value as Record<string, unknown> | undefined)?.[key] ?? "—"));
}

function paymentStatus(order: Record<string, unknown>): string {
  return String(order.payment_status ?? "PENDING PAYMENT");
}

function readPaymentProof(file: File): Promise<Record<string, unknown> | undefined> {
  if (!file || !file.size) return Promise.resolve(undefined);
  if (file.size > 5 * 1024 * 1024) return Promise.reject(new Error("Payment proof must be 5 MB or smaller"));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Payment proof could not be read"));
    reader.onload = () => resolve({ name: file.name.slice(0, 160), type: file.type.slice(0, 120), size: file.size, data: String(reader.result ?? "") });
    reader.readAsDataURL(file);
  });
}

function paymentFormContent(order: Record<string, unknown>, existing?: Record<string, unknown>): HTMLElement {
  const content = document.createElement("div");
  const snapshot = existing ?? {};
  const customer = order.customer_snapshot as Record<string, unknown> | undefined;
  content.innerHTML = `<form class="stack-form" data-payment-form>
    <p class="form-hint">Record or update the customer payment for this Order Confirmation. It will remain awaiting Superadmin confirmation until submitted.</p>
    <section class="panel"><strong>${snapshotValue(order, "order_number")}</strong><p>${escapeHtml(String(customer?.company_name ?? customer?.name ?? order.customer_id ?? "Customer"))} · Expected ${formatMoney(Number(order.order_amount ?? (order.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</p></section>
    <div class="form-grid"><label>Payment amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required value="${Number(snapshot.amount ?? order.order_amount ?? 0).toFixed(2)}"></label><label>Payment date<input name="payment_date" type="date" required value="${escapeHtml(formatDateInput(snapshot.payment_date || new Date()))}"></label><label>Bank name<input name="bank_name" value="${snapshotValue(snapshot, "bank_name") === "—" ? "" : snapshotValue(snapshot, "bank_name")}"></label><label>Bank account<input name="bank_account" value="${snapshotValue(snapshot, "bank_account") === "—" ? "" : snapshotValue(snapshot, "bank_account")}"></label><label>UTR / transaction reference<input name="utr" value="${snapshotValue(snapshot, "utr") === "—" ? "" : snapshotValue(snapshot, "utr")}"></label><label>Payment mode<input name="payment_mode" value="${snapshotValue(snapshot, "payment_mode") === "—" ? "" : snapshotValue(snapshot, "payment_mode")}"></label><label>Payment reference<input name="reference_number" value="${snapshotValue(snapshot, "reference_number") === "—" ? "" : snapshotValue(snapshot, "reference_number")}"></label><label class="span-2">Payment proof<input name="attachment" type="file" accept="image/*,.pdf"></label><label class="span-2">Notes<textarea name="notes" rows="2">${snapshotValue(snapshot, "notes") === "—" ? "" : snapshotValue(snapshot, "notes")}</textarea></label></div>
    <small class="field-error" data-payment-error></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">${existing ? "Update & submit" : "Record & submit"}</button></div>
  </form>`;
  const form = content.querySelector<HTMLFormElement>("[data-payment-form]");
  if (form) {
    form.classList.add("payment-workflow-modal");
    const summary = document.createElement("section");
    summary.className = "panel payment-summary-card";
    summary.innerHTML = `<span class="eyebrow">Banking &amp; Payment</span><div class="detail-grid"><div><span>Order Confirmation</span><strong>${snapshotValue(order, "order_number")}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer?.company_name ?? customer?.name ?? order.customer_id ?? "Customer"))}</strong></div><div><span>Expected amount</span><strong>${formatMoney(Number(order.order_amount ?? (order.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</strong></div><div><span>Current status</span><strong>${statusBadge(String(snapshot.status ?? "PENDING PAYMENT"))}</strong></div></div>`;
    form.prepend(summary);
    const modeInput = form.querySelector<HTMLInputElement>('input[name="payment_mode"]');
    if (modeInput) {
      const select = document.createElement("select");
      select.name = "payment_mode";
      select.innerHTML = `<option value="">Select payment mode</option>${["Bank Transfer", "NEFT", "RTGS", "IMPS", "SWIFT", "Cheque", "Other"].map((mode) => `<option value="${mode}">${mode}</option>`).join("")}`;
      select.value = modeInput.value;
      modeInput.replaceWith(select);
    }
  }
  return content;
}

async function openPaymentForm(order: Record<string, unknown>, existing?: Record<string, unknown>): Promise<void> {
  // New and editable payments use the single Banking workflow. It starts with
  // the server-authorized customer and eligible OC selectors instead of a
  // second order-only form with different validation/state behavior.
  void paymentFormContent;
  void readPaymentProof;
  const params = new URLSearchParams({ order_id: String(order._id || "") });
  if (!existing) params.set("new", "1");
  window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/payments?${params.toString()}` }));
}

async function confirmPayment(order: Record<string, unknown>, paymentId: string, payment?: Record<string, unknown>): Promise<void> {
  const paymentRecord = await financeApi.payments(`order_id=${encodeURIComponent(String(order._id))}`)
    .then((result) => result.items.find((item) => String(item._id) === paymentId) ?? payment)
    .catch(() => payment);
  const content = document.createElement("div");
  const customer = order.customer_snapshot as Record<string, unknown> | undefined;
  const proofName = String(paymentRecord?.attachment && typeof paymentRecord.attachment === "object" ? (paymentRecord.attachment as Record<string, unknown>).name ?? "Payment proof attached" : "No payment proof attached");
  content.innerHTML = `<div class="stack-form"><p>Confirm that the payment has been received in the bank. This activates the existing incentive and locks the financial records.</p><section class="panel"><div class="detail-grid"><div><span>Customer</span><strong>${escapeHtml(String(customer?.company_name ?? customer?.name ?? order.customer_id ?? "Customer"))}</strong></div><div><span>Order Confirmation</span><strong>${snapshotValue(order, "order_number")}</strong></div><div><span>Payment amount</span><strong>${formatMoney(Number(paymentRecord?.amount ?? 0), "EUR")}</strong></div><div><span>Payment date</span><strong>${formatDate(String(paymentRecord?.payment_date ?? ""))}</strong></div><div><span>Bank</span><strong>${snapshotValue(paymentRecord, "bank_name")}</strong></div><div><span>UTR / reference</span><strong>${snapshotValue(paymentRecord, "utr")}</strong></div><div><span>Payment proof</span><strong>${escapeHtml(proofName)}</strong></div></div></section><small class="field-error" data-confirm-error></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="button" data-confirm>Confirm Payment Received</button></div></div>`;
  const dialog = openModal("Confirm Payment Received", content, "wide");
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  content.querySelector<HTMLButtonElement>("[data-confirm]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    const error = content.querySelector<HTMLElement>("[data-confirm-error]");
    button.disabled = true;
    try { await financeApi.confirmPayment(paymentId); dialog.close(); toast("Payment confirmed and incentive activated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/orders" })); }
    catch (err) { if (error) error.textContent = err instanceof Error ? err.message : "Payment could not be confirmed"; button.disabled = false; }
  });
  refreshIcons(content);
}

function showIncentiveDetails(incentive: Record<string, unknown>): void {
  const internalLines = Array.isArray(incentive.incentive_lines) ? incentive.incentive_lines as Record<string, unknown>[] : [];
  const customerLines = Array.isArray(incentive.customer_incentive_lines) ? incentive.customer_incentive_lines as Record<string, unknown>[] : [];
  const allLines = [...internalLines, ...customerLines];
  const customer = (incentive.customer_snapshot as Record<string, unknown> | undefined) ?? {};
  const creator = (incentive.creator_snapshot as Record<string, unknown> | undefined) ?? {};
  const manager = (incentive.manager_snapshot as Record<string, unknown> | undefined) ?? {};
  const displayName = (source: Record<string, unknown>): string => {
    const snapshot = (source.recipient_snapshot as Record<string, unknown> | undefined) ?? {};
    return String(source.recipient_name ?? snapshot.name ?? snapshot.email ?? source.customer_name_snapshot ?? source.bearer_name_snapshot ?? source.recipient_user_id ?? source.customer_id ?? "—");
  };
  const isCustomer = (line: Record<string, unknown>): boolean => String(line.recipient_type ?? "").toUpperCase() === "CUSTOMER" || String(line.category_id ?? "") === "customer_incentive";
  const isManager = (line: Record<string, unknown>): boolean => {
    const role = String(line.recipient_role ?? line.recipient_type ?? "").toLowerCase();
    return role.includes("manager") || String(line.allocation_type ?? "").toLowerCase().includes("manager");
  };
  const roleLabel = (line: Record<string, unknown>): string => {
    if (isCustomer(line)) return "Customer";
    if (isManager(line)) return "Manager";
    const role = String(line.recipient_role ?? line.recipient_type ?? line.allocation_type ?? "").toLowerCase();
    if (role.includes("user") || role.includes("creator") || role.includes("sales")) return "Sales Person";
    return String(line.recipient_role ?? line.recipient_type ?? "Recipient");
  };
  const lineAmount = (line: Record<string, unknown>): number => Number.isFinite(Number(line.incentive_amount ?? line.amount)) ? Number(line.incentive_amount ?? line.amount ?? 0) : 0;
  const lineRate = (line: Record<string, unknown>): string => {
    const rate = Number(line.incentive_rate_snapshot ?? line.rate);
    return Number.isFinite(rate) ? `${rate.toFixed(1).replace(/\.0$/, "")}%` : "—";
  };
  type AllocationGroup = { key: string; label: string; lines: Record<string, unknown>[] };
  const groupsFromSnapshot: AllocationGroup[] = (Array.isArray(incentive.allocations) ? incentive.allocations : [])
    .map((allocation, index): AllocationGroup | null => {
      if (!allocation || typeof allocation !== "object") return null;
      const source = allocation as Record<string, unknown>;
      const lines = Array.isArray(source.lines) ? source.lines.filter((line): line is Record<string, unknown> => Boolean(line && typeof line === "object")) : [];
      if (!lines.length) return null;
      const merged = { ...lines[0], ...source };
      return { key: `recipient-${index}`, label: `${roleLabel(merged)} — ${displayName(merged)}`, lines };
    })
    .filter((group): group is AllocationGroup => Boolean(group));
  const fallbackGroups = new Map<string, AllocationGroup>();
  if (!groupsFromSnapshot.length) {
    allLines.forEach((line) => {
      const key = `${String(line.recipient_user_id ?? line.customer_id ?? displayName(line))}:${String(line.allocation_type ?? (isCustomer(line) ? "customer" : "creator"))}`;
      const existing = fallbackGroups.get(key);
      if (existing) existing.lines.push(line);
      else fallbackGroups.set(key, { key: `recipient-${fallbackGroups.size}`, label: `${roleLabel(line)} — ${displayName(line)}`, lines: [line] });
    });
  }
  const groups = groupsFromSnapshot.length ? groupsFromSnapshot : [...fallbackGroups.values()];
  const tabs = [{ key: "total", label: "Total", lines: allLines }, ...groups];
  const totalAmount = allLines.reduce((sum, line) => sum + lineAmount(line), 0);
  const orderNumber = String(incentive.oc_number ?? incentive.order_number ?? incentive.order_id ?? "—");
  const orderDate = formatDate(String(incentive.order_date ?? incentive.oc_date ?? incentive.created_at ?? ""));
  const paymentStatus = String(incentive.payment_status ?? "Pending Payment");
  const renderLines = (lines: Record<string, unknown>[], total = false): string => {
    if (!lines.length) return `<div class="empty-state compact"><strong>No allocation</strong><span>No incentive allocation was captured for this Order Confirmation.</span></div>`;
    if (total) return `<div class="incentive-total-breakdown"><div class="incentive-total-breakdown-title">Product Breakdown</div><div class="incentive-total-breakdown-table" role="table" aria-label="Incentive allocation by product"><div class="incentive-total-breakdown-row is-head" role="row"><span>Product</span><span>Category</span><span>Amount</span><span>Rate</span><span>Incentive</span></div>${lines.map((line) => `<div class="incentive-total-breakdown-row" role="row"><strong>${snapshotValue(line, "product_name")}</strong><span>${snapshotValue(line, "category_name")}</span><span>${formatMoney(Number(line.product_amount ?? line.incentive_base_amount_eur ?? line.amount ?? 0), "EUR")}</span><span>${escapeHtml(lineRate(line))}</span><span>${formatMoney(lineAmount(line), "EUR")}</span></div>`).join("")}</div></div>`;
    return `<div class="incentive-detail-lines">${lines.map((line) => `<div class="incentive-detail-line"><div><strong>${snapshotValue(line, "product_name")}</strong><small>${snapshotValue(line, "category_name")}</small></div><div class="incentive-detail-line-values"><span>Rate ${escapeHtml(lineRate(line))}</span><strong>${formatMoney(lineAmount(line), "EUR")}</strong></div></div>`).join("")}</div>`;
  };
  const renderPanel = (tab: { key: string; label: string; lines: Record<string, unknown>[] }): string => {
    const lines = tab.lines;
    const panelTotal = lines.reduce((sum, line) => sum + lineAmount(line), 0);
    const rates = [...new Set(lines.map(lineRate))];
    if (tab.key === "total") return `<section class="incentive-detail-panel" data-detail-panel="total"><section class="incentive-order-information"><div class="incentive-detail-panel-head"><div><span class="eyebrow">Order Information</span><h3>${escapeHtml(orderNumber)}</h3></div><strong class="incentive-detail-panel-total">${formatMoney(totalAmount, "EUR")}</strong></div><div class="detail-grid"><div><span>OC Number</span><strong>${escapeHtml(orderNumber)}</strong></div><div><span>Sales Person</span><strong>${escapeHtml(String((incentive.salesperson_snapshot as Record<string, unknown> | undefined)?.name ?? creator.name ?? incentive.salesperson_id ?? "—"))}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? incentive.customer_id ?? "—"))}</strong></div><div><span>Manager</span><strong>${escapeHtml(String(manager.name ?? incentive.manager_user_id ?? "—"))}</strong></div><div><span>Customer Type</span><strong>${escapeHtml(customerTypeLabel(String(incentive.client_type_snapshot ?? incentive.customer_type_snapshot ?? customer.client_type ?? "")))}</strong></div><div><span>Gross Incentive</span><strong>${formatMoney(Number(incentive.gross_incentive_amount ?? totalAmount), "EUR")}</strong></div><div><span>Status</span><strong>${statusBadge(paymentStatus)}</strong></div><div><span>Already Paid</span><strong>${formatMoney(Number(incentive.paid_amount ?? 0), "EUR")}</strong></div><div><span>Order Date</span><strong>${escapeHtml(orderDate)}</strong></div><div><span>Quote Number</span><strong>${escapeHtml(String(incentive.quotation_number ?? incentive.quote_number ?? "—"))}</strong></div><div><span>Remaining Payable</span><strong>${formatMoney(Number(incentive.remaining_amount ?? incentive.net_payable_incentive ?? totalAmount), "EUR")}</strong></div></div></section><section class="incentive-allocation-summary"><div class="incentive-detail-panel-head"><div><span class="eyebrow">Allocation Summary</span><p>Incentive allocations for this Order Confirmation.</p></div><strong class="incentive-detail-panel-total">${formatMoney(totalAmount, "EUR")}</strong></div>${groups.map((group) => `<div class="incentive-allocation-summary-row"><div><strong>${escapeHtml(group.label)}</strong><small>${escapeHtml([...new Set(group.lines.map(lineRate))].join(" · ") || "Rate unavailable")}</small></div><strong>${formatMoney(group.lines.reduce((sum, line) => sum + lineAmount(line), 0), "EUR")}</strong></div>`).join("") || `<p class="muted">No visible recipient allocations.</p>`}</section>${renderLines(lines, true)}</section>`;
    return `<section class="incentive-detail-panel" data-detail-panel="${escapeHtml(tab.key)}" hidden><div class="incentive-detail-panel-head"><div><span class="eyebrow">${escapeHtml(tab.label)}</span><h3>${escapeHtml(displayName(lines[0] ?? {}))}</h3></div><strong class="incentive-detail-panel-total">${formatMoney(panelTotal, "EUR")}</strong></div><div class="incentive-detail-summary"><div><span>Recipient</span><strong>${escapeHtml(displayName(lines[0] ?? {}))}</strong></div><div><span>Role</span><strong>${escapeHtml(roleLabel(lines[0] ?? {}))}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? incentive.customer_id ?? "—"))}</strong></div><div><span>Order Confirmation</span><strong>${escapeHtml(orderNumber)}</strong></div><div><span>Order amount</span><strong>${formatMoney(Number(incentive.order_amount ?? 0), "EUR")}</strong></div><div><span>Incentive rate</span><strong>${escapeHtml(rates.join(" · ") || "—")}</strong></div><div><span>Payment status</span><strong>${escapeHtml(paymentStatus)}</strong></div><div><span>Incentive status</span><strong>${escapeHtml(String(incentive.status ?? "PENDING PAYMENT"))}</strong></div></div>${renderLines(lines)}</section>`;
  };
  const content = document.createElement("div");
  content.innerHTML = `<div class="notice compact"><i data-lucide="lock-keyhole"></i><div><strong>Historical incentive snapshot</strong><p>Recipients, percentages and amounts are read-only values captured when this Order Confirmation was created.</p></div></div><div class="incentive-detail-tabs" role="tablist" aria-label="Incentive recipients">${tabs.map((tab, index) => `<button class="incentive-detail-tab${index === 0 ? " is-active" : ""}" type="button" role="tab" aria-selected="${index === 0 ? "true" : "false"}" data-detail-tab="${escapeHtml(tab.key)}">${escapeHtml(tab.label)}</button>`).join("")}</div><div class="incentive-detail-panels">${tabs.map(renderPanel).join("")}</div>`;
  content.insertAdjacentHTML("afterbegin", `<p class="modal-subtitle">View incentive allocations and payment information for this Order Confirmation.</p>`);
  const modalSubtitle = content.querySelector<HTMLElement>(".modal-subtitle");
  const tabBar = content.querySelector<HTMLElement>(".incentive-detail-tabs");
  if (modalSubtitle && tabBar) modalSubtitle.after(tabBar);
  const dialog = openModal("Incentive Details", content, "wide");
  dialog.classList.add("incentive-details-modal");
  content.querySelectorAll<HTMLButtonElement>("[data-detail-tab]").forEach((button) => button.addEventListener("click", () => {
    const key = button.dataset.detailTab ?? "total";
    content.querySelectorAll<HTMLButtonElement>("[data-detail-tab]").forEach((tab) => { const active = tab.dataset.detailTab === key; tab.classList.toggle("is-active", active); tab.setAttribute("aria-selected", String(active)); });
    content.querySelectorAll<HTMLElement>("[data-detail-panel]").forEach((panel) => { panel.hidden = panel.dataset.detailPanel !== key; });
  }));
  refreshIcons(content);
}

async function showOrderDetails(order: Record<string, unknown>): Promise<void> {
  const [latestPayment, incentive] = await Promise.all([financeApi.payments(`order_id=${encodeURIComponent(String(order._id))}`).then((result) => result.items.at(-1)).catch(() => undefined), order.incentive_id ? financeApi.incentive(String(order.incentive_id)).catch(() => undefined) : Promise.resolve(undefined)]);
  const customer = order.customer_snapshot as Record<string, unknown> | undefined;
  const content = document.createElement("div");
  content.innerHTML = `<div class="stack-form"><section class="panel"><span class="eyebrow">Overview</span><div class="detail-grid"><div><span>OC Number</span><strong>${snapshotValue(order, "order_number")}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer?.company_name ?? customer?.name ?? order.customer_id ?? "—"))}</strong></div><div><span>Quote</span><strong>${snapshotValue(order, "quotation_number")}</strong></div><div><span>OC Date</span><strong>${formatDate(String(order.oc_date ?? order.created_at ?? ""))}</strong></div><div><span>Order amount</span><strong>${formatMoney(Number(order.order_amount ?? (order.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</strong></div><div><span>Payment terms</span><strong>${snapshotValue(order, "payment_terms")}</strong></div><div><span>Sales Person / Manager</span><strong>${snapshotValue(order.salesperson_snapshot, "name")}</strong></div><div><span>OC status</span><strong>${statusBadge(String(order.status ?? "Pending"))}</strong></div><div><span>Created by</span><strong>${snapshotValue(order, "created_by_user_id")}</strong></div><div><span>Created at</span><strong>${formatDate(String(order.created_at ?? ""))}</strong></div></div></section><section class="panel"><span class="eyebrow">Payment</span><div class="detail-grid"><div><span>Status</span><strong>${statusBadge(String(latestPayment?.status ?? order.payment_status ?? "PENDING PAYMENT"))}</strong></div><div><span>Amount</span><strong>${formatMoney(Number(latestPayment?.amount ?? 0), "EUR")}</strong></div><div><span>Payment date</span><strong>${formatDate(String(latestPayment?.payment_date ?? ""))}</strong></div><div><span>Bank</span><strong>${snapshotValue(latestPayment, "bank_name")}</strong></div><div><span>UTR / reference</span><strong>${snapshotValue(latestPayment, "utr")}</strong></div><div><span>Confirmed by</span><strong>${snapshotValue(latestPayment, "confirmed_by_user_id")}</strong></div></div></section><section class="panel"><span class="eyebrow">Incentive</span><p>${incentive ? `${formatMoney(Number(incentive.gross_incentive_amount ?? 0), "EUR")} · ${statusBadge(String(incentive.status ?? "PENDING PAYMENT"))}` : "No incentive snapshot"}</p></section><section class="panel"><span class="eyebrow">History</span>${Array.isArray(order.history) && order.history.length ? `<div class="timeline-list">${(order.history as Record<string, unknown>[]).map((item) => `<div class="timeline-item"><i data-lucide="circle-check"></i><div><strong>${escapeHtml(String(item.status ?? "Updated"))}</strong><small>${formatDate(String(item.at ?? ""))}</small></div></div>`).join("")}</div>` : `<p class="muted">No history recorded.</p>`}</section></div>`;
  const detailActions = document.createElement("div");
  detailActions.className = "modal-actions oc-detail-actions";
  const latestStatus = String(latestPayment?.status ?? order.payment_status ?? "PENDING PAYMENT").toUpperCase();
  const canConfirmDetails = appStore.state.user?.role_id === "superadmin";
  const paymentId = String(latestPayment?._id ?? "");
  detailActions.innerHTML = `<a class="button button-secondary" href="${orderApi.pdfUrl(String(order._id), true)}" target="_blank" rel="noopener"><i data-lucide="eye"></i>View OC PDF</a><a class="button button-secondary" href="/payments?order_id=${encodeURIComponent(String(order._id))}" data-route="/payments?order_id=${encodeURIComponent(String(order._id))}"><i data-lucide="landmark"></i>Open Banking</a>${latestStatus !== "CONFIRMED" ? `<button class="button button-secondary" type="button" data-detail-payment>${latestPayment ? "Edit Payment Details" : "Record Payment"}</button>` : ""}${canConfirmDetails && paymentId && latestStatus === "AWAITING SUPERADMIN CONFIRMATION" ? `<button class="button button-primary" type="button" data-detail-confirm>Confirm Payment Received</button>` : ""}`;
  content.append(detailActions);
  const dialog = openModal("Order Confirmation Details", content, "wide");
  detailActions.querySelector<HTMLButtonElement>("[data-detail-payment]")?.addEventListener("click", () => { dialog.close(); void openPaymentForm(order, latestPayment); });
  detailActions.querySelector<HTMLButtonElement>("[data-detail-confirm]")?.addEventListener("click", () => { dialog.close(); void confirmPayment(order, paymentId, latestPayment); });
  refreshIcons(content);
}

export async function ordersPage(): Promise<HTMLElement> {
  const page = pageScaffold("Commercial", "Order Confirmations", "Track accepted business from confirmation through completion.", '<button class="button button-secondary"><i data-lucide="download"></i>Export</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(6);
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  try {
    const data = await orderApi.list(customerCompany?._id);
    const scopeLabel = customerCompany ? "For this customer" : "Across all authorized customers";
    const canConfirm = appStore.state.user?.role_id === "superadmin";
    const canDelete = appStore.state.user?.role_id === "superadmin" || appStore.state.user?.permissions?.includes("orders.delete");
    body.innerHTML = `<div class="metric-grid compact-metrics"><article class="metric-card"><div class="metric-top"><span>All Order Confirmations</span><i data-lucide="shopping-bag"></i></div><strong>${data.total}</strong><p>${scopeLabel}</p></article><article class="metric-card"><div class="metric-top"><span>Pending payment</span><i data-lucide="clock-3"></i></div><strong>${data.items.filter((item) => !["CONFIRMED", "PAYMENT CONFIRMED"].includes(paymentStatus(item).toUpperCase())).length}</strong><p>Awaiting bank confirmation</p></article><article class="metric-card"><div class="metric-top"><span>Confirmed</span><i data-lucide="circle-check"></i></div><strong>${data.items.filter((item) => paymentStatus(item).toUpperCase() === "CONFIRMED").length}</strong><p>Payment received</p></article></div>${data.items.length ? `<div class="data-table panel"><table><thead><tr><th>OC Number</th><th>Customer</th><th>Quote</th><th>OC Date</th><th>Amount</th><th>OC Status</th><th>Payment</th><th>Incentive</th><th>Actions</th></tr></thead><tbody>${data.items.map((item) => { const payment = paymentStatus(item); const confirmed = payment.toUpperCase() === "CONFIRMED"; const paymentId = String((item.payment_snapshot as Record<string, unknown> | undefined)?._id ?? ""); const orderId = String(item._id ?? ""); const confirmDisabled = !canConfirm || confirmed || !paymentId; return `<tr><td><strong>${escapeHtml(String(item.order_number ?? item._id))}</strong></td><td>${escapeHtml(String((item.customer_snapshot as Record<string, unknown> | undefined)?.company_name ?? (item.customer_snapshot as Record<string, unknown> | undefined)?.name ?? item.customer_id ?? "—"))}</td><td>${escapeHtml(String(item.quotation_number ?? item.quotation_id ?? "—"))}</td><td>${formatDate(String(item.oc_date ?? item.created_at ?? ""))}</td><td class="money">${formatMoney(Number(item.order_amount ?? (item.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</td><td>${statusBadge(String(item.status ?? "Pending"))}</td><td><a href="/payments?order_id=${encodeURIComponent(orderId)}" data-route="/payments?order_id=${encodeURIComponent(orderId)}">${statusBadge(payment)}</a></td><td>${statusBadge(String(item.incentive_status ?? "PENDING PAYMENT"))}</td><td><div class="table-actions"><button class="icon-button" type="button" data-order-action="bank" data-id="${escapeHtml(String(item._id))}" title="Update Banking Details" aria-label="Update Banking Details"><i data-lucide="landmark"></i></button><button class="icon-button" type="button" data-order-action="confirm" data-id="${escapeHtml(String(item._id))}" data-payment-id="${escapeHtml(paymentId)}" title="${confirmDisabled ? "Payment confirmation unavailable" : "Confirm Payment Received"}" aria-label="Confirm Payment Received" ${confirmDisabled ? "disabled" : ""}><i data-lucide="circle-check"></i></button><button class="icon-button" type="button" data-order-action="incentive" data-id="${escapeHtml(String(item._id))}" title="View Incentive Details" aria-label="View Incentive Details"><i data-lucide="percent"></i></button><button class="icon-button" type="button" data-order-action="view" data-id="${escapeHtml(String(item._id))}" title="View Order Confirmation" aria-label="View Order Confirmation"><i data-lucide="eye"></i></button></div></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("shopping-bag", "No Order Confirmations yet", "Accepted quotations can be converted into Order Confirmations.")}`;
    if (canDelete) body.querySelectorAll<HTMLTableRowElement>("tbody tr").forEach((row, index) => {
      const order = data.items[index];
      const actions = row.querySelector<HTMLElement>(".table-actions");
      if (!order || !actions || actions.querySelector('[data-order-action="delete"]')) return;
      const button = document.createElement("button");
      button.className = "icon-button icon-danger";
      button.type = "button";
      button.dataset.orderAction = "delete";
      button.dataset.id = String(order._id ?? "");
      button.title = "Delete Order Confirmation";
      button.setAttribute("aria-label", "Delete Order Confirmation");
      button.innerHTML = '<i data-lucide="trash-2"></i>';
      actions.append(button);
    });
    if (!canConfirm) body.querySelectorAll<HTMLButtonElement>('[data-order-action="confirm"]').forEach((button) => button.remove());
    body.querySelectorAll<HTMLButtonElement>('[data-order-action="bank"]').forEach((button) => { button.title = "Record / View Payment Details"; button.setAttribute("aria-label", "Record / View Payment Details"); });
    body.querySelectorAll<HTMLButtonElement>('[data-order-action="incentive"]').forEach((button) => {
      const order = data.items.find((item) => String(item._id) === String(button.dataset.id));
      if (!order?.incentive_id) button.remove();
    });
    body.querySelectorAll<HTMLButtonElement>("[data-order-action]").forEach((button) => button.addEventListener("click", async () => {
      const order = data.items.find((item) => String(item._id) === String(button.dataset.id)); if (!order) return;
      const action = button.dataset.orderAction;
      try {
        if (action === "bank") await openPaymentForm(order, order.payment_snapshot as Record<string, unknown> | undefined);
        else if (action === "incentive") { if (!order.incentive_id) { toast("No incentive snapshot is linked to this Order Confirmation", "info"); return; } const incentive = await financeApi.incentive(String(order.incentive_id)); showIncentiveDetails(incentive); }
        else if (action === "view") await showOrderDetails(order);
        else if (action === "confirm") { const paymentId = button.dataset.paymentId; if (!paymentId) return; await confirmPayment(order, paymentId, order.payment_snapshot as Record<string, unknown> | undefined); }
        else if (action === "delete") { if (!window.confirm("Delete this Order Confirmation? Its payment and incentive history will be retained for audit.")) return; const reason = window.prompt("Deletion reason (optional):", "Deleted from Order Confirmations")?.trim() || "Deleted from Order Confirmations"; button.disabled = true; await orderApi.remove(String(order._id), reason); toast("Order Confirmation deleted"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/orders" })); }
      } catch (error) { toast(error instanceof Error ? error.message : "Order action failed", "error"); }
    }));
  }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Orders unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function orderDetailPage(orderId: string): Promise<HTMLElement> {
  const canUpdate = appStore.state.user?.permissions?.includes("orders.update");
  const emailActions = `<a class="button button-secondary" href="${orderApi.pdfUrl(orderId, true)}" target="_blank" rel="noopener"><i data-lucide="eye"></i>View OC PDF</a><a class="button button-secondary" href="${orderApi.pdfUrl(orderId)}"><i data-lucide="download"></i>Download OC PDF</a>${canUpdate ? '<button class="button button-secondary" type="button" data-order-resend><i data-lucide="mail-check"></i>Resend OC</button><button class="button button-quiet" type="button" data-order-status><i data-lucide="send"></i>Send status email</button>' : ""}`;
  const page = pageScaffold("Commercial", "Order Confirmation detail", "Snapshot-backed Order Confirmation with fulfilment status and timeline.", `${emailActions}<a class="button button-secondary" href="/orders" data-route="/orders"><i data-lucide="arrow-left"></i>Back to Order Confirmations</a>`);
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  try {
    const order = await orderApi.get(orderId);
    const totals = (order.totals ?? {}) as Record<string, unknown>;
    const history = Array.isArray(order.history) ? order.history as Record<string, unknown>[] : [];
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar"><i data-lucide="shopping-bag"></i></div><div><span class="eyebrow">${escapeHtml(String(order.order_number ?? order._id))}</span><h2>${escapeHtml(String((order.customer_snapshot as Record<string, unknown> | undefined)?.name ?? order.customer_id ?? "Customer"))}</h2><p>${statusBadge(String(order.status ?? "Pending"))} · ${escapeHtml(String(order.currency ?? "EUR"))}</p><p class="form-hint">Document: ${escapeHtml(String(order.oc_pdf_status ?? "Pending"))} · Email: ${escapeHtml(String(order.email_status ?? "Pending"))}</p></div><div class="detail-hero-total"><span>Order total</span><strong>${formatMoney(Number(totals.grand_total ?? 0), String(order.currency ?? "EUR"))}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Products snapshot</span><h2>${Array.isArray(order.products_snapshot) ? order.products_snapshot.length : 0} line items</h2></div></div><div class="detail-lines">${(Array.isArray(order.products_snapshot) ? order.products_snapshot as Record<string, unknown>[] : []).map((line) => `<div class="detail-line"><div><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))}</strong><small>Qty ${escapeHtml(String(line.quantity ?? 0))}</small></div><strong>${formatMoney(Number(line.line_total ?? 0), String(order.currency ?? "EUR"))}</strong></div>`).join("")}</div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Order timeline</span><h2>Status history</h2></div></div>${history.length ? `<div class="timeline-list">${history.map((item) => `<div class="timeline-item"><i data-lucide="circle-check"></i><div><strong>${escapeHtml(String(item.status ?? "Updated"))}</strong><small>${escapeHtml(String(item.at ?? ""))}</small></div></div>`).join("")}</div>` : '<p class="muted">No status events recorded.</p>'}</section>${order.incentive_id ? `<section class="panel"><div class="section-title"><div><span class="eyebrow">Incentive</span><h2><a href="/incentives?order_id=${encodeURIComponent(String(order._id))}" data-route="/incentives?order_id=${encodeURIComponent(String(order._id))}">View incentive</a></h2></div></div><p class="form-hint">${formatMoney(Number(order.incentive_amount ?? 0), "EUR")} · ${escapeHtml(String(order.incentive_status ?? "PENDING PAYMENT"))}</p></section>` : ""}</div>`;
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Order unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector<HTMLButtonElement>("[data-order-resend]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement; button.disabled = true;
    try { const result = await orderApi.resendConfirmation(orderId); toast(`Order confirmation resent${result.diagnostic_id ? ` (${result.diagnostic_id})` : ""}`); }
    catch (error) { toast(error instanceof Error ? error.message : "Order confirmation could not be sent", "error"); }
    finally { button.disabled = false; }
  });
  page.querySelector<HTMLButtonElement>("[data-order-status]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement; button.disabled = true;
    try { const result = await orderApi.sendStatus(orderId); toast(`Order status email sent${result.diagnostic_id ? ` (${result.diagnostic_id})` : ""}`); }
    catch (error) { toast(error instanceof Error ? error.message : "Order status email could not be sent", "error"); }
    finally { button.disabled = false; }
  });
  refreshIcons(page); return page;
}
