import { financeApi } from "../api";
import { refreshIcons } from "../components/icons";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";

export async function paymentsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Payments", "Payments / Banking", "Record customer payments and submit them for Superadmin confirmation.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.payments();
    body.innerHTML = `<form class="panel stack-form" id="payment-entry"><span class="eyebrow">Record payment</span><div class="form-grid"><label>Order Confirmation ID<input name="order_id" required></label><label>Amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Payment date<input name="payment_date" type="date" required></label><label>Bank / UTR<input name="utr" placeholder="Optional reference"></label></div><button class="button button-primary" type="submit">Record &amp; submit for confirmation</button></form>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Order Confirmation</th><th>Customer</th><th>Amount</th><th>Payment date</th><th>Status</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(String(item.order_id ?? item.oc_id ?? "—"))}</td><td>${escapeHtml(String((item.customer_snapshot as Record<string, unknown> | undefined)?.name ?? item.customer_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td>${formatDate(String(item.payment_date ?? item.created_at ?? ""))}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("landmark", "No payments recorded", "Payments entered by the team will appear here.")}`;
    body.querySelector<HTMLFormElement>("#payment-entry")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); try { const payment = await financeApi.createPayment({ order_id: data.get("order_id"), amount: Number(data.get("amount")), payment_date: data.get("payment_date"), utr: data.get("utr") }); await financeApi.submitPayment(String(payment._id)); toast("Payment submitted for Superadmin confirmation", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Payment could not be recorded", "error"); } });
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Payments unavailable")}</div>`; }
  refreshIcons(page); return page;
}

export async function incentivesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Incentives", "Incentive Management", "Track salesperson incentive snapshots, payments and deductions.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.incentives();
    body.innerHTML = result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Order Confirmation</th><th>Sales Person</th><th>Rate</th><th>Gross</th><th>Credit deductions</th><th>Net payable</th><th>Status</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(String(item.oc_number ?? item.order_number ?? item.order_id ?? "—"))}</td><td>${escapeHtml(String((item.salesperson_snapshot as Record<string, unknown> | undefined)?.name ?? item.salesperson_id ?? "—"))}</td><td>${Number(item.incentive_percentage_snapshot ?? 0).toFixed(2)}%</td><td class="money">${formatMoney(Number(item.gross_incentive_amount ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.credit_note_deduction ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.net_payable_incentive ?? 0), "EUR")}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("badge-euro", "No incentives yet", "An incentive snapshot is created when an Order Confirmation is created.");
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Incentives unavailable")}</div>`; }
  refreshIcons(page); return page;
}

export async function creditNotesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Sales", "Credit Notes", "Credit Notes linked to Order Confirmations and incentive adjustments.");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try {
    const result = await financeApi.creditNotes();
    body.innerHTML = `<form class="panel stack-form" id="credit-note-entry"><span class="eyebrow">Create Credit Note</span><div class="form-grid"><label>Order Confirmation ID<input name="order_id" required></label><label>Amount (EUR)<input name="amount" type="number" min="0.01" step="0.01" required></label><label>Credit Note date<input name="credit_note_date" type="date" required></label><label>Reason<input name="reason" required></label></div><button class="button button-primary" type="submit">Create Credit Note</button></form>${result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Credit Note</th><th>Order Confirmation</th><th>Amount</th><th>Incentive deduction</th><th>Date</th><th>Status</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${escapeHtml(String(item.credit_note_number ?? item._id))}</td><td>${escapeHtml(String(item.order_id ?? item.oc_id ?? "—"))}</td><td class="money">${formatMoney(Number(item.amount ?? 0), "EUR")}</td><td class="money">${formatMoney(Number(item.incentive_deduction_amount ?? 0), "EUR")}</td><td>${formatDate(String(item.credit_note_date ?? item.created_at ?? ""))}</td><td>${statusBadge(String(item.status ?? ""))}</td></tr>`).join("")}</tbody></table></div>` : emptyState("file-minus", "No Credit Notes", "Approved Credit Notes will be linked to their Order Confirmation.")}`;
    body.querySelector<HTMLFormElement>("#credit-note-entry")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); try { await financeApi.createCreditNote({ order_id: data.get("order_id"), amount: Number(data.get("amount")), credit_note_date: data.get("credit_note_date"), reason: data.get("reason") }); toast("Credit Note created", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Credit Note could not be created", "error"); } });
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Credit Notes unavailable")}</div>`; }
  refreshIcons(page); return page;
}
