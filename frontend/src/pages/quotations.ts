import { cartApi, customerCompanyApi, orderApi, quotationApi } from "../api";
import { refreshIcons } from "../components/icons";
import { createWatermark } from "../components/watermark";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { CartItem, PriceLine, Quotation } from "../types/domain";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";
import { openCartItemEditor } from "./catalog";

interface PreviewBundle { payload: Record<string, unknown>; document: Quotation }

const PDF_API_ORIGIN = (import.meta.env.VITE_API_ORIGIN ?? "").replace(/\/$/, "");
const quotationPdfUrl = (quotationId: string, preview = false): string =>
  `${PDF_API_ORIGIN}/api/v1/quotations/${encodeURIComponent(quotationId)}/pdf${preview ? "?preview=true" : ""}`;

function commercialUnit(line: PriceLine | undefined, item?: CartItem): string {
  const value = String(line?.commercial_unit ?? "").toLowerCase();
  if (value) return value;
  if (item?.product_id === "mtech-mpack" || String(line?.product_name ?? "").toLowerCase().includes("mpack") || line?.price_per_box_eur !== undefined) return "box";
  return "pc";
}

function quantityMarkup(line: PriceLine, item?: CartItem): string {
  const quantity = Number(line?.quantity ?? 1);
  const quantityText = Number.isInteger(quantity) ? String(quantity) : quantity.toFixed(2);
  const unit = commercialUnit(line, item);
  const unitText = unit === "box" ? "Box" : unit === "pc" ? "Pc" : unit;
  const sheets = Number(line?.sheets_per_box ?? line?.configuration?.sheets_per_box ?? 0);
  return `<strong>${escapeHtml(quantityText)} ${escapeHtml(unitText)}</strong>${unit === "box" && sheets > 0 ? `<span>(${sheets.toLocaleString()} sheets)</span>` : ""}`;
}

function isMpackLine(line: PriceLine | undefined, configuration: Record<string, unknown>): boolean {
  const productId = String(line?.product_id ?? "").toLowerCase().replace(/[-_]/g, "");
  return line?.commercial_unit === "box" || productId === "mtechmpack"
    || String(line?.product_name ?? "").toLowerCase().includes("mpack")
    || line?.price_per_box_eur !== undefined || configuration.machine_model !== undefined
    || configuration.width_mm !== undefined || configuration.underpacking_type !== undefined;
}

function mpackSize(configuration: Record<string, unknown>): string | null {
  const width = configuration.width_mm;
  const length = configuration.length_mm;
  if (width !== undefined && length !== undefined) return `${width} × ${length} mm`;
  const raw = configuration.size ?? configuration.machine_size ?? configuration.size_mm;
  const values = String(raw ?? "").match(/\d+(?:\.\d+)?/g) ?? [];
  if (values.length >= 2) return `${values[0]} × ${values[1]} mm`;
  if (values.length === 1) return `${values[0]} mm`;
  return length !== undefined ? `${length} mm` : null;
}

function openQuotationEmailComposer(quote: Quotation, onSent: () => Promise<void> | void): void {
  const recipient = quote.customer_snapshot?.email ?? "";
  const resend = quote.status === "Sent" || quote.status === "send_failed";
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form email-composer-form"><label>To<input name="to" type="email" value="${escapeHtml(recipient)}" disabled></label><p class="form-hint">Customer-facing CC/BCC routing is managed centrally in Settings → Zoho Mail.</p><label>Subject<input name="subject" value="Quotation ${escapeHtml(quote.quotation_number)} - Moneda Technologies" required></label><label>Message<textarea name="message" rows="6" required>Please find quotation ${escapeHtml(quote.quotation_number)} attached.</textarea></label><div class="attachment-chip"><i data-lucide="paperclip"></i><span>${escapeHtml(quote.quotation_number)}.pdf</span><small>PDF attachment</small></div><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit"><i data-lucide="send"></i>Send Email</button></div></form>`;
  const submit = content.querySelector<HTMLButtonElement>("[type=submit]");
  if (submit && resend) submit.innerHTML = '<i data-lucide="send"></i>Send Again';
  const dialog = openModal(resend ? "Send quotation again" : "Email quotation", content, "wide");
  const form = content.querySelector<HTMLFormElement>("form")!;
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form); const button = form.querySelector<HTMLButtonElement>("[type=submit]")!;
    button.disabled = true; button.textContent = "Sending…";
    try {
      await quotationApi.send(quote._id, { subject: String(data.get("subject") ?? ""), message: String(data.get("message") ?? "") });
      dialog.close(); toast(resend ? `${quote.quotation_number} sent again successfully to ${recipient}.` : `${quote.quotation_number} sent successfully to ${recipient}.`); await onSent();
    } catch (error) { toast(error instanceof Error ? error.message : "Email failed. Retry when configuration is available.", "error"); button.disabled = false; button.innerHTML = '<i data-lucide="send"></i>Retry'; refreshIcons(button); }
  });
  refreshIcons(content);
}

function configurationSummary(item: CartItem | { configuration: Record<string, unknown>; pricing_preview?: CartItem["pricing_preview"] }): string {
  const configuration = item.configuration;
  const line = item.pricing_preview;
  const parts: string[] = [];
  const isMpack = isMpackLine(line, configuration);
  if (isMpack) {
    const manufacturer = configuration.manufacturer ?? configuration.machine_manufacturer;
    const model = configuration.machine_model ?? configuration.model;
    if (manufacturer) parts.push(`Machine: ${manufacturer}`);
    if (model) parts.push(`Model: ${model}`);
    const thicknessMm = Number(configuration.thickness_mm ?? 0);
    const micron = Number(configuration.thickness_micron ?? (thicknessMm ? thicknessMm * 1000 : 0));
    if (thicknessMm || micron) parts.push(`Thickness: ${(thicknessMm || micron / 1000).toFixed(2)} mm (${Math.round(micron)} micron)`);
    const size = mpackSize(configuration);
    if (size) parts.push(`Size: ${size}`);
  }
  if (!isMpack && configuration.machine) parts.push(String(configuration.machine));
  if (!isMpack && configuration.length && configuration.width) parts.push(`${configuration.length} × ${configuration.width} ${configuration.dimension_unit ?? "mm"}`);
  if (!isMpack && configuration.thickness_mm) parts.push(`${Number(configuration.thickness_mm).toFixed(2)} mm`);
  if (!isMpack && configuration.thickness_micron) parts.push(`${configuration.thickness_micron} micron`);
  if (configuration.format_type) parts.push(configuration.format_type === "bar_format" ? "Bar Format" : "Cut Format");
  if (configuration.requested_litres) parts.push(`${configuration.requested_litres} L requested`);
  const adjustments = item.pricing_preview?.adjustments ?? [];
  adjustments.forEach((row) => parts.push(`${row.label}${row.article_no ? ` · Art. ${row.article_no}` : ""}${row.quantity ? ` × ${row.quantity}` : ""}`));
  return parts.join(" · ") || "Standard configuration";
}

function discountText(value: unknown, prefix = " · Discount ", suffix = "%"): string {
  const discount = Number(value ?? 0);
  return Number.isFinite(discount) && discount > 0 ? `${prefix}${discount}${suffix}` : "";
}

function cartLine(item: CartItem, editableOrIndex: boolean | number = true): string {
  const editable = typeof editableOrIndex === "boolean" ? editableOrIndex : true;
  const line = item.pricing_preview;
  const masterUnitPrice = line.master_unit_price ?? (line.quantity ? (line.master_subtotal ?? 0) / line.quantity : line.master_subtotal ?? 0);
  const unit = commercialUnit(line, item);
  const unitText = unit === "box" ? "Box" : unit === "pc" ? "Pc" : unit;
  const displayCurrency = line.display_currency ?? line.currency;
  const displayReference = displayCurrency !== "EUR"
    ? `<span class="cart-reference">${escapeHtml(displayCurrency)} reference ${formatMoney(line.display_final_total ?? 0, displayCurrency)}</span>`
    : "";
  return `<article class="cart-line${editable ? " cart-line-editable" : " quotation-line-readonly"}"><div class="cart-icon"><i data-lucide="package"></i></div><div class="cart-product">${editable ? `<span class="article-label">Art. ${escapeHtml(line.article_no ?? item.product_id)}</span>` : ""}<strong>${escapeHtml(line.product_name)}</strong>${line.description ? `<span class="cart-product-description">${escapeHtml(line.description)}</span>` : ""}<span>${escapeHtml(configurationSummary(item))}</span><small>EUR Unit Price ${formatMoney(masterUnitPrice, "EUR")} / ${escapeHtml(unitText)}${discountText(line.discount_percent)}</small></div><div class="cart-qty"><small>Qty</small>${quantityMarkup(line, item)}</div><div class="cart-price"><small>EUR commercial</small><strong>${formatMoney(line.master_final_total ?? 0, "EUR")}</strong>${displayReference}</div>${editable ? `<div class="cart-actions"><button class="button button-quiet edit-cart" data-id="${escapeHtml(item._id)}"><i data-lucide="pencil"></i>Edit</button><button class="button button-quiet remove-cart" data-id="${escapeHtml(item._id)}"><i data-lucide="trash-2"></i>Delete</button></div>` : ""}</article>`;
}

function quotationRows(quotations: Quotation[], emptyTitle = "No quotations match these filters.", emptyCopy = "Clear filters or create a new quotation from the Cart."): string {
  if (!quotations.length) return `<div class="quotation-empty">${emptyState("file-text", emptyTitle, emptyCopy)}</div>`;
  const canSend = appStore.can("quotations.send");
  const canDownload = appStore.can("quotations.download");
  const canConvert = appStore.can("orders.create");
  const canDelete = appStore.can("quotations.delete") || appStore.can("quotations.archive");
  const canRestore = appStore.can("quotations.restore") || appStore.can("quotations.delete");
  const rows = quotations.map((quote) => {
    const status = quote.status.toLowerCase();
    const resend = status === "sent" || status === "send_failed";
    const sendAction = canSend && (status === "draft" || resend)
      ? `<button class="icon-button send-quote" data-id="${escapeHtml(quote._id)}" aria-label="${resend ? "Send quotation again" : "Send quotation"}" title="${resend ? "Send quotation again" : "Send quotation"}"><i data-lucide="mail"></i></button>`
      : "";
    const lifecycleAction = quote.status === "archived"
      ? (canRestore ? `<button class="icon-button restore-quote" data-id="${escapeHtml(quote._id)}" aria-label="Restore quotation" title="Restore quotation"><i data-lucide="archive-restore"></i></button>` : "")
      : canDelete ? `<button class="icon-button ${quote.status === "Draft" ? "delete-quote" : "archive-quote"}" data-id="${escapeHtml(quote._id)}" aria-label="${quote.status === "Draft" ? "Delete quotation" : "Archive quotation"}" title="${quote.status === "Draft" ? "Delete quotation" : "Archive quotation"}"><i data-lucide="${quote.status === "Draft" ? "trash-2" : "archive"}"></i></button>` : "";
    return `<tr><td><strong>${escapeHtml(quote.quotation_number)}</strong><small>${quote.lines.length} line${quote.lines.length === 1 ? "" : "s"}</small></td><td>${escapeHtml(quote.customer_snapshot?.company_name ?? quote.customer_snapshot?.name ?? "Customer")}</td><td>${statusBadge(quote.status)}</td><td>${formatDate(quote.created_at)}</td><td><span class="currency-tag">${escapeHtml(quote.currency)}</span></td><td class="money">${formatMoney(quote.totals.grand_total, quote.currency)}</td><td><div class="row-actions"><a class="icon-button" href="/quotation-preview?id=${encodeURIComponent(quote._id)}" target="_blank" aria-label="View quotation" title="View quotation"><i data-lucide="eye"></i></a>${canDownload ? `<a class="icon-button" href="${quotationPdfUrl(quote._id)}" aria-label="Download PDF" title="Download PDF"><i data-lucide="download"></i></a><button class="icon-button print-quote" data-id="${escapeHtml(quote._id)}" aria-label="Print quotation" title="Print quotation"><i data-lucide="printer"></i></button>` : ""}${sendAction}${canSend ? `<button class="icon-button whatsapp-quote" data-id="${escapeHtml(quote._id)}" aria-label="Send via WhatsApp" title="Send via WhatsApp"><i data-lucide="message-circle"></i></button>` : ""}${canConvert && quote.status !== "Converted to Order" ? `<button class="icon-button convert-quote" data-id="${escapeHtml(quote._id)}" aria-label="Convert to order" title="Convert to order"><i data-lucide="shopping-bag"></i></button>` : ""}${lifecycleAction}</div></td></tr>`;
  }).join("");
  return `<div class="data-table panel"><table><thead><tr><th>Quotation</th><th>Customer</th><th>Status</th><th>Created</th><th>Currency</th><th>Total</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export async function legacyQuotationPreparationPage(): Promise<HTMLElement> {
  // Kept only as an import-compatible alias while old clients transition.
  return quotationPreparationPage();
  /* Historical implementation intentionally unreachable.
  const activeCustomer = appStore.state.customer;
  const customerCompany = appStore.state.customerCompany ?? appStore.state.company;
  const customerId = activeCustomer?.customer_id ?? activeCustomer?._id ?? customerCompany?._id;
  const customerDisplayName = activeCustomer?.company_name ?? activeCustomer?.name ?? customerCompany?.name;
  const page = pageScaffold("Quotation Workflow", "Quotation Preparation (legacy)", customerDisplayName ? `Quotation For: ${customerDisplayName}` : "Select a customer before using the quotation cart.");
  page.classList.add("quotation-workflow-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(7);
  if (!customerId || !customerCompany) { body.innerHTML = emptyState("building-2", "Customer selection required", "Choose a customer before using the quotation cart."); return page; }
  let cartItems: CartItem[] = []; let customers: Customer[] = []; let quotations: Quotation[] = [];

  const load = async () => {
    const [cart, quoteResult] = await Promise.all([
      cartApi.get(customerId, appStore.state.currency), quotationApi.list(customerId),
    ]);
    cartItems = cart.items; quotations = quoteResult.items;
    appStore.set({ cartCount: cartItems.length });
    const showTax = false;
    body.innerHTML = `<div class="workspace-steps"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="done"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span class="done"><b>3</b>Configure</span><i data-lucide="chevron-right"></i><span class="active"><b>4</b>Quotation</span></div><div class="quote-layout"><section class="quote-main"><div class="section-title"><div><span class="eyebrow">${escapeHtml(appStore.state.currency)}</span><h2>Current quotation items</h2></div><div class="section-title-actions"><span class="count-badge">${cartItems.length} ${cartItems.length === 1 ? "line" : "lines"}</span>${cartItems.length ? '<button id="clear-cart" class="button button-quiet"><i data-lucide="trash-2"></i>Clear</button>' : ""}</div></div>${cartItems.length ? `<div class="cart-lines">${cartItems.map(cartLine).join("")}</div>` : emptyState("shopping-cart", "Quotation cart is empty", "Use Calculator in the sidebar to configure a product.")}<div class="section-title quote-history-title"><div><span class="eyebrow">Saved Records</span><h2>Quotation History</h2></div><span>${quotations.length} records</span></div>${quotationRows(quotations)}</section><aside class="quote-summary panel"><div class="summary-head"><span class="eyebrow">From Moneda Technologies</span><h2>Quotation Details</h2><p>Every line is recalculated before preview and again before saving.</p></div><div class="summary-row"><span>Subtotal</span><strong>${formatMoney(Number(cart.totals.subtotal ?? 0), appStore.state.currency)}</strong></div>${Number(cart.totals.discount_amount ?? 0) ? `<div class="summary-row"><span>Discount</span><strong>- ${formatMoney(Number(cart.totals.discount_amount), appStore.state.currency)}</strong></div>` : ""}${showTax ? `<div class="summary-row"><span>Taxable Amount</span><strong>${formatMoney(Number(cart.totals.taxable_amount ?? 0), appStore.state.currency)}</strong></div><div class="summary-row"><span>GST / Tax</span><strong>${formatMoney(Number(cart.totals.tax_amount ?? 0), appStore.state.currency)}</strong></div>` : ""}<div class="summary-total"><span>Estimated Total</span><strong>${formatMoney(Number(cart.totals.grand_total ?? 0), appStore.state.currency)}</strong></div><form id="quote-details-form" class="stack-form"><label>Customer<input value="${escapeHtml(customerCompany.name)}" disabled><small class="customer-detail">This quotation will be sent from Moneda Technologies to the selected customer.</small></label><div class="form-grid"><label>Quotation Currency<select name="currency"><option ${appStore.state.currency === "EUR" ? "selected" : ""}>EUR</option><option ${appStore.state.currency === "USD" ? "selected" : ""}>USD</option><option ${appStore.state.currency === "INR" ? "selected" : ""}>INR</option></select></label><label>Proforma Validity (days)<input name="proforma_validity_days" type="number" min="1" max="365" value="30" required></label></div><label>Payment Terms<select name="payment_terms"><option>Advance</option><option>POD</option><option>15 Days</option><option>30 Days</option></select></label><label>Transport<select name="transport_mode"><option value="by_consignee">By Consignee</option><option value="by_moneda_team">By Moneda Team</option></select></label><label class="transport-charge" hidden>Transport Charges (${escapeHtml(appStore.state.currency)})<input name="transport_charges" type="number" min="0" step="0.01" placeholder="Enter freight charge"></label><label>Notes<textarea name="notes" rows="3" placeholder="Optional commercial notes"></textarea></label><label class="check-row"><input name="create_lead" type="checkbox" checked><span>Create a linked CRM lead and follow-up</span></label><label>Follow-up Date<input name="follow_up_date" type="date"></label><button class="button button-primary button-full" ${!cartItems.length ? "disabled" : ""}><i data-lucide="eye"></i>Preview Quotation</button></form></aside></div>`;
    const customerNotes = body.querySelector<HTMLTextAreaElement>('[name="notes"]');
    if (customerNotes) {
      customerNotes.placeholder = "Add quotation-specific notes for the customer";
      const label = customerNotes.closest("label");
      if (label?.firstChild) label.firstChild.textContent = "Customer Notes";
    }
    bindActions(); refreshIcons(body);
  };

  const bindActions = () => {
    body.querySelectorAll<HTMLButtonElement>(".edit-cart").forEach((button) => button.addEventListener("click", async () => {
      const item = cartItems.find((row) => row._id === button.dataset.id); if (!item) return;
      try { await openCartItemEditor(item, load); } catch (error) { toast(error instanceof Error ? error.message : "Could not open editor", "error"); }
    }));
    body.querySelectorAll<HTMLButtonElement>(".remove-cart").forEach((button) => button.addEventListener("click", async () => {
      if (!window.confirm("Remove this product from the quotation cart?")) return;
      try { await cartApi.remove(button.dataset.id!); toast("Item removed", "info"); await load(); } catch (error) { toast(error instanceof Error ? error.message : "Could not remove item", "error"); }
    }));
    body.querySelector<HTMLButtonElement>("#clear-cart")?.addEventListener("click", async () => {
      if (!window.confirm(`Clear every item from the ${customerDisplayName} quotation cart?`)) return;
      try { await cartApi.clear(customerId); toast("Cart cleared", "info"); await load(); } catch (error) { toast(error instanceof Error ? error.message : "Could not clear cart", "error"); }
    });
    body.querySelectorAll<HTMLButtonElement>(".send-quote").forEach((button) => button.addEventListener("click", () => { const quote = quotations.find((item) => item._id === button.dataset.id); if (quote) openQuotationEmailComposer(quote, load); }));
    body.querySelectorAll<HTMLButtonElement>(".whatsapp-quote").forEach((button) => button.addEventListener("click", async () => { button.disabled = true; try { const result = await quotationApi.whatsapp(button.dataset.id!); toast(result.delivery.status === "mocked" ? "WhatsApp share recorded in mock mode" : "WhatsApp share queued", "info"); } catch (error) { toast(error instanceof Error ? error.message : "Could not share quotation", "error"); } finally { button.disabled = false; } }));
    body.querySelectorAll<HTMLButtonElement>(".convert-quote").forEach((button) => button.addEventListener("click", async () => { button.disabled = true; try { await quotationApi.convert(button.dataset.id!); toast("Quotation converted to order"); await load(); } catch (error) { toast(error instanceof Error ? error.message : "Could not create order", "error"); button.disabled = false; } }));
    const form = body.querySelector<HTMLFormElement>("#quote-details-form");
    const transport = form?.querySelector<HTMLSelectElement>("[name=transport_mode]");
    const charge = form?.querySelector<HTMLElement>(".transport-charge");
    transport?.addEventListener("change", () => {
      const required = transport.value === "by_moneda_team";
      if (charge) charge.hidden = !required;
      const input = charge?.querySelector<HTMLInputElement>("input");
      if (input) { input.required = required; if (!required) input.value = ""; }
    });
    form?.querySelector<HTMLInputElement>("[name=customer]")?.addEventListener("change", (event) => {
      const customer = customers.find((item) => customerChoice(item).toLowerCase() === (event.currentTarget as HTMLInputElement).value.trim().toLowerCase());
      const detail = form.querySelector<HTMLElement>(".customer-detail");
      if (detail) detail.textContent = customer ? [customer.contact_name, customer.email, customer.phone].filter(Boolean).join(" · ") : "Select a customer from the list.";
    });
    form?.addEventListener("submit", async (event) => {
      event.preventDefault(); const data = new FormData(form);
      const customerValue = String(data.get("customer") ?? "").trim().toLowerCase();
      const customer = customers.find((item) => customerChoice(item).toLowerCase() === customerValue);
      if (customerValue && !customer) { toast("Select a customer contact from the searchable list, or leave it blank", "error"); return; }
      const previewWindow = window.open("about:blank", "_blank");
      if (!previewWindow) { toast("Allow pop-ups to open the quotation preview", "error"); return; }
      previewWindow.document.write("<title>Preparing quotation preview…</title><p style='font:16px sans-serif;padding:40px'>Preparing secure quotation preview…</p>");
      const payload: Record<string, unknown> = {
        customer_id: customerId, currency: data.get("currency"),
        proforma_validity_days: Number(data.get("proforma_validity_days")), payment_terms: data.get("payment_terms"),
        transport_mode: data.get("transport_mode"), transport_charges: data.get("transport_mode") === "by_moneda_team" ? Number(data.get("transport_charges")) : 0,
        customer_notes: data.get("notes"), create_lead: data.get("create_lead") === "on", follow_up_date: data.get("follow_up_date") || null,
        reminder_frequency: "none", idempotency_key: crypto.randomUUID(),
      };
      try {
        const document = await quotationApi.preview(payload);
        const key = `moneda-quotation-preview-${crypto.randomUUID()}`;
        localStorage.setItem(key, JSON.stringify({ payload, document } satisfies PreviewBundle));
        previewWindow.location.href = `/quotation-preview?draft=${encodeURIComponent(key)}`;
      } catch (error) { previewWindow.close(); toast(error instanceof Error ? error.message : "Quotation preview failed", "error"); }
    });
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Quotations unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
  */
}

function previewDocumentMarkup(quote: Quotation, saved: boolean, pdfSource: string): string {
  const resend = quote.status === "Sent" || quote.status === "send_failed";
  const sendAction = saved
    ? (appStore.can("quotations.send") && (quote.status === "Draft" || resend) ? `<button class="button button-primary preview-send"><i data-lucide="send"></i>${resend ? "Send Again" : "Send Email"}</button>` : "")
    : '<button class="button button-primary preview-generate"><i data-lucide="send"></i>Generate & Send Email</button>';
  return `<div class="preview-toolbar"><button class="button button-quiet preview-back"><i data-lucide="arrow-left"></i>Back to Quotation Preparation</button><div><button class="button button-quiet preview-print"><i data-lucide="printer"></i>Print</button>${saved ? `<a class="button button-secondary" href="${quotationPdfUrl(quote._id!)}"><i data-lucide="download"></i>Download PDF</a>` : '<button class="button button-secondary preview-download" disabled title="Generate and save the quotation first"><i data-lucide="download"></i>Download PDF</button>'}${sendAction}</div></div><iframe class="quotation-pdf-preview" title="Quotation ${escapeHtml(quote.quotation_number)}" src="${pdfSource}"></iframe>`;
}

export async function quotationPreviewPage(): Promise<HTMLElement> {
  const root = document.createElement("div"); root.className = "quotation-preview-page";
  const watermark = createWatermark(appStore.state.user, appStore.state.watermarkEnabled);
  const params = new URLSearchParams(location.search);
  const savedId = params.get("id"); const draftKey = params.get("draft");
  let bundle: PreviewBundle | null = null;
  try {
    if (savedId) bundle = { payload: {}, document: await quotationApi.get(savedId) };
    else if (draftKey) bundle = JSON.parse(localStorage.getItem(draftKey) ?? "null") as PreviewBundle | null;
  } catch (error) { root.innerHTML = emptyState("file-warning", "Preview unavailable", error instanceof Error ? error.message : "The quotation preview could not be loaded."); return root; }
  if (!bundle) { root.innerHTML = emptyState("file-warning", "Preview expired", "Return to the cart and create a new preview."); return root; }
  const render = () => {
    const saved = Boolean(bundle?.document._id && !bundle.document.preview);
    const pdfSource = saved
      ? quotationPdfUrl(bundle!.document._id!, true)
      : `data:application/pdf;base64,${bundle!.document.preview_pdf_base64 ?? ""}`;
    root.innerHTML = previewDocumentMarkup(bundle!.document, saved, pdfSource);
    root.prepend(watermark);
    root.querySelector(".preview-back")?.addEventListener("click", () => { if (window.opener) { window.close(); return; } window.location.assign(draftKey ? `/quotation/create?draft=${encodeURIComponent(draftKey)}` : "/quotation/create"); });
    root.querySelector(".preview-print")?.addEventListener("click", () => root.querySelector<HTMLIFrameElement>(".quotation-pdf-preview")?.contentWindow?.print());
    const sendSaved = async (quote: Quotation, button: HTMLButtonElement) => {
      const resend = quote.status === "Sent" || quote.status === "send_failed";
      button.disabled = true; button.textContent = "Sending...";
      try {
        const sent = await quotationApi.send(quote._id!, {}); bundle = { ...bundle!, document: sent };
        if (draftKey) localStorage.setItem(draftKey, JSON.stringify(bundle));
        toast(resend ? `${sent.quotation_number} sent again successfully to ${sent.customer_snapshot?.email ?? "the customer"}.` : `${sent.quotation_number} sent to ${sent.customer_snapshot?.email ?? "the customer"}`); render();
      } catch (error) {
        try { bundle = { ...bundle!, document: await quotationApi.get(quote._id!) }; } catch { /* keep saved document */ }
        toast(error instanceof Error ? error.message : "Quotation email could not be delivered. Retry from this preview.", "error"); render();
      }
    };
    root.querySelector(".preview-generate")?.addEventListener("click", async (event) => {
      const button = event.currentTarget as HTMLButtonElement; button.disabled = true; button.textContent = "Generating…";
      try {
        const quote = await quotationApi.create(bundle!.payload); bundle = { ...bundle!, document: quote };
        if (draftKey) localStorage.setItem(draftKey, JSON.stringify(bundle));
        appStore.set({ cartCount: 0 }); await sendSaved(quote, button);
      } catch (error) { toast(error instanceof Error ? error.message : "Quotation could not be generated", "error"); button.disabled = false; button.textContent = "Generate & Send Email"; }
    });
    root.querySelector(".preview-send")?.addEventListener("click", async (event) => sendSaved(bundle!.document, event.currentTarget as HTMLButtonElement));
    refreshIcons(root);
  };
  render(); return root;
}

export async function quotationDetailPage(quotationId: string): Promise<HTMLElement> {
  const page = pageScaffold("Quotation Workflow", "Quotation Detail", `Quotation For: ${appStore.state.customerCompany?.name ?? appStore.state.company?.name ?? ""}`, '<a class="button button-secondary" href="/quotations" data-route="/quotations"><i data-lucide="arrow-left"></i>Back to Quotations</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(6);
  try {
    const quote = await quotationApi.get(quotationId);
    const communicationResult = await quotationApi.communications(quotationId);
    const communication = communicationResult.items ?? [];
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar"><i data-lucide="file-text"></i></div><div><span class="eyebrow">${escapeHtml(quote.quotation_number)}</span><h2>${escapeHtml(quote.customer_snapshot.name)}</h2><p>${formatDate(quote.created_at)} · ${statusBadge(quote.status)}</p></div><div class="detail-hero-total"><span>Grand Total</span><strong>${formatMoney(quote.totals.grand_total, "EUR")}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Quotation Items</span><h2>Line Items</h2></div><a class="button button-quiet" href="/quotation-preview?id=${encodeURIComponent(quote._id)}" target="_blank"><i data-lucide="eye"></i>Full-screen Preview</a></div><div class="detail-lines">${quote.lines.map((line) => `<div class="detail-line"><div><strong>${escapeHtml(line.product_name)}</strong><small>${escapeHtml(line.description ?? "")}</small><small>${escapeHtml(configurationSummary({ configuration: line.configuration ?? {}, pricing_preview: line }))} · Qty ${quantityMarkup(line).replace(/<[^>]+>/g, "")}${discountText(line.discount_percent, " · ", "% discount")}</small></div><strong>${formatMoney(line.line_total, "EUR")}</strong></div>`).join("")}</div><div class="summary-total"><span>Grand Total</span><strong>${formatMoney(quote.totals.grand_total, "EUR")}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Communication History</span><h2>Delivery Timeline</h2></div><span class="count-badge">${communication.length} events</span></div>${communication.length ? `<div class="timeline-list">${communication.map((item) => `<div class="timeline-item"><i data-lucide="${item.channel === "email" ? "mail" : item.channel === "whatsapp" ? "message-circle" : "file-text"}"></i><div><strong>${escapeHtml(String(item.action ?? item.status ?? item.channel ?? "Activity"))}</strong><small>${escapeHtml(String(item.recipient ?? item.provider_id ?? "Internal record"))}</small></div></div>`).join("")}</div>` : '<p class="muted">No delivery events recorded yet.</p>'}</section></div>`;
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Quotation unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

function customerContext() {
  const customer = appStore.state.customer;
  const company = appStore.state.customerCompany ?? appStore.state.company;
  return {
    id: customer?.customer_id ?? customer?._id ?? company?._id,
    name: customer?.company_name ?? customer?.name ?? (company as (typeof company & { company_name?: string }) | null)?.company_name ?? company?.name ?? "Customer",
    company,
  };
}

function cartTotalsMarkup(cart: { totals: Record<string, number>; master_totals?: Record<string, number> }, currency: string, action: string): string {
  const master = cart.master_totals ?? cart.totals;
  const reference = currency !== "EUR" ? `<div class="summary-row cart-summary-reference"><span>${escapeHtml(currency)} reference</span><strong>${formatMoney(Number(cart.totals.grand_total ?? 0), currency)}</strong></div>` : "";
  const discountAmount = Number(master.discount_amount ?? 0);
  const discountRow = Number.isFinite(discountAmount) && discountAmount > 0
    ? `<div class="summary-row"><span>Discount</span><strong>- ${formatMoney(discountAmount, "EUR")}</strong></div>`
    : "";
  return `<aside class="quote-summary panel"><div class="summary-head"><span class="eyebrow">EUR commercial</span><h2>Cart Summary</h2><p>The quotation is issued in EUR. USD and INR are reference values only.</p></div><div class="summary-row"><span>Subtotal</span><strong>${formatMoney(Number(master.subtotal ?? 0), "EUR")}</strong></div>${discountRow}<div class="summary-total"><span>Total</span><strong>${formatMoney(Number(master.grand_total ?? 0), "EUR")}</strong></div>${reference}${action}</aside>`;
}

async function bindSeparatedCartActions(body: HTMLElement, items: CartItem[], customerId: string, customerName: string, reload: () => Promise<void>): Promise<void> {
  body.querySelectorAll<HTMLButtonElement>(".edit-cart").forEach((button) => button.addEventListener("click", async () => {
    const item = items.find((row) => row._id === button.dataset.id); if (!item) return;
    try { await openCartItemEditor(item, reload); } catch (error) { toast(error instanceof Error ? error.message : "Could not open editor", "error"); }
  }));
  body.querySelectorAll<HTMLButtonElement>(".remove-cart").forEach((button) => button.addEventListener("click", async () => {
    if (!window.confirm("Remove this product from the quotation cart?")) return;
    try { await cartApi.remove(button.dataset.id!); toast("Item removed", "info"); await reload(); } catch (error) { toast(error instanceof Error ? error.message : "Could not remove item", "error"); }
  }));
  body.querySelector<HTMLButtonElement>("#clear-cart")?.addEventListener("click", async () => {
    if (!window.confirm(`Clear every item from the ${customerName} quotation cart?`)) return;
    try { await cartApi.clear(customerId); toast("Cart cleared", "info"); await reload(); } catch (error) { toast(error instanceof Error ? error.message : "Could not clear cart", "error"); }
  });
}

export async function cartPage(): Promise<HTMLElement> {
  const context = customerContext();
  const page = pageScaffold("Workspace", "Cart", context.id ? `Quotation For: ${context.name}` : "Select a customer before adding quotation items.");
  page.classList.add("quotation-workflow-page");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(6);
  if (!context.id || !context.company) { body.innerHTML = emptyState("building-2", "Customer selection required", "Choose a customer before using the cart."); refreshIcons(page); return page; }
  let items: CartItem[] = [];
  const load = async () => {
    const cart = await cartApi.get(context.id!, appStore.state.currency); items = cart.items; appStore.set({ cartCount: items.length });
    body.innerHTML = `<div class="workspace-steps"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="done"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span class="active"><b>3</b>Cart</span><i data-lucide="chevron-right"></i><span><b>4</b>Quotation</span></div><div class="quote-layout"><section class="quote-main"><div class="section-title"><div><span class="eyebrow">${escapeHtml(appStore.state.currency)} display</span><h2>Your current quotation items</h2></div><div class="section-title-actions"><span class="count-badge">${items.length} ${items.length === 1 ? "line" : "lines"}</span>${items.length ? '<button id="clear-cart" class="button button-quiet" title="Clear cart"><i data-lucide="trash-2"></i>Clear</button>' : ""}</div></div>${items.length ? `<div class="cart-lines">${items.map(cartLine).join("")}</div>` : emptyState("shopping-cart", "Cart is empty", "Use Calculator to configure a product.")}</section>${cartTotalsMarkup(cart, appStore.state.currency, items.length ? '<div class="cart-summary-actions"><a class="button button-secondary button-full" href="/calculator" data-route="/calculator"><i data-lucide="plus"></i>Add More Products</a><a class="button button-primary button-full" href="/quotation/create" data-route="/quotation/create"><i data-lucide="file-plus"></i>Continue to Quotation</a></div>' : '<a class="button button-secondary button-full" href="/calculator" data-route="/calculator"><i data-lucide="calculator"></i>Go to Calculator</a>')}</div>`;
    await bindSeparatedCartActions(body, items, context.id!, context.name, load); refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Cart unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; refreshIcons(body); }
  refreshIcons(page); return page;
}

export async function quotationsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Commercial", "Quotations", "View and manage previously generated quotations.");
  page.classList.add("quotation-history-page");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  try {
    const customerResult = await customerCompanyApi.list();
    const customers = customerResult.items ?? [];
    const continents = [...new Set(customers.map((customer) => customer.continent ?? customer.region?.continent).filter((value): value is string => Boolean(value)))].sort();
    const countries = [...new Set(customers.map((customer) => customer.country_name ?? customer.country).filter((value): value is string => Boolean(value)))].sort();
    const customerOptions = customers.map((customer) => `<option value="${escapeHtml(customer._id)}">${escapeHtml(customer.name)}</option>`).join("");
    const optionList = (values: string[]) => values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    body.innerHTML = `<section class="quotation-filters panel"><div class="quotation-filter-head"><div><span class="eyebrow">Saved records</span><h2>Quotation History</h2><p>Search every quotation you are authorized to view, independent of the active customer.</p></div><button class="button button-quiet" type="button" id="quotation-reset"><i data-lucide="rotate-ccw"></i>Clear Filters</button></div><div class="quotation-filter-grid"><label class="filter-search"><span>Search quotations</span><div class="field-search"><i data-lucide="search"></i><input id="quotation-search" placeholder="Number, customer, email" aria-label="Search quotations"></div></label><label><span>Customer</span><select id="quotation-customer"><option value="">All Customers</option>${customerOptions}</select></label><label><span>Status</span><select id="quotation-status"><option value="">All statuses</option><option>Draft</option><option>Sent</option><option>Viewed</option><option>Accepted</option><option>Rejected</option><option>Expired</option><option>Converted to Order</option><option>Cancelled</option><option value="send_failed">Send Failed</option></select></label><label><span>Region</span><select id="quotation-region"><option value="">All regions</option>${optionList(continents)}</select></label><label><span>Country</span><select id="quotation-country"><option value="">All countries</option>${optionList(countries)}</select></label><label><span>From date</span><input id="quotation-from" type="date"></label><label><span>To date</span><input id="quotation-to" type="date"></label><label><span>Min total (EUR)</span><input id="quotation-min" type="number" min="0" step="0.01" placeholder="0.00"></label><label><span>Max total (EUR)</span><input id="quotation-max" type="number" min="0" step="0.01" placeholder="0.00"></label></div></section><div class="quotation-list-meta"><span data-quotation-count>Loading quotations…</span><span data-quotation-scope></span></div><div data-quotation-rows></div><div class="quotation-pagination" data-quotation-pagination></div>`;
    const get = (id: string) => body.querySelector<HTMLInputElement | HTMLSelectElement>(`#${id}`)?.value.trim() ?? "";
    let pageNumber = 1;
    const load = async () => {
      const params = new URLSearchParams({ page: String(pageNumber), limit: "25" });
      const fields: Record<string, string> = { search: get("quotation-search"), customer_id: get("quotation-customer"), status: get("quotation-status"), region: get("quotation-region"), country: get("quotation-country"), from_date: get("quotation-from"), to_date: get("quotation-to"), min_total: get("quotation-min"), max_total: get("quotation-max") };
      Object.entries(fields).forEach(([key, value]) => { if (value) params.set(key, value); });
      const result = await quotationApi.list(params);
      const pagination = result.pagination ?? { page: pageNumber, limit: 25, total: result.items.length };
      const count = body.querySelector<HTMLElement>("[data-quotation-count]"); if (count) count.textContent = `${pagination.total} record${pagination.total === 1 ? "" : "s"}`;
      const scope = body.querySelector<HTMLElement>("[data-quotation-scope]"); if (scope) scope.textContent = result.scope === "all" ? "All authorized quotations" : "Your quotations";
      const hasFilters = Object.values(fields).some(Boolean);
      const emptyTitle = hasFilters ? "No quotations match these filters." : result.scope === "all" ? "No quotations found." : "You haven't created any quotations yet.";
      const emptyCopy = hasFilters ? "Clear filters or adjust the search criteria." : "Create a quotation from the Cart to see it here.";
      const rows = body.querySelector<HTMLElement>("[data-quotation-rows]"); if (rows) { rows.innerHTML = quotationRows(result.items, emptyTitle, emptyCopy); refreshIcons(rows); }
      const pager = body.querySelector<HTMLElement>("[data-quotation-pagination]"); const pages = Math.max(1, Math.ceil(pagination.total / pagination.limit));
      if (pager) pager.innerHTML = pages > 1 ? `<button class="button button-quiet" data-history-page="${Math.max(1, pageNumber - 1)}" ${pageNumber <= 1 ? "disabled" : ""}>Previous</button><span>Page ${pageNumber} of ${pages}</span><button class="button button-quiet" data-history-page="${Math.min(pages, pageNumber + 1)}" ${pageNumber >= pages ? "disabled" : ""}>Next</button>` : "";
    };
    const reload = () => { pageNumber = 1; load().catch((error) => { const rows = body.querySelector<HTMLElement>("[data-quotation-rows]"); if (rows) rows.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Quotations unavailable")}</div>`; }); };
    ["quotation-customer", "quotation-status", "quotation-region", "quotation-country", "quotation-from", "quotation-to", "quotation-min", "quotation-max"].forEach((id) => body.querySelector(`#${id}`)?.addEventListener("change", reload));
    let searchTimer: number | undefined; body.querySelector<HTMLInputElement>("#quotation-search")?.addEventListener("input", () => { window.clearTimeout(searchTimer); searchTimer = window.setTimeout(reload, 250); });
    body.querySelector("#quotation-reset")?.addEventListener("click", () => { ["quotation-search", "quotation-customer", "quotation-status", "quotation-region", "quotation-country", "quotation-from", "quotation-to", "quotation-min", "quotation-max"].forEach((id) => { const input = body.querySelector<HTMLInputElement | HTMLSelectElement>(`#${id}`); if (input) input.value = ""; }); reload(); });
    body.addEventListener("click", async (event) => { const element = event.target as HTMLElement; const pageButton = element.closest<HTMLButtonElement>("[data-history-page]"); if (pageButton) { pageNumber = Number(pageButton.dataset.historyPage) || 1; await load(); return; } const target = element.closest<HTMLButtonElement>("button[data-id]"); if (!target) return; if (target.classList.contains("print-quote")) { const printWindow = window.open(`/quotation-preview?id=${encodeURIComponent(target.dataset.id!)}`, "_blank"); if (!printWindow) toast("Allow pop-ups to print the quotation", "error"); return; } if (target.classList.contains("send-quote")) { const quote = (await quotationApi.get(target.dataset.id!)); openQuotationEmailComposer(quote, load); return; } target.disabled = true; try { if (target.classList.contains("whatsapp-quote")) { const result = await quotationApi.whatsapp(target.dataset.id!); toast(result.delivery.status === "mocked" ? "WhatsApp share recorded in mock mode" : "WhatsApp share queued", "info"); } else if (target.classList.contains("convert-quote")) { const config = await orderApi.configuration(target.dataset.id!); const quote = config.quote; const defaults = config.defaults; const content = document.createElement("div"); content.innerHTML = `<form class="stack-form"><p class="form-hint">The Quote remains unchanged. Review and confirm the Order Confirmation details below.</p><section class="panel"><strong>Quote ${escapeHtml(String(quote.quotation_number ?? ""))}</strong><p>${escapeHtml(String(quote.customer_snapshot?.company_name ?? quote.customer_snapshot?.name ?? "Customer"))} · ${formatMoney(Number(quote.totals?.grand_total ?? 0), "EUR")}</p><small>Original payment terms: ${escapeHtml(String(quote.payment_terms ?? "—"))}</small></section><label>OC Number<input name="oc_number" placeholder="Auto-generate"></label><label>OC Date<input name="oc_date" type="date" required value="${new Date().toISOString().slice(0, 10)}"></label><label>Final payment terms<input name="payment_terms" required value="${escapeHtml(String(defaults.payment_terms ?? ""))}"></label><label>Order amount (EUR)<input name="order_amount" type="number" min="0" step="0.01" required value="${Number(defaults.order_amount ?? quote.totals?.grand_total ?? 0).toFixed(2)}"></label><p class="form-hint">Sales Person: ${escapeHtml(String((defaults.salesperson as Record<string, unknown> | undefined)?.name ?? "Assigned from Quote"))}</p><label>CC (optional)<input name="cc" placeholder="email@example.com"></label><label>BCC (optional)<input name="bcc" placeholder="email@example.com"></label><small class="field-error" data-oc-error></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">Confirm &amp; Convert to Order</button></div></form>`; const dialog = openModal("Order Confirmation Configuration", content, "wide"); content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close()); content.querySelector("form")?.addEventListener("submit", async (submitEvent) => { submitEvent.preventDefault(); const form = submitEvent.currentTarget as HTMLFormElement; const data = new FormData(form); const errorNode = content.querySelector<HTMLElement>("[data-oc-error]"); try { const split = (value: FormDataEntryValue | null) => String(value ?? "").split(",").map((item) => item.trim()).filter(Boolean); await orderApi.convert(target.dataset.id!, { oc_number: data.get("oc_number"), oc_date: data.get("oc_date"), payment_terms: data.get("payment_terms"), order_amount: Number(data.get("order_amount")), cc: split(data.get("cc")), bcc: split(data.get("bcc")) }); dialog.close(); toast("Order Confirmation created and email queued"); await load(); } catch (error) { if (errorNode) errorNode.textContent = error instanceof Error ? error.message : "Order Confirmation could not be created"; } }); refreshIcons(content); } else if (target.classList.contains("restore-quote")) { await quotationApi.restore(target.dataset.id!); toast("Quotation restored"); await load(); } else if (target.classList.contains("delete-quote") || target.classList.contains("archive-quote")) { const archive = target.classList.contains("archive-quote"); if (!window.confirm(`${archive ? "Archive" : "Delete"} quotation?`)) return; const reason = window.prompt(`${archive ? "Archive" : "Deletion"} reason (required):`, "")?.trim() ?? ""; if (!reason) { toast("A reason is required", "error"); return; } await quotationApi.remove(target.dataset.id!, reason, !archive); toast(archive ? "Quotation archived" : "Quotation deleted"); await load(); } } catch (error) { toast(error instanceof Error ? error.message : "Quotation action failed", "error"); } finally { target.disabled = false; } });
    await load();
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Quotations unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function quotationPreparationPage(): Promise<HTMLElement> {
  const context = customerContext();
  const page = pageScaffold("Quotation Workflow", "Quotation Preparation", context.id ? `Prepare a quotation for ${context.name}.` : "Select a customer before preparing a quotation.", '<a class="button button-secondary" href="/cart" data-route="/cart"><i data-lucide="arrow-left"></i>Back to Cart</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(6);
  if (!context.id || !context.company) { body.innerHTML = emptyState("building-2", "Customer selection required", "Choose a customer before preparing a quotation."); refreshIcons(page); return page; }
  try {
    const cart = await cartApi.get(context.id, "EUR"); appStore.set({ cartCount: cart.items.length });
    if (!cart.items.length) { body.innerHTML = `${emptyState("shopping-cart", "Quotation cart is empty", "Configure a product in Calculator before preparing a quotation.")}<a class="button button-secondary" href="/calculator" data-route="/calculator"><i data-lucide="calculator"></i>Go to Calculator</a>`; refreshIcons(page); return page; }
    body.innerHTML = `<div class="workspace-steps"><span class="done"><b>1</b>Customer</span><span class="done"><b>2</b>Products</span><span class="done"><b>3</b>Cart</span><span class="active"><b>4</b>Quotation</span></div><div class="quote-layout"><section class="quote-main"><section class="panel quotation-customer-card"><span class="eyebrow">Customer</span><h2>${escapeHtml(context.name)}</h2><p class="muted">Issued by Moneda Technologies .</p></section><section class="panel quotation-items-panel"><div class="section-title"><div><span class="eyebrow">EUR Quotation Items</span><h2>Items being prepared</h2></div><span class="count-badge">${cart.items.length} line item${cart.items.length === 1 ? "" : "s"}</span></div><div class="cart-lines">${cart.items.map((item) => cartLine(item, false)).join("")}</div></section></section>${cartTotalsMarkup(cart, "EUR", "")}</div>`;
    body.querySelectorAll<HTMLElement>(".quotation-items-panel .cart-price").forEach((price, index) => {
      const line = cart.items[index]?.pricing_preview; if (!line) return;
      const label = price.querySelector("small"); const amount = price.querySelector("strong");
      if (label) label.textContent = "Line total";
      if (amount) amount.textContent = formatMoney(line.master_final_total ?? 0, "EUR");
    });
    const summary = body.querySelector<HTMLElement>(".quote-summary")!; const form = document.createElement("form"); form.id = "quote-details-form"; form.className = "stack-form"; form.innerHTML = `<label>Proforma Validity (days)<input name="proforma_validity_days" type="number" min="1" max="365" value="30" required></label><label>Payment Terms<select name="payment_terms"><option>Advance</option><option>POD</option><option>30 Days from receipt</option><option>60 Days</option><option>Custom</option></select></label><label class="custom-payment-days" hidden>Custom payment days<input name="custom_payment_days" type="number" min="1" step="1" placeholder="Enter number of days"></label><label>Transport<select name="transport_mode"><option value="by_consignee">By Consignee</option><option value="by_moneda_team">By Moneda Team</option></select></label><label class="transport-charge" hidden>Transport Charges (EUR)<input name="transport_charges" type="number" min="0" step="0.01" placeholder="Enter freight charge"></label><label>Customer Notes<textarea name="customer_notes" rows="4" placeholder="Add quotation-specific notes for the customer"></textarea></label><label class="check-row"><input name="create_lead" type="checkbox" checked><span>Create a linked CRM lead and follow-up</span></label><label>Follow-up Date<input name="follow_up_date" type="date"></label><button class="button button-primary button-full"><i data-lucide="eye"></i>Preview Quotation</button>`; summary.append(form);
    const transport = form.querySelector<HTMLSelectElement>("[name=transport_mode]"); const charge = form.querySelector<HTMLElement>(".transport-charge"); const payment = form.querySelector<HTMLSelectElement>("[name=payment_terms]"); const customDays = form.querySelector<HTMLElement>(".custom-payment-days"); payment?.addEventListener("change", () => { const custom = payment.value === "Custom"; if (customDays) customDays.hidden = !custom; const input = customDays?.querySelector<HTMLInputElement>("input"); if (input) input.required = custom; }); transport?.addEventListener("change", () => { const required = transport.value === "by_moneda_team"; if (charge) charge.hidden = !required; const input = charge?.querySelector<HTMLInputElement>("input"); if (input) { input.required = required; if (!required) input.value = ""; } });
    const draftKey = new URLSearchParams(location.search).get("draft"); const draft = draftKey ? JSON.parse(localStorage.getItem(draftKey) ?? "null") as PreviewBundle | null : null; const draftPayload = draft?.payload ?? {};
    ["proforma_validity_days", "payment_terms", "custom_payment_days", "transport_mode", "transport_charges", "customer_notes"].forEach((name) => { const input = form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null; const value = draftPayload[name]; if (input && value !== undefined && value !== null) input.value = String(value); });
    payment?.dispatchEvent(new Event("change")); transport?.dispatchEvent(new Event("change"));
    form.addEventListener("submit", async (event) => { event.preventDefault(); const data = new FormData(form); const previewWindow = window.open("about:blank", "_blank"); if (!previewWindow) { toast("Allow pop-ups to open the quotation preview", "error"); return; } previewWindow.document.write("<title>Preparing quotation preview…</title><p style='font:16px sans-serif;padding:40px'>Preparing secure quotation preview…</p>"); const payload: Record<string, unknown> = { customer_id: context.id, currency: "EUR", proforma_validity_days: Number(data.get("proforma_validity_days")), payment_terms: data.get("payment_terms"), custom_payment_days: data.get("payment_terms") === "Custom" ? Number(data.get("custom_payment_days")) : null, transport_mode: data.get("transport_mode"), transport_charges: data.get("transport_mode") === "by_moneda_team" ? Number(data.get("transport_charges")) : 0, customer_notes: data.get("customer_notes"), create_lead: data.get("create_lead") === "on", follow_up_date: data.get("follow_up_date") || null, reminder_frequency: "none", idempotency_key: crypto.randomUUID() }; try { const document = await quotationApi.preview(payload); const key = `moneda-quotation-preview-${crypto.randomUUID()}`; localStorage.setItem(key, JSON.stringify({ payload, document } satisfies PreviewBundle)); previewWindow.location.href = `/quotation-preview?draft=${encodeURIComponent(key)}`; } catch (error) { previewWindow.close(); toast(error instanceof Error ? error.message : "Quotation preview failed", "error"); } });
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Quotation preparation unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
