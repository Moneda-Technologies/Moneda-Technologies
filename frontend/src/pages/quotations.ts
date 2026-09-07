import { cartApi, quotationApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { CartItem, Customer, Quotation } from "../types/domain";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";
import { openCartItemEditor } from "./catalog";

interface PreviewBundle { payload: Record<string, unknown>; document: Quotation }

function openQuotationEmailComposer(quote: Quotation, onSent: () => Promise<void> | void): void {
  const recipient = quote.customer_snapshot?.email ?? "";
  const content = document.createElement("div");
  content.innerHTML = `<form class="stack-form email-composer-form"><label>To<input name="to" type="email" value="${escapeHtml(recipient)}" disabled></label><p class="form-hint">Customer-facing CC/BCC routing is managed centrally in Settings → Zoho Mail.</p><label>Subject<input name="subject" value="Quotation ${escapeHtml(quote.quotation_number)} - Moneda Technologies" required></label><label>Message<textarea name="message" rows="6" required>Please find quotation ${escapeHtml(quote.quotation_number)} attached.</textarea></label><div class="attachment-chip"><i data-lucide="paperclip"></i><span>${escapeHtml(quote.quotation_number)}.pdf</span><small>PDF attachment</small></div><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit"><i data-lucide="send"></i>Send Email</button></div></form>`;
  const dialog = openModal("Email quotation", content, "wide");
  const form = content.querySelector<HTMLFormElement>("form")!;
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form); const button = form.querySelector<HTMLButtonElement>("[type=submit]")!;
    button.disabled = true; button.textContent = "Sending…";
    try {
      await quotationApi.send(quote._id, { subject: String(data.get("subject") ?? ""), message: String(data.get("message") ?? "") });
      dialog.close(); toast("Email sent ✓"); await onSent();
    } catch (error) { toast(error instanceof Error ? error.message : "Email failed. Retry when configuration is available.", "error"); button.disabled = false; button.innerHTML = '<i data-lucide="send"></i>Retry'; refreshIcons(button); }
  });
  refreshIcons(content);
}

function configurationSummary(item: CartItem | { configuration: Record<string, unknown>; pricing_preview?: CartItem["pricing_preview"] }): string {
  const configuration = item.configuration;
  const parts: string[] = [];
  if (configuration.machine) parts.push(String(configuration.machine));
  if (configuration.length && configuration.width) parts.push(`${configuration.length} × ${configuration.width} ${configuration.dimension_unit ?? "mm"}`);
  if (configuration.thickness_mm) parts.push(`${Number(configuration.thickness_mm).toFixed(2)} mm`);
  if (configuration.thickness_micron) parts.push(`${configuration.thickness_micron} micron`);
  if (configuration.format_type) parts.push(configuration.format_type === "bar_format" ? "Bar Format" : "Cut Format");
  if (configuration.requested_litres) parts.push(`${configuration.requested_litres} L requested`);
  const adjustments = item.pricing_preview?.adjustments ?? [];
  if ("tax_enabled" in item && item.pricing_preview?.currency === "INR") {
    parts.push(item.tax_enabled && item.pricing_preview?.tax_amount
      ? `INR tax ${item.pricing_preview.tax_rate}% ${item.tax_mode ?? item.pricing_preview.tax_mode}`
      : "INR tax off");
  }
  adjustments.forEach((row) => parts.push(`${row.label}${row.quantity ? ` × ${row.quantity}` : ""}`));
  return parts.join(" · ") || "Standard configuration";
}

function cartLine(item: CartItem, editableOrIndex: boolean | number = true): string {
  const editable = typeof editableOrIndex === "boolean" ? editableOrIndex : true;
  const rawLine = item.pricing_preview;
  const line = rawLine.currency === "INR" && rawLine.tax_amount > 0 ? { ...rawLine, subtotal: rawLine.line_total } : rawLine;
  const netUnitPrice = line.quantity ? line.subtotal / line.quantity : line.subtotal;
  return `<article class="cart-line${editable ? " cart-line-editable" : " quotation-line-readonly"}"><div class="cart-icon"><i data-lucide="package"></i></div><div class="cart-product"><span class="article-label">Art. ${escapeHtml(line.article_no ?? item.product_id)}</span><strong>${escapeHtml(line.product_name)}</strong><span>${escapeHtml(configurationSummary(item))}</span><small>Unit Price ${formatMoney(netUnitPrice, line.currency)}${line.discount_percent ? ` · Discount ${line.discount_percent}%` : ""}</small></div><div class="cart-qty"><small>Qty</small><strong>${line.quantity}</strong></div><div class="cart-price"><small>${escapeHtml(line.currency)}</small><strong>${formatMoney(line.subtotal, line.currency)}</strong></div>${editable ? `<div class="cart-actions"><button class="button button-quiet edit-cart" data-id="${escapeHtml(item._id)}"><i data-lucide="pencil"></i>Edit</button><button class="button button-quiet remove-cart" data-id="${escapeHtml(item._id)}"><i data-lucide="trash-2"></i>Delete</button></div>` : ""}</article>`;
}

function quotationRows(quotations: Quotation[]): string {
  if (!quotations.length) return `${emptyState("file-text", "No quotations yet", "You haven't generated any quotations yet. Create a quotation from the Cart.")}<a class="button button-primary" href="/cart" data-route="/cart"><i data-lucide="shopping-cart"></i>Go to Cart</a>`;
  return `<div class="data-table panel"><table><thead><tr><th>Quotation</th><th>Customer</th><th>Status</th><th>Created</th><th>Currency</th><th>Total</th><th>Actions</th></tr></thead><tbody>${quotations.map((quote) => `<tr><td><strong>${escapeHtml(quote.quotation_number)}</strong><small>${quote.lines.length} line${quote.lines.length === 1 ? "" : "s"}</small></td><td>${escapeHtml(quote.customer_snapshot?.company_name ?? quote.customer_snapshot?.name ?? "Customer")}</td><td>${statusBadge(quote.status)}</td><td>${formatDate(quote.created_at)}</td><td><span class="currency-tag">${escapeHtml(quote.currency)}</span></td><td class="money">${formatMoney(quote.totals.grand_total, quote.currency)}</td><td><div class="row-actions"><a class="icon-button" href="/quotation-preview?id=${encodeURIComponent(quote._id)}" target="_blank" aria-label="View quotation" title="View quotation"><i data-lucide="eye"></i></a><a class="icon-button" href="/api/v1/quotations/${quote._id}/pdf" aria-label="Download PDF" title="Download PDF"><i data-lucide="download"></i></a><button class="icon-button print-quote" data-id="${quote._id}" aria-label="Print quotation" title="Print quotation"><i data-lucide="printer"></i></button>${quote.status === "Draft" ? `<button class="icon-button send-quote" data-id="${quote._id}" aria-label="Email quotation" title="Email quotation"><i data-lucide="mail"></i></button>` : ""}<button class="icon-button whatsapp-quote" data-id="${quote._id}" aria-label="Send via WhatsApp" title="Send via WhatsApp"><i data-lucide="message-circle"></i></button>${quote.status !== "Converted to Order" ? `<button class="icon-button convert-quote" data-id="${quote._id}" aria-label="Convert to order" title="Convert to order"><i data-lucide="shopping-bag"></i></button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>`;
}

function customerChoice(customer: Customer): string {
  return [customer.name, customer.contact_name, customer.email].filter(Boolean).join(" — ");
}

export async function legacyQuotationPreparationPage(): Promise<HTMLElement> {
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
    const showTax = appStore.state.currency === "INR" && cartItems.some((item) => Number(item.pricing_preview.tax_amount ?? 0) > 0);
    body.innerHTML = `<div class="workspace-steps"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="done"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span class="done"><b>3</b>Configure</span><i data-lucide="chevron-right"></i><span class="active"><b>4</b>Quotation</span></div><div class="quote-layout"><section class="quote-main"><div class="section-title"><div><span class="eyebrow">${escapeHtml(appStore.state.currency)}</span><h2>Current quotation items</h2></div><div class="section-title-actions"><span class="count-badge">${cartItems.length} ${cartItems.length === 1 ? "line" : "lines"}</span>${cartItems.length ? '<button id="clear-cart" class="button button-quiet"><i data-lucide="trash-2"></i>Clear</button>' : ""}</div></div>${cartItems.length ? `<div class="cart-lines">${cartItems.map(cartLine).join("")}</div>` : emptyState("shopping-cart", "Quotation cart is empty", "Use Calculator in the sidebar to configure a product.")}<div class="section-title quote-history-title"><div><span class="eyebrow">Saved Records</span><h2>Quotation History</h2></div><span>${quotations.length} records</span></div>${quotationRows(quotations)}</section><aside class="quote-summary panel"><div class="summary-head"><span class="eyebrow">From Moneda Technologies</span><h2>Quotation Details</h2><p>Every line is recalculated before preview and again before saving.</p></div><div class="summary-row"><span>Subtotal</span><strong>${formatMoney(Number(cart.totals.subtotal ?? 0), appStore.state.currency)}</strong></div>${Number(cart.totals.discount_amount ?? 0) ? `<div class="summary-row"><span>Discount</span><strong>- ${formatMoney(Number(cart.totals.discount_amount), appStore.state.currency)}</strong></div>` : ""}${showTax ? `<div class="summary-row"><span>Taxable Amount</span><strong>${formatMoney(Number(cart.totals.taxable_amount ?? 0), appStore.state.currency)}</strong></div><div class="summary-row"><span>GST / Tax</span><strong>${formatMoney(Number(cart.totals.tax_amount ?? 0), appStore.state.currency)}</strong></div>` : ""}<div class="summary-total"><span>Estimated Total</span><strong>${formatMoney(Number(cart.totals.grand_total ?? 0), appStore.state.currency)}</strong></div><form id="quote-details-form" class="stack-form"><label>Customer<input value="${escapeHtml(customerCompany.name)}" disabled><small class="customer-detail">This quotation will be sent from Moneda Technologies to the selected customer.</small></label><div class="form-grid"><label>Quotation Currency<select name="currency"><option ${appStore.state.currency === "EUR" ? "selected" : ""}>EUR</option><option ${appStore.state.currency === "USD" ? "selected" : ""}>USD</option><option ${appStore.state.currency === "INR" ? "selected" : ""}>INR</option></select></label><label>Proforma Validity (days)<input name="proforma_validity_days" type="number" min="1" max="365" value="30" required></label></div><label>Payment Terms<select name="payment_terms"><option>Advance</option><option>POD</option><option>15 Days</option><option>30 Days</option></select></label><label>Transport<select name="transport_mode"><option value="by_consignee">By Consignee</option><option value="by_moneda_team">By Moneda Team</option></select></label><label class="transport-charge" hidden>Transport Charges (${escapeHtml(appStore.state.currency)})<input name="transport_charges" type="number" min="0" step="0.01" placeholder="Enter freight charge"></label><label>Notes<textarea name="notes" rows="3" placeholder="Optional commercial notes"></textarea></label><label class="check-row"><input name="create_lead" type="checkbox" checked><span>Create a linked CRM lead and follow-up</span></label><label>Follow-up Date<input name="follow_up_date" type="date"></label><button class="button button-primary button-full" ${!cartItems.length ? "disabled" : ""}><i data-lucide="eye"></i>Preview Quotation</button></form></aside></div>`;
    if (appStore.state.currency === "INR") {
      const summary = body.querySelector<HTMLElement>(".quote-summary");
      const enabled = cartItems.length > 0 && cartItems.every((item) => item.tax_enabled === true);
      const mode = cartItems.find((item) => item.tax_mode)?.tax_mode ?? "exclusive";
      summary?.insertAdjacentHTML("afterbegin", `<div class="tax-controls cart-tax-controls"><span class="eyebrow">INR Tax</span><label class="check-row"><input id="cart-tax-enabled" type="checkbox" ${enabled ? "checked" : ""}><span>Apply tax to all line items</span></label><label>Tax mode<select id="cart-tax-mode"><option value="exclusive" ${mode === "exclusive" ? "selected" : ""}>Tax exclusive</option><option value="inclusive" ${mode === "inclusive" ? "selected" : ""}>Tax inclusive</option></select></label></div>`);
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
    const cartTaxEnabled = body.querySelector<HTMLInputElement>("#cart-tax-enabled");
    const cartTaxMode = body.querySelector<HTMLSelectElement>("#cart-tax-mode");
    const applyCartTax = async () => {
      if (!cartItems.length || !cartTaxEnabled) return;
      const enabled = cartTaxEnabled.checked;
      const mode = cartTaxMode?.value === "inclusive" ? "inclusive" : "exclusive";
      cartTaxEnabled.disabled = true; if (cartTaxMode) cartTaxMode.disabled = true;
      try {
        for (const item of cartItems) {
          await cartApi.update(item._id, { customer_id: customerId, tax_enabled: enabled, tax_mode: mode });
        }
        toast(enabled ? `INR tax enabled (${mode}) for all line items` : "INR tax disabled for all line items", "info");
        await load();
      } catch (error) {
        toast(error instanceof Error ? error.message : "Could not update cart tax", "error");
        cartTaxEnabled.disabled = false; if (cartTaxMode) cartTaxMode.disabled = false;
      }
    };
    cartTaxEnabled?.addEventListener("change", applyCartTax);
    cartTaxMode?.addEventListener("change", applyCartTax);
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
        notes: data.get("notes"), create_lead: data.get("create_lead") === "on", follow_up_date: data.get("follow_up_date") || null,
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
}

function previewDocumentMarkup(quote: Quotation, saved: boolean): string {
  const issuer = quote.issuer_snapshot ?? { name: "Moneda Technologies", email: "business@monedatechnologies.com" };
  const customerCompany = quote.customer_company_snapshot ?? quote.company_snapshot ?? appStore.state.customerCompany ?? appStore.state.company;
  const customer = quote.customer_snapshot;
  const hasTax = quote.currency === "INR" && Number(quote.totals.tax_amount ?? 0) > 0;
  const taxRows = hasTax ? `<div><span>Taxable Amount</span><strong>${formatMoney(quote.totals.taxable_amount ?? 0, quote.currency)}</strong></div><div><span>Tax</span><strong>${formatMoney(quote.totals.tax_amount, quote.currency)}</strong></div>` : "";
  const transportRow = quote.totals.transport_cost ? `<div><span>Transport</span><strong>${formatMoney(quote.totals.transport_cost, quote.currency)}</strong></div>` : "";
  return `<div class="preview-toolbar"><button class="button button-quiet preview-back"><i data-lucide="arrow-left"></i>Close / Back</button><div><button class="button button-quiet preview-print"><i data-lucide="printer"></i>Print</button>${saved ? `<a class="button button-secondary" href="/api/v1/quotations/${quote._id}/pdf"><i data-lucide="download"></i>Download PDF</a>` : '<button class="button button-secondary preview-download" disabled title="Generate and save the quotation first"><i data-lucide="download"></i>Download PDF</button>'}${saved ? "" : '<button class="button button-primary preview-generate"><i data-lucide="file-check-2"></i>Generate & Save Quotation</button>'}</div></div><main class="quotation-document"><header class="document-head"><img src="${escapeHtml(appStore.state.brandLogoPath)}" alt="Moneda Technologies"><div><span>${saved ? "Quotation" : "Quotation Preview"}</span><h1>${escapeHtml(saved ? quote.quotation_number : "PROFORMA")}</h1><p>${formatDate(quote.created_at)}</p></div></header><section class="document-meta"><div><span>Proforma Validity</span><strong>${quote.proforma_validity_days ?? quote.validity_days ?? 30} Days</strong></div><div><span>Currency</span><strong>${escapeHtml(quote.currency)}</strong></div><div><span>Payment Terms</span><strong>${escapeHtml(quote.payment_terms ?? "Advance")}</strong></div><div><span>Transport</span><strong>${escapeHtml(quote.transport?.label ?? "By Consignee")}</strong></div></section><section class="document-parties"><div><span>From</span><h2>${escapeHtml(issuer.name)}</h2><p>${escapeHtml(issuer.address ?? "")}</p><p>${escapeHtml(issuer.email ?? "")}</p><p>Prepared by: ${escapeHtml(quote.salesperson_snapshot?.name ?? "")}</p></div><div><span>To</span><h2>${escapeHtml(customerCompany?.name ?? customer.name)}</h2><p>${escapeHtml(customer.contact_name ?? "")}</p><p>${escapeHtml(customer.email ?? "")} ${escapeHtml(customer.phone ?? "")}</p><p>${escapeHtml(customer.address ?? customerCompany?.address ?? "")}</p></div></section><section class="document-products"><table><thead><tr><th>Article</th><th>Product & Description</th><th>Qty</th><th>Unit Price</th><th>Total</th></tr></thead><tbody>${quote.lines.map((line) => `<tr><td>${escapeHtml(line.article_no ?? line.product_id)}</td><td><strong>${escapeHtml(line.product_name)}</strong><small>${escapeHtml(line.description ?? "")}</small><small>${escapeHtml(configurationSummary({ configuration: line.configuration ?? {}, pricing_preview: line }))}</small></td><td>${line.quantity}</td><td>${formatMoney(line.unit_price, quote.currency)}</td><td>${formatMoney(line.line_total, quote.currency)}</td></tr>`).join("")}</tbody></table></section><section class="document-total"><div><span>Subtotal</span><strong>${formatMoney(quote.totals.subtotal, quote.currency)}</strong></div>${quote.totals.discount_amount ? `<div><span>Discount</span><strong>- ${formatMoney(quote.totals.discount_amount, quote.currency)}</strong></div>` : ""}${transportRow}${taxRows}<div class="grand"><span>Grand Total</span><strong>${formatMoney(quote.totals.grand_total, quote.currency)}</strong></div></section><section class="document-terms"><div><span>Commercial Terms</span><p>Payment Terms: ${escapeHtml(quote.payment_terms ?? "Advance")}</p><p>Transport: ${escapeHtml(quote.transport?.description ?? "To be borne by consignee")}</p>${quote.notes ? `<p>Notes: ${escapeHtml(quote.notes)}</p>` : ""}</div><div class="signature"><span>For Moneda Technologies</span><i></i><strong>Authorized Signatory</strong></div></section></main>`;
}

export async function quotationPreviewPage(): Promise<HTMLElement> {
  const root = document.createElement("div"); root.className = "quotation-preview-page";
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
    root.innerHTML = previewDocumentMarkup(bundle!.document, saved);
    root.querySelector(".preview-back")?.addEventListener("click", () => { if (window.opener) window.close(); else history.back(); });
    root.querySelector(".preview-print")?.addEventListener("click", () => window.print());
    root.querySelector(".preview-generate")?.addEventListener("click", async (event) => {
      const button = event.currentTarget as HTMLButtonElement; button.disabled = true; button.textContent = "Generating…";
      try {
        const quote = await quotationApi.create(bundle!.payload); bundle = { ...bundle!, document: quote };
        if (draftKey) localStorage.setItem(draftKey, JSON.stringify(bundle));
        appStore.set({ cartCount: 0 }); toast(`${quote.quotation_number} generated and saved`); render();
      } catch (error) { toast(error instanceof Error ? error.message : "Quotation could not be generated", "error"); button.disabled = false; button.textContent = "Generate & Save Quotation"; }
    });
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
    body.innerHTML = `<div class="detail-layout"><section class="panel detail-hero"><div class="profile-avatar"><i data-lucide="file-text"></i></div><div><span class="eyebrow">${escapeHtml(quote.quotation_number)}</span><h2>${escapeHtml(quote.customer_snapshot.name)}</h2><p>${formatDate(quote.created_at)} · ${escapeHtml(quote.currency)} · ${statusBadge(quote.status)}</p></div><div class="detail-hero-total"><span>Grand Total</span><strong>${formatMoney(quote.totals.grand_total, quote.currency)}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Quotation Items</span><h2>Line Items</h2></div><a class="button button-quiet" href="/quotation-preview?id=${encodeURIComponent(quote._id)}" target="_blank"><i data-lucide="eye"></i>Full-screen Preview</a></div><div class="detail-lines">${quote.lines.map((line) => `<div class="detail-line"><div><span class="article-label">Art. ${escapeHtml(line.article_no ?? line.product_id)}</span><strong>${escapeHtml(line.product_name)}</strong><small>${escapeHtml(line.description ?? "")}</small><small>${escapeHtml(configurationSummary({ configuration: line.configuration ?? {}, pricing_preview: line }))} · Qty ${line.quantity}${line.tax_amount ? ` · Tax ${line.tax_rate}%` : ""}</small></div><strong>${formatMoney(line.line_total, quote.currency)}</strong></div>`).join("")}</div><div class="summary-total"><span>Grand Total</span><strong>${formatMoney(quote.totals.grand_total, quote.currency)}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">Communication History</span><h2>Delivery Timeline</h2></div><span class="count-badge">${communication.length} events</span></div>${communication.length ? `<div class="timeline-list">${communication.map((item) => `<div class="timeline-item"><i data-lucide="${item.channel === "email" ? "mail" : item.channel === "whatsapp" ? "message-circle" : "file-text"}"></i><div><strong>${escapeHtml(String(item.action ?? item.status ?? item.channel ?? "Activity"))}</strong><small>${escapeHtml(String(item.recipient ?? item.provider_id ?? "Internal record"))}</small></div></div>`).join("")}</div>` : '<p class="muted">No delivery events recorded yet.</p>'}</section></div>`;
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

function cartTotalsMarkup(cart: { totals: Record<string, number> }, currency: string, showTax: boolean, action: string): string {
  return `<aside class="quote-summary panel"><div class="summary-head"><span class="eyebrow">Current quotation</span><h2>Cart Summary</h2><p>Prices are recalculated by the server before quotation preview.</p></div><div class="summary-row"><span>Subtotal</span><strong>${formatMoney(Number(cart.totals.subtotal ?? 0), currency)}</strong></div>${Number(cart.totals.discount_amount ?? 0) ? `<div class="summary-row"><span>Discount</span><strong>- ${formatMoney(Number(cart.totals.discount_amount), currency)}</strong></div>` : ""}${showTax ? `<div class="summary-row"><span>Taxable Amount</span><strong>${formatMoney(Number(cart.totals.taxable_amount ?? 0), currency)}</strong></div><div class="summary-row"><span>Tax</span><strong>${formatMoney(Number(cart.totals.tax_amount ?? 0), currency)}</strong></div>` : ""}<div class="summary-total"><span>Total</span><strong>${formatMoney(Number(cart.totals.grand_total ?? 0), currency)}</strong></div>${action}</aside>`;
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
  const enabled = body.querySelector<HTMLInputElement>("#cart-tax-enabled");
  const mode = body.querySelector<HTMLSelectElement>("#cart-tax-mode");
  const applyTax = async () => {
    if (!items.length || !enabled) return;
    enabled.disabled = true; if (mode) mode.disabled = true;
    const taxMode = mode?.value === "inclusive" ? "inclusive" : "exclusive";
    try { for (const item of items) await cartApi.update(item._id, { customer_id: customerId, tax_enabled: enabled.checked, tax_mode: taxMode }); toast(enabled.checked ? `INR tax enabled (${taxMode}) for all line items` : "INR tax disabled for all line items", "info"); await reload(); }
    catch (error) { toast(error instanceof Error ? error.message : "Could not update cart tax", "error"); enabled.disabled = false; if (mode) mode.disabled = false; }
  };
  enabled?.addEventListener("change", applyTax); mode?.addEventListener("change", applyTax);
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
    const showTax = appStore.state.currency === "INR" && items.some((item) => Number(item.pricing_preview.tax_amount ?? 0) > 0);
    body.innerHTML = `<div class="workspace-steps"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="done"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span class="active"><b>3</b>Cart</span><i data-lucide="chevron-right"></i><span><b>4</b>Quotation</span></div><div class="quote-layout"><section class="quote-main"><div class="section-title"><div><span class="eyebrow">${escapeHtml(appStore.state.currency)}</span><h2>Your current quotation items</h2></div><div class="section-title-actions"><span class="count-badge">${items.length} ${items.length === 1 ? "line" : "lines"}</span>${items.length ? '<button id="clear-cart" class="button button-quiet" title="Clear cart"><i data-lucide="trash-2"></i>Clear</button>' : ""}</div></div>${items.length ? `<div class="cart-lines">${items.map(cartLine).join("")}</div>` : emptyState("shopping-cart", "Cart is empty", "Use Calculator to configure a product.")}</section>${cartTotalsMarkup(cart, appStore.state.currency, showTax, items.length ? '<a class="button button-primary button-full" href="/quotation/create" data-route="/quotation/create"><i data-lucide="file-plus"></i>Continue to Quotation</a>' : '<a class="button button-secondary button-full" href="/calculator" data-route="/calculator"><i data-lucide="calculator"></i>Go to Calculator</a>')}</div>`;
    if (appStore.state.currency === "INR") {
      const summary = body.querySelector<HTMLElement>(".quote-summary"); const allEnabled = items.length > 0 && items.every((item) => item.tax_enabled === true); const taxMode = items.find((item) => item.tax_mode)?.tax_mode ?? "exclusive";
      summary?.insertAdjacentHTML("afterbegin", `<div class="tax-controls cart-tax-controls"><span class="eyebrow">INR Tax</span><label class="check-row"><input id="cart-tax-enabled" type="checkbox" ${allEnabled ? "checked" : ""}><span>Apply tax to all line items</span></label><label>Tax mode<select id="cart-tax-mode"><option value="exclusive" ${taxMode === "exclusive" ? "selected" : ""}>Tax exclusive</option><option value="inclusive" ${taxMode === "inclusive" ? "selected" : ""}>Tax inclusive</option></select></label></div>`);
    }
    await bindSeparatedCartActions(body, items, context.id!, context.name, load); refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Cart unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; refreshIcons(body); }
  refreshIcons(page); return page;
}

export async function quotationsPage(): Promise<HTMLElement> {
  const context = customerContext();
  const page = pageScaffold("Commercial", "Quotations", "View and manage previously generated quotations.");
  page.classList.add("quotation-history-page");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  if (!context.id) { body.innerHTML = emptyState("building-2", "Customer selection required", "Choose a customer before viewing quotation history."); refreshIcons(page); return page; }
  try {
    const result = await quotationApi.list(context.id); const quotations = result.items;
    body.innerHTML = `<div class="table-toolbar"><div class="field-search"><i data-lucide="search"></i><input id="quotation-search" placeholder="Search quotations" aria-label="Search quotations"></div><label class="compact-select"><span>Status</span><select id="quotation-status"><option value="">All</option><option>Draft</option><option>Sent</option><option>Viewed</option><option>Accepted</option><option>Rejected</option><option>Expired</option><option>Converted to Order</option><option>Cancelled</option></select></label><label class="compact-select"><span>Currency</span><select id="quotation-currency"><option value="">All</option><option>EUR</option><option>USD</option><option>INR</option></select></label><label class="compact-select"><span>Date</span><input id="quotation-date" type="date" aria-label="Filter quotations by date"></label><span data-quotation-count>${quotations.length} records</span></div><div data-quotation-rows>${quotationRows(quotations)}</div>`;
    const render = () => { const search = body.querySelector<HTMLInputElement>("#quotation-search")?.value.trim().toLowerCase() ?? ""; const status = body.querySelector<HTMLSelectElement>("#quotation-status")?.value ?? ""; const currency = body.querySelector<HTMLSelectElement>("#quotation-currency")?.value ?? ""; const date = body.querySelector<HTMLInputElement>("#quotation-date")?.value ?? ""; const filtered = quotations.filter((quote) => { const name = quote.customer_snapshot?.company_name ?? quote.customer_snapshot?.name ?? ""; return (!status || quote.status === status) && (!currency || quote.currency === currency) && (!date || String(quote.created_at ?? "").startsWith(date)) && (!search || [quote.quotation_number, name, quote.status, quote.customer_snapshot?.contact_name].some((value) => String(value ?? "").toLowerCase().includes(search))); }); const count = body.querySelector<HTMLElement>("[data-quotation-count]"); if (count) count.textContent = `${filtered.length} record${filtered.length === 1 ? "" : "s"}`; const rows = body.querySelector<HTMLElement>("[data-quotation-rows]"); if (rows) { rows.innerHTML = quotationRows(filtered); refreshIcons(rows); } };
    body.querySelector<HTMLInputElement>("#quotation-search")?.addEventListener("input", render); body.querySelector<HTMLSelectElement>("#quotation-status")?.addEventListener("change", render); body.querySelector<HTMLSelectElement>("#quotation-currency")?.addEventListener("change", render); body.querySelector<HTMLInputElement>("#quotation-date")?.addEventListener("change", render);
    body.addEventListener("click", async (event) => { const target = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-id]"); if (!target) return; if (target.classList.contains("print-quote")) { const printWindow = window.open(`/quotation-preview?id=${encodeURIComponent(target.dataset.id!)}`, "_blank"); if (!printWindow) toast("Allow pop-ups to print the quotation", "error"); return; } if (target.classList.contains("send-quote")) { const quote = quotations.find((item) => item._id === target.dataset.id); if (quote) openQuotationEmailComposer(quote, () => undefined); return; } target.disabled = true; try { if (target.classList.contains("whatsapp-quote")) { const result = await quotationApi.whatsapp(target.dataset.id!); toast(result.delivery.status === "mocked" ? "WhatsApp share recorded in mock mode" : "WhatsApp share queued", "info"); } else if (target.classList.contains("convert-quote")) { await quotationApi.convert(target.dataset.id!); toast("Quotation converted to order"); } } catch (error) { toast(error instanceof Error ? error.message : "Quotation action failed", "error"); } finally { target.disabled = false; } });
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Quotations unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function quotationPreparationPage(): Promise<HTMLElement> {
  const context = customerContext();
  const page = pageScaffold("Quotation Workflow", "Quotation Preparation", context.id ? `Prepare a quotation for ${context.name}.` : "Select a customer before preparing a quotation.", '<a class="button button-secondary" href="/cart" data-route="/cart"><i data-lucide="arrow-left"></i>Back to Cart</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(6);
  if (!context.id || !context.company) { body.innerHTML = emptyState("building-2", "Customer selection required", "Choose a customer before preparing a quotation."); refreshIcons(page); return page; }
  try {
    const cart = await cartApi.get(context.id, appStore.state.currency); appStore.set({ cartCount: cart.items.length });
    if (!cart.items.length) { body.innerHTML = `${emptyState("shopping-cart", "Quotation cart is empty", "Configure a product in Calculator before preparing a quotation.")}<a class="button button-secondary" href="/calculator" data-route="/calculator"><i data-lucide="calculator"></i>Go to Calculator</a>`; refreshIcons(page); return page; }
    const showTax = appStore.state.currency === "INR" && cart.items.some((item) => Number(item.pricing_preview.tax_amount ?? 0) > 0);
    body.innerHTML = `<div class="workspace-steps"><span class="done"><b>1</b>Customer</span><span class="done"><b>2</b>Products</span><span class="done"><b>3</b>Cart</span><span class="active"><b>4</b>Quotation</span></div><div class="quote-layout"><section class="quote-main"><section class="panel quotation-customer-card"><span class="eyebrow">Customer</span><h2>${escapeHtml(context.name)}</h2><p class="muted">Issued by Moneda Technologies to the selected customer.</p></section><section class="panel quotation-items-panel"><div class="section-title"><div><span class="eyebrow">Quotation Items</span><h2>Items being prepared</h2></div><span class="count-badge">${cart.items.length} line item${cart.items.length === 1 ? "" : "s"}</span></div><div class="cart-lines">${cart.items.map((item) => cartLine(item, false)).join("")}</div></section></section>${cartTotalsMarkup(cart, appStore.state.currency, showTax, "")}</div>`;
    body.querySelectorAll<HTMLElement>(".quotation-items-panel .cart-price").forEach((price, index) => {
      const line = cart.items[index]?.pricing_preview; if (!line) return;
      const label = price.querySelector("small"); const amount = price.querySelector("strong");
      if (label) label.textContent = "Line total";
      if (amount) amount.textContent = formatMoney(line.line_total, line.currency);
    });
    const summary = body.querySelector<HTMLElement>(".quote-summary")!; const form = document.createElement("form"); form.id = "quote-details-form"; form.className = "stack-form"; form.innerHTML = `<label>Quotation Currency<select name="currency"><option ${appStore.state.currency === "EUR" ? "selected" : ""}>EUR</option><option ${appStore.state.currency === "USD" ? "selected" : ""}>USD</option><option ${appStore.state.currency === "INR" ? "selected" : ""}>INR</option></select></label><label>Proforma Validity (days)<input name="proforma_validity_days" type="number" min="1" max="365" value="30" required></label><label>Payment Terms<select name="payment_terms"><option>Advance</option><option>POD</option><option>15 Days</option><option>30 Days</option></select></label><label>Transport<select name="transport_mode"><option value="by_consignee">By Consignee</option><option value="by_moneda_team">By Moneda Team</option></select></label><label class="transport-charge" hidden>Transport Charges (${escapeHtml(appStore.state.currency)})<input name="transport_charges" type="number" min="0" step="0.01" placeholder="Enter freight charge"></label><label>Notes<textarea name="notes" rows="3" placeholder="Optional commercial notes"></textarea></label><label class="check-row"><input name="create_lead" type="checkbox" checked><span>Create a linked CRM lead and follow-up</span></label><label>Follow-up Date<input name="follow_up_date" type="date"></label><button class="button button-primary button-full"><i data-lucide="eye"></i>Preview Quotation</button>`; summary.append(form);
    const transport = form.querySelector<HTMLSelectElement>("[name=transport_mode]"); const charge = form.querySelector<HTMLElement>(".transport-charge"); transport?.addEventListener("change", () => { const required = transport.value === "by_moneda_team"; if (charge) charge.hidden = !required; const input = charge?.querySelector<HTMLInputElement>("input"); if (input) { input.required = required; if (!required) input.value = ""; } });
    form.addEventListener("submit", async (event) => { event.preventDefault(); const data = new FormData(form); const previewWindow = window.open("about:blank", "_blank"); if (!previewWindow) { toast("Allow pop-ups to open the quotation preview", "error"); return; } previewWindow.document.write("<title>Preparing quotation preview…</title><p style='font:16px sans-serif;padding:40px'>Preparing secure quotation preview…</p>"); const payload: Record<string, unknown> = { customer_id: context.id, currency: data.get("currency"), proforma_validity_days: Number(data.get("proforma_validity_days")), payment_terms: data.get("payment_terms"), transport_mode: data.get("transport_mode"), transport_charges: data.get("transport_mode") === "by_moneda_team" ? Number(data.get("transport_charges")) : 0, notes: data.get("notes"), create_lead: data.get("create_lead") === "on", follow_up_date: data.get("follow_up_date") || null, reminder_frequency: "none", idempotency_key: crypto.randomUUID() }; try { const document = await quotationApi.preview(payload); const key = `moneda-quotation-preview-${crypto.randomUUID()}`; localStorage.setItem(key, JSON.stringify({ payload, document } satisfies PreviewBundle)); previewWindow.location.href = `/quotation-preview?draft=${encodeURIComponent(key)}`; } catch (error) { previewWindow.close(); toast(error instanceof Error ? error.message : "Quotation preview failed", "error"); } });
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Quotation preparation unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}
