import { adminApi, catalogApi, customerCompanyApi, financeApi, orderApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { renderIncentiveConfigurator } from "./incentive-configurator";
import { emptyState, escapeHtml, formatDate, formatDateInput, formatMoney, skeleton } from "../utils/dom";

function readPaymentProof(file: File): Promise<Record<string, unknown> | undefined> {
  if (!file || !file.size) return Promise.resolve(undefined);
  if (file.size > 5 * 1024 * 1024) return Promise.reject(new Error("Payment proof must be 5 MB or smaller"));
  const allowedTypes = new Set(["application/pdf", "image/jpeg", "image/png", "image/webp"]);
  if (!allowedTypes.has(file.type.toLowerCase())) return Promise.reject(new Error("Payment proof must be a PDF, JPG, PNG or WEBP file"));
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("Payment proof could not be read"));
    reader.onload = () => resolve({ name: file.name.slice(0, 160), type: file.type.slice(0, 120), size: file.size, data: String(reader.result ?? "") });
    reader.readAsDataURL(file);
  });
}

function paymentStatus(item: Record<string, unknown>): string {
  return String(item.status ?? "PAYMENT RECORDED");
}

function reconciliationStatus(item: Record<string, unknown>): string {
  const value = item.reconciliation_status ?? item.reconciliationStatus;
  if (value) return String(value);
  // A payment is only reconciled when the server has explicitly recorded it.
  // Do not infer reconciliation from confirmation or manufacture a status.
  return "UNRECONCILED";
}

function paymentCustomer(item: Record<string, unknown>): string {
  const snapshot = item.customer_snapshot as Record<string, unknown> | undefined;
  return String(snapshot?.company_name ?? snapshot?.name ?? item.customer_id ?? "—");
}

export async function legacyBankingPage(): Promise<HTMLElement> {
  const page = pageScaffold("Finance", "Banking", "Manage incoming payments and reconciliation.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.payments("limit=500");
    const items = result.items;
    const pending = items.filter((item) => paymentStatus(item).toUpperCase() !== "CONFIRMED").length;
    const awaiting = items.filter((item) => paymentStatus(item).toUpperCase() === "AWAITING SUPERADMIN CONFIRMATION").length;
    const confirmed = items.filter((item) => paymentStatus(item).toUpperCase() === "CONFIRMED").length;
    const reconciled = items.filter((item) => reconciliationStatus(item).toUpperCase() === "RECONCILED").length;
    const cards = [
      ["Total pending", pending, "clock-3", "Payments not yet confirmed"],
      ["Awaiting confirmation", awaiting, "hourglass", "Submitted for review"],
      ["Confirmed", confirmed, "circle-check", "Receipt confirmed"],
      ["Reconciled", reconciled, "clipboard-check", "Explicitly reconciled records"],
    ] as const;
    const rows = items.length ? `<div class="data-table panel banking-table"><table><thead><tr><th>Payment / Transaction ID</th><th>Customer</th><th>Order Confirmation</th><th>Quote</th><th>Date</th><th>Amount</th><th>Currency</th><th>Payment Status</th><th>Reconciliation</th><th>Actions</th></tr></thead><tbody>${items.map((item) => {
      const id = String(item._id ?? item.payment_id ?? item.transaction_id ?? "—");
      const orderId = String(item.order_id ?? item.order_confirmation_id ?? item.oc_id ?? "");
      const orderNumber = String(item.order_number ?? item.oc_number ?? (orderId || "—"));
      const quoteNumber = String(item.quotation_number ?? item.quote_number ?? item.quotation_id ?? "—");
      const status = paymentStatus(item);
      const reconciliation = reconciliationStatus(item);
      const orderLink = orderId ? `<a href="/orders/${encodeURIComponent(orderId)}" data-route="/orders/${encodeURIComponent(orderId)}">${escapeHtml(orderNumber)}</a>` : escapeHtml(orderNumber);
      const paymentLink = `<a href="/payments?order_id=${encodeURIComponent(orderId)}" data-route="/payments?order_id=${encodeURIComponent(orderId)}"><strong>${escapeHtml(id)}</strong></a>`;
      return `<tr><td>${paymentLink}</td><td>${escapeHtml(paymentCustomer(item))}</td><td>${orderLink}</td><td>${escapeHtml(quoteNumber)}</td><td>${formatDate(String(item.payment_date ?? item.created_at ?? ""))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), String(item.currency ?? "EUR"))}</td><td>${escapeHtml(String(item.currency ?? "EUR"))}</td><td>${statusBadge(status)}</td><td>${statusBadge(reconciliation)}</td><td>${orderId ? `<a class="button button-quiet" href="/orders/${encodeURIComponent(orderId)}" data-route="/orders/${encodeURIComponent(orderId)}">Open OC</a>` : "—"}</td></tr>`;
    }).join("")}</tbody></table></div>` : emptyState("landmark", "No payments recorded", "Payments entered by the team will appear here.");
    body.innerHTML = `<div class="metric-grid compact-metrics banking-summary">${cards.map(([label, value, icon, hint]) => `<article class="metric-card"><div class="metric-top"><span>${label}</span><i data-lucide="${icon}"></i></div><strong>${value}</strong><p>${hint}</p></article>`).join("")}</div>${rows}`;
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => {
      event.preventDefault();
      window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") }));
    }));
  } catch (error) {
    body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Banking unavailable")}</div>`;
  }
  refreshIcons(page);
  return page;
}

// Banking and Payments are one operational payment-record workflow. Keep the
// overview route on the same record-first screen so the two sidebar entries do
// not drift into different forms or status calculations.
export async function bankingPage(): Promise<HTMLElement> {
  return paymentsPage();
}

export async function legacyPaymentsPage(): Promise<HTMLElement> {
  const query = new URLSearchParams(window.location.search);
  const view = query.get("view");
  const title = view === "transactions" ? "Bank Transactions" : view === "reconciliation" ? "Reconciliation" : query.get("status") ? "Pending Payments" : "Payments";
  const page = pageScaffold("Finance", title, "Record customer payments and submit them for Superadmin confirmation.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.payments(query.toString());
    const canConfirm = appStore.state.user?.role_id === "superadmin";
    body.innerHTML = `<form class="panel stack-form" id="payment-entry"><span class="eyebrow">Record payment</span><div class="form-grid"><label>Order Confirmation ID<input name="order_id" required></label><label>Amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Payment date<input name="payment_date" type="date" required></label><label>Bank name<input name="bank_name"></label><label>Bank account<input name="bank_account"></label><label>UTR / transaction reference<input name="utr"></label><label>Payment mode<input name="payment_mode" placeholder="Bank transfer, cheque..."></label><label>Payment reference<input name="reference_number"></label><label class="span-2">Notes<textarea name="notes" rows="2"></textarea></label><label class="span-2">Payment proof<input name="attachment" type="file"></label></div><button class="button button-primary" type="submit">Record &amp; submit for confirmation</button></form>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Order Confirmation</th><th>Customer</th><th>Amount</th><th>Payment date</th><th>Status</th>${canConfirm ? "<th>Actions</th>" : ""}</tr></thead><tbody>${result.items.map((item) => { const status = String(item.status ?? ""); const action = canConfirm && status !== "CONFIRMED" ? `<button class="button button-quiet confirm-payment" data-id="${escapeHtml(String(item._id))}">Confirm receipt</button>` : "—"; return `<tr><td>${escapeHtml(String(item.order_id ?? item.oc_id ?? "—"))}</td><td>${escapeHtml(String((item.customer_snapshot as Record<string, unknown> | undefined)?.name ?? item.customer_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td>${formatDate(String(item.payment_date ?? item.created_at ?? ""))}</td><td>${statusBadge(status)}</td>${canConfirm ? `<td>${action}</td>` : ""}</tr>`; }).join("")}</tbody></table></div>` : emptyState("landmark", "No payments recorded", "Payments entered by the team will appear here.")}`;
    body.querySelector<HTMLFormElement>("#payment-entry")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); try { const file = data.get("attachment"); const attachment = file instanceof File ? await readPaymentProof(file) : undefined; const payment = await financeApi.createPayment({ order_id: data.get("order_id"), amount: Number(data.get("amount")), payment_date: data.get("payment_date"), bank_name: data.get("bank_name"), bank_account: data.get("bank_account"), utr: data.get("utr"), payment_mode: data.get("payment_mode"), reference_number: data.get("reference_number"), notes: data.get("notes"), attachment }); await financeApi.submitPayment(String(payment._id)); toast("Payment submitted for Superadmin confirmation", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Payment could not be recorded", "error"); } });
    body.querySelectorAll<HTMLButtonElement>(".confirm-payment").forEach((button) => button.addEventListener("click", async () => {
      const payment = result.items.find((item) => String(item._id) === String(button.dataset.id));
      if (!payment) return;
      const customer = payment.customer_snapshot as Record<string, unknown> | undefined;
      const proof = payment.attachment && typeof payment.attachment === "object" ? String((payment.attachment as Record<string, unknown>).name ?? "Payment proof attached") : "No payment proof attached";
      const content = document.createElement("div");
      content.innerHTML = `<div class="stack-form"><p>Confirm that this customer payment has been received in the bank. This activates the existing incentive and locks the financial records.</p><section class="panel"><div class="detail-grid"><div><span>Customer</span><strong>${escapeHtml(String(customer?.company_name ?? customer?.name ?? payment.customer_id ?? "Customer"))}</strong></div><div><span>Order Confirmation</span><strong>${escapeHtml(String(payment.order_id ?? payment.oc_id ?? "—"))}</strong></div><div><span>Payment amount</span><strong>${formatMoney(Number(payment.amount ?? 0), "EUR")}</strong></div><div><span>Payment date</span><strong>${formatDate(String(payment.payment_date ?? ""))}</strong></div><div><span>Bank</span><strong>${escapeHtml(String(payment.bank_name ?? "—"))}</strong></div><div><span>UTR / reference</span><strong>${escapeHtml(String(payment.utr ?? payment.reference_number ?? "—"))}</strong></div><div><span>Payment proof</span><strong>${escapeHtml(proof)}</strong></div></div></section><small class="field-error" data-confirm-error></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="button" data-confirm>Confirm Payment Received</button></div></div>`;
      const dialog = openModal("Confirm Payment Received", content, "wide");
      content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
      content.querySelector<HTMLButtonElement>("[data-confirm]")?.addEventListener("click", async (event) => {
        const confirmButton = event.currentTarget as HTMLButtonElement;
        const error = content.querySelector<HTMLElement>("[data-confirm-error]");
        confirmButton.disabled = true;
        try { await financeApi.confirmPayment(String(payment._id)); dialog.close(); toast("Payment confirmed and incentive activated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/payments" })); }
        catch (errorValue) { if (error) error.textContent = errorValue instanceof Error ? errorValue.message : "Payment could not be confirmed"; confirmButton.disabled = false; }
      });
      refreshIcons(content);
    }));
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Payments unavailable")}</div>`; }
  refreshIcons(page); return page;
}

type BankingRecord = Record<string, unknown>;

function bankingName(value: BankingRecord | undefined): string {
  return String(value && (value.company_name || value.name || value._id) || "");
}

function bankingOrderName(value: BankingRecord | undefined): string {
  return String(value && (value.order_number || value.oc_number || value._id) || "");
}

function bankingAmount(value: BankingRecord | undefined): number {
  const totals = value && value.totals as BankingRecord | undefined;
  return Number(value && (value.order_amount || (totals && totals.grand_total) || 0) || 0);
}

function bankingStatus(value: BankingRecord): string {
  return String(value.workflow_status || value.status || "AWAITING SUPERADMIN CONFIRMATION");
}

function bankingInvoiceStatus(value: BankingRecord): string {
  return String(value.invoice_status || value.derived_status || "PENDING");
}

function bankingPaymentId(value: BankingRecord, fallbackIndex = 0): string {
  const explicit = value.payment_number || value.payment_id;
  if (explicit) return String(explicit);
  const internal = String(value._id || "").trim();
  return internal ? `PAY-${internal.slice(-6).toUpperCase()}` : `PAY-${String(fallbackIndex + 1).padStart(5, "0")}`;
}

async function bankingContexts(): Promise<{ customers: BankingRecord[]; orders: BankingRecord[] }> {
  const results = await Promise.all([customerCompanyApi.list(), orderApi.list(undefined, 100)]);
  return { customers: results[0].items as unknown as BankingRecord[], orders: results[1].items as BankingRecord[] };
}

async function openBankingPayment(customers: BankingRecord[], orders: BankingRecord[], existing?: BankingRecord, readOnly = false, initialOrderId = ""): Promise<void> {
  let selectedCustomer = existing ? customers.find((row) => String(row._id) === String(existing.customer_id)) : undefined;
  let selectedOrder = orders.find((row) => String(row._id) === String(existing?.order_id || existing?.oc_id || initialOrderId));
  if (!selectedOrder && existing && existing.order_id) selectedOrder = await orderApi.get(String(existing.order_id)).catch(() => undefined);
  if (!selectedCustomer && selectedOrder) selectedCustomer = customers.find((row) => String(row._id) === String(selectedOrder && (selectedOrder.customer_id || selectedOrder.customer_company_id || selectedOrder.company_id)));
  let orderPayments: BankingRecord[] = [];
  let attachment: BankingRecord | undefined = existing?.attachment as BankingRecord | undefined;
  const content = document.createElement("div");
  const customerOptions = customers.map((row) => "<option value=\"" + escapeHtml(bankingName(row)) + "\"></option>").join("");
  const existingOverview = existing ? "<section class=\"payment-workflow-section payment-overview-section\"><div class=\"detail-grid\"><div><span>Payment ID</span><strong>" + escapeHtml(bankingPaymentId(existing)) + "</strong></div><div><span>Status</span><strong>" + statusBadge(bankingStatus(existing)) + "</strong></div></div></section>" : "";
  content.innerHTML = "<form class=\"payment-workflow stack-form\" data-banking-form novalidate>" +
    existingOverview + "<section class=\"payment-workflow-section\"><div class=\"section-title\"><span class=\"step-number\">01</span><div><span class=\"eyebrow\">Customer &amp; Invoice</span><h2>Select the transaction being paid</h2></div></div><div class=\"form-grid\">" +
    "<label>Customer *<input name=\"customer_display\" list=\"banking-customers\" placeholder=\"Select customer\" autocomplete=\"off\"><datalist id=\"banking-customers\">" + customerOptions + "</datalist><input name=\"customer_id\" type=\"hidden\"><small class=\"field-error\" data-error=\"customer_id\"></small></label>" +
    "<label>Invoice / Order Confirmation *<input name=\"order_display\" list=\"banking-orders\" placeholder=\"Select customer first\" autocomplete=\"off\" disabled><datalist id=\"banking-orders\"></datalist><input name=\"order_id\" type=\"hidden\"><small class=\"field-error\" data-error=\"order_id\"></small></label></div></section>" +
    "<section class=\"payment-workflow-section\" data-details hidden><div class=\"section-title\"><span class=\"step-number\">02</span><div><span class=\"eyebrow\">Invoice Details</span><h2>Read-only transaction information</h2></div></div><div class=\"detail-grid readonly-payment-details\">" +
    ["customer", "invoice", "oc", "invoice_date", "due_date", "payment_terms", "salesperson", "currency", "invoice_amount", "paid", "outstanding"].map((key) => "<div><span>" + key.replaceAll("_", " ") + "</span><strong data-detail=\"" + key + "\">—</strong></div>").join("") +
    "</div></section>" +
    "<section class=\"payment-workflow-section\"><div class=\"section-title\"><span class=\"step-number\">03</span><div><span class=\"eyebrow\">Payment Details</span><h2>Enter payment information</h2></div></div><div class=\"form-grid\">" +
    "<label>Payment amount *<input name=\"amount\" type=\"number\" min=\"0.01\" step=\"0.01\" required><small class=\"field-error\" data-error=\"amount\"></small></label>" +
    "<label>Payment date *<input name=\"payment_date\" type=\"date\" required><small class=\"field-error\" data-error=\"payment_date\"></small></label>" +
    "<label>Payment mode *<select name=\"payment_mode\" required><option value=\"\">Select payment mode</option>" + ["Bank Transfer", "NEFT", "RTGS", "IMPS", "SWIFT", "Cheque", "Other"].map((mode) => "<option>" + mode + "</option>").join("") + "</select><small class=\"field-error\" data-error=\"payment_mode\"></small></label>" +
    "<label>Bank name *<input name=\"bank_name\" required><small class=\"field-error\" data-error=\"bank_name\"></small></label><label>Bank account *<input name=\"bank_account\" required><small class=\"field-error\" data-error=\"bank_account\"></small></label><label>UTR / transaction reference *<input name=\"utr\" required><small class=\"field-error\" data-error=\"utr\"></small></label><label>Payment reference *<input name=\"reference_number\" required><small class=\"field-error\" data-error=\"reference_number\"></small></label></div></section>" +
    "<section class=\"payment-workflow-section\"><div class=\"section-title\"><span class=\"step-number\">04</span><div><span class=\"eyebrow\">Supporting Documents</span><h2>Proof and notes</h2></div></div><div class=\"form-grid\"><label>Payment proof *<span class=\"payment-proof-dropzone\" data-proof-zone tabindex=\"0\"><i data-lucide=\"upload-cloud\"></i><strong>Upload payment proof</strong><small>PDF, JPG, PNG or WEBP up to 5 MB</small><button class=\"button button-quiet\" type=\"button\" data-choose-proof>Choose file</button><input name=\"attachment\" type=\"file\" accept=\"application/pdf,image/jpeg,image/png,image/webp\" hidden></span><small data-file class=\"form-hint\">Required</small><button class=\"button button-quiet\" type=\"button\" data-remove-file hidden>Remove proof</button><small class=\"field-error\" data-error=\"attachment\"></small></label><label class=\"span-2\">Notes<textarea name=\"notes\" rows=\"3\" placeholder=\"Optional notes\"></textarea></label></div></section>" +
    "<section class=\"payment-workflow-section payment-summary-section\"><div class=\"section-title\"><span class=\"step-number\">05</span><div><span class=\"eyebrow\">Payment Summary</span><h2>Balance after this payment</h2></div></div><div class=\"detail-grid\"><div><span>Invoice amount</span><strong data-summary=\"invoice\">€0.00</strong></div><div><span>Previously paid</span><strong data-summary=\"previous\">€0.00</strong></div><div><span>This payment</span><strong data-summary=\"current\">€0.00</strong></div><div><span>Remaining balance</span><strong data-summary=\"balance\">€0.00</strong></div><div><span>Status</span><strong data-summary=\"status\">Select an invoice</strong></div><div><span>Customer credit</span><strong data-summary=\"credit\">€0.00</strong></div></div></section>" +
    "<small class=\"field-error\" data-form-error></small><div class=\"modal-actions\"><button class=\"button button-quiet\" type=\"button\" data-cancel>Cancel</button><button class=\"button button-primary\" type=\"submit\" data-submit>" + (existing ? "Update Payment" : "Record Payment") + "</button></div></form>";
  const dialog = openModal(existing ? (readOnly ? "View Payment" : "Edit Payment") : "New Payment", content, "wide");
  const form = content.querySelector<HTMLFormElement>("[data-banking-form]")!;
  const customerInput = form.elements.namedItem("customer_display") as HTMLInputElement;
  const customerId = form.elements.namedItem("customer_id") as HTMLInputElement;
  const orderInput = form.elements.namedItem("order_display") as HTMLInputElement;
  const orderId = form.elements.namedItem("order_id") as HTMLInputElement;
  const orderList = form.querySelector<HTMLDataListElement>("#banking-orders")!;
  const details = form.querySelector<HTMLElement>("[data-details]")!;
  const amountInput = form.elements.namedItem("amount") as HTMLInputElement;
  const setError = (field: string, message: string) => { const node = form.querySelector<HTMLElement>("[data-error=\"" + field + "\"]"); if (node) node.textContent = message; };
  const setOutput = (field: string, value: string) => { const node = form.querySelector<HTMLElement>("[data-detail=\"" + field + "\"]"); if (node) node.textContent = value; };
  const setSummary = (field: string, value: string) => { const node = form.querySelector<HTMLElement>("[data-summary=\"" + field + "\"]"); if (node) node.textContent = value; };
  const invoiceAmountDetail = form.querySelector<HTMLElement>('[data-detail="invoice_amount"]')?.closest("div");
  const detailsGrid = form.querySelector<HTMLElement>(".readonly-payment-details");
  if (invoiceAmountDetail && detailsGrid) detailsGrid.prepend(invoiceAmountDetail);
  const relabel = (selector: string, label: string) => {
    const value = form.querySelector<HTMLElement>(selector);
    const labelNode = value?.parentElement?.querySelector<HTMLElement>("span");
    if (labelNode) labelNode.textContent = label;
  };
  relabel('[data-detail="paid"]', "Confirmed received");
  relabel('[data-summary="previous"]', "Confirmed received");
  relabel('[data-summary="current"]', "This payment (awaiting)");
  relabel('[data-summary="balance"]', "Current confirmed balance");
  relabel('[data-summary="credit"]', "Current customer credit");
  const summaryHeading = form.querySelector<HTMLElement>(".payment-summary-section h2");
  if (summaryHeading) summaryHeading.textContent = "Confirmation impact";
  const statusSummary = form.querySelector<HTMLElement>('[data-summary="status"]')?.closest("div");
  if (statusSummary) {
    const projected = document.createElement("div");
    projected.innerHTML = '<span>Balance if confirmed</span><strong data-summary="projected">EUR 0.00</strong>';
    statusSummary.before(projected);
  }
  const eligibleOrders = (customerIdValue: string) => orders.filter((row) => {
    const ownsOrder = String(row.customer_id || row.customer_company_id || row.company_id) === customerIdValue;
    const isExistingOrder = Boolean(existing && String(row._id) === String(existing.order_id || existing.oc_id));
    const remaining = Number(row.remaining_balance ?? bankingAmount(row));
    return ownsOrder && (isExistingOrder || remaining > 0);
  });
  const orderLabel = (row: BankingRecord) => { const references = [row.invoice_number, row.quotation_number, row.quotation_id, row.customer_reference].map((value) => String(value || "").trim()).filter((value, index, values) => value && values.indexOf(value) === index).join(" · ") || "No reference"; return bankingOrderName(row) + " · " + references + " · " + formatMoney(bankingAmount(row), "EUR") + " · " + formatDate(String(row.oc_date || row.created_at || "")); };
  const updateOrderOptions = () => { const rows = eligibleOrders(customerId.value); orderList.innerHTML = rows.map((row) => "<option value=\"" + escapeHtml(orderLabel(row)) + "\"></option>").join(""); orderInput.disabled = !customerId.value; orderInput.placeholder = rows.length ? "Search invoice or Order Confirmation" : "No invoices available for this customer"; };
  const calculate = () => {
    const invoice = bankingAmount(selectedOrder);
    const confirmed = orderPayments
      .filter((row) => String(row.status || "").toUpperCase() === "CONFIRMED")
      .reduce((sum, row) => sum + Number(row.amount || row.payment_amount || 0), 0);
    const current = Number(amountInput.value || 0);
    const confirmedBalance = Math.max(invoice - confirmed, 0);
    const projectedBalance = Math.max(invoice - confirmed - current, 0);
    const confirmedCredit = Math.max(confirmed - invoice, 0);
    const unavailable = Boolean(existing && !selectedOrder);
    const state = unavailable ? "Invoice information unavailable" : !selectedOrder ? "Select an invoice" : bankingStatus(existing || {});
    const moneyOrUnavailable = (value: number) => unavailable ? "Invoice information unavailable" : formatMoney(value, "EUR");
    setOutput("paid", moneyOrUnavailable(confirmed));
    setOutput("outstanding", moneyOrUnavailable(confirmedBalance));
    setSummary("invoice", moneyOrUnavailable(invoice));
    setSummary("previous", moneyOrUnavailable(confirmed));
    setSummary("current", moneyOrUnavailable(current));
    setSummary("balance", moneyOrUnavailable(confirmedBalance));
    setSummary("projected", moneyOrUnavailable(projectedBalance));
    setSummary("credit", moneyOrUnavailable(confirmedCredit));
    setSummary("status", state);
  };
  const loadOrder = async (row: BankingRecord | undefined) => { selectedOrder = row; orderPayments = []; if (!row) { orderId.value = ""; details.hidden = true; calculate(); return; } orderId.value = String(row._id || ""); selectedCustomer = customers.find((candidate) => String(candidate._id) === String(row.customer_id || row.customer_company_id || row.company_id)) || selectedCustomer; customerInput.value = bankingName(selectedCustomer); customerId.value = String(selectedCustomer && selectedCustomer._id || row.customer_id || ""); orderPayments = (await financeApi.payments("order_id=" + encodeURIComponent(String(row._id)))).items; details.hidden = false; setOutput("customer", bankingName(selectedCustomer)); setOutput("invoice", String(row.invoice_number || row.quotation_number || bankingOrderName(row))); setOutput("oc", bankingOrderName(row)); setOutput("invoice_date", formatDate(String(row.oc_date || row.created_at || ""))); setOutput("due_date", formatDate(String(row.due_date || "—"))); setOutput("payment_terms", String(row.payment_terms || "—")); setOutput("salesperson", String((row.salesperson_snapshot as BankingRecord | undefined)?.name || row.salesperson_id || "—")); setOutput("currency", String(row.currency || "EUR")); setOutput("invoice_amount", formatMoney(bankingAmount(row), "EUR")); const paid = orderPayments.filter((item) => String(item._id) !== String(existing && existing._id) && String(item.status || "").toUpperCase() !== "REJECTED").reduce((sum, item) => sum + Number(item.amount || 0), 0); setOutput("paid", formatMoney(paid, "EUR")); setOutput("outstanding", formatMoney(Math.max(bankingAmount(row) - paid, 0), "EUR")); calculate(); };
  const findCustomer = () => customers.find((row) => bankingName(row).toLowerCase() === customerInput.value.trim().toLowerCase() || String(row._id) === customerInput.value.trim());
  const findOrder = () => eligibleOrders(customerId.value).find((row) => orderLabel(row).toLowerCase() === orderInput.value.trim().toLowerCase() || bankingOrderName(row).toLowerCase() === orderInput.value.trim().toLowerCase() || String(row._id) === orderInput.value.trim());
  const syncCustomer = () => { selectedCustomer = findCustomer(); customerId.value = String(selectedCustomer && selectedCustomer._id || ""); setError("customer_id", selectedCustomer ? "" : "Select an eligible customer"); updateOrderOptions(); if (selectedOrder && String(selectedOrder.customer_id || selectedOrder.customer_company_id || selectedOrder.company_id) !== customerId.value) void loadOrder(undefined); };
  customerInput.addEventListener("input", syncCustomer); customerInput.addEventListener("change", syncCustomer); orderInput.addEventListener("change", () => { const row = findOrder(); setError("order_id", row ? "" : "Select an eligible invoice or Order Confirmation"); void loadOrder(row); }); amountInput.addEventListener("input", calculate);
  form.querySelector<HTMLInputElement>('input[name="payment_date"]')!.value = formatDateInput();
  const proofInput = form.querySelector<HTMLInputElement>('input[name="attachment"]')!;
  const proofZone = form.querySelector<HTMLElement>("[data-proof-zone]")!;
  const chooseProof = form.querySelector<HTMLButtonElement>("[data-choose-proof]")!;
  const removeProof = form.querySelector<HTMLButtonElement>("[data-remove-file]")!;
  const proofLabel = form.querySelector<HTMLElement>("[data-file]");
  if (attachment?.name) {
    if (proofLabel) proofLabel.textContent = String(attachment.name);
    chooseProof.textContent = "Replace";
    removeProof.hidden = false;
  }
  chooseProof.addEventListener("click", (event) => { event.preventDefault(); proofInput.click(); });
  proofZone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); proofInput.click(); } });
  proofInput.addEventListener("change", async (event) => { const file = (event.target as HTMLInputElement).files?.[0]; if (!file) return; try { attachment = await readPaymentProof(file); const node = form.querySelector<HTMLElement>("[data-file]"); if (node) node.textContent = file.name + " · " + (file.size / 1024).toFixed(1) + " KB"; chooseProof.textContent = "Replace"; removeProof.hidden = false; } catch (error) { const node = form.querySelector<HTMLElement>("[data-form-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Payment proof could not be read"; proofInput.value = ""; } });
  removeProof.addEventListener("click", () => { attachment = undefined; proofInput.value = ""; removeProof.hidden = true; chooseProof.textContent = "Choose file"; const node = form.querySelector<HTMLElement>("[data-file]"); if (node) node.textContent = "Required"; setError("attachment", "Select a payment proof"); });
  form.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (readOnly) return;
    syncCustomer();
    const row = findOrder();
    if (!selectedCustomer) setError("customer_id", "Select an eligible customer");
    if (!row) setError("order_id", "Select an eligible unpaid or partially paid invoice / Order Confirmation");
    if (!attachment) setError("attachment", "Select a payment proof");
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    if (!selectedCustomer || !row || !attachment) return;
    const data = new FormData(form);
    const payload = {
      workflow: "banking", customer_id: selectedCustomer._id, order_id: row._id,
      amount: Number(data.get("amount")), payment_date: data.get("payment_date"),
      payment_mode: data.get("payment_mode"), bank_name: data.get("bank_name"),
      bank_account: data.get("bank_account"), utr: data.get("utr"),
      reference_number: data.get("reference_number"), notes: data.get("notes"),
      ...(proofInput.files?.length ? { attachment } : {}), currency: "EUR",
    };
    const submit = form.querySelector<HTMLButtonElement>("[data-submit]")!;
    submit.disabled = true;
    try {
      const payment = existing
        ? await financeApi.updatePayment(String(existing._id), payload)
        : await financeApi.createPayment({ ...payload, attachment });
      dialog.close();
      showPaymentRecorded(payment, selectedCustomer, row, Number(payment.remaining_balance ?? bankingAmount(row)));
    } catch (error) {
      const node = form.querySelector<HTMLElement>("[data-form-error]");
      if (node) node.textContent = error instanceof Error ? error.message : "Payment could not be recorded";
      submit.disabled = false;
    }
  });
  if (selectedCustomer) { customerInput.value = bankingName(selectedCustomer); customerId.value = String(selectedCustomer._id || ""); updateOrderOptions(); }
  if (selectedOrder) { orderInput.value = orderLabel(selectedOrder); await loadOrder(selectedOrder); }
  else if (existing) { details.hidden = false; setOutput("invoice", "Invoice information unavailable"); setOutput("oc", "Invoice information unavailable"); setOutput("invoice_amount", "Invoice information unavailable"); setOutput("paid", "Invoice information unavailable"); setOutput("outstanding", "Invoice information unavailable"); }
  if (existing) ["amount", "payment_date", "payment_mode", "bank_name", "bank_account", "utr", "reference_number", "notes"].forEach((name) => { const field = form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null; if (field && existing[name] != null) field.value = String(existing[name]); });
  calculate();
  if (readOnly) {
    form.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement>("input, select, textarea").forEach((field) => { field.disabled = true; });
    form.querySelector<HTMLButtonElement>("[data-submit]")?.remove();
    const cancel = form.querySelector<HTMLButtonElement>("[data-cancel]"); if (cancel) cancel.textContent = "Close";
    const firstHeading = form.querySelector<HTMLElement>(".payment-workflow-section h2"); if (firstHeading) firstHeading.textContent = "Associated customer and invoice";
    chooseProof.remove(); proofZone.remove();
    const proofFile = form.querySelector<HTMLElement>("[data-file]");
    const proof = existing?.attachment as BankingRecord | undefined;
    if (proofFile) proofFile.textContent = proof?.name ? String(proof.name) : "No payment proof attached";
    if (proof?.name && existing?._id) {
      const viewProof = document.createElement("button"); viewProof.type = "button"; viewProof.className = "button button-quiet"; viewProof.textContent = "View proof";
      viewProof.addEventListener("click", async () => { const opened = window.open("about:blank", "_blank", "noopener"); try { const result = await financeApi.paymentProof(String(existing._id)); if (opened) opened.location.href = result.data; else toast("Allow pop-ups to view payment proof", "error"); } catch (error) { opened?.close(); toast(error instanceof Error ? error.message : "Payment proof could not be opened", "error"); } });
      proofFile?.after(viewProof);
    }
  if (existing && appStore.can("payments.manage") && bankingStatus(existing).toUpperCase() !== "CONFIRMED") {
      const edit = document.createElement("button"); edit.type = "button"; edit.className = "button button-secondary"; edit.textContent = "Edit Payment";
      edit.addEventListener("click", () => { dialog.close(); void openBankingPayment(customers, orders, existing, false); });
      form.querySelector("[data-cancel]")?.after(edit);
    }
  }
  refreshIcons(content);
}

function showPaymentRecorded(payment: BankingRecord, customer: BankingRecord | undefined, order: BankingRecord | undefined, balance: number): void {
  const content = document.createElement("div");
  content.innerHTML = "<div class=\"stack-form\"><section class=\"panel\"><span class=\"eyebrow\">Payment Recorded</span><div class=\"detail-grid\"><div><span>Payment ID</span><strong>" + escapeHtml(bankingPaymentId(payment)) + "</strong></div><div><span>Customer</span><strong>" + escapeHtml(bankingName(customer)) + "</strong></div><div><span>Invoice / OC</span><strong>" + escapeHtml(bankingOrderName(order)) + "</strong></div><div><span>Payment</span><strong>" + formatMoney(Number(payment.amount || 0), "EUR") + "</strong></div><div><span>Remaining</span><strong>" + formatMoney(balance, "EUR") + "</strong></div><div><span>Status</span><strong>" + statusBadge(bankingStatus(payment)) + "</strong></div></div></section><div class=\"modal-actions\"><button class=\"button button-quiet\" type=\"button\" data-payment-back>Back to Payments</button><button class=\"button button-primary\" type=\"button\" data-payment-view>View Payment</button></div></div>";
  const dialog = openModal("Payment Recorded", content, "wide");
  const back = () => { dialog.close(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/payments" })); };
  content.querySelector("[data-payment-back]")?.addEventListener("click", back); content.querySelector("[data-payment-view]")?.addEventListener("click", back); refreshIcons(content);
}

export async function legacyOperationalPaymentsPage(): Promise<HTMLElement> {
  const canCreatePayment = appStore.can("payments.create") || appStore.can("payments.manage");
  const page = pageScaffold("Finance", "Payments / Banking", "Track customer payments, outstanding balances and payment history.", canCreatePayment ? '<button class="button button-primary" type="button" data-new-payment><i data-lucide="plus"></i>New Payment</button>' : "");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  try {
    const paymentQuery = new URLSearchParams(window.location.search);
    paymentQuery.set("limit", "500");
    const results = await Promise.all([financeApi.payments(paymentQuery.toString()), orderApi.list(undefined, 100)]);
    const items = results[0].items as BankingRecord[]; const orders = results[1].items as BankingRecord[];
    const received = items.filter((item) => String(item.workflow_status || item.status || "").toUpperCase() === "CONFIRMED").reduce((sum, item) => sum + Number(item.amount || 0), 0);
    const pending = items.filter((item) => String(item.workflow_status || item.status || "").toUpperCase() !== "CONFIRMED").length;
    const byOrder = new Map<string, BankingRecord[]>(); items.forEach((item) => { const key = String(item.order_id || item.oc_id || ""); byOrder.set(key, [...(byOrder.get(key) || []), item]); });
    let outstanding = 0; let credits = 0;
    orders.forEach((order) => { const rows = byOrder.get(String(order._id)) || []; const paid = rows.filter((row) => String(row.workflow_status || row.status || "").toUpperCase() !== "REJECTED").reduce((sum, row) => sum + Number(row.amount || 0), 0); const invoice = bankingAmount(order); outstanding += Math.max(invoice - paid, 0); credits += Math.max(paid - invoice, 0); });
    const cards = [["Total payments", String(items.length), "landmark", "Payment records"], ["Total received", formatMoney(received, "EUR"), "circle-check", "Confirmed receipts"], ["Pending / under review", String(pending), "clock-3", "Awaiting confirmation"], ["Outstanding", formatMoney(outstanding, "EUR"), "wallet-cards", "Customer credits " + formatMoney(credits, "EUR")]];
    const customerFilters = Array.from(new Map<string, { id: string; name: string }>(items.map((item): [string, { id: string; name: string }] => { const snapshot = item.customer_snapshot as BankingRecord | undefined; const id = String(item.customer_id || ""); return [id, { id, name: bankingName(snapshot) || id }]; }).filter(([id]) => Boolean(id))).values());
    const statusFilters = [...new Set(items.map((item) => String(item.workflow_status || item.status || "").trim()).filter(Boolean))];
    const modeFilters = [...new Set(items.map((item) => String(item.payment_mode || "").trim()).filter(Boolean))];
    body.innerHTML = "<div class=\"metric-grid compact-metrics banking-summary\">" + cards.map((card) => "<article class=\"metric-card\"><div class=\"metric-top\"><span>" + card[0] + "</span><i data-lucide=\"" + card[2] + "\"></i></div><strong>" + card[1] + "</strong><p>" + card[3] + "</p></article>").join("") + "</div><section class=\"panel banking-filters\"><div class=\"form-grid\"><label class=\"span-2\">Search payments<input data-payment-filter=\"search\" placeholder=\"Payment ID, customer, invoice, UTR or reference\"></label><label>Customer<select data-payment-filter=\"customer\"><option value=\"\">All customers</option>" + customerFilters.map((customer) => "<option value=\"" + escapeHtml(customer.id) + "\">" + escapeHtml(customer.name) + "</option>").join("") + "</select></label><label>Status<select data-payment-filter=\"status\"><option value=\"\">All statuses</option>" + statusFilters.map((status) => "<option value=\"" + escapeHtml(status) + "\">" + escapeHtml(status) + "</option>").join("") + "</select></label><label>From date<input type=\"date\" data-payment-filter=\"from\"></label><label>To date<input type=\"date\" data-payment-filter=\"to\"></label><label>Payment mode<select data-payment-filter=\"mode\"><option value=\"\">All modes</option>" + modeFilters.map((mode) => "<option value=\"" + escapeHtml(mode) + "\">" + escapeHtml(mode) + "</option>").join("") + "</select></label></div></section><div data-payment-table></div>";
    const tableHost = body.querySelector<HTMLElement>("[data-payment-table]")!;
    const contextPromise = bankingContexts();
    const bindRowActions = () => { tableHost.querySelectorAll<HTMLButtonElement>("[data-payment-view], [data-payment-edit]").forEach((button) => button.addEventListener("click", async () => { const id = button.dataset.paymentView || button.dataset.paymentEdit; const record = items.find((item) => String(item._id) === String(id)); if (!record) return; try { const context = await contextPromise; await openBankingPayment(context.customers, context.orders, record, Boolean(button.dataset.paymentView)); } catch (error) { toast(error instanceof Error ? error.message : "Payment could not be opened", "error"); } })); };
    const applyFilters = () => { const read = (name: string) => body.querySelector<HTMLInputElement | HTMLSelectElement>("[data-payment-filter=\"" + name + "\"]")?.value.trim() || ""; const search = read("search").toLowerCase(); const customer = read("customer"); const status = read("status"); const from = read("from"); const to = read("to"); const mode = read("mode"); const visible = items.filter((item) => { const order = orders.find((row) => String(row._id) === String(item.order_id || item.oc_id)); const snapshot = item.customer_snapshot as BankingRecord | undefined; const searchable = [bankingPaymentId(item), bankingName(snapshot), bankingOrderName(order), item.invoice_number, item.quotation_number, item.customer_reference, item.utr, item.reference_number].map((value) => String(value || "").toLowerCase()).join(" "); const date = String(item.payment_date || item.created_at || "").slice(0, 10); return (!search || searchable.includes(search)) && (!customer || String(item.customer_id || "") === customer) && (!status || String(item.workflow_status || item.status || "") === status) && (!mode || String(item.payment_mode || "") === mode) && (!from || date >= from) && (!to || date <= to); }); const rows = visible.map((item, index) => { const order = orders.find((row) => String(row._id) === String(item.order_id || item.oc_id)); const customer = item.customer_snapshot as BankingRecord | undefined; const status = bankingStatus(item); const editable = canCreatePayment && String(item.workflow_status || item.status || "").toUpperCase() !== "CONFIRMED"; return "<tr><td><strong>" + escapeHtml(bankingPaymentId(item, index)) + "</strong></td><td>" + escapeHtml(bankingName(customer)) + "</td><td>" + escapeHtml(bankingOrderName(order) || String(item.order_number || item.order_id || "—")) + "</td><td>" + formatDate(String(item.payment_date || item.created_at || "")) + "</td><td class=\"money\">" + formatMoney(Number(item.invoice_amount || bankingAmount(order)), "EUR") + "</td><td class=\"money\">" + formatMoney(Number(item.amount || 0), "EUR") + "</td><td class=\"money\">" + formatMoney(Number(item.balance || item.remaining_balance || 0), "EUR") + "</td><td>" + statusBadge(status) + "</td><td>" + escapeHtml(String(item.payment_mode || "—")) + "</td><td>" + escapeHtml(String(item.utr || item.reference_number || "—")) + "</td><td><div class=\"table-actions\"><button class=\"button button-quiet\" type=\"button\" data-payment-view=\"" + escapeHtml(String(item._id)) + "\">View</button>" + (editable ? "<button class=\"button button-secondary\" type=\"button\" data-payment-edit=\"" + escapeHtml(String(item._id)) + "\">Edit</button>" : "") + "</div></td></tr>"; }).join(""); tableHost.innerHTML = visible.length ? "<div class=\"data-table panel banking-table\"><table><thead><tr><th>Payment ID</th><th>Customer</th><th>Invoice / OC</th><th>Payment date</th><th>Invoice amount</th><th>Payment amount</th><th>Balance</th><th>Status</th><th>Payment mode</th><th>UTR / reference</th><th>Actions</th></tr></thead><tbody>" + rows + "</tbody></table></div>" : emptyState("landmark", "No matching payments", "Adjust the filters or record a new payment."); bindRowActions(); refreshIcons(tableHost); };
    body.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-payment-filter]").forEach((control) => control.addEventListener("input", applyFilters));
    body.querySelectorAll<HTMLSelectElement>("[data-payment-filter]").forEach((control) => control.addEventListener("change", applyFilters));
    applyFilters();
    page.querySelector<HTMLButtonElement>("[data-new-payment]")?.addEventListener("click", async (event) => { const button = event.currentTarget as HTMLButtonElement; button.disabled = true; try { const context = await contextPromise; await openBankingPayment(context.customers, context.orders); } catch (error) { toast(error instanceof Error ? error.message : "Payment form could not be opened", "error"); } finally { button.disabled = false; } });
  } catch (error) { body.innerHTML = "<div class=\"notice error\">" + escapeHtml(error instanceof Error ? error.message : "Payments unavailable") + "</div>"; }
  refreshIcons(page);
  return page;
}

function isAwaitingBankReview(payment: BankingRecord): boolean {
  return ["AWAITING SUPERADMIN CONFIRMATION", "AWAITING BANK CONFIRMATION"].includes(bankingStatus(payment).toUpperCase());
}

async function rejectBankingPayment(payment: BankingRecord): Promise<void> {
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form" data-reject-payment><p>Reject ${escapeHtml(bankingPaymentId(payment))}. Rejected payments do not change the confirmed balance or customer credit.</p><label>Reason *<textarea name="reason" rows="4" required></textarea></label><small class="field-error" data-reject-error></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-danger" type="submit">Reject Payment</button></div></form>`;
  const dialog = openModal("Reject Payment", content, "wide");
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  content.querySelector<HTMLFormElement>("[data-reject-payment]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    if (!form.reportValidity()) return;
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = true;
    try {
      await financeApi.rejectPayment(String(payment._id), String(new FormData(form).get("reason") || ""));
      dialog.close();
      toast("Payment rejected; confirmed totals are unchanged", "info");
      window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/payments" }));
    } catch (error) {
      const node = form.querySelector<HTMLElement>("[data-reject-error]");
      if (node) node.textContent = error instanceof Error ? error.message : "Payment could not be rejected";
      button.disabled = false;
    }
  });
}

export async function paymentsPage(): Promise<HTMLElement> {
  const canCreatePayment = appStore.can("payments.create") || appStore.can("payments.manage");
  const canReviewPayment = appStore.state.user?.role_id === "superadmin";
  const page = pageScaffold(
    "Finance",
    "Payments / Banking",
    "Track customer payments, outstanding balances and payment history.",
    canCreatePayment ? '<button class="button button-primary" type="button" data-new-payment><i data-lucide="plus"></i>New Payment</button>' : "",
  );
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  try {
    const routeQuery = new URLSearchParams(window.location.search);
    const paymentQuery = new URLSearchParams();
    paymentQuery.set("limit", "500");
    if (routeQuery.get("order_id")) paymentQuery.set("order_id", String(routeQuery.get("order_id")));
    const [paymentResult, orderResult] = await Promise.all([
      financeApi.payments(paymentQuery.toString()),
      orderApi.list(undefined, 100),
    ]);
    const items = paymentResult.items as BankingRecord[];
    const orders = orderResult.items as BankingRecord[];
    const contexts = { customers: (await customerCompanyApi.list()).items as unknown as BankingRecord[], orders };
    const confirmedItems = items.filter((item) => bankingStatus(item).toUpperCase() === "CONFIRMED");
    const awaitingItems = items.filter(isAwaitingBankReview);
    const received = confirmedItems.reduce((sum, item) => sum + Number(item.amount || item.payment_amount || 0), 0);
    const orderRollups = new Map<string, { invoice: number; confirmed: number }>();
    orders.forEach((order) => orderRollups.set(String(order._id), { invoice: bankingAmount(order), confirmed: Number(order.confirmed_received || 0) }));
    const outstanding = [...orderRollups.values()].reduce((sum, row) => sum + Math.max(row.invoice - row.confirmed, 0), 0);
    const credits = [...orderRollups.values()].reduce((sum, row) => sum + Math.max(row.confirmed - row.invoice, 0), 0);
    const cards = [
      ["Total payments", String(items.length), "landmark", "Payment records"],
      ["Confirmed received", formatMoney(received, "EUR"), "circle-check", "Confirmed receipts only"],
      ["Awaiting confirmation", String(awaitingItems.length), "clock-3", "Superadmin review queue"],
      ["Outstanding", formatMoney(outstanding, "EUR"), "wallet-cards", `Customer credits ${formatMoney(credits, "EUR")}`],
    ];
    const customerFilters = Array.from(new Map<string, { id: string; name: string }>(items.map((item): [string, { id: string; name: string }] => {
      const id = String(item.customer_id || "");
      return [id, { id, name: bankingName(item.customer_snapshot as BankingRecord | undefined) || id }];
    }).filter(([id]) => Boolean(id))).values());
    const workflowStatuses = [...new Set(items.map(bankingStatus).filter(Boolean))];
    const invoiceStatuses = [...new Set(items.map(bankingInvoiceStatus).filter(Boolean))];
    const modeFilters = [...new Set(items.map((item) => String(item.payment_mode || "").trim()).filter(Boolean))];
    body.innerHTML = `<div class="metric-grid compact-metrics banking-summary">${cards.map((card) => `<article class="metric-card"><div class="metric-top"><span>${card[0]}</span><i data-lucide="${card[2]}"></i></div><strong>${card[1]}</strong><p>${card[3]}</p></article>`).join("")}</div>
      <section class="panel banking-filters"><div class="form-grid"><label class="span-2">Search payments<input data-payment-filter="search" placeholder="Payment ID, customer, invoice, UTR or reference"></label><label>Customer<select data-payment-filter="customer"><option value="">All customers</option>${customerFilters.map((customer) => `<option value="${escapeHtml(customer.id)}">${escapeHtml(customer.name)}</option>`).join("")}</select></label><label>Status<select data-payment-filter="status"><option value="">All statuses</option>${[...workflowStatuses, ...invoiceStatuses.filter((value) => !workflowStatuses.includes(value))].map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(status.replaceAll("_", " "))}</option>`).join("")}</select></label><label>From date<input type="date" data-payment-filter="from"></label><label>To date<input type="date" data-payment-filter="to"></label><label>Payment mode<select data-payment-filter="mode"><option value="">All modes</option>${modeFilters.map((mode) => `<option value="${escapeHtml(mode)}">${escapeHtml(mode)}</option>`).join("")}</select></label></div></section><div data-payment-table></div>`;
    const tableHost = body.querySelector<HTMLElement>("[data-payment-table]")!;
    const render = () => {
      const value = (name: string) => body.querySelector<HTMLInputElement | HTMLSelectElement>(`[data-payment-filter="${name}"]`)?.value.trim() || "";
      const search = value("search").toLowerCase();
      const customerFilter = value("customer");
      const statusFilter = value("status");
      const from = value("from");
      const to = value("to");
      const mode = value("mode");
      const visible = items.filter((item) => {
        const order = orders.find((row) => String(row._id) === String(item.order_id || item.oc_id));
        const searchable = [bankingPaymentId(item), bankingName(item.customer_snapshot as BankingRecord | undefined), bankingOrderName(order), item.invoice_number, item.quotation_number, item.utr, item.reference_number].map((entry) => String(entry || "").toLowerCase()).join(" ");
        const date = formatDateInput(item.payment_date || item.created_at);
        const statuses = [bankingStatus(item), bankingInvoiceStatus(item)];
        return (!search || searchable.includes(search))
          && (!customerFilter || String(item.customer_id || "") === customerFilter)
          && (!statusFilter || statuses.includes(statusFilter))
          && (!mode || String(item.payment_mode || "") === mode)
          && (!from || Boolean(date && date >= from))
          && (!to || Boolean(date && date <= to));
      });
      const rows = visible.map((item, index) => {
        const order = orders.find((row) => String(row._id) === String(item.order_id || item.oc_id));
        const workflowStatus = bankingStatus(item);
        const invoiceStatus = bankingInvoiceStatus(item);
        const editable = canCreatePayment && !["CONFIRMED"].includes(workflowStatus.toUpperCase());
        const reviewActions = canReviewPayment && isAwaitingBankReview(item)
          ? `<button class="button button-secondary" type="button" data-payment-confirm="${escapeHtml(String(item._id))}">Confirm</button><button class="button button-danger" type="button" data-payment-reject="${escapeHtml(String(item._id))}">Reject</button>`
          : "";
        const status = `${statusBadge(workflowStatus)}${invoiceStatus !== workflowStatus ? `<small class="payment-invoice-status">Invoice: ${escapeHtml(invoiceStatus.replaceAll("_", " "))}</small>` : ""}`;
        return `<tr><td><strong>${escapeHtml(bankingPaymentId(item, index))}</strong></td><td>${escapeHtml(bankingName(item.customer_snapshot as BankingRecord | undefined))}</td><td>${escapeHtml(bankingOrderName(order) || String(item.order_number || item.order_id || "\u2014"))}</td><td>${formatDate(item.payment_date || item.created_at)}</td><td class="money">${formatMoney(Number(item.invoice_amount || bankingAmount(order)), "EUR")}</td><td class="money">${formatMoney(Number(item.amount || 0), "EUR")}</td><td class="money">${formatMoney(Number(item.remaining_balance ?? item.balance ?? bankingAmount(order)), "EUR")}</td><td><div class="payment-status-stack">${status}</div></td><td>${escapeHtml(String(item.payment_mode || "\u2014"))}</td><td>${escapeHtml(String(item.utr || item.reference_number || "\u2014"))}</td><td><div class="table-actions"><button class="button button-quiet" type="button" data-payment-view="${escapeHtml(String(item._id))}">View</button>${editable ? `<button class="button button-secondary" type="button" data-payment-edit="${escapeHtml(String(item._id))}">Edit</button>` : ""}${reviewActions}</div></td></tr>`;
      }).join("");
      tableHost.innerHTML = visible.length
        ? `<div class="data-table panel banking-table"><table><thead><tr><th>Payment ID</th><th>Customer</th><th>Invoice / OC</th><th>Payment date</th><th>Invoice amount</th><th>Payment amount</th><th>Balance</th><th>Status</th><th>Payment mode</th><th>UTR / reference</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : emptyState("landmark", "No matching payments", "Adjust the filters or record a new payment.");
      tableHost.querySelectorAll<HTMLButtonElement>("[data-payment-view], [data-payment-edit]").forEach((button) => button.addEventListener("click", () => {
        const id = button.dataset.paymentView || button.dataset.paymentEdit;
        const record = items.find((item) => String(item._id) === String(id));
        if (record) void openBankingPayment(contexts.customers, contexts.orders, record, Boolean(button.dataset.paymentView));
      }));
      tableHost.querySelectorAll<HTMLButtonElement>("[data-payment-confirm]").forEach((button) => button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await financeApi.confirmPayment(String(button.dataset.paymentConfirm));
          toast("Payment confirmed; the invoice balance was recalculated from confirmed receipts");
          window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/payments" }));
        } catch (error) {
          toast(error instanceof Error ? error.message : "Payment could not be confirmed", "error");
          button.disabled = false;
        }
      }));
      tableHost.querySelectorAll<HTMLButtonElement>("[data-payment-reject]").forEach((button) => button.addEventListener("click", () => {
        const record = items.find((item) => String(item._id) === String(button.dataset.paymentReject));
        if (record) void rejectBankingPayment(record);
      }));
      refreshIcons(tableHost);
    };
    body.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-payment-filter]").forEach((control) => control.addEventListener("input", render));
    body.querySelectorAll<HTMLSelectElement>("[data-payment-filter]").forEach((control) => control.addEventListener("change", render));
    render();
    page.querySelector<HTMLButtonElement>("[data-new-payment]")?.addEventListener("click", () => void openBankingPayment(contexts.customers, contexts.orders));
    const requestedOrderId = String(routeQuery.get("order_id") || "");
    if (requestedOrderId && routeQuery.get("new") === "1" && canCreatePayment) {
      queueMicrotask(() => void openBankingPayment(contexts.customers, contexts.orders, undefined, false, requestedOrderId));
    }
  } catch (error) {
    body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Payments unavailable")}</div>`;
  }
  refreshIcons(page);
  return page;
}

export async function incentivesPage(): Promise<HTMLElement> {
  const routePath = window.location.pathname.replace(/\/$/, "");
  const routeView = new URLSearchParams(window.location.search).get("view") || (routePath.endsWith("/rules") ? "rules" : routePath.endsWith("/payouts") ? "payouts" : "overview");
  const page = pageScaffold("Incentives", routeView === "rules" ? "Incentive Rules" : routeView === "payouts" ? "Incentive Payouts" : "Incentive Overview", routeView === "rules" ? "Configure incentive rates for users and managers based on customer type and product type." : "Track sales incentives generated from orders and activated after payment receipt.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  const requestedView = routeView;
  if (requestedView === "rules" && appStore.can("incentives.manage")) {
    try {
      await renderIncentiveConfigurator(page, body);
    } catch (error) {
      body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentive configuration unavailable")}</div>`;
      refreshIcons(page);
    }
    return page;
  }
  const canViewUsers = appStore.state.user?.permissions.includes("users.view") || appStore.state.user?.role_id === "superadmin";
  const canConfirmPayments = appStore.can("payments.confirm") || appStore.state.user?.role_id === "superadmin";
  let customers: Record<string, unknown>[] = [];
  let users: Record<string, unknown>[] = [];
  const renderSummary = (items: Record<string, unknown>[]) => {
    const pending = items.filter((item) => !["paid", "confirmed"].includes(String(item.payment_status ?? (item.payment_confirmation_date ? "Paid" : "Pending Payment")).toLowerCase())).length;
    const active = items.filter((item) => String(item.status ?? "").toUpperCase() === "ACTIVE").length;
    const due = items.filter((item) => ["DUE", "OVERDUE"].includes(String(item.status ?? "").toUpperCase())).length;
    const overdue = items.filter((item) => String(item.status ?? "").toUpperCase() === "OVERDUE").length;
    const paid = items.filter((item) => String(item.status ?? "").toUpperCase() === "PAID").length;
    const gross = items.reduce((sum, item) => sum + Number(item.gross_incentive_amount ?? 0), 0);
    const deductions = items.reduce((sum, item) => sum + Number(item.credit_note_deduction ?? 0), 0);
    const total = items.reduce((sum, item) => sum + Number(item.net_payable_incentive ?? item.gross_incentive_amount ?? 0), 0);
    return `<div class="metric-grid compact-metrics"><article class="metric-card"><div class="metric-top"><span>Total incentives</span><i data-lucide="badge-euro"></i></div><strong>${items.length}</strong><p>Order Confirmation snapshots</p></article><article class="metric-card"><div class="metric-top"><span>Pending payment</span><i data-lucide="clock-3"></i></div><strong>${pending}</strong><p>Awaiting receipt confirmation</p></article><article class="metric-card"><div class="metric-top"><span>Active</span><i data-lucide="circle-check"></i></div><strong>${active}</strong><p>Payment confirmed</p></article><article class="metric-card"><div class="metric-top"><span>Due / overdue</span><i data-lucide="calendar-clock"></i></div><strong>${due} / ${overdue}</strong><p>${paid} paid</p></article><article class="metric-card"><div class="metric-top"><span>Credit Note deductions</span><i data-lucide="file-minus"></i></div><strong>${formatMoney(deductions, "EUR")}</strong><p>Gross ${formatMoney(gross, "EUR")}</p></article><article class="metric-card"><div class="metric-top"><span>Net payable</span><i data-lucide="coins"></i></div><strong>${formatMoney(total, "EUR")}</strong><p>After deductions and payments</p></article></div>`;
  };
  const renderRows = (items: Record<string, unknown>[]) => items.length ? `<div class="data-table panel"><table><thead><tr><th>OC Number</th><th>Customer</th><th>Sales Person</th><th>Products / Categories</th><th>Order amount</th><th>Incentive %</th><th>Incentive amount</th><th>Payment status</th><th>Incentive status</th><th>Created</th><th>Actions</th></tr></thead><tbody>${items.map((item) => { const salesperson = item.salesperson_snapshot as Record<string, unknown> | undefined; const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {}; const status = String(item.status ?? "PENDING PAYMENT"); const paymentStatus = String(item.payment_status ?? (item.payment_confirmation_date ? "Paid" : "Pending Payment")); const lines = Array.isArray(item.incentive_lines) ? item.incentive_lines as Record<string, unknown>[] : []; const productSummary = lines.length ? lines.map((line) => `${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))} · ${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))} · ${Number(line.incentive_rate_snapshot ?? 0).toFixed(0)}% · ${formatMoney(Number(line.incentive_amount ?? 0), "EUR")}`).join("<br>") : "—"; const rateSummary = item.incentive_percentage_snapshot == null ? "By category" : `${Number(item.incentive_percentage_snapshot).toFixed(0)}%`; const action = paymentStatus.toLowerCase() === "paid" || !canConfirmPayments ? "—" : `<a class="button button-quiet" href="/payments" data-route="/payments">Confirm in Payments</a>`; return `<tr><td><a href="/orders/${encodeURIComponent(String(item.order_id ?? ""))}" data-route="/orders/${encodeURIComponent(String(item.order_id ?? ""))}"><strong>${escapeHtml(String(item.oc_number ?? item.order_number ?? item.order_id ?? "—"))}</strong></a></td><td>${escapeHtml(String(customer.company_name ?? customer.name ?? item.customer_id ?? "—"))}</td><td>${escapeHtml(String(salesperson?.name ?? item.salesperson_id ?? "—"))}</td><td>${productSummary}</td><td class="money">${formatMoney(Number(item.order_amount ?? 0), "EUR")}</td><td>${rateSummary}</td><td class="money">${formatMoney(Number(item.gross_incentive_amount ?? 0), "EUR")}</td><td>${statusBadge(paymentStatus)}</td><td>${statusBadge(status)}</td><td>${formatDate(String(item.created_at ?? ""))}</td><td>${action}</td></tr>`; }).join("")}</tbody></table></div>` : emptyState("badge-euro", "No incentives yet", "An incentive snapshot is created when an Order Confirmation is created.");
  const load = async () => {
    const params = new URLSearchParams();
    ["search", "salesperson_id", "customer_id", "category_id", "payment_status", "status", "from_date", "to_date", "order_id"].forEach((name) => { const value = body.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)?.value.trim(); if (value) params.set(name, value); });
    const result = await financeApi.incentives(params.toString());
    const table = body.querySelector<HTMLElement>("[data-incentive-results]"); if (table) table.innerHTML = renderRows(result.items);
    const summary = body.querySelector<HTMLElement>("[data-incentive-summary]"); if (summary) summary.innerHTML = renderSummary(result.items);
    const count = body.querySelector<HTMLElement>("[data-incentive-count]"); if (count) count.textContent = `${result.total} record${result.total === 1 ? "" : "s"}`;
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    refreshIcons(body);
  };
  try {
    const initialOrder = new URLSearchParams(window.location.search).get("order_id") ?? "";
    const [customerResult, userResult, productTypes] = await Promise.all([
      customerCompanyApi.list().catch(() => ({ items: [], total: 0 })),
      canViewUsers ? adminApi.users().catch(() => ({ items: [], total: 0 })) : Promise.resolve({ items: [], total: 0 }),
      catalogApi.families().catch(() => []),
    ]);
    customers = customerResult.items as unknown as Record<string, unknown>[];
    users = userResult.items;
    body.innerHTML = `<section class="panel quotation-filters incentive-filters"><div class="quotation-filter-head"><div><span class="eyebrow">Payment-linked earnings</span><h2>Incentive history</h2><p>Incentives remain pending until a payment receipt is confirmed.</p></div><span data-incentive-count>Loading…</span></div><div class="quotation-filter-grid"><label class="filter-search"><span>Search</span><div class="field-search"><i data-lucide="search"></i><input name="search" placeholder="Sales person, customer or order" aria-label="Search incentives"></div></label>${users.length ? `<label><span>Sales Person</span><select name="salesperson_id"><option value="">All salespeople</option>${users.map((user) => `<option value="${escapeHtml(String(user._id))}">${escapeHtml(String(user.name ?? user.email ?? user._id))}</option>`).join("")}</select></label>` : ""}<label><span>Customer</span><select name="customer_id"><option value="">All customers</option>${customers.map((customer) => `<option value="${escapeHtml(String(customer._id))}">${escapeHtml(String(customer.company_name ?? customer.name ?? customer._id))}</option>`).join("")}</select></label><label><span>Payment status</span><select name="payment_status"><option value="">All payment statuses</option><option value="pending">Pending Payment</option><option value="paid">Paid</option></select></label><label><span>Incentive status</span><select name="status"><option value="">All incentive statuses</option><option>PENDING PAYMENT</option><option>ACTIVE</option><option>DUE</option><option>OVERDUE</option><option>PAID</option></select></label><label><span>From date</span><input name="from_date" type="date"></label><label><span>To date</span><input name="to_date" type="date"></label><input name="order_id" type="hidden" value="${escapeHtml(initialOrder)}"></div></section><div data-incentive-summary></div><div data-incentive-results></div>`;
    const filterGrid = body.querySelector<HTMLElement>(".incentive-filters .quotation-filter-grid");
    if (filterGrid) filterGrid.insertAdjacentHTML("beforeend", `<label><span>Product Type</span><select name="category_id"><option value="">All Product Types</option>${productTypes.map((productType) => `<option value="${escapeHtml(productType.id)}">${escapeHtml(productType.name)}</option>`).join("")}</select></label><label><span>Product ID</span><input name="product_id" placeholder="Product ID"></label>`);
    ["payment_status", "status", "salesperson_id", "customer_id", "category_id", "product_id", "from_date", "to_date"].forEach((name) => body.querySelector(`[name="${name}"]`)?.addEventListener("change", () => { void load().catch((error) => { const results = body.querySelector<HTMLElement>("[data-incentive-results]"); if (results) results.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }); }));
    let searchTimer: number | undefined; body.querySelector<HTMLInputElement>('[name="search"]')?.addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => { void load().catch((error) => { const results = body.querySelector<HTMLElement>("[data-incentive-results]"); if (results) results.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }); }, 250); });
    await load();
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }
  refreshIcons(page); return page;
}

export async function legacyCreditNotesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Credit Notes", "Credit Notes linked to Order Confirmations and incentive adjustments.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.creditNotes();
    body.innerHTML = `<form class="panel stack-form" id="credit-note-entry"><span class="eyebrow">Create Credit Note</span><div class="form-grid"><label>Order Confirmation ID<input name="order_id" required></label><label>Amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Credit Note date<input name="credit_note_date" type="date" required></label><label>Reason<input name="reason" required></label></div><button class="button button-primary" type="submit">Create Credit Note</button></form>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Credit Note</th><th>Order Confirmation</th><th>Amount</th><th>Incentive deduction</th><th>Date</th><th>Status</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(String(item.credit_note_number ?? item._id))}</td><td>${escapeHtml(String(item.order_id ?? item.oc_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.incentive_deduction_amount ?? 0), "EUR")}</td><td>${formatDate(String(item.credit_note_date ?? item.created_at ?? ""))}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("file-minus", "No Credit Notes", "Approved Credit Notes will be linked to their Order Confirmation.")}`;
    body.querySelector<HTMLFormElement>("#credit-note-entry")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); try { await financeApi.createCreditNote({ order_id: data.get("order_id"), amount: Number(data.get("amount")), credit_note_date: data.get("credit_note_date"), reason: data.get("reason") }); toast("Credit Note created", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Credit Note could not be created", "error"); } });
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Credit Notes unavailable")}</div>`; }
  refreshIcons(page); return page;
}

export async function creditNotesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Credit Notes", "Credit Notes linked to Order Confirmations and incentive adjustments.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.creditNotes();
    body.innerHTML = `<form class="panel stack-form" id="credit-note-entry"><span class="eyebrow">Create Credit Note</span><p class="form-hint">Select the affected OC product line so the historical category incentive rate is used.</p><div class="form-grid"><label>Order Confirmation ID<input name="order_id" required></label><label>Credit Note date<input name="credit_note_date" type="date" required></label><label class="span-2">Reason<input name="reason" required></label></div><div class="credit-note-lines" data-credit-note-lines><span class="muted">Enter an Order Confirmation ID to load its product lines.</span></div><button class="button button-primary" type="submit">Create Credit Note</button></form>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Credit Note</th><th>Order Confirmation</th><th>Amount</th><th>Incentive deduction</th><th>Date</th><th>Status</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(String(item.credit_note_number ?? item._id))}</td><td>${escapeHtml(String(item.order_id ?? item.oc_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.incentive_deduction_amount ?? 0), "EUR")}</td><td>${formatDate(String(item.credit_note_date ?? item.created_at ?? ""))}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("file-minus", "No Credit Notes", "Approved Credit Notes will be linked to their Order Confirmation.")}`;
    const lineContainer = body.querySelector<HTMLElement>("[data-credit-note-lines]")!;
    const loadLines = async () => {
      const orderId = body.querySelector<HTMLInputElement>('[name="order_id"]')?.value.trim() ?? "";
      if (!orderId) { lineContainer.innerHTML = '<span class="muted">Enter an Order Confirmation ID to load its product lines.</span>'; return; }
      try {
        const result = await financeApi.incentives(`order_id=${encodeURIComponent(orderId)}`);
        const incentive = result.items[0] ?? {};
        const lines = Array.isArray(incentive.incentive_lines) ? incentive.incentive_lines as Record<string, unknown>[] : [];
        lineContainer.innerHTML = lines.length ? `<span class="form-hint">Credit Note amount by product (EUR)</span>${lines.map((line, index) => `<label>${escapeHtml(String(line.product_name ?? line.product_id ?? `Product ${index + 1}`))} · ${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))}<input data-credit-line="${escapeHtml(String(line.oc_line_id ?? line.product_id ?? index))}" type="number" min="0" step="0.01" placeholder="0.00"></label>`).join("")}` : '<span class="muted">This OC has no product-level incentive lines.</span>';
      } catch { lineContainer.innerHTML = '<span class="muted">Order Confirmation not found or not accessible.</span>'; }
    };
    body.querySelector<HTMLInputElement>('[name="order_id"]')?.addEventListener("change", () => void loadLines());
    body.querySelector<HTMLFormElement>("#credit-note-entry")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget as HTMLFormElement;
      const data = new FormData(form);
      const lines = [...body.querySelectorAll<HTMLInputElement>("[data-credit-line]")].map((input) => ({ oc_line_id: input.dataset.creditLine, amount: Number(input.value || 0) })).filter((line) => line.amount > 0);
      try {
        await financeApi.createCreditNote({ order_id: data.get("order_id"), ...(lines.length ? { lines } : {}), credit_note_date: data.get("credit_note_date"), reason: data.get("reason") });
        toast("Credit Note created", "info");
      } catch (error) { toast(error instanceof Error ? error.message : "Credit Note could not be created", "error"); }
    });
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Credit Notes unavailable")}</div>`; }
  refreshIcons(page); return page;
}

/**
 * Customer credit is derived from confirmed-payment rollups on authorized
 * Order Confirmations. It intentionally has no separate writable ledger.
 */
export async function customerCreditsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Finance", "Customer Credits", "Confirmed overpayments available for future invoices.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(3);
  try {
    const result = await orderApi.list(undefined, 500);
    const credits = new Map<string, { name: string; amount: number; orders: number }>();
    (result.items as Record<string, unknown>[]).forEach((order) => {
      const amount = Number(order.customer_credit ?? 0);
      if (!(amount > 0)) return;
      const customer = order.customer_snapshot as Record<string, unknown> | undefined;
      const id = String(order.customer_id ?? order.customer_company_id ?? order.company_id ?? customer?._id ?? "");
      if (!id) return;
      const current = credits.get(id) ?? { name: bankingName(customer), amount: 0, orders: 0 };
      current.amount += amount;
      current.orders += 1;
      credits.set(id, current);
    });
    const rows = [...credits.entries()].sort(([, a], [, b]) => b.amount - a.amount);
    body.innerHTML = rows.length
      ? `<div class="metric-grid compact-metrics"><article class="metric-card"><div class="metric-top"><span>Customer credits</span><i data-lucide="coins"></i></div><strong>${formatMoney(rows.reduce((sum, [, value]) => sum + value.amount, 0), "EUR")}</strong><p>Confirmed overpayments only</p></article><article class="metric-card"><div class="metric-top"><span>Customers</span><i data-lucide="users"></i></div><strong>${rows.length}</strong><p>With available credit</p></article></div><div class="data-table panel"><table><thead><tr><th>Customer</th><th>Available credit</th><th>Source order confirmations</th></tr></thead><tbody>${rows.map(([id, value]) => `<tr><td><strong>${escapeHtml(value.name || id)}</strong></td><td class="money">${formatMoney(value.amount, "EUR")}</td><td>${value.orders}</td></tr>`).join("")}</tbody></table></div>`
      : emptyState("coins", "No customer credits", "Confirmed overpayments will appear here automatically.");
  } catch (error) {
    body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Customer credits unavailable")}</div>`;
  }
  refreshIcons(page);
  return page;
}
