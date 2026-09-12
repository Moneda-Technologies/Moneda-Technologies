import { orderApi } from "../api";
import { financeApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";

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
    <div class="form-grid"><label>Payment amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required value="${Number(snapshot.amount ?? order.order_amount ?? 0).toFixed(2)}"></label><label>Payment date<input name="payment_date" type="date" required value="${escapeHtml(String(snapshot.payment_date ?? new Date().toISOString().slice(0, 10)).slice(0, 10))}"></label><label>Bank name<input name="bank_name" value="${snapshotValue(snapshot, "bank_name") === "—" ? "" : snapshotValue(snapshot, "bank_name")}"></label><label>Bank account<input name="bank_account" value="${snapshotValue(snapshot, "bank_account") === "—" ? "" : snapshotValue(snapshot, "bank_account")}"></label><label>UTR / transaction reference<input name="utr" value="${snapshotValue(snapshot, "utr") === "—" ? "" : snapshotValue(snapshot, "utr")}"></label><label>Payment mode<input name="payment_mode" value="${snapshotValue(snapshot, "payment_mode") === "—" ? "" : snapshotValue(snapshot, "payment_mode")}"></label><label>Payment reference<input name="reference_number" value="${snapshotValue(snapshot, "reference_number") === "—" ? "" : snapshotValue(snapshot, "reference_number")}"></label><label class="span-2">Payment proof<input name="attachment" type="file" accept="image/*,.pdf"></label><label class="span-2">Notes<textarea name="notes" rows="2">${snapshotValue(snapshot, "notes") === "—" ? "" : snapshotValue(snapshot, "notes")}</textarea></label></div>
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
  const content = paymentFormContent(order, existing);
  const dialog = openModal(existing ? "Update Banking & Payment" : "Banking & Payment", content, "wide");
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  content.querySelector<HTMLFormElement>("[data-payment-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    const file = data.get("attachment");
    const attachment = file instanceof File ? await readPaymentProof(file) : undefined;
    const payload = { amount: Number(data.get("amount")), payment_date: data.get("payment_date"), bank_name: data.get("bank_name"), bank_account: data.get("bank_account"), utr: data.get("utr"), payment_mode: data.get("payment_mode"), reference_number: data.get("reference_number"), notes: data.get("notes"), ...(attachment ? { attachment } : {}) };
    const error = content.querySelector<HTMLElement>("[data-payment-error]");
    try {
      const payment = existing ? await financeApi.updatePayment(String(existing._id), payload) : await financeApi.createPayment({ ...payload, order_id: order._id });
      if (existing) await financeApi.submitPayment(String(existing._id)); else await financeApi.submitPayment(String(payment._id));
      dialog.close(); toast("Payment submitted for Superadmin confirmation");
      window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/orders" }));
    } catch (err) { if (error) error.textContent = err instanceof Error ? err.message : "Payment could not be saved"; }
  });
  refreshIcons(content);
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
  const lines = Array.isArray(incentive.incentive_lines) ? incentive.incentive_lines as Record<string, unknown>[] : [];
  const content = document.createElement("div");
  content.innerHTML = `<div class="stack-form"><section class="panel"><div class="detail-grid"><div><span>OC</span><strong>${snapshotValue(incentive, "oc_number")}</strong></div><div><span>Sales Person</span><strong>${snapshotValue(incentive.salesperson_snapshot, "name")}</strong></div><div><span>Status</span><strong>${statusBadge(String(incentive.status ?? "PENDING PAYMENT"))}</strong></div><div><span>Payment confirmed</span><strong>${formatDate(String(incentive.payment_confirmation_date ?? ""))}</strong></div><div><span>Due date</span><strong>${formatDate(String(incentive.incentive_due_date ?? ""))}</strong></div><div><span>Gross incentive</span><strong>${formatMoney(Number(incentive.gross_incentive_amount ?? 0), "EUR")}</strong></div><div><span>Credit Note deductions</span><strong>${formatMoney(Number(incentive.credit_note_deduction ?? 0), "EUR")}</strong></div><div><span>Already paid</span><strong>${formatMoney(Number(incentive.paid_amount ?? 0), "EUR")}</strong></div><div><span>Remaining payable</span><strong>${formatMoney(Number(incentive.remaining_amount ?? incentive.net_payable_incentive ?? 0), "EUR")}</strong></div></div></section>${lines.length ? `<div class="data-table panel"><table><thead><tr><th>Product</th><th>Category</th><th>Amount</th><th>Rate</th><th>Incentive</th></tr></thead><tbody>${lines.map((line) => `<tr><td>${snapshotValue(line, "product_name")}</td><td>${snapshotValue(line, "category_name")}</td><td>${formatMoney(Number(line.amount ?? 0), "EUR")}</td><td>${Number(line.incentive_rate_snapshot ?? 0).toFixed(0)}%</td><td>${formatMoney(Number(line.incentive_amount ?? 0), "EUR")}</td></tr>`).join("")}</tbody></table></div>` : ""}</div>`;
  const state = document.createElement("section");
  state.className = "panel incentive-status-card";
  state.innerHTML = `<span class="eyebrow">Incentive status</span><div class="detail-grid"><div><span>Activation date</span><strong>${formatDate(String(incentive.incentive_activation_date ?? ""))}</strong></div><div><span>Due date</span><strong>${formatDate(String(incentive.incentive_due_date ?? ""))}</strong></div><div><span>Credit Note deductions</span><strong>${formatMoney(Number(incentive.credit_note_deduction ?? 0), "EUR")}</strong></div><div><span>Remaining payable</span><strong>${formatMoney(Number(incentive.remaining_amount ?? incentive.net_payable_incentive ?? 0), "EUR")}</strong></div></div>`;
  content.append(state);
  openModal("Incentive Details", content, "wide");
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
    body.innerHTML = `<div class="metric-grid compact-metrics"><article class="metric-card"><div class="metric-top"><span>All Order Confirmations</span><i data-lucide="shopping-bag"></i></div><strong>${data.total}</strong><p>${scopeLabel}</p></article><article class="metric-card"><div class="metric-top"><span>Pending payment</span><i data-lucide="clock-3"></i></div><strong>${data.items.filter((item) => !["CONFIRMED", "PAYMENT CONFIRMED"].includes(paymentStatus(item).toUpperCase())).length}</strong><p>Awaiting bank confirmation</p></article><article class="metric-card"><div class="metric-top"><span>Confirmed</span><i data-lucide="circle-check"></i></div><strong>${data.items.filter((item) => paymentStatus(item).toUpperCase() === "CONFIRMED").length}</strong><p>Payment received</p></article></div>${data.items.length ? `<div class="data-table panel"><table><thead><tr><th>OC Number</th><th>Customer</th><th>Quote</th><th>OC Date</th><th>Amount</th><th>OC Status</th><th>Payment</th><th>Incentive</th><th>Actions</th></tr></thead><tbody>${data.items.map((item) => { const payment = paymentStatus(item); const confirmed = payment.toUpperCase() === "CONFIRMED"; const paymentId = String((item.payment_snapshot as Record<string, unknown> | undefined)?._id ?? ""); const orderId = String(item._id ?? ""); const confirmDisabled = !canConfirm || confirmed || !paymentId; return `<tr><td><strong>${escapeHtml(String(item.order_number ?? item._id))}</strong></td><td>${escapeHtml(String((item.customer_snapshot as Record<string, unknown> | undefined)?.company_name ?? (item.customer_snapshot as Record<string, unknown> | undefined)?.name ?? item.customer_id ?? "—"))}</td><td>${escapeHtml(String(item.quotation_number ?? item.quotation_id ?? "—"))}</td><td>${formatDate(String(item.oc_date ?? item.created_at ?? ""))}</td><td class="money">${formatMoney(Number(item.order_amount ?? (item.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</td><td>${statusBadge(String(item.status ?? "Pending"))}</td><td><a href="/payments?order_id=${encodeURIComponent(orderId)}" data-route="/payments?order_id=${encodeURIComponent(orderId)}">${statusBadge(payment)}</a></td><td>${statusBadge(String(item.incentive_status ?? "PENDING PAYMENT"))}</td><td><div class="table-actions"><button class="icon-button" type="button" data-order-action="bank" data-id="${escapeHtml(String(item._id))}" title="Update Banking Details" aria-label="Update Banking Details"><i data-lucide="landmark"></i></button><button class="icon-button" type="button" data-order-action="confirm" data-id="${escapeHtml(String(item._id))}" data-payment-id="${escapeHtml(paymentId)}" title="${confirmDisabled ? "Payment confirmation unavailable" : "Confirm Payment Received"}" aria-label="Confirm Payment Received" ${confirmDisabled ? "disabled" : ""}><i data-lucide="circle-check"></i></button><button class="icon-button" type="button" data-order-action="incentive" data-id="${escapeHtml(String(item._id))}" title="View Incentive Details" aria-label="View Incentive Details"><i data-lucide="percent"></i></button><button class="icon-button" type="button" data-order-action="view" data-id="${escapeHtml(String(item._id))}" title="View Order Confirmation" aria-label="View Order Confirmation"><i data-lucide="eye"></i></button></div></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("shopping-bag", "No Order Confirmations yet", "Accepted quotations can be converted into Order Confirmations.")}`;
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
