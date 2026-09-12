import { adminApi, customerCompanyApi, financeApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";

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

export async function paymentsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Payments", "Payments / Banking", "Record customer payments and submit them for Superadmin confirmation.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.payments();
    const canConfirm = appStore.state.user?.role_id === "superadmin" && appStore.state.user?.permissions.includes("payments.confirm");
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

export async function incentivesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Incentives", "Incentive Management", "Track sales incentives generated from orders and activated after payment receipt.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  const canViewUsers = appStore.state.user?.permissions.includes("users.view") || appStore.state.user?.role_id === "superadmin";
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
  const renderRows = (items: Record<string, unknown>[]) => items.length ? `<div class="data-table panel"><table><thead><tr><th>OC Number</th><th>Customer</th><th>Sales Person</th><th>Products / Categories</th><th>Order amount</th><th>Incentive %</th><th>Incentive amount</th><th>Payment status</th><th>Incentive status</th><th>Created</th><th>Actions</th></tr></thead><tbody>${items.map((item) => { const salesperson = item.salesperson_snapshot as Record<string, unknown> | undefined; const customer = (item.customer_snapshot as Record<string, unknown> | undefined) ?? {}; const status = String(item.status ?? "PENDING PAYMENT"); const paymentStatus = String(item.payment_status ?? (item.payment_confirmation_date ? "Paid" : "Pending Payment")); const lines = Array.isArray(item.incentive_lines) ? item.incentive_lines as Record<string, unknown>[] : []; const productSummary = lines.length ? lines.map((line) => `${escapeHtml(String(line.product_name ?? line.product_id ?? "Product"))} · ${escapeHtml(String(line.category_name ?? line.category_id ?? "Category"))} · ${Number(line.incentive_rate_snapshot ?? 0).toFixed(0)}% · ${formatMoney(Number(line.incentive_amount ?? 0), "EUR")}`).join("<br>") : "—"; const rateSummary = item.incentive_percentage_snapshot == null ? "By category" : `${Number(item.incentive_percentage_snapshot).toFixed(0)}%`; return `<tr><td><a href="/orders/${encodeURIComponent(String(item.order_id ?? ""))}" data-route="/orders/${encodeURIComponent(String(item.order_id ?? ""))}"><strong>${escapeHtml(String(item.oc_number ?? item.order_number ?? item.order_id ?? "—"))}</strong></a></td><td>${escapeHtml(String(customer.company_name ?? customer.name ?? item.customer_id ?? "—"))}</td><td>${escapeHtml(String(salesperson?.name ?? item.salesperson_id ?? "—"))}</td><td>${productSummary}</td><td class="money">${formatMoney(Number(item.order_amount ?? 0), "EUR")}</td><td>${rateSummary}</td><td class="money">${formatMoney(Number(item.gross_incentive_amount ?? 0), "EUR")}</td><td>${statusBadge(paymentStatus)}</td><td>${statusBadge(status)}</td><td>${formatDate(String(item.created_at ?? ""))}</td><td>${paymentStatus.toLowerCase() === "paid" ? "—" : `<a class="button button-quiet" href="/payments" data-route="/payments">Confirm in Payments</a>`}</td></tr>`; }).join("")}</tbody></table></div>` : emptyState("badge-euro", "No incentives yet", "An incentive snapshot is created when an Order Confirmation is created.");
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
    const [customerResult, userResult] = await Promise.all([customerCompanyApi.list().catch(() => ({ items: [], total: 0 })), canViewUsers ? adminApi.users().catch(() => ({ items: [], total: 0 })) : Promise.resolve({ items: [], total: 0 })]);
    customers = customerResult.items as unknown as Record<string, unknown>[];
    users = userResult.items;
    body.innerHTML = `<section class="panel quotation-filters incentive-filters"><div class="quotation-filter-head"><div><span class="eyebrow">Payment-linked earnings</span><h2>Incentive history</h2><p>Incentives remain pending until a payment receipt is confirmed.</p></div><span data-incentive-count>Loading…</span></div><div class="quotation-filter-grid"><label class="filter-search"><span>Search</span><div class="field-search"><i data-lucide="search"></i><input name="search" placeholder="Sales person, customer or order" aria-label="Search incentives"></div></label>${users.length ? `<label><span>Sales Person</span><select name="salesperson_id"><option value="">All salespeople</option>${users.map((user) => `<option value="${escapeHtml(String(user._id))}">${escapeHtml(String(user.name ?? user.email ?? user._id))}</option>`).join("")}</select></label>` : ""}<label><span>Customer</span><select name="customer_id"><option value="">All customers</option>${customers.map((customer) => `<option value="${escapeHtml(String(customer._id))}">${escapeHtml(String(customer.company_name ?? customer.name ?? customer._id))}</option>`).join("")}</select></label><label><span>Payment status</span><select name="payment_status"><option value="">All payment statuses</option><option value="pending">Pending Payment</option><option value="paid">Paid</option></select></label><label><span>Incentive status</span><select name="status"><option value="">All incentive statuses</option><option>PENDING PAYMENT</option><option>ACTIVE</option><option>DUE</option><option>OVERDUE</option><option>PAID</option></select></label><label><span>From date</span><input name="from_date" type="date"></label><label><span>To date</span><input name="to_date" type="date"></label><input name="order_id" type="hidden" value="${escapeHtml(initialOrder)}"></div></section><div data-incentive-summary></div><div data-incentive-results></div>`;
    const filterGrid = body.querySelector<HTMLElement>(".incentive-filters .quotation-filter-grid");
    if (filterGrid) filterGrid.insertAdjacentHTML("beforeend", '<label><span>Category</span><select name="category_id"><option value="">All categories</option><option value="blankets">Blanket</option><option value="mpacks">Underpacking</option><option value="chemicals">Chemical</option></select></label><label><span>Product ID</span><input name="product_id" placeholder="Product ID"></label>');
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
