import { adminApi, catalogApi, customerCompanyApi, financeApi, orderApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { customerTypeLabel } from "../config/businessConfig";
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
    const legacyPaymentFields: Record<string, string> = { order_id: "legacy-entry-order-id", amount: "legacy-entry-amount", payment_date: "legacy-entry-payment-date", bank_name: "legacy-entry-bank-name", bank_account: "legacy-entry-bank-account", utr: "legacy-entry-utr", payment_mode: "legacy-entry-payment-mode", reference_number: "legacy-entry-reference", notes: "legacy-entry-notes", attachment: "legacy-entry-attachment" };
    Object.entries(legacyPaymentFields).forEach(([name, id]) => body.querySelector<HTMLElement>(`[name="${name}"]`)?.setAttribute("id", id));
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
  const results = await Promise.all([customerCompanyApi.list(), orderApi.listConfirmations(undefined, 100)]);
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
    "<label>Customer *<input id=\"new-payment-customer\" name=\"customer_display\" list=\"banking-customers\" placeholder=\"Select customer\" autocomplete=\"off\"><datalist id=\"banking-customers\">" + customerOptions + "</datalist><input id=\"payment-customer-id\" name=\"customer_id\" type=\"hidden\"><small class=\"field-error\" data-error=\"customer_id\"></small></label>" +
    "<label>Invoice / Order Confirmation *<input id=\"payment-order\" name=\"order_display\" list=\"banking-orders\" placeholder=\"Select customer first\" autocomplete=\"off\" disabled><datalist id=\"banking-orders\"></datalist><input id=\"payment-order-id\" name=\"order_id\" type=\"hidden\"><small class=\"field-error\" data-error=\"order_id\"></small></label></div></section>" +
    "<section class=\"payment-workflow-section\" data-details hidden><div class=\"section-title\"><span class=\"step-number\">02</span><div><span class=\"eyebrow\">Invoice Details</span><h2>Read-only transaction information</h2></div></div><div class=\"detail-grid readonly-payment-details\">" +
    ["customer", "invoice", "oc", "invoice_date", "due_date", "payment_terms", "salesperson", "currency", "invoice_amount", "paid", "outstanding"].map((key) => "<div><span>" + key.replaceAll("_", " ") + "</span><strong data-detail=\"" + key + "\">—</strong></div>").join("") +
    "</div></section>" +
    "<section class=\"payment-workflow-section\"><div class=\"section-title\"><span class=\"step-number\">03</span><div><span class=\"eyebrow\">Payment Details</span><h2>Enter payment information</h2></div></div><div class=\"form-grid\">" +
    "<label>Payment amount *<input id=\"payment-amount\" name=\"amount\" type=\"number\" min=\"0.01\" step=\"0.01\" required><small class=\"field-error\" data-error=\"amount\"></small></label>" +
    "<label>Payment date *<input id=\"payment-date\" name=\"payment_date\" type=\"date\" required><small class=\"field-error\" data-error=\"payment_date\"></small></label>" +
    "<label>Payment mode *<select id=\"payment-mode\" name=\"payment_mode\" required><option value=\"\">Select payment mode</option>" + ["Bank Transfer", "NEFT", "RTGS", "IMPS", "SWIFT", "Cheque", "Other"].map((mode) => "<option>" + mode + "</option>").join("") + "</select><small class=\"field-error\" data-error=\"payment_mode\"></small></label>" +
    "<label>Bank name *<input id=\"payment-bank-name\" name=\"bank_name\" required><small class=\"field-error\" data-error=\"bank_name\"></small></label><label>Bank account *<input id=\"payment-bank-account\" name=\"bank_account\" required><small class=\"field-error\" data-error=\"bank_account\"></small></label><label>UTR / transaction reference *<input id=\"payment-utr\" name=\"utr\" required><small class=\"field-error\" data-error=\"utr\"></small></label><label>Payment reference *<input id=\"payment-reference\" name=\"reference_number\" required><small class=\"field-error\" data-error=\"reference_number\"></small></label></div></section>" +
    "<section class=\"payment-workflow-section\"><div class=\"section-title\"><span class=\"step-number\">04</span><div><span class=\"eyebrow\">Supporting Documents</span><h2>Proof and notes</h2></div></div><div class=\"form-grid\"><label>Payment proof *<span class=\"payment-proof-dropzone\" data-proof-zone tabindex=\"0\"><i data-lucide=\"upload-cloud\"></i><strong>Upload payment proof</strong><small>PDF, JPG, PNG or WEBP up to 5 MB</small><button class=\"button button-quiet\" type=\"button\" data-choose-proof>Choose file</button><input id=\"payment-attachment\" name=\"attachment\" type=\"file\" accept=\"application/pdf,image/jpeg,image/png,image/webp\" hidden></span><small data-file class=\"form-hint\">Required</small><button class=\"button button-quiet\" type=\"button\" data-remove-file hidden>Remove proof</button><small class=\"field-error\" data-error=\"attachment\"></small></label><label class=\"span-2\">Notes<textarea id=\"payment-notes\" name=\"notes\" rows=\"3\" placeholder=\"Optional notes\"></textarea></label></div></section>" +
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
    body.innerHTML = "<div class=\"metric-grid compact-metrics banking-summary\">" + cards.map((card) => "<article class=\"metric-card\"><div class=\"metric-top\"><span>" + card[0] + "</span><i data-lucide=\"" + card[2] + "\"></i></div><strong>" + card[1] + "</strong><p>" + card[3] + "</p></article>").join("") + "</div><section class=\"panel banking-filters\"><div class=\"form-grid\"><label class=\"span-2\">Search payments<input id=\"legacy-payment-search\" name=\"payment_search\" data-payment-filter=\"search\" placeholder=\"Payment ID, customer, invoice, UTR or reference\"></label><label>Customer<select id=\"legacy-payment-customer\" name=\"customer_id\" data-payment-filter=\"customer\"><option value=\"\">All customers</option>" + customerFilters.map((customer) => "<option value=\"" + escapeHtml(customer.id) + "\">" + escapeHtml(customer.name) + "</option>").join("") + "</select></label><label>Status<select id=\"legacy-payment-status\" name=\"status\" data-payment-filter=\"status\"><option value=\"\">All statuses</option>" + statusFilters.map((status) => "<option value=\"" + escapeHtml(status) + "\">" + escapeHtml(status) + "</option>").join("") + "</select></label><label>From date<input id=\"legacy-payment-from-date\" name=\"from_date\" type=\"date\" data-payment-filter=\"from\"></label><label>To date<input id=\"legacy-payment-to-date\" name=\"to_date\" type=\"date\" data-payment-filter=\"to\"></label><label>Payment mode<select id=\"legacy-payment-mode\" name=\"payment_mode\" data-payment-filter=\"mode\"><option value=\"\">All modes</option>" + modeFilters.map((mode) => "<option value=\"" + escapeHtml(mode) + "\">" + escapeHtml(mode) + "</option>").join("") + "</select></label></div></section><div data-payment-table></div>";
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
  content.innerHTML = `<form class="stack-form" data-reject-payment><p>Reject ${escapeHtml(bankingPaymentId(payment))}. Rejected payments do not change the confirmed balance or customer credit.</p><label>Reason *<textarea id="payment-reject-reason" name="reason" rows="4" required></textarea></label><small class="field-error" data-reject-error></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-danger" type="submit">Reject Payment</button></div></form>`;
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

async function voidBankingPayment(payment: BankingRecord): Promise<void> {
  const reason = window.prompt(`Why are you voiding ${bankingPaymentId(payment)}?`);
  if (!reason?.trim()) return;
  try {
    await financeApi.voidPayment(String(payment._id), reason.trim());
    toast("Payment voided; the audit record was retained", "info");
    window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/payments" }));
  } catch (error) {
    toast(error instanceof Error ? error.message : "Payment could not be voided", "error");
  }
}

async function deleteVoidedBankingPayment(payment: BankingRecord): Promise<void> {
  if (!window.confirm(`Delete ${bankingPaymentId(payment)} after it has been voided? Its audit history will be retained.`)) return;
  try {
    await financeApi.deletePayment(String(payment._id));
    toast("Voided payment deleted from the operational list", "info");
    window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/payments" }));
  } catch (error) {
    toast(error instanceof Error ? error.message : "Payment could not be deleted", "error");
  }
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
      orderApi.listConfirmations(undefined, 100),
    ]);
    const items = paymentResult.items as BankingRecord[];
    const orders = orderResult.items as BankingRecord[];
    const contexts = { customers: (await customerCompanyApi.list()).items as unknown as BankingRecord[], orders };
    const summary = paymentResult.summary;
    const cards = [
      ["Total payments", String(summary.total_payments), "landmark", "Payment records"],
      ["Confirmed received", formatMoney(summary.confirmed_received, "EUR"), "circle-check", "Confirmed receipts only"],
      ["Awaiting confirmation", String(summary.awaiting_confirmation), "clock-3", "Superadmin review queue"],
      ["Outstanding", formatMoney(summary.outstanding, "EUR"), "wallet-cards", `Customer credits ${formatMoney(summary.customer_credit, "EUR")}`],
    ];
    const customerFilters = Array.from(new Map<string, { id: string; name: string }>(items.map((item): [string, { id: string; name: string }] => {
      const id = String(item.customer_id || "");
      return [id, { id, name: bankingName(item.customer_snapshot as BankingRecord | undefined) || id }];
    }).filter(([id]) => Boolean(id))).values());
    const workflowStatuses = [...new Set(items.map(bankingStatus).filter(Boolean))];
    const invoiceStatuses = [...new Set(items.map(bankingInvoiceStatus).filter(Boolean))];
    const modeFilters = [...new Set(items.map((item) => String(item.payment_mode || "").trim()).filter(Boolean))];
    body.innerHTML = `<div class="metric-grid compact-metrics banking-summary">${cards.map((card) => `<article class="metric-card"><div class="metric-top"><span>${card[0]}</span><i data-lucide="${card[2]}"></i></div><strong>${card[1]}</strong><p>${card[3]}</p></article>`).join("")}</div>
      <section class="panel banking-filters" aria-label="Payment filters"><div class="banking-filter-row banking-filter-row-primary"><label>Search payments<input id="payment-search" name="payment_search" data-payment-filter="search" placeholder="Payment ID, customer, invoice, UTR or reference"></label><label>Customer<select id="payment-customer" name="customer_id" data-payment-filter="customer"><option value="">All customers</option>${customerFilters.map((customer) => `<option value="${escapeHtml(customer.id)}">${escapeHtml(customer.name)}</option>`).join("")}</select></label></div><div class="banking-filter-row banking-filter-row-secondary"><label>Status<select id="payment-status" name="status" data-payment-filter="status"><option value="">All statuses</option>${[...workflowStatuses, ...invoiceStatuses.filter((value) => !workflowStatuses.includes(value))].map((status) => `<option value="${escapeHtml(status)}">${escapeHtml(status.replaceAll("_", " "))}</option>`).join("")}</select></label><label>From date<input id="payment-from-date" name="from_date" type="date" data-payment-filter="from"></label><label>To date<input id="payment-to-date" name="to_date" type="date" data-payment-filter="to"></label><label>Payment mode<select id="payment-mode-filter" name="payment_mode" data-payment-filter="mode"><option value="">All modes</option>${modeFilters.map((mode) => `<option value="${escapeHtml(mode)}">${escapeHtml(mode)}</option>`).join("")}</select></label></div><div class="banking-filter-actions"><button class="button button-secondary" type="button" data-payment-reset>Reset</button><button class="button button-primary" type="button" data-payment-apply>Apply Filters</button></div></section><div data-payment-table></div>`;
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
        const normalizedStatus = workflowStatus.toUpperCase();
        const voidAction = canCreatePayment && !["VOIDED", "DELETED"].includes(normalizedStatus)
          ? `<button class="button button-secondary" type="button" data-payment-void="${escapeHtml(String(item._id))}">Void</button>`
          : "";
        const deleteAction = canCreatePayment && normalizedStatus === "VOIDED"
          ? `<button class="button button-danger" type="button" data-payment-delete="${escapeHtml(String(item._id))}">Delete</button>`
          : "";
        const reviewActions = canReviewPayment && isAwaitingBankReview(item)
          ? `<button class="button button-secondary" type="button" data-payment-confirm="${escapeHtml(String(item._id))}">Confirm</button><button class="button button-danger" type="button" data-payment-reject="${escapeHtml(String(item._id))}">Reject</button>`
          : "";
        const status = `${statusBadge(workflowStatus)}${invoiceStatus !== workflowStatus ? `<small class="payment-invoice-status">Invoice: ${escapeHtml(invoiceStatus.replaceAll("_", " "))}</small>` : ""}`;
        return `<tr><td><strong>${escapeHtml(bankingPaymentId(item, index))}</strong></td><td>${escapeHtml(bankingName(item.customer_snapshot as BankingRecord | undefined))}</td><td>${escapeHtml(bankingOrderName(order) || String(item.order_number || item.order_id || "\u2014"))}</td><td>${formatDate(item.payment_date || item.created_at)}</td><td class="money">${formatMoney(Number(item.invoice_amount || bankingAmount(order)), "EUR")}</td><td class="money">${formatMoney(Number(item.amount || 0), "EUR")}</td><td class="money">${formatMoney(Number(item.remaining_balance ?? item.balance ?? bankingAmount(order)), "EUR")}</td><td><div class="payment-status-stack">${status}</div></td><td>${escapeHtml(String(item.payment_mode || "\u2014"))}</td><td>${escapeHtml(String(item.utr || item.reference_number || "\u2014"))}</td><td><div class="table-actions"><button class="button button-quiet" type="button" data-payment-view="${escapeHtml(String(item._id))}">View</button>${editable ? `<button class="button button-secondary" type="button" data-payment-edit="${escapeHtml(String(item._id))}">Edit</button>` : ""}${reviewActions}${voidAction}${deleteAction}</div></td></tr>`;
      }).join("");
      tableHost.innerHTML = visible.length
        ? `<div class="data-table panel banking-table"><table><thead><tr><th>Payment ID</th><th>Customer</th><th>Invoice / OC</th><th>Payment date</th><th>Invoice amount</th><th>Payment amount</th><th>Balance</th><th>Status</th><th>Payment mode</th><th>UTR / reference</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : emptyState("landmark", items.length ? "No matching payments" : "No payments found", items.length ? "Adjust the filters or record a new payment." : "Payment records will appear here after a payment is recorded.");
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
      tableHost.querySelectorAll<HTMLButtonElement>("[data-payment-void]").forEach((button) => button.addEventListener("click", () => {
        const record = items.find((item) => String(item._id) === String(button.dataset.paymentVoid));
        if (record) void voidBankingPayment(record);
      }));
      tableHost.querySelectorAll<HTMLButtonElement>("[data-payment-delete]").forEach((button) => button.addEventListener("click", () => {
        const record = items.find((item) => String(item._id) === String(button.dataset.paymentDelete));
        if (record) void deleteVoidedBankingPayment(record);
      }));
      refreshIcons(tableHost);
    };
    body.querySelector<HTMLButtonElement>("[data-payment-apply]")?.addEventListener("click", render);
    body.querySelector<HTMLButtonElement>("[data-payment-reset]")?.addEventListener("click", () => {
      body.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-payment-filter]").forEach((control) => { control.value = ""; });
      render();
    });
    body.querySelector<HTMLInputElement>('[data-payment-filter="search"]')?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); render(); }
    });
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
  const routeView = new URLSearchParams(window.location.search).get("view") || (routePath.endsWith("/rules") ? "rules" : routePath.endsWith("/payouts") ? "payouts" : routePath.endsWith("/user") ? "user" : "overview");
  const pageTitle = routeView === "rules" ? "Incentive Rules" : routeView === "payouts" ? "Incentive Payouts" : routeView === "user" ? "User Incentive" : "Incentive Overview";
  const pageDescription = routeView === "rules"
    ? "Configure incentive rates for users and managers based on customer type and product type."
    : routeView === "user"
      ? "View immutable user and manager incentive allocations captured from Order Confirmations."
      : "Track sales incentives generated from orders and activated after payment receipt.";
  const page = pageScaffold("Incentives", pageTitle, pageDescription);
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
  const canDeleteIncentives = appStore.can("incentives.delete") || appStore.state.user?.role_id === "superadmin";
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
  const roleLabel = (value: unknown): string => String(value || "—")
    .replaceAll("manager_sales_admin", "Manager")
    .replaceAll("superadmin", "Superadmin")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
  const snapshotName = (value: unknown, fallback = "—"): string => {
    const snapshot = (value && typeof value === "object" ? value : {}) as Record<string, unknown>;
    return String(snapshot.name ?? snapshot.email ?? fallback);
  };
  const renderUserRows = (items: Record<string, unknown>[]) => {
    if (!items.length) return emptyState("badge-euro", "No user incentives yet", "Internal incentive allocations are captured when an eligible user or manager creates an Order Confirmation.");
    return `<div class="data-table panel user-incentive-table"><table><thead><tr><th>OC Number</th><th>Customer</th><th>Created By</th><th>Creator Role</th><th>Manager</th><th>Recipient</th><th>Recipient Role</th><th>Product / Category</th><th>Order Amount</th><th>Incentive %</th><th>Incentive Amount</th><th>Payment Status</th><th>Incentive Status</th><th>Created</th><th>Actions</th></tr></thead><tbody>${items.map((item) => {
      const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {};
      const creator = (item.creator_snapshot as Record<string, unknown> | undefined) ?? {};
      const manager = (item.manager_snapshot as Record<string, unknown> | undefined) ?? {};
      const recipient = (item.recipient_snapshot as Record<string, unknown> | undefined) ?? {};
      const creatorRole = String(item.creator_role_snapshot ?? creator.role_id ?? "—");
      const managerName = snapshotName(manager, ["manager", "manager_sales_admin"].includes(creatorRole) ? snapshotName(creator) : "—");
      const status = String(item.status ?? "PENDING PAYMENT");
      const paymentStatus = String(item.payment_status ?? (item.payment_confirmation_date ? "Paid" : "Pending Payment"));
      const lines = Array.isArray(item.incentive_lines) ? item.incentive_lines as Record<string, unknown>[] : [];
      const productSummary = lines.map((line) => `<span class="user-incentive-product"><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))}</strong><small>${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))}</small></span>`).join("");
      const rateSummary = item.incentive_percentage_snapshot == null ? "By category" : `${Number(item.incentive_percentage_snapshot).toFixed(1).replace(/\.0$/, "")}%`;
      const viewKey = escapeHtml(String(item.allocation_key ?? item._id ?? ""));
      return `<tr><td><strong>${escapeHtml(String(item.oc_number ?? item.order_number ?? item.order_id ?? "—"))}</strong></td><td>${escapeHtml(String(customer.company_name ?? customer.name ?? item.customer_id ?? "—"))}</td><td>${escapeHtml(snapshotName(creator, String(item.salesperson_id ?? "—")))}</td><td>${escapeHtml(roleLabel(creatorRole))}</td><td>${escapeHtml(managerName)}</td><td><strong>${escapeHtml(snapshotName(recipient, String(item.recipient_user_id ?? "—")))}</strong></td><td>${escapeHtml(roleLabel(item.recipient_type ?? item.recipient_role))}</td><td>${productSummary || "—"}</td><td class="money">${formatMoney(Number(item.order_amount ?? 0), "EUR")}</td><td>${escapeHtml(rateSummary)}</td><td class="money"><strong>${formatMoney(Number(item.gross_incentive_amount ?? 0), "EUR")}</strong></td><td>${statusBadge(paymentStatus)}</td><td>${statusBadge(status)}</td><td>${formatDate(String(item.created_at ?? ""))}</td><td><button class="icon-button" type="button" data-incentive-detail="${viewKey}" title="View incentive snapshot" aria-label="View incentive snapshot"><i data-lucide="eye"></i></button></td></tr>`;
    }).join("")}</tbody></table></div>`;
  };
  const openAllocationDetail = async (item: Record<string, unknown>) => {
    // Load the complete authorized snapshot once.  Recipient tabs below are
    // derived from the returned allocations, never from a guessed role.
    const detail = await financeApi.incentive(String(item._id ?? ""));
    const customer = (detail.customer_snapshot as Record<string, unknown> | undefined) ?? {};
    const creator = (detail.creator_snapshot as Record<string, unknown> | undefined) ?? {};
    const manager = (detail.manager_snapshot as Record<string, unknown> | undefined) ?? {};
    const recipient = (detail.recipient_snapshot as Record<string, unknown> | undefined) ?? {};
    const creatorRole = String(detail.creator_role_snapshot ?? creator.role_id ?? "—");
    const lines = Array.isArray(detail.incentive_lines) ? detail.incentive_lines as Record<string, unknown>[] : [];
    const rates = [...new Set(lines.map((line) => Number(line.incentive_rate_snapshot ?? 0)))];
    const total = lines.reduce((sum, line) => sum + Number(line.incentive_amount ?? 0), 0);
    const productRows = lines.map((line) => `<li><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))}</strong><span>${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))} · ${Number(line.incentive_rate_snapshot ?? 0).toFixed(1).replace(/\.0$/, "")}% · ${formatMoney(Number(line.incentive_amount ?? 0), "EUR")}</span></li>`).join("");
    const content = document.createElement("div");
    content.innerHTML = `<div class="notice compact"><i data-lucide="lock-keyhole"></i><div><strong>Historical incentive snapshot</strong><p>The recipient, manager, percentage and amount are read-only values captured when this Order Confirmation was created.</p></div></div><div class="detail-grid user-incentive-detail"><div><span>OC Number</span><strong>${escapeHtml(String(detail.oc_number ?? detail.order_number ?? detail.order_id ?? "—"))}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? detail.customer_id ?? "—"))}</strong></div><div><span>Client Type</span><strong>${escapeHtml(String(detail.client_type_snapshot ?? "—"))}</strong></div><div><span>OC Creator</span><strong>${escapeHtml(snapshotName(creator))}</strong></div><div><span>Creator Role</span><strong>${escapeHtml(roleLabel(creatorRole))}</strong></div><div><span>Manager at OC Creation</span><strong>${escapeHtml(snapshotName(manager, ["manager", "manager_sales_admin"].includes(creatorRole) ? snapshotName(creator) : "—"))}</strong></div><div><span>Recipient</span><strong>${escapeHtml(snapshotName(recipient, String(detail.recipient_user_id ?? "—")))}</strong></div><div><span>Recipient Role</span><strong>${escapeHtml(roleLabel(detail.recipient_type ?? detail.recipient_role))}</strong></div><div><span>Order Amount</span><strong>${formatMoney(Number(detail.order_amount ?? 0), "EUR")}</strong></div><div><span>Incentive %</span><strong>${rates.length === 1 ? `${rates[0].toFixed(1).replace(/\.0$/, "")}%` : "By category"}</strong></div><div><span>Incentive Amount</span><strong>${formatMoney(total, "EUR")}</strong></div><div><span>Payment Status</span><strong>${escapeHtml(String(detail.payment_status ?? "Pending Payment"))}</strong></div><div><span>Incentive Status</span><strong>${escapeHtml(String(detail.status ?? "PENDING PAYMENT"))}</strong></div><div><span>Created At</span><strong>${formatDate(String(detail.created_at ?? ""))}</strong></div></div><ul class="user-incentive-detail-lines">${productRows}</ul>`;
    openModal("User Incentive Snapshot", content, "wide");
    refreshIcons(content);
  };
  // Keep the legacy renderer available for older integrations while routing
  // the UI through the tabbed renderer below.
  void openAllocationDetail;
  const openAllocationDetailV2 = async (item: Record<string, unknown>) => {
    const detail = await financeApi.incentive(String(item._id ?? ""));
    const customer = (detail.customer_snapshot as Record<string, unknown> | undefined) ?? {};
    const creator = (detail.creator_snapshot as Record<string, unknown> | undefined) ?? {};
    const manager = (detail.manager_snapshot as Record<string, unknown> | undefined) ?? {};
    const internalLines = Array.isArray(detail.incentive_lines) ? detail.incentive_lines as Record<string, unknown>[] : [];
    const customerLines = Array.isArray(detail.customer_incentive_lines) ? detail.customer_incentive_lines as Record<string, unknown>[] : [];
    const allLines = [...internalLines, ...customerLines];
    const lineAmount = (line: Record<string, unknown>): number => {
      const value = Number(line.incentive_amount ?? line.amount ?? 0);
      return Number.isFinite(value) ? value : 0;
    };
    const lineBase = (line: Record<string, unknown>): string => {
      const value = Number(line.product_amount ?? line.incentive_base_amount_eur ?? line.base_amount_eur ?? line.order_amount);
      return Number.isFinite(value) ? formatMoney(value, "EUR") : "—";
    };
    const lineRate = (line: Record<string, unknown>): string => {
      const value = Number(line.incentive_rate_snapshot ?? line.rate ?? NaN);
      return Number.isFinite(value) ? `${value.toFixed(1).replace(/\.0$/, "")}%` : "—";
    };
    const isCustomerLine = (line: Record<string, unknown>): boolean =>
      String(line.recipient_type ?? "").toUpperCase() === "CUSTOMER" || String(line.category_id ?? "") === "customer_incentive";
    const isManagerLine = (line: Record<string, unknown>): boolean => {
      const role = String(line.recipient_role ?? line.recipient_type ?? "").toLowerCase();
      const allocation = String(line.allocation_type ?? "").toLowerCase();
      return role.includes("manager") || allocation.includes("manager");
    };
    const recipientName = (source: Record<string, unknown>): string => {
      const snapshot = (source.recipient_snapshot as Record<string, unknown> | undefined) ?? {};
      return String(source.recipient_name ?? snapshot.name ?? snapshot.email ?? source.customer_name_snapshot ?? source.bearer_name_snapshot ?? source.recipient_user_id ?? source.customer_id ?? "—");
    };
    const recipientRoleLabel = (source: Record<string, unknown>): string => {
      if (isCustomerLine(source) || String(source.recipient_type ?? "").toUpperCase() === "CUSTOMER") return "Customer";
      if (isManagerLine(source)) return "Manager";
      const role = String(source.recipient_role ?? source.recipient_type ?? source.allocation_type ?? "").toLowerCase();
      if (role.includes("user") || role.includes("creator") || role.includes("sales")) return "Sales Person";
      return roleLabel(source.recipient_role ?? source.recipient_type ?? source.allocation_type ?? "—");
    };
    const renderTotalBreakdown = () => {
      if (!allLines.length) return `<div class="empty-state compact"><strong>No allocation</strong><span>No incentive allocation was captured for this Order Confirmation.</span></div>`;
      return `<div class="incentive-total-breakdown"><div class="incentive-total-breakdown-title">Product / category breakdown</div><div class="incentive-total-breakdown-table" role="table" aria-label="Incentive allocation by product"><div class="incentive-total-breakdown-row is-head" role="row"><span>Product</span><span>Category</span><span>Amount</span><span>Rate</span><span>Incentive</span></div>${allLines.map((line) => `<div class="incentive-total-breakdown-row" role="row"><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))}</strong><span>${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))}</span><span>${escapeHtml(lineBase(line))}</span><span>${escapeHtml(lineRate(line))}</span><span>${formatMoney(lineAmount(line), "EUR")}</span></div>`).join("")}</div></div>`;
    };
    const renderProductRows = (lines: Record<string, unknown>[]) => lines === allLines ? renderTotalBreakdown() : lines.length
      ? `<div class="incentive-detail-lines">${lines.map((line) => `<div class="incentive-detail-line"><div><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))}</strong><small>${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))}</small></div><div class="incentive-detail-line-values"><span>Base ${lineBase(line)}</span><span>Rate ${lineRate(line)}</span><strong>${formatMoney(lineAmount(line), "EUR")}</strong></div></div>`).join("")}</div>`
      : `<div class="empty-state compact"><strong>No allocation</strong><span>No incentive allocation was captured for this recipient.</span></div>`;
    const renderPanel = (key: string, title: string, lines: Record<string, unknown>[]) => {
      const panelTotal = lines.reduce((sum, line) => sum + lineAmount(line), 0);
      const rates = [...new Set(lines.map(lineRate).filter((rate) => rate !== "—"))];
      return `<section class="incentive-detail-panel" data-detail-panel="${key}" ${key === "total" ? "" : "hidden"}><div class="incentive-detail-panel-head"><div><span class="eyebrow">${escapeHtml(title)}</span><h3>${escapeHtml(key === "total" ? "Incentive summary" : recipientName(lines[0] ?? {}))}</h3></div><strong class="incentive-detail-panel-total">${formatMoney(panelTotal, "EUR")}</strong></div>${key !== "total" && lines[0] ? `<div class="incentive-detail-recipient"><span>Role</span><strong>${escapeHtml(recipientRoleLabel(lines[0]))}</strong><span>Rates</span><strong>${escapeHtml(rates.join(" · ") || "By category")}</strong></div>` : `<div class="incentive-detail-summary"><div><span>Order amount</span><strong>${formatMoney(Number(detail.order_amount ?? 0), "EUR")}</strong></div><div><span>Payment status</span><strong>${escapeHtml(String(detail.payment_status ?? "Pending Payment"))}</strong></div><div><span>Incentive status</span><strong>${escapeHtml(String(detail.status ?? "PENDING PAYMENT"))}</strong></div><div><span>Visible allocations</span><strong>${lines.length}</strong></div></div>`}${renderProductRows(lines)}</section>`;
    };
    type AllocationGroup = { key: string; label: string; role: string; name: string; lines: Record<string, unknown>[] };
    const persistedAllocations = Array.isArray(detail.allocations)
      ? (detail.allocations as Record<string, unknown>[])
        .map((allocation, index): AllocationGroup | null => {
          const lines = Array.isArray(allocation.lines) ? allocation.lines.filter((line): line is Record<string, unknown> => Boolean(line && typeof line === "object")) : [];
          if (!lines.length) return null;
          const source = { ...lines[0], ...allocation };
          const role = recipientRoleLabel(source);
          const name = recipientName(source);
          return { key: `recipient-${index}`, label: `${role} — ${name}`, role, name, lines };
        })
        .filter((allocation): allocation is AllocationGroup => Boolean(allocation))
      : [];
    const fallbackGroups = new Map<string, AllocationGroup>();
    if (!persistedAllocations.length) {
      allLines.forEach((line) => {
        const recipientId = String(line.recipient_user_id ?? line.customer_id ?? recipientName(line));
        const allocationType = String(line.allocation_type ?? (isCustomerLine(line) ? "customer" : "creator"));
        const recipientType = String(line.recipient_type ?? (isCustomerLine(line) ? "CUSTOMER" : "USER"));
        const key = `${recipientId}:${allocationType}:${recipientType}`;
        const existing = fallbackGroups.get(key);
        if (existing) existing.lines.push(line);
        else {
          const role = recipientRoleLabel(line);
          const name = recipientName(line);
          fallbackGroups.set(key, { key: `recipient-${fallbackGroups.size}`, label: `${role} — ${name}`, role, name, lines: [line] });
        }
      });
    }
    const recipientGroups = persistedAllocations.length ? persistedAllocations : [...fallbackGroups.values()];
    const tabs = [{ key: "total", label: "Total", lines: allLines }, ...recipientGroups];
    const content = document.createElement("div");
    const creatorRole = String(detail.creator_role_snapshot ?? creator.role_id ?? "—");
    content.innerHTML = `<div class="notice compact"><i data-lucide="lock-keyhole"></i><div><strong>Historical incentive snapshot</strong><p>Recipients, percentages and amounts are read-only values captured when this Order Confirmation was created.</p></div></div><div class="detail-grid user-incentive-detail"><div><span>OC Number</span><strong>${escapeHtml(String(detail.oc_number ?? detail.order_number ?? detail.order_id ?? "—"))}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? detail.customer_id ?? "—"))}</strong></div><div><span>Customer Type</span><strong>${escapeHtml(customerTypeLabel(String(detail.client_type_snapshot ?? "")))}</strong></div><div><span>OC Creator</span><strong>${escapeHtml(snapshotName(creator))}</strong></div><div><span>Creator Role</span><strong>${escapeHtml(roleLabel(creatorRole))}</strong></div><div><span>Manager at OC Creation</span><strong>${escapeHtml(snapshotName(manager, ["manager", "manager_sales_admin"].includes(creatorRole) ? snapshotName(creator) : "—"))}</strong></div><div><span>Order Amount</span><strong>${formatMoney(Number(detail.order_amount ?? 0), "EUR")}</strong></div><div><span>Created At</span><strong>${formatDate(String(detail.created_at ?? ""))}</strong></div></div><div class="incentive-detail-tabs" role="tablist" aria-label="Incentive recipients">${tabs.map((tab, index) => `<button class="incentive-detail-tab${index === 0 ? " is-active" : ""}" type="button" role="tab" aria-selected="${index === 0 ? "true" : "false"}" data-detail-tab="${tab.key}">${escapeHtml(tab.label)}</button>`).join("")}</div><div class="incentive-detail-panels">${tabs.map((tab) => renderPanel(tab.key, tab.label, tab.lines)).join("")}</div>`;
    content.insertAdjacentHTML("afterbegin", `<p class="modal-subtitle">View incentive allocation details for this Order Confirmation.</p>`);
    const modalSubtitle = content.querySelector<HTMLElement>(".modal-subtitle");
    const historicalNotice = content.querySelector<HTMLElement>(".notice");
    const tabBar = content.querySelector<HTMLElement>(".incentive-detail-tabs");
    if (modalSubtitle && tabBar) {
      modalSubtitle.after(tabBar);
      if (historicalNotice) tabBar.after(historicalNotice);
    }
    const customerTypeValue = content.querySelectorAll<HTMLElement>(".detail-grid > div")[2]?.querySelector("strong");
    if (customerTypeValue) customerTypeValue.textContent = customerTypeLabel(String(detail.client_type_snapshot ?? detail.customer_type_snapshot ?? customer.client_type ?? ""));
    const totalSummary = content.querySelector<HTMLElement>('[data-detail-panel="total"] .incentive-detail-summary');
    if (totalSummary) {
      const totalRates = [...new Set(allLines.map(lineRate).filter((rate) => rate !== "â€”"))];
      totalSummary.insertAdjacentHTML("beforeend", `<div><span>Total incentive rate</span><strong>${escapeHtml(totalRates.length === 1 ? totalRates[0] : totalRates.length > 1 ? "Multiple rates" : "â€”")}</strong></div>`);
    }
    content.querySelectorAll<HTMLElement>("[data-detail-panel]").forEach((panel) => {
      const key = panel.dataset.detailPanel ?? "";
      if (!key || key === "total") return;
      const tab = tabs.find((candidate) => candidate.key === key);
      const first = tab?.lines[0];
      if (!first) return;
      const designation = first.bearer_designation_snapshot ?? first.designation_snapshot;
      const details = `<div class="incentive-detail-summary"><div><span>Recipient</span><strong>${escapeHtml(recipientName(first))}</strong></div><div><span>Role</span><strong>${escapeHtml(recipientRoleLabel(first))}</strong></div><div><span>Order Confirmation</span><strong>${escapeHtml(String(detail.oc_number ?? detail.order_number ?? detail.order_id ?? "â€”"))}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? detail.customer_id ?? "â€”"))}</strong></div><div><span>Order amount</span><strong>${formatMoney(Number(detail.order_amount ?? 0), "EUR")}</strong></div><div><span>Incentive rate</span><strong>${escapeHtml([...new Set((tab?.lines ?? []).map(lineRate))].join(" Â· ") || "â€”")}</strong></div><div><span>Payment status</span><strong>${escapeHtml(String(detail.payment_status ?? "Pending Payment"))}</strong></div><div><span>Incentive status</span><strong>${escapeHtml(String(detail.status ?? "PENDING PAYMENT"))}</strong></div>${designation ? `<div><span>Designation</span><strong>${escapeHtml(String(designation))}</strong></div>` : ""}</div>`;
      const breakdown = panel.querySelector<HTMLElement>(".incentive-detail-lines, .empty-state");
      if (breakdown) breakdown.insertAdjacentHTML("beforebegin", details);
      else panel.insertAdjacentHTML("beforeend", details);
    });
    const dialog = openModal("Incentive Details", content, "wide");
    dialog.classList.add("incentive-details-modal");
    const totalPanel = content.querySelector<HTMLElement>('[data-detail-panel="total"]');
    if (totalPanel) {
      const recipientTabs = tabs.filter((tab) => tab.key !== "total");
      if (recipientTabs.length) {
        const summary = document.createElement("div");
        summary.className = "incentive-allocation-summary";
        summary.innerHTML = `<span class="eyebrow">Allocation summary</span>${recipientTabs.map((tab) => {
          const rates = [...new Set(tab.lines.map(lineRate))];
          const amount = tab.lines.reduce((sum, line) => sum + lineAmount(line), 0);
          const rate = rates.length === 1 ? rates[0] : rates.length > 1 ? "Multiple rates" : "Rate unavailable";
          return `<div class="incentive-allocation-summary-row"><div><strong>${escapeHtml(tab.label)}</strong><small>${escapeHtml(rate)}</small></div><strong>${formatMoney(amount, "EUR")}</strong></div>`;
        }).join("")}<div class="incentive-allocation-summary-total"><strong>Total incentive</strong><strong>${formatMoney(allLines.reduce((sum, line) => sum + lineAmount(line), 0), "EUR")}</strong></div>`;
        const lines = totalPanel.querySelector<HTMLElement>(".incentive-total-breakdown, .incentive-detail-lines");
        if (lines) totalPanel.insertBefore(summary, lines);
        else totalPanel.append(summary);
      }
    }
    const numericRateLabel = (source: Record<string, unknown>[]) => {
      const values = [...new Set(source.map((line) => Number(line.incentive_rate_snapshot ?? line.rate ?? NaN)).filter(Number.isFinite))];
      return values.length === 1 ? `${values[0].toFixed(1).replace(".0", "")}%` : values.length > 1 ? "Multiple rates" : "Not available";
    };
    const summaryValue = (panel: HTMLElement | null, label: string, value: string) => {
      const row = [...(panel?.querySelectorAll<HTMLElement>(".incentive-detail-summary > div") ?? [])].find((candidate) => candidate.querySelector("span")?.textContent?.trim() === label);
      const target = row?.querySelector("strong");
      if (target) target.textContent = value;
    };
    summaryValue(content.querySelector<HTMLElement>('[data-detail-panel="total"]'), "Total incentive rate", numericRateLabel(allLines));
    tabs.filter((tab) => tab.key !== "total").forEach((tab) => summaryValue(content.querySelector<HTMLElement>(`[data-detail-panel="${tab.key}"]`), "Incentive rate", numericRateLabel(tab.lines)));
    content.querySelectorAll<HTMLButtonElement>("[data-detail-tab]").forEach((button) => button.addEventListener("click", () => {
      const key = button.dataset.detailTab ?? "total";
      content.querySelectorAll<HTMLButtonElement>("[data-detail-tab]").forEach((tab) => { const active = tab.dataset.detailTab === key; tab.classList.toggle("is-active", active); tab.setAttribute("aria-selected", String(active)); });
      content.querySelectorAll<HTMLElement>("[data-detail-panel]").forEach((panel) => { panel.hidden = panel.dataset.detailPanel !== key; });
    }));
    refreshIcons(content);
  };
  const openCancellation = (item: Record<string, unknown>) => {
    const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {};
    const salesperson = (item.salesperson_snapshot as Record<string, unknown> | undefined) ?? {};
    const content = document.createElement("div");
    content.innerHTML = `<div class="notice warning compact"><i data-lucide="triangle-alert"></i><div><strong>Cancel this unpaid incentive?</strong><p>It will leave active totals while its financial snapshot and audit trail are retained.</p></div></div><div class="detail-grid"><div><span>Order Confirmation</span><strong>${escapeHtml(String(item.oc_number ?? item.order_number ?? item.order_id ?? "—"))}</strong></div><div><span>Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? item.customer_id ?? "—"))}</strong></div><div><span>Recipient</span><strong>${escapeHtml(String(salesperson.name ?? item.salesperson_id ?? "—"))}</strong></div><div><span>Amount</span><strong>${formatMoney(Number(item.net_payable_incentive ?? item.gross_incentive_amount ?? 0), "EUR")}</strong></div><div><span>Status</span><strong>${escapeHtml(String(item.status ?? "PENDING PAYMENT"))}</strong></div></div><form class="stack-form"><label>Reason<textarea name="reason" rows="3" maxlength="500" required>Cancelled from Incentive Overview</textarea></label><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Keep Incentive</button><button class="button button-danger" type="submit"><i data-lucide="trash-2"></i>Cancel Incentive</button></div></form>`;
    const dialog = openModal("Cancel Incentive", content, "normal");
    content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
    content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
       const form = event.currentTarget as HTMLFormElement;
      const submit = form.querySelector<HTMLButtonElement>("[type=submit]")!;
      submit.disabled = true;
      try {
        await financeApi.deleteIncentive(String(item._id), String(new FormData(form).get("reason") ?? "").trim());
        dialog.close();
        toast("Incentive cancelled");
        await load();
      } catch (error) {
        toast(error instanceof Error ? error.message : "Incentive could not be cancelled", "error");
        submit.disabled = false;
      }
    });
    refreshIcons(content);
  };
  const load = async () => {
    const params = new URLSearchParams();
    ["search", "salesperson_id", "customer_id", "category_id", "product_id", "recipient_role", "payment_status", "status", "from_date", "to_date", "order_id"].forEach((name) => { const value = body.querySelector<HTMLInputElement | HTMLSelectElement>(`[name="${name}"]`)?.value.trim(); if (value) params.set(name, value); });
    const result = await financeApi.incentives(params.toString());
    const table = body.querySelector<HTMLElement>("[data-incentive-results]"); if (table) table.innerHTML = renderRows(result.items);
    if (requestedView === "user" && table) table.innerHTML = renderUserRows(result.items);
    if (requestedView !== "user" && canDeleteIncentives && table) {
      table.querySelectorAll<HTMLTableRowElement>("tbody tr").forEach((row, index) => {
        const item = result.items[index];
        const status = String(item?.status ?? "").toUpperCase();
        const isPaid = status === "PAID" || Number(item?.paid_amount ?? 0) > 0;
        if (!item || isPaid) return;
        const actions = row.lastElementChild as HTMLTableCellElement | null;
        if (!actions) return;
        if (actions.textContent?.trim() === "—") actions.textContent = "";
        actions.classList.add("table-actions");
        actions.insertAdjacentHTML("beforeend", `<button class="icon-button incentive-delete-button" type="button" data-incentive-delete="${escapeHtml(String(item._id ?? ""))}" title="Cancel incentive" aria-label="Cancel incentive"><i data-lucide="trash-2"></i></button>`);
      });
    }
    const summary = body.querySelector<HTMLElement>("[data-incentive-summary]"); if (summary) summary.innerHTML = renderSummary(result.items);
    const count = body.querySelector<HTMLElement>("[data-incentive-count]"); if (count) count.textContent = `${result.total} record${result.total === 1 ? "" : "s"}`;
    body.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    body.querySelectorAll<HTMLButtonElement>("[data-incentive-detail]").forEach((button) => button.addEventListener("click", () => {
      const item = result.items.find((entry) => String(entry.allocation_key ?? entry._id) === String(button.dataset.incentiveDetail));
      if (item) void openAllocationDetailV2(item).catch((error) => toast(error instanceof Error ? error.message : "Incentive details unavailable", "error"));
    }));
    body.querySelectorAll<HTMLButtonElement>("[data-incentive-delete]").forEach((button) => button.addEventListener("click", () => {
      const item = result.items.find((entry) => String(entry._id) === String(button.dataset.incentiveDelete));
      if (item) openCancellation(item);
    }));
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
    ["payment_status", "status", "salesperson_id", "customer_id", "category_id", "product_id", "recipient_role", "from_date", "to_date"].forEach((name) => body.querySelector(`[name="${name}"]`)?.addEventListener("change", () => { void load().catch((error) => { const results = body.querySelector<HTMLElement>("[data-incentive-results]"); if (results) results.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }); }));
    let searchTimer: number | undefined; body.querySelector<HTMLInputElement>('[name="search"]')?.addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(() => { void load().catch((error) => { const results = body.querySelector<HTMLElement>("[data-incentive-results]"); if (results) results.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }); }, 250); });
    await load();
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }
  refreshIcons(page); return page;
}

export async function customerIncentivesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Finance", "Customer Incentive", "Track customer incentives captured with each Order Confirmation.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  const isSuperadmin = appStore.state.user?.role_id === "superadmin";
  let customerOptionsReady = false;
  const renderRows = (items: Record<string, unknown>[]) => {
    if (!items.length) return emptyState("building-2", "No customer incentives yet", "Customer incentives appear here when an enabled customer is used on a new Order Confirmation.");
    return `<div class="data-table panel"><table><thead><tr><th>Customer</th><th>Incentive bearer</th><th>Designation</th><th>Customer type</th><th>OC number</th><th>Sales person</th><th>Manager</th><th>Order amount</th><th>Incentive %</th><th>Incentive amount</th><th>Payment status</th><th>Incentive status</th><th>Created</th><th>Actions</th></tr></thead><tbody>${items.map((item) => {
      const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {};
      const salesperson = (item.salesperson_snapshot as Record<string, unknown> | undefined) ?? {};
      const manager = (item.manager_snapshot as Record<string, unknown> | undefined) ?? {};
      const line = (Array.isArray(item.incentive_lines) ? item.incentive_lines : []).find((entry) => String((entry as Record<string, unknown>)?.recipient_type ?? "").toUpperCase() === "CUSTOMER") as Record<string, unknown> | undefined;
      const percentage = item.customer_incentive_percentage_snapshot ?? line?.incentive_rate_snapshot;
      const amount = item.customer_incentive_amount_snapshot ?? line?.incentive_amount ?? 0;
      const bearer = item.customer_incentive_bearer_name_snapshot ?? line?.bearer_name_snapshot;
      const designation = item.customer_incentive_designation_snapshot ?? item.customer_incentive_bearer_designation_snapshot ?? line?.bearer_designation_snapshot ?? line?.designation_snapshot;
      return `<tr><td><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? item.customer_id ?? "—"))}</strong></td><td>${escapeHtml(String(bearer ?? "—"))}</td><td>${escapeHtml(String(designation ?? "—"))}</td><td>${escapeHtml(customerTypeLabel(String(line?.customer_type_snapshot ?? item.customer_type_snapshot ?? item.client_type_snapshot ?? "")))}</td><td><a href="/orders/${encodeURIComponent(String(item.order_id ?? ""))}" data-route="/orders/${encodeURIComponent(String(item.order_id ?? ""))}"><strong>${escapeHtml(String(item.oc_number ?? item.order_number ?? "—"))}</strong></a></td><td>${escapeHtml(String(salesperson.name ?? item.salesperson_id ?? "—"))}</td><td>${escapeHtml(String(manager.name ?? item.manager_user_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.order_amount ?? 0), "EUR")}</td><td>${percentage == null ? "—" : `${Number(percentage).toFixed(1)}%`}</td><td class="money">${formatMoney(Number(amount), "EUR")}</td><td>${statusBadge(String(item.payment_status ?? (item.payment_confirmation_date ? "Paid" : "Pending Payment")))}</td><td>${statusBadge(String(item.status ?? "PENDING PAYMENT"))}</td><td>${formatDate(String(item.created_at ?? ""))}</td></tr>`;
    }).join("")}</tbody></table></div>`;
  };
  const load = async () => {
    const search = body.querySelector<HTMLInputElement>('[name="search"]')?.value.trim() ?? "";
    const result = await financeApi.customerIncentives(search ? `search=${encodeURIComponent(search)}` : "");
    if (!customerOptionsReady) {
      const select = body.querySelector<HTMLSelectElement>('[name="customer_id"]');
      const seen = new Set<string>();
      for (const item of result.items) {
        const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {};
        const id = String(item.customer_id ?? customer._id ?? "");
        if (select && id && !seen.has(id)) { seen.add(id); select.insertAdjacentHTML("beforeend", `<option value="${escapeHtml(id)}">${escapeHtml(String(customer.company_name ?? customer.name ?? id))}</option>`); }
      }
      customerOptionsReady = true;
    }
    const customerFilter = body.querySelector<HTMLSelectElement>('[name="customer_id"]')?.value ?? "";
    const typeFilter = body.querySelector<HTMLSelectElement>('[name="customer_type"]')?.value ?? "";
    const paymentFilter = body.querySelector<HTMLSelectElement>('[name="payment_status"]')?.value ?? "";
    const statusFilter = body.querySelector<HTMLSelectElement>('[name="incentive_status"]')?.value ?? "";
    const fromDate = body.querySelector<HTMLInputElement>('[name="from_date"]')?.value ?? "";
    const toDate = body.querySelector<HTMLInputElement>('[name="to_date"]')?.value ?? "";
    const items = result.items.filter((item) => {
      const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {};
      const line = (Array.isArray(item.incentive_lines) ? item.incentive_lines : []).find((entry) => String((entry as Record<string, unknown>)?.recipient_type ?? "").toUpperCase() === "CUSTOMER") as Record<string, unknown> | undefined;
      const customerId = String(item.customer_id ?? customer._id ?? "");
      const customerType = String(line?.customer_type_snapshot ?? item.customer_type_snapshot ?? item.client_type_snapshot ?? "");
      const paymentStatus = String(item.payment_status ?? (item.payment_confirmation_date ? "Paid" : "Pending Payment"));
      const incentiveStatus = String(item.status ?? "PENDING PAYMENT");
      const created = String(item.created_at ?? "").slice(0, 10);
      return (!customerFilter || customerId === customerFilter) && (!typeFilter || customerType === typeFilter) && (!paymentFilter || paymentStatus.toUpperCase() === paymentFilter.toUpperCase()) && (!statusFilter || incentiveStatus.toUpperCase() === statusFilter.toUpperCase()) && (!fromDate || created >= fromDate) && (!toDate || created <= toDate);
    });
    const target = body.querySelector<HTMLElement>("[data-customer-incentive-results]");
    if (target) target.innerHTML = renderRows(items);
    target?.querySelectorAll<HTMLTableRowElement>("tbody tr").forEach((row, index) => {
      const item = items[index];
      if (item) row.insertAdjacentHTML("beforeend", `<td><button class="icon-button" type="button" data-customer-incentive-view="${escapeHtml(String(item._id ?? ""))}" title="View customer incentive" aria-label="View customer incentive"><i data-lucide="eye"></i></button></td>`);
    });
    target?.querySelectorAll<HTMLAnchorElement>("a[data-route]").forEach((link) => link.addEventListener("click", (event) => { event.preventDefault(); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: link.getAttribute("href") })); }));
    target?.querySelectorAll<HTMLButtonElement>("[data-customer-incentive-view]").forEach((button) => button.addEventListener("click", async () => {
      try {
        const detail = await financeApi.customerIncentive(String(button.dataset.customerIncentiveView));
        const customer = (detail.customer_snapshot as Record<string, unknown> | undefined) ?? {};
        const line = (Array.isArray(detail.incentive_lines) ? detail.incentive_lines : []).find((entry) => String((entry as Record<string, unknown>)?.recipient_type ?? "").toUpperCase() === "CUSTOMER") as Record<string, unknown> | undefined;
        const detailBody = document.createElement("div");
        detailBody.className = "stack-form";
        detailBody.innerHTML = `<div class="detail-grid"><div><span class="eyebrow">Customer</span><strong>${escapeHtml(String(customer.company_name ?? customer.name ?? detail.customer_name_snapshot ?? detail.customer_id ?? "—"))}</strong></div><div><span class="eyebrow">Customer type</span><strong>${escapeHtml(String(line?.customer_type_snapshot ?? detail.customer_type_snapshot ?? detail.client_type_snapshot ?? "—"))}</strong></div><div><span class="eyebrow">Order Confirmation</span><strong>${escapeHtml(String(detail.oc_number ?? detail.order_number ?? "—"))}</strong></div><div><span class="eyebrow">Order amount</span><strong>${formatMoney(Number(detail.order_amount ?? 0), "EUR")}</strong></div><div><span class="eyebrow">Incentive percentage</span><strong>${detail.customer_incentive_percentage_snapshot == null ? "—" : `${Number(detail.customer_incentive_percentage_snapshot).toFixed(1)}%`}</strong></div><div><span class="eyebrow">Incentive amount</span><strong>${formatMoney(Number(detail.customer_incentive_amount_snapshot ?? line?.incentive_amount ?? 0), "EUR")}</strong></div><div><span class="eyebrow">Bearer</span><strong>${escapeHtml(String(detail.customer_incentive_bearer_name_snapshot ?? detail.bearer_name_snapshot ?? line?.bearer_name_snapshot ?? "—"))}</strong></div><div><span class="eyebrow">Designation</span><strong>${escapeHtml(String(detail.customer_incentive_designation_snapshot ?? detail.customer_incentive_bearer_designation_snapshot ?? detail.bearer_designation_snapshot ?? line?.bearer_designation_snapshot ?? line?.designation_snapshot ?? "—"))}</strong></div><div><span class="eyebrow">Payment status</span><strong>${escapeHtml(String(detail.payment_status ?? "Pending Payment"))}</strong></div><div><span class="eyebrow">Incentive status</span><strong>${escapeHtml(String(detail.status ?? "PENDING PAYMENT"))}</strong></div><div><span class="eyebrow">Created</span><strong>${formatDate(String(detail.created_at ?? ""))}</strong></div></div><hr><p class="form-hint">Configuration snapshot: the bearer, designation, percentage and EUR base above were captured when the OC was created.</p>`;
        openModal("Customer Incentive", detailBody, "wide");
      } catch (error) { toast(error instanceof Error ? error.message : "Customer incentive details unavailable", "error"); }
    }));
    const count = body.querySelector<HTMLElement>("[data-customer-incentive-count]");
    if (count) count.textContent = `${items.length} record${items.length === 1 ? "" : "s"}`;
    refreshIcons(body);
  };
  try {
    let visibility = { show_customer_incentives_to_manager: false, show_customer_incentives_to_salesperson: false, can_manage: false };
    if (isSuperadmin) visibility = await adminApi.customerIncentiveVisibility();
    body.innerHTML = `<section class="panel quotation-filters incentive-filters"><div class="quotation-filter-head"><div><span class="eyebrow">Customer-linked earnings</span><h2>Customer Incentive</h2><p>Customer incentive snapshots are captured at Order Confirmation creation and follow the existing payment lifecycle.</p></div><span data-customer-incentive-count>Loading…</span></div><label class="filter-search"><span>Search</span><div class="field-search"><i data-lucide="search"></i><input name="search" placeholder="Customer, OC or salesperson" aria-label="Search customer incentives"></div></label></section>${isSuperadmin ? `<section class="panel customer-incentive-visibility"><div class="quotation-filter-head"><div><span class="eyebrow">Superadmin visibility</span><h2>Customer incentive privacy</h2><p>Customer incentive details are hidden by default from managers and sales people.</p></div></div><div class="form-grid"><label><span>Show customer incentives to Manager</span><select name="show_customer_incentives_to_manager"><option value="false" ${!visibility.show_customer_incentives_to_manager ? "selected" : ""}>No</option><option value="true" ${visibility.show_customer_incentives_to_manager ? "selected" : ""}>Yes</option></select></label><label><span>Show customer incentives to Sales Person</span><select name="show_customer_incentives_to_salesperson"><option value="false" ${!visibility.show_customer_incentives_to_salesperson ? "selected" : ""}>No</option><option value="true" ${visibility.show_customer_incentives_to_salesperson ? "selected" : ""}>Yes</option></select></label></div><button class="button button-primary" type="button" data-save-customer-incentive-visibility>Save visibility</button></section>` : ""}<div data-customer-incentive-results></div>`;
    body.querySelector<HTMLElement>(".filter-search")?.insertAdjacentHTML("afterend", `<div class="quotation-filter-grid"><label><span>Customer</span><select name="customer_id"><option value="">All customers</option></select></label><label><span>Customer type</span><select name="customer_type"><option value="">All types</option><option value="WHOLESALER">Distributor</option><option value="DEALER">Dealer</option><option value="CUSTOMER">Customer</option></select></label><label><span>Payment status</span><select name="payment_status"><option value="">All payment statuses</option><option value="Pending Payment">Pending Payment</option><option value="Paid">Paid</option></select></label><label><span>Incentive status</span><select name="incentive_status"><option value="">All incentive statuses</option><option value="PENDING PAYMENT">Pending Payment</option><option value="ACTIVE">Active</option><option value="PAID">Paid</option><option value="CANCELLED">Cancelled</option></select></label><label><span>From date</span><input name="from_date" type="date"></label><label><span>To date</span><input name="to_date" type="date"></label></div>`);
    body.querySelector<HTMLInputElement>('[name="search"]')?.addEventListener("input", () => { window.clearTimeout((body as HTMLElement & { _customerIncentiveTimer?: number })._customerIncentiveTimer); (body as HTMLElement & { _customerIncentiveTimer?: number })._customerIncentiveTimer = window.setTimeout(() => { void load().catch((error) => toast(error instanceof Error ? error.message : "Customer incentives unavailable", "error")); }, 250); });
    ["customer_id", "customer_type", "payment_status", "incentive_status", "from_date", "to_date"].forEach((name) => body.querySelector(`[name="${name}"]`)?.addEventListener("change", () => { void load().catch((error) => toast(error instanceof Error ? error.message : "Customer incentives unavailable", "error")); }));
    const visibilityManager = body.querySelector<HTMLSelectElement>('[name="show_customer_incentives_to_manager"]');
    const visibilitySalesperson = body.querySelector<HTMLSelectElement>('[name="show_customer_incentives_to_salesperson"]');
    const visibilitySave = body.querySelector<HTMLButtonElement>("[data-save-customer-incentive-visibility]");
    const initialVisibility = {
      manager: Boolean(visibility.show_customer_incentives_to_manager),
      salesperson: Boolean(visibility.show_customer_incentives_to_salesperson),
    };
    const syncVisibilitySaveState = () => {
      if (!visibilitySave) return;
      const manager = visibilityManager?.value === "true";
      const salesperson = visibilitySalesperson?.value === "true";
      visibilitySave.disabled = manager === initialVisibility.manager && salesperson === initialVisibility.salesperson;
    };
    visibilityManager?.addEventListener("change", syncVisibilitySaveState);
    visibilitySalesperson?.addEventListener("change", syncVisibilitySaveState);
    syncVisibilitySaveState();
    visibilitySave?.addEventListener("click", async () => {
      const manager = body.querySelector<HTMLSelectElement>('[name="show_customer_incentives_to_manager"]')?.value === "true";
      const salesperson = body.querySelector<HTMLSelectElement>('[name="show_customer_incentives_to_salesperson"]')?.value === "true";
      visibilitySave.disabled = true;
      try { await adminApi.updateCustomerIncentiveVisibility({ show_customer_incentives_to_manager: manager, show_customer_incentives_to_salesperson: salesperson }); toast("Customer incentive visibility saved"); }
      catch (error) { toast(error instanceof Error ? error.message : "Visibility settings could not be saved", "error"); }
      finally { syncVisibilitySaveState(); }
    });
    await load();
  } catch (error) {
    body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Customer incentives unavailable")}</div>`;
  }
  refreshIcons(page);
  return page;
}

/* Obsolete pre-workflow Credit Note implementations retained only in history.
Legacy implementation one:
  const page = pageScaffold("Sales", "Credit Notes", "Credit Notes linked to Order Confirmations and incentive adjustments.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.creditNotes();
    body.innerHTML = `<form class="panel stack-form" id="credit-note-entry"><span class="eyebrow">Create Credit Note</span><div class="form-grid"><label>Order Confirmation ID<input name="order_id" required></label><label>Amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Credit Note date<input name="credit_note_date" type="date" required></label><label>Reason<input name="reason" required></label></div><button class="button button-primary" type="submit">Create Credit Note</button></form>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Credit Note</th><th>Order Confirmation</th><th>Amount</th><th>Incentive deduction</th><th>Date</th><th>Status</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(String(item.credit_note_number ?? item._id))}</td><td>${escapeHtml(String(item.order_id ?? item.oc_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.incentive_deduction_amount ?? 0), "EUR")}</td><td>${formatDate(String(item.credit_note_date ?? item.created_at ?? ""))}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("file-minus", "No Credit Notes", "Approved Credit Notes will be linked to their Order Confirmation.")}`;
    body.querySelector<HTMLFormElement>("#credit-note-entry")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); try { await financeApi.createCreditNote({ order_id: data.get("order_id"), amount: Number(data.get("amount")), credit_note_date: data.get("credit_note_date"), reason: data.get("reason") }); toast("Credit Note created", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Credit Note could not be created", "error"); } });
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Credit Notes unavailable")}</div>`; }
  refreshIcons(page); return page;
}

Legacy implementation two:
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

*/
export async function creditNotesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Finance", "Credit Notes", "Create immutable adjustments against authorized Order Confirmations using historical snapshots.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  try {
    const [notes, confirmations, customers] = await Promise.all([financeApi.creditNotes(), orderApi.listConfirmations(undefined, 500), customerCompanyApi.list()]);
    const today = new Date().toISOString().slice(0, 10);
    body.innerHTML = `<form class="panel stack-form credit-note-workflow" id="credit-note-workflow"><div class="section-title"><div><span class="eyebrow">Create Credit Note</span><h2>Select the affected transaction</h2></div></div><div class="form-grid"><label>Customer<select name="customer_id" required><option value="">Select customer</option>${customers.items.map((customer) => `<option value="${escapeHtml(String(customer._id))}">${escapeHtml(String(customer.name ?? customer.company_name ?? customer._id))}</option>`).join("")}</select></label><label>Order Confirmation<select name="order_id" required disabled><option value="">Select customer first</option></select></label><label>Credit Note date<input name="credit_note_date" type="date" value="${today}" required></label><label>Reason<input name="reason" maxlength="2000" required></label></div><div data-credit-note-oc>${emptyState("file-check-2", "Select an Order Confirmation", "OC details and historical product snapshots will appear here.")}</div><div class="credit-note-lines" data-credit-note-lines></div><div class="credit-note-impact" data-credit-note-impact hidden><span>Credit total <strong data-credit-total>€0.00</strong></span><span>Historical incentive adjustment <strong data-credit-incentive>€0.00</strong></span></div><small class="field-error" data-credit-note-error></small><button class="button button-primary" type="submit" disabled>Create Credit Note</button></form>${notes.items.length ? `<div class="data-table panel"><table><thead><tr><th>Credit Note</th><th>Order Confirmation</th><th>Customer</th><th>Amount</th><th>Incentive deduction</th><th>Date</th><th>Status</th></tr></thead><tbody>${notes.items.map((item) => `<tr><td><strong>${escapeHtml(String(item.credit_note_number ?? item._id))}</strong></td><td>${escapeHtml(String(item.order_number ?? item.order_id ?? item.oc_id ?? "—"))}</td><td>${escapeHtml(String(item.customer_name ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.incentive_deduction_amount ?? 0), "EUR")}</td><td>${formatDate(String(item.credit_note_date ?? item.created_at ?? ""))}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("file-minus", "No Credit Notes", "Approved Credit Notes will be linked to their Order Confirmation.")}`;
    const form = body.querySelector<HTMLFormElement>("#credit-note-workflow")!;
    const customerSelect = form.elements.namedItem("customer_id") as HTMLSelectElement;
    const orderSelect = form.elements.namedItem("order_id") as HTMLSelectElement;
    const detail = body.querySelector<HTMLElement>("[data-credit-note-oc]")!;
    const lineContainer = body.querySelector<HTMLElement>("[data-credit-note-lines]")!;
    const submit = form.querySelector<HTMLButtonElement>('[type="submit"]')!;
    const orderCustomerId = (order: Record<string, unknown>) => String(order.customer_id ?? order.customer_company_id ?? order.company_id ?? "");
    const orderCustomerName = (order: Record<string, unknown>) => { const snapshot = (order.customer_snapshot as Record<string, unknown> | undefined) ?? (order.customer_company_snapshot as Record<string, unknown> | undefined) ?? {}; return String(snapshot.company_name ?? snapshot.name ?? orderCustomerId(order)); };
    customerSelect.addEventListener("change", () => {
      const options = confirmations.items.filter((order) => orderCustomerId(order) === customerSelect.value);
      orderSelect.innerHTML = `<option value="">Select Order Confirmation</option>${options.map((order) => `<option value="${escapeHtml(String(order._id))}">${escapeHtml(String(order.order_number ?? order.oc_number ?? order._id))} · ${formatMoney(Number(order.order_amount ?? (order.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</option>`).join("")}`;
      orderSelect.disabled = !options.length;
      detail.innerHTML = options.length ? '<p class="muted">Select an Order Confirmation to load its immutable snapshot.</p>' : '<div class="notice compact">No authorized Order Confirmations exist for this customer.</div>';
      lineContainer.innerHTML = ""; submit.disabled = true;
    });
    const updateImpact = () => {
      const inputs = [...lineContainer.querySelectorAll<HTMLInputElement>("[data-credit-line]")];
      const total = inputs.reduce((sum, input) => sum + Number(input.value || 0), 0);
      const deduction = inputs.reduce((sum, input) => sum + Number(input.value || 0) * Number(input.dataset.rate || 0) / 100, 0);
      const impact = body.querySelector<HTMLElement>("[data-credit-note-impact]"); if (impact) impact.hidden = inputs.length === 0;
      body.querySelector<HTMLElement>("[data-credit-total]")!.textContent = formatMoney(total, "EUR");
      body.querySelector<HTMLElement>("[data-credit-incentive]")!.textContent = formatMoney(deduction, "EUR");
      submit.disabled = total <= 0;
    };
    orderSelect.addEventListener("change", async () => {
      const orderId = orderSelect.value;
      if (!orderId) { lineContainer.innerHTML = ""; submit.disabled = true; return; }
      try {
        const [order, incentiveResult] = await Promise.all([orderApi.get(orderId), financeApi.incentives(`order_id=${encodeURIComponent(orderId)}`)]);
        const incentive = incentiveResult.items[0] ?? {};
        const lines = Array.isArray(incentive.incentive_lines) ? incentive.incentive_lines as Record<string, unknown>[] : [];
        detail.innerHTML = `<div class="detail-grid"><div><span>OC number</span><strong>${escapeHtml(String(order.order_number ?? order.oc_number ?? order._id))}</strong></div><div><span>Customer</span><strong>${escapeHtml(orderCustomerName(order))}</strong></div><div><span>OC date</span><strong>${formatDate(String(order.finalized_at ?? order.created_at ?? ""))}</strong></div><div><span>Total</span><strong>${formatMoney(Number(order.order_amount ?? (order.totals as Record<string, unknown> | undefined)?.grand_total ?? 0), "EUR")}</strong></div><div><span>Currency</span><strong>${escapeHtml(String(order.currency ?? "EUR"))}</strong></div><div><span>Status</span><strong>${escapeHtml(String(order.status ?? "Finalized"))}</strong></div></div>`;
        lineContainer.innerHTML = lines.length ? `<div class="section-title"><div><span class="eyebrow">Historical product snapshots</span><h2>Select affected lines</h2></div></div>${lines.map((line, index) => `<label class="credit-note-line"><input type="checkbox" data-credit-toggle><span><strong>${escapeHtml(String(line.product_name ?? line.product_id ?? `Product ${index + 1}`))}</strong><small>${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))} · Historical incentive ${Number(line.incentive_rate_snapshot ?? 0).toFixed(2)}%</small></span><input data-credit-line="${escapeHtml(String(line.oc_line_id ?? line.product_id ?? index))}" data-rate="${Number(line.incentive_rate_snapshot ?? 0)}" type="number" min="0" step="0.01" placeholder="Credit EUR" disabled></label>`).join("")}` : '<div class="notice warning">This OC has no product-level historical incentive snapshots, so a line-level Credit Note cannot be created safely.</div>';
        lineContainer.querySelectorAll<HTMLInputElement>("[data-credit-toggle]").forEach((toggle) => toggle.addEventListener("change", () => { const amount = toggle.closest("label")?.querySelector<HTMLInputElement>("[data-credit-line]"); if (amount) { amount.disabled = !toggle.checked; if (!toggle.checked) amount.value = ""; } updateImpact(); }));
        lineContainer.querySelectorAll<HTMLInputElement>("[data-credit-line]").forEach((input) => input.addEventListener("input", updateImpact));
        updateImpact();
      } catch (error) { detail.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Order Confirmation not found or not accessible")}</div>`; lineContainer.innerHTML = ""; submit.disabled = true; }
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault(); const data = new FormData(form);
      const lines = [...body.querySelectorAll<HTMLInputElement>("[data-credit-line]:not(:disabled)")].map((input) => ({ oc_line_id: input.dataset.creditLine, amount: Number(input.value || 0) })).filter((line) => line.amount > 0);
      const errorNode = body.querySelector<HTMLElement>("[data-credit-note-error]");
      if (!lines.length) { if (errorNode) errorNode.textContent = "Select at least one product line and enter a positive credit amount."; return; }
      submit.disabled = true; if (errorNode) errorNode.textContent = "";
      try { await financeApi.createCreditNote({ order_id: data.get("order_id"), lines, credit_note_date: data.get("credit_note_date"), reason: data.get("reason") }); toast("Credit Note created", "info"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/credit-notes" })); }
      catch (error) { if (errorNode) errorNode.textContent = error instanceof Error ? error.message : "Credit Note could not be created"; submit.disabled = false; }
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
    const result = await orderApi.listConfirmations(undefined, 500);
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
