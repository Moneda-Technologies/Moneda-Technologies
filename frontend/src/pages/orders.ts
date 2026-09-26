import { financeApi, orderApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { openPdfViewer } from "../components/pdf-viewer";
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

function orderCategoryLabel(line: Record<string, unknown>): string {
  const category = String(line.category_name ?? line.category_label ?? line.category ?? "").trim();
  if (category) return category;
  const id = String(line.category_id ?? "").trim().toLowerCase();
  const format = String((line.configuration as Record<string, unknown> | undefined)?.format_type ?? line.format_type ?? "").toLowerCase();
  if (id === "blankets") return format === "bar_format" ? "Bar Type" : format === "cut_format" ? "Cut Pcs" : "Printing Blankets";
  if (id === "mpacks") return "Underpacking";
  if (id === "chemicals") return "Chemicals & Maintenance";
  return id ? id.replaceAll("_", " ") : "Product";
}

function orderConfigurationLabel(line: Record<string, unknown>): string {
  const configuration = (line.configuration as Record<string, unknown> | undefined) ?? {};
  const values: string[] = [];
  const manufacturer = configuration.manufacturer ?? configuration.machine_manufacturer;
  const machine = configuration.machine_model ?? configuration.model ?? configuration.machine;
  if (manufacturer) values.push(`Machine: ${manufacturer}`);
  if (machine) values.push(`Model: ${machine}`);
  const length = configuration.length ?? line.length ?? line.length_mm;
  const width = configuration.width ?? line.width ?? line.width_mm;
  const unit = configuration.dimension_unit ?? line.dimension_unit ?? "mm";
  if (length && width) values.push(`Size: ${length} × ${width} ${unit}`);
  const size = configuration.size ?? configuration.machine_size ?? configuration.size_mm ?? line.size;
  if (size && !(length && width)) values.push(`Size: ${size}`);
  const thickness = configuration.thickness_mm ?? line.thickness_mm;
  const micron = configuration.thickness_micron ?? line.thickness_micron;
  if (thickness) values.push(`Thickness: ${Number(thickness).toFixed(2)} mm`);
  else if (micron) values.push(`Thickness: ${micron} micron`);
  const format = configuration.format_type ?? line.format;
  if (format) values.push(String(format).replaceAll("_", " ").replace(/\b\w/g, (value) => value.toUpperCase()));
  return values.join(" · ") || "Configuration unavailable";
}

function revisionTimestamp(version: Record<string, unknown>): string {
  const value = version.updated_at ?? version.created_at ?? version.at;
  if (!value) return "Time unavailable";
  const date = new Date(String(value));
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function confirmOrderDeletion(order: Record<string, unknown>, finalConfirmation: boolean): Promise<string | null> {
  const customer = (order.customer_snapshot as Record<string, unknown> | undefined) ?? (order.customer_company_snapshot as Record<string, unknown> | undefined) ?? {};
  const customerName = String(customer.company_name ?? customer.name ?? order.customer_id ?? "Unknown customer");
  const number = String(order.order_number ?? order._id ?? "Unknown order");
  const sourceQuotation = String(order.quotation_number ?? order.source_quotation_number ?? order.quotation_id ?? "").trim();
  const amount = Number(order.order_amount ?? (order.totals as Record<string, unknown> | undefined)?.grand_total ?? 0);
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form order-delete-confirmation"><div class="notice warning"><i data-lucide="triangle-alert"></i><div><strong>${finalConfirmation ? "Archive this Order Confirmation?" : `Delete working Order ${escapeHtml(number)}?`}</strong><p>${finalConfirmation ? "It will be removed from operational screens, while audit and financial history remain retained." : `${sourceQuotation ? `This archives the working order and returns quotation ${escapeHtml(sourceQuotation)} to its previous editable state. ` : "This archives the working order. "}A finalized OC, active payment, paid incentive, or Credit Note will block this action.`}</p></div></div><div class="detail-grid"><div><span>${finalConfirmation ? "OC number" : "Order number"}</span><strong>${escapeHtml(number)}</strong></div><div><span>Customer</span><strong>${escapeHtml(customerName)}</strong></div><div><span>Amount</span><strong>${formatMoney(amount, "EUR")}</strong></div><div><span>Policy</span><strong>Superadmin only</strong></div></div><label>Reason<textarea name="reason" maxlength="500" required>${escapeHtml(finalConfirmation ? "Archived by Superadmin" : "Deleted working Order by Superadmin")}</textarea></label><small class="field-error" data-delete-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-danger" type="submit">${finalConfirmation ? "Archive OC" : "Delete Order"}</button></div></form>`;
  const dialog = openModal(finalConfirmation ? "Archive Order Confirmation" : "Delete Working Order", content);
  refreshIcons(content);
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: string | null) => { if (settled) return; settled = true; resolve(value); };
    content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => { finish(null); dialog.close(); });
    content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", (event) => {
      event.preventDefault();
      const reason = String(new FormData(event.currentTarget as HTMLFormElement).get("reason") ?? "").trim();
      if (!reason) { const error = content.querySelector<HTMLElement>("[data-delete-error]"); if (error) error.textContent = "A reason is required."; return; }
      finish(reason);
      dialog.close();
    });
    dialog.addEventListener("close", () => finish(null), { once: true });
  });
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

export async function ordersPage(mode: "working" | "final" = "final"): Promise<HTMLElement> {
  const isWorking = mode === "working";
  const title = isWorking ? "Orders" : "Order Confirmations";
  const subtitle = isWorking ? "Working orders — editable until finalization." : "Final locked confirmations created from finalized orders.";
  const page = pageScaffold("Commercial", title, subtitle, '<button class="button button-secondary"><i data-lucide="download"></i>Export</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  const role = String(appStore.state.user?.role_id ?? "").toLowerCase();
  const canFinalize = role === "superadmin";
  const canUpdate = appStore.can("orders.update");
  const canDelete = role === "superadmin" && appStore.can("orders.delete");
  try {
    const data = isWorking ? await orderApi.list(customerCompany?._id) : await orderApi.listConfirmations(customerCompany?._id);
    const scopeLabel = customerCompany ? "For this customer" : "Across all authorized customers";
    const pendingCount = data.items.filter((item) => !["CONFIRMED", "PAYMENT CONFIRMED", "PAID"].includes(paymentStatus(item).toUpperCase())).length;
    const confirmedCount = data.items.filter((item) => ["CONFIRMED", "PAYMENT CONFIRMED", "PAID"].includes(paymentStatus(item).toUpperCase())).length;
    const metrics = isWorking
      ? `<article class="metric-card"><div class="metric-top"><span>Working orders</span><i data-lucide="shopping-bag"></i></div><strong>${data.total}</strong><p>${scopeLabel}</p></article><article class="metric-card"><div class="metric-top"><span>Awaiting action</span><i data-lucide="clock-3"></i></div><strong>${pendingCount}</strong><p>Payment or fulfilment pending</p></article><article class="metric-card"><div class="metric-top"><span>Ready to finalize</span><i data-lucide="circle-check"></i></div><strong>${data.items.filter((item) => !item.finalized && (item.materials_ready_for_dispatch || ["Advance", "ADVANCE"].includes(String(item.payment_terms ?? "")) && String(item.payment_status ?? "").toUpperCase() === "CONFIRMED")).length}</strong><p>Superadmin eligible</p></article>`
      : `<article class="metric-card"><div class="metric-top"><span>Final confirmations</span><i data-lucide="file-check-2"></i></div><strong>${data.total}</strong><p>${scopeLabel}</p></article><article class="metric-card"><div class="metric-top"><span>Pending payment</span><i data-lucide="clock-3"></i></div><strong>${pendingCount}</strong><p>Awaiting bank confirmation</p></article><article class="metric-card"><div class="metric-top"><span>Confirmed</span><i data-lucide="circle-check"></i></div><strong>${confirmedCount}</strong><p>Payment received</p></article>`;
    const rows = data.items.map((item) => {
      const id = String(item._id ?? "");
      const orderNumber = String(item.order_number ?? item._id ?? "—");
      const sourceNumber = item.source_order_number ?? item.order_id;
      const workflowOc = String(item.confirmation_type ?? "") === "LINKED_FINAL_OC" || Boolean(sourceNumber && String(sourceNumber).startsWith("MT-ORD-"));
      const workingNumber = workflowOc
        ? String(sourceNumber)
        : item.source_order_id
          ? String(item.source_order_id)
          : "No linked working order";
      const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {};
      const customerName = String(customer.company_name ?? customer.name ?? item.customer_id ?? "—");
      const amount = Number(item.order_amount ?? (item.totals as Record<string, unknown> | undefined)?.grand_total ?? 0);
      const payment = paymentStatus(item);
      const finalReady = ["READY_FOR_DISPATCH", "READY_TO_FINALIZE", "PAYMENT_CONFIRMED"].includes(String(item.lifecycle_state ?? item.order_status ?? "").toUpperCase()) || Boolean(item.materials_ready_for_dispatch) || (String(item.payment_terms ?? "").toUpperCase() === "ADVANCE" && ["CONFIRMED", "PAID"].includes(String(item.payment_status ?? "").toUpperCase()));
      const actionButtons = isWorking
        ? `<a class="button button-secondary" href="/orders/${encodeURIComponent(id)}" data-route="/orders/${encodeURIComponent(id)}"><i data-lucide="eye"></i>View</a><details class="table-action-menu"><summary class="button button-quiet">More<i data-lucide="chevron-down"></i></summary><div class="table-action-menu__popover">${canUpdate ? `<a href="/orders/${encodeURIComponent(id)}/edit" data-route="/orders/${encodeURIComponent(id)}/edit"><i data-lucide="pencil"></i>Edit Order / Add Items</a>` : ""}${item.quotation_id ? `<a href="/quotations/${encodeURIComponent(String(item.quotation_id))}" data-route="/quotations/${encodeURIComponent(String(item.quotation_id))}"><i data-lucide="file-text"></i>View source quotation</a>` : ""}${appStore.can("payments.view") ? `<button type="button" data-order-action="bank" data-id="${escapeHtml(id)}"><i data-lucide="landmark"></i>Payment</button>` : ""}<button type="button" data-order-action="ready" data-id="${escapeHtml(id)}" ${!canFinalize || item.materials_ready_for_dispatch ? "disabled" : ""}><i data-lucide="package-check"></i>${item.materials_ready_for_dispatch ? "Materials ready" : "Mark materials ready"}</button><button type="button" data-order-action="finalize" data-id="${escapeHtml(id)}" ${!canFinalize || !finalReady ? "disabled" : ""}><i data-lucide="file-check-2"></i>Finalize Order Confirmation</button>${canDelete ? `<button class="danger" type="button" data-order-action="delete" data-id="${escapeHtml(id)}"><i data-lucide="trash-2"></i>Delete Order</button>` : ""}</div></details>`
        : `<a class="button button-secondary oc-primary-action" href="/order-confirmations/${encodeURIComponent(id)}" data-route="/order-confirmations/${encodeURIComponent(id)}"><i data-lucide="eye"></i>View</a><details class="table-action-menu"><summary class="button button-quiet">More<i data-lucide="chevron-down"></i></summary><div class="table-action-menu__popover"><a href="/order-confirmations/${encodeURIComponent(id)}" data-route="/order-confirmations/${encodeURIComponent(id)}"><i data-lucide="file-check-2"></i>View OC</a><a href="${orderApi.pdfUrl(id)}"><i data-lucide="download"></i>Download PDF</a>${appStore.can("payments.view") ? `<button type="button" data-order-action="bank" data-id="${escapeHtml(id)}"><i data-lucide="landmark"></i>Payment</button>` : ""}${item.incentive_id && appStore.can("incentives.view") ? `<button type="button" data-order-action="incentive" data-id="${escapeHtml(id)}"><i data-lucide="percent"></i>Incentive</button>` : ""}<a href="/order-confirmations/${encodeURIComponent(id)}#audit-history" data-route="/order-confirmations/${encodeURIComponent(id)}#audit-history"><i data-lucide="history"></i>Audit history</a>${canDelete ? `<button class="danger" type="button" data-order-action="delete" data-id="${escapeHtml(id)}"><i data-lucide="archive-x"></i>Delete / archive</button>` : ""}</div></details>`;
      if (isWorking) return `<tr><td><strong>${escapeHtml(orderNumber)}</strong></td><td>${escapeHtml(customerName)}</td><td>${escapeHtml(String(item.quotation_number ?? item.quotation_id ?? "—"))}</td><td>${formatDate(String(item.oc_date ?? item.created_at ?? ""))}</td><td class="money">${formatMoney(amount, "EUR")}</td><td>${statusBadge(payment)}</td><td>${statusBadge(String(item.status ?? item.lifecycle_state ?? "Working"))}</td><td>${item.finalized ? statusBadge("Finalized") : finalReady ? statusBadge("Ready for dispatch") : statusBadge("Working")}</td><td><div class="table-actions">${actionButtons}</div></td></tr>`;
      return `<tr><td>${statusBadge(workflowOc ? "LINKED FINAL OC" : "HISTORICAL OC")}</td><td><strong>${escapeHtml(orderNumber)}</strong></td><td><span class="oc-linked-order${workflowOc ? "" : " is-legacy"}"${workflowOc ? "" : ` title="${escapeHtml(String(item.confirmation_origin_explanation ?? "Historical OC without a linked working order"))}"`}>${escapeHtml(workingNumber)}</span></td><td>${escapeHtml(customerName)}</td><td>${formatDate(String(item.finalized_at ?? item.created_at ?? ""))}</td><td class="money">${formatMoney(amount, "EUR")}</td><td>${statusBadge(payment)}</td><td>${statusBadge(String(item.incentive_status ?? "PENDING PAYMENT"))}</td><td><div class="table-actions oc-table-actions">${actionButtons}</div></td></tr>`;
    }).join("");
    const table = isWorking
      ? `<div class="data-table panel"><table><thead><tr><th>Order Number</th><th>Customer</th><th>Source Quotation</th><th>Order Date</th><th>Current Amount</th><th>Payment</th><th>Fulfilment</th><th>Finalization</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="data-table panel"><table><thead><tr><th>Type</th><th>OC Number</th><th>Linked Order</th><th>Customer</th><th>Finalized</th><th>Final Amount</th><th>Payment</th><th>Incentive</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`;
    const empty = isWorking ? emptyState("shopping-bag", "No working orders yet", "Convert an accepted quotation to create an editable working order.") : emptyState("file-check-2", "No final Order Confirmations yet", "A Superadmin creates the final confirmation when a working order is eligible.");
    const historyNotice = !isWorking && data.items.some((item) => item.is_legacy_confirmation === true || String(item.confirmation_type ?? "") === "HISTORICAL_OC")
      ? '<div class="notice compact"><i data-lucide="history"></i><div><strong>Historical Order Confirmations</strong><p>A Historical OC predates the current Working Order to Final OC workflow, or has no source working-order link. These records remain unchanged for audit and financial history.</p></div></div>'
      : "";
    body.innerHTML = `<div class="metric-grid compact-metrics">${metrics}</div>${historyNotice}${data.items.length ? table : empty}`;
    body.querySelectorAll<HTMLButtonElement>("[data-order-action]").forEach((button) => button.addEventListener("click", async () => {
      const item = data.items.find((candidate) => String(candidate._id) === String(button.dataset.id)); if (!item) return;
      const action = button.dataset.orderAction;
      try {
        if (action === "bank") await openPaymentForm(item, item.payment_snapshot as Record<string, unknown> | undefined);
        else if (action === "incentive") { if (!item.incentive_id) return; showIncentiveDetails(await financeApi.incentive(String(item.incentive_id))); }
        else if (action === "ready") { button.disabled = true; await orderApi.markMaterialsReady(String(item._id)); toast("Materials marked ready for dispatch"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/orders" })); }
        else if (action === "finalize") { if (!window.confirm("Create the final locked Order Confirmation from this order?")) return; button.disabled = true; const result = await orderApi.finalize(String(item._id)); toast(`Order finalized${result.order_number ? ` as ${String(result.order_number)}` : ""}`); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/order-confirmations" })); }
        else if (action === "delete") { const reason = await confirmOrderDeletion(item, !isWorking); if (!reason) return; button.disabled = true; await orderApi.remove(String(item._id), reason); toast(isWorking ? "Working Order archived" : "Order Confirmation archived"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: isWorking ? "/orders" : "/order-confirmations" })); }
      } catch (error) { toast(error instanceof Error ? error.message : "Order action failed", "error"); button.disabled = false; }
    }));
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>${isWorking ? "Orders" : "Order Confirmations"} unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function orderDetailPage(orderId: string): Promise<HTMLElement> {
  const canUpdate = appStore.can("orders.update");
  const isSuperadmin = String(appStore.state.user?.role_id ?? "").toLowerCase() === "superadmin";
  let order: Record<string, unknown>;
  let revisions: Record<string, unknown>[] = [];
  try {
    order = await orderApi.get(orderId);
    try { revisions = (await orderApi.history(orderId)).items ?? []; } catch { revisions = []; }
  } catch (error) {
    const page = pageScaffold("Commercial", "Order detail", "The requested order could not be loaded.", '<a class="button button-secondary" href="/orders" data-route="/orders"><i data-lucide="arrow-left"></i>Back to Orders</a>');
    page.querySelector<HTMLElement>(".page-body")!.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Order unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
    refreshIcons(page); return page;
  }
  const isFinal = String(order.record_type ?? "").toUpperCase() === "ORDER_CONFIRMATION"
    || Boolean(order.finalized)
    || ["FINAL", "FINALIZED"].includes(String(order.lifecycle_state ?? "").toUpperCase());
  const title = isFinal ? "Order Confirmation detail" : `Working Order — ${String(order.order_number ?? order._id)}`;
  const subtitle = isFinal ? "FINALIZED — LOCKED. Snapshot-backed Order Confirmation with fulfilment status and timeline." : "WORKING ORDER — EDITABLE. Changes are recalculated before finalization.";
  const backHref = isFinal ? "/order-confirmations" : "/orders";
  const finalMoreActions = `<a href="${orderApi.pdfUrl(orderId)}"><i data-lucide="download"></i>Download OC PDF</a>${canUpdate ? '<button type="button" data-order-resend><i data-lucide="mail-check"></i>Resend OC</button><button type="button" data-order-status><i data-lucide="send"></i>Send status email</button>' : ""}`;
  const workingMoreActions = `${canUpdate ? `<a href="/orders/${encodeURIComponent(orderId)}/edit" data-route="/orders/${encodeURIComponent(orderId)}/edit"><i data-lucide="calculator"></i>Edit Order / Add Items</a>` : ""}${isSuperadmin ? '<button type="button" data-order-materials><i data-lucide="package-check"></i>Materials ready</button><button type="button" data-order-finalize><i data-lucide="file-check-2"></i>Finalize Order Confirmation</button>' : ""}`;
  const detailActions = `<a class="button button-secondary detail-back-action" href="${backHref}" data-route="${backHref}"><i data-lucide="arrow-left"></i>Back to ${isFinal ? "Order Confirmations" : "Orders"}</a>`;
  const page = pageScaffold("Commercial", title, subtitle, detailActions);
  if (!isFinal) page.classList.add("working-order-detail-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  const totals = (order.totals ?? {}) as Record<string, unknown>;
  const history = Array.isArray(order.history) ? order.history as Record<string, unknown>[] : [];
  const changesFromQuotation = Array.isArray(order.changes_from_quotation) ? order.changes_from_quotation as Record<string, unknown>[] : [];
  const lines = Array.isArray(order.products_snapshot) ? order.products_snapshot as Record<string, unknown>[] : Array.isArray(order.lines) ? order.lines as Record<string, unknown>[] : [];
  const customer = order.customer_snapshot as Record<string, unknown> | undefined;
  const sourceOrderNumber = String(order.source_order_number ?? (String(order.order_id ?? "").startsWith("MT-ORD-") ? order.order_id : ""));
  const legacyOc = isFinal && !sourceOrderNumber;
  const creator = order.creator_snapshot as Record<string, unknown> | undefined ?? order.salesperson_snapshot as Record<string, unknown> | undefined;
  const manager = order.manager_at_creation as Record<string, unknown> | undefined;
  const lineMarkup = lines.map((line) => {
    return `<tr><td><strong>${escapeHtml(String(line.product_name ?? line.name ?? line.product_id ?? "Product"))}</strong><small>${escapeHtml(orderCategoryLabel(line))}</small></td><td>${escapeHtml(orderConfigurationLabel(line))}</td><td>${escapeHtml(String(line.quantity ?? line.requested_quantity ?? 0))}</td><td>${formatMoney(Number(line.unit_price ?? line.master_unit_price ?? 0), "EUR")}</td><td>${escapeHtml(String(line.discount_percent ?? 0))}%</td><td class="money"><strong>${formatMoney(Number(line.line_total ?? line.final_total ?? line.master_final_total ?? 0), "EUR")}</strong></td></tr>`;
  }).join("");
  const finalOverview = isFinal ? `<section class="panel oc-detail-overview"><div class="section-title"><div><span class="eyebrow">Locked Order Confirmation</span><h2>Commercial snapshot</h2></div>${statusBadge(legacyOc ? "LEGACY OC" : "WORKFLOW")}</div><div class="detail-grid"><div><span>OC number</span><strong>${escapeHtml(String(order.order_number ?? order.oc_number ?? order._id))}</strong></div><div><span>Linked order</span><strong>${escapeHtml(sourceOrderNumber || "No linked working order")}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer?.name ?? customer?.company_name ?? order.customer_id ?? "Customer"))}</strong></div><div><span>Customer type</span><strong>${escapeHtml(String(order.client_type_at_creation ?? customer?.client_type ?? customer?.account_type ?? "—"))}</strong></div><div><span>Created by</span><strong>${escapeHtml(String(creator?.name ?? order.created_by_user_id ?? "—"))}</strong></div><div><span>Manager</span><strong>${escapeHtml(String(manager?.name ?? order.manager_id_at_creation ?? "—"))}</strong></div><div><span>Finalized date</span><strong>${formatDate(String(order.finalized_at ?? order.created_at ?? ""))}</strong></div><div><span>Final amount</span><strong>${formatMoney(Number(totals.grand_total ?? order.order_amount ?? 0), "EUR")}</strong></div><div><span>Currency</span><strong>${escapeHtml(String(order.currency ?? order.master_currency ?? "EUR"))}</strong></div><div><span>Payment status</span><strong>${statusBadge(paymentStatus(order))}</strong></div><div><span>Incentive status</span><strong>${statusBadge(String(order.incentive_status ?? "NOT ASSIGNED"))}</strong></div></div></section>` : "";
  const paymentSection = isFinal ? `<section class="panel" id="payment-information"><div class="section-title"><div><span class="eyebrow">Payment</span><h2>Payment information</h2></div></div><div class="detail-grid"><div><span>Status</span><strong>${statusBadge(paymentStatus(order))}</strong></div><div><span>Confirmed received</span><strong>${formatMoney(Number(order.confirmed_received ?? 0), "EUR")}</strong></div><div><span>Remaining balance</span><strong>${formatMoney(Number(order.remaining_balance ?? totals.grand_total ?? order.order_amount ?? 0), "EUR")}</strong></div><div><span>Customer credit</span><strong>${formatMoney(Number(order.customer_credit ?? 0), "EUR")}</strong></div></div></section>` : "";
  const incentiveSection = isFinal && order.incentive_id && appStore.can("incentives.view") ? `<section class="panel" id="incentive-information"><div class="section-title"><div><span class="eyebrow">Incentives</span><h2>Finalization snapshot</h2></div><a class="button button-secondary" href="/incentives?order_id=${encodeURIComponent(String(order._id))}" data-route="/incentives?order_id=${encodeURIComponent(String(order._id))}">View incentive</a></div><div class="detail-grid"><div><span>Amount</span><strong>${formatMoney(Number(order.incentive_amount ?? 0), "EUR")}</strong></div><div><span>Status</span><strong>${statusBadge(String(order.incentive_status ?? "PENDING PAYMENT"))}</strong></div></div></section>` : "";
  const documentSection = isFinal ? `<section class="panel" id="documents"><div class="section-title"><div><span class="eyebrow">Documents</span><h2>Order Confirmation PDF</h2></div></div><div class="oc-document-actions"><button class="button button-secondary" type="button" data-order-pdf><i data-lucide="eye"></i>View OC</button><a class="button button-secondary" href="${orderApi.pdfUrl(orderId)}"><i data-lucide="download"></i>Download PDF</a></div></section>` : "";
  const changesSection = !isFinal && changesFromQuotation.length ? `<section class="panel"><div class="section-title"><div><span class="eyebrow">Changes from original quotation</span><h2>${escapeHtml(String(order.quotation_number ?? order.source_quotation_number ?? "Source quotation"))}</h2></div></div><div class="timeline-list">${changesFromQuotation.map((change) => `<div class="timeline-item"><i data-lucide="git-compare-arrows"></i><div><strong>${escapeHtml(String(change.type ?? "Changed").replaceAll("_", " "))}</strong><small>${change.from !== undefined ? `${escapeHtml(String(change.from))} → ${escapeHtml(String(change.to))}` : escapeHtml(String(change.product_id ?? "Product"))}</small></div></div>`).join("")}</div></section>` : "";
  const heroActions = `<div class="detail-hero-actions"><button class="button button-primary detail-current-pdf" type="button" data-order-pdf><i data-lucide="file-text"></i>View Current PDF</button><details class="table-action-menu"><summary class="button button-secondary">More<i data-lucide="chevron-down"></i></summary><div class="table-action-menu__popover">${isFinal ? finalMoreActions : workingMoreActions}</div></details></div>`;
  body.innerHTML = `<div class="document-detail-grid"><section class="panel detail-hero document-summary-card"><div class="profile-avatar"><i data-lucide="${isFinal ? "file-check-2" : "shopping-bag"}"></i></div><div class="document-summary-copy"><span class="eyebrow">${escapeHtml(String(order.order_number ?? order._id))}</span><h2>${escapeHtml(String(customer?.name ?? customer?.company_name ?? order.customer_id ?? "Customer"))}</h2><p>${statusBadge(String(order.status ?? order.lifecycle_state ?? "Working"))} · ${escapeHtml(String(order.currency ?? "EUR"))}</p><p class="form-hint">${isFinal ? "Immutable finalized snapshot" : `Payment: ${escapeHtml(paymentStatus(order))} · Fulfilment: ${escapeHtml(String(order.materials_ready_for_dispatch ? "Materials ready" : "Not ready"))}`}</p></div><div class="document-summary-side">${heroActions}<div class="detail-hero-total"><span>${isFinal ? "Final amount" : "Current order total"}</span><strong>${formatMoney(Number(totals.grand_total ?? order.order_amount ?? 0), String(order.currency ?? "EUR"))}</strong></div></div></section><div class="document-detail-main">${finalOverview}<section class="panel document-items-panel"><div class="section-title"><div><span class="eyebrow">${isFinal ? "Items" : "Working order items"}</span><h2>${lines.length} line item${lines.length === 1 ? "" : "s"}</h2></div></div><div class="document-items-table"><table><thead><tr><th>Product</th><th>Configuration</th><th>Quantity</th><th>Unit price</th><th>Discount</th><th>Total</th></tr></thead><tbody>${lineMarkup}</tbody></table></div></section>${changesSection}${paymentSection}${incentiveSection}${documentSection}</div><section class="panel document-timeline" id="audit-history"><div class="section-title"><div><span class="eyebrow">Audit history</span><h2>Status timeline</h2></div></div>${history.length ? `<div class="timeline-list document-timeline-list">${history.map((item) => `<div class="timeline-item"><i data-lucide="circle-check"></i><div><strong>${escapeHtml(String(item.status ?? "Updated"))}</strong><small>${escapeHtml(revisionTimestamp(item))}</small></div></div>`).join("")}</div>` : '<p class="muted">No status events recorded.</p>'}</section></div>`;
  if (revisions.length) {
    const markup = `<section class="panel revision-history"><div class="section-title"><div><span class="eyebrow">Revision History</span><h2>${escapeHtml(String(order.order_number ?? order._id))}</h2></div><span class="count-badge">Current V${escapeHtml(String(order.version ?? 1))} · Showing latest 3</span></div><div class="timeline-list revision-history-list">${revisions.map((version) => `<div class="timeline-item revision-row${Number(version.version) === Number(order.version ?? 1) ? " is-current" : ""}"><i data-lucide="history"></i><div><strong>V${escapeHtml(String(version.version))}${Number(version.version) === Number(order.version ?? 1) ? " · Current" : ""}</strong><small>${escapeHtml(revisionTimestamp(version))} · ${escapeHtml(String((version.created_by_snapshot as Record<string, unknown> | undefined)?.name ?? version.created_by ?? "User"))}</small><small>${escapeHtml(String(version.reason ?? "Revision"))}</small></div><button class="button button-quiet button-small revision-pdf-button" type="button" data-order-version-pdf="${escapeHtml(String(version._id))}" data-version="${escapeHtml(String(version.version))}"><i data-lucide="file-text"></i>View PDF</button></div>`).join("")}</div></section>`;
    body.querySelector(".document-detail-main")?.insertAdjacentHTML("beforeend", markup);
  }
  page.querySelectorAll<HTMLButtonElement>("[data-order-pdf]").forEach((button) => button.addEventListener("click", () => openPdfViewer({
    title: "Order Confirmation PDF", subtitle: `${String(order.order_number ?? orderId)} · immutable PDF`,
    filename: `${String(order.order_number ?? orderId)}.pdf`, path: `/orders/${encodeURIComponent(orderId)}/pdf?preview=true`,
  })));
  page.querySelectorAll<HTMLButtonElement>("[data-order-version-pdf]").forEach((button) => button.addEventListener("click", () => {
    const versionId = button.dataset.orderVersionPdf;
    if (!versionId) return;
    openPdfViewer({ title: `Order V${button.dataset.version ?? ""}`, subtitle: `${String(order.order_number ?? orderId)} · immutable version`, filename: `${String(order.order_number ?? orderId)}-V${button.dataset.version ?? ""}.pdf`, path: `/orders/${encodeURIComponent(orderId)}/history/${encodeURIComponent(versionId)}/pdf` });
  }));
  // Item editing is intentionally centralized in the document-scoped
  // calculator editor opened by the primary Edit Order / Add Items action.
  if (false && !isFinal && canUpdate && lines.length) {
    const editor = document.createElement("section");
    editor.className = "panel working-order-editor";
    editor.innerHTML = `<div class="section-title"><div><span class="eyebrow">Working Order — editable</span><h2>Adjust quantities</h2><p class="form-hint">Changes are repriced by the server. Add products from the calculator or remove an item here.</p></div></div><div class="working-order-edit-list">${lines.map((line) => {
      const itemId = String(line.item_id ?? line.line_id ?? line._id ?? "");
      return `<div class="working-order-edit-row"><div><strong>${escapeHtml(String(line.product_name ?? line.name ?? line.product_id ?? "Product"))}</strong><small>Current quantity: ${escapeHtml(String(line.requested_quantity ?? line.quantity ?? 0))}</small></div><div class="table-actions"><button class="button button-secondary" type="button" data-edit-line="${escapeHtml(itemId)}">Change quantity</button><button class="button button-quiet" type="button" data-remove-line="${escapeHtml(itemId)}">Remove</button></div></div>`;
    }).join("")}</div>`;
    body.append(editor);
    editor.querySelectorAll<HTMLButtonElement>("[data-edit-line]").forEach((button) => button.addEventListener("click", async () => {
      const line = lines.find((candidate) => String(candidate.item_id ?? candidate.line_id ?? candidate._id ?? "") === button.dataset.editLine);
      if (!line) return;
      const quantity = Number(window.prompt("New quantity", String(line.requested_quantity ?? line.quantity ?? 1)));
      if (!Number.isFinite(quantity) || quantity <= 0) return;
      button.disabled = true;
      try { await orderApi.updateItem(orderId, String(button.dataset.editLine), { quantity }); toast("Working Order quantity updated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/orders/${encodeURIComponent(orderId)}` })); }
      catch (error) { toast(error instanceof Error ? error.message : "Quantity could not be updated", "error"); button.disabled = false; }
    }));
    editor.querySelectorAll<HTMLButtonElement>("[data-remove-line]").forEach((button) => button.addEventListener("click", async () => {
      if (!window.confirm("Remove this item from the working Order?")) return;
      button.disabled = true;
      try { await orderApi.removeItem(orderId, String(button.dataset.removeLine)); toast("Item removed and totals recalculated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/orders/${encodeURIComponent(orderId)}` })); }
      catch (error) { toast(error instanceof Error ? error.message : "Item could not be removed", "error"); button.disabled = false; }
    }));
  }
  page.querySelector<HTMLButtonElement>("[data-order-materials]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement; button.disabled = true;
    try { await orderApi.markMaterialsReady(orderId); toast("Materials marked ready for dispatch"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/orders/${encodeURIComponent(orderId)}` })); }
    catch (error) { toast(error instanceof Error ? error.message : "Materials could not be marked ready", "error"); button.disabled = false; }
  });
  page.querySelector<HTMLButtonElement>("[data-order-finalize]")?.addEventListener("click", async (event) => {
    if (!window.confirm("Create the final locked Order Confirmation from this working order?")) return;
    const button = event.currentTarget as HTMLButtonElement; button.disabled = true;
    try { const result = await orderApi.finalize(orderId); toast(`Order finalized${result.order_number ? ` as ${String(result.order_number)}` : ""}`); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/order-confirmations/${encodeURIComponent(String(result._id ?? orderId))}` })); }
    catch (error) { toast(error instanceof Error ? error.message : "Order could not be finalized", "error"); button.disabled = false; }
  });
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
