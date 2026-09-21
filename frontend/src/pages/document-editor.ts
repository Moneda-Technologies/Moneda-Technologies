import { customerCompanyApi, orderApi, quotationApi } from "../api";
import { refreshIcons } from "../components/icons";
import { pageScaffold } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { CartItem, Currency, PriceLine } from "../types/domain";
import { escapeHtml, formatMoney, skeleton } from "../utils/dom";
import { openDocumentItemEditor } from "./catalog";

type EditorMode = "quotation" | "order";

function lineItem(line: Record<string, unknown>, index: number): CartItem {
  const id = String(line.item_id ?? line.line_id ?? index);
  return {
    _id: id, product_id: String(line.product_id ?? ""),
    configuration: (line.configuration as Record<string, unknown>) ?? {},
    quantity: Number(line.requested_quantity ?? line.quantity ?? 1),
    discount_percent: Number(line.requested_discount_percent ?? line.discount_percent ?? 0),
    currency: String(line.currency ?? "EUR") as Currency,
    master_currency: "EUR", display_currency: String(line.display_currency ?? "EUR") as Currency,
    pricing_preview: line as unknown as PriceLine,
  };
}

function itemPayload(item: CartItem): Record<string, unknown> {
  return { item_id: item._id, product_id: item.product_id, configuration: item.configuration,
    quantity: item.quantity, discount_percent: item.discount_percent, display_currency: item.display_currency ?? "EUR" };
}

function itemRows(items: CartItem[]): string {
  return items.map((item) => {
    const line = item.pricing_preview;
    const unit = Number(line.master_unit_price ?? line.unit_price ?? 0);
    const total = Number(line.master_final_total ?? line.line_total ?? 0);
    const config = Object.values(item.configuration).filter((value) => typeof value === "string" || typeof value === "number").slice(0, 3).join(" · ") || "—";
    return `<tr><td><strong>${escapeHtml(line.product_name ?? item.product_id)}</strong><small>${escapeHtml(line.article_no ?? "")}</small></td><td>${escapeHtml(config)}</td><td>${item.quantity}</td><td>${formatMoney(unit, "EUR")}</td><td>${item.discount_percent}%</td><td class="money">${formatMoney(total, "EUR")}</td><td><div class="row-actions"><button class="button button-quiet" data-edit-item="${escapeHtml(item._id)}"><i data-lucide="pencil"></i>Edit</button><button class="button button-quiet" data-remove-item="${escapeHtml(item._id)}"><i data-lucide="trash-2"></i>Remove</button></div></td></tr>`;
  }).join("");
}

export async function documentEditorPage(mode: EditorMode, id: string): Promise<HTMLElement> {
  const title = mode === "quotation" ? "Edit Quotation" : "Edit Working Order";
  const page = pageScaffold("Commercial", title, "Customer and items are loaded from this document. Changes are isolated until you save.", "");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  try {
    const document = mode === "quotation" ? await quotationApi.get(id) as unknown as Record<string, unknown> : await orderApi.get(id);
    const customerId = String(document.customer_id ?? document.customer_company_id ?? document.company_id ?? "");
    if (!customerId) throw new Error("This document has no customer context.");
    await customerCompanyApi.select(customerId);
    const customers = appStore.state.customers.length ? appStore.state.customers : (await customerCompanyApi.list()).items;
    const customer = customers.find((row) => String(row._id) === customerId);
    const snapshot = (document.customer_snapshot as Record<string, unknown> | undefined) ?? {};
    const customerName = String(customer?.name ?? snapshot.company_name ?? snapshot.name ?? "Customer");
    if (customer) appStore.set({ customer, activeCustomerId: customerId });
    let items = ((document.lines ?? document.products_snapshot ?? []) as Record<string, unknown>[]).map(lineItem);
    const number = String(document.quotation_number ?? document.order_number ?? id);
    const back = mode === "quotation" ? "/quotations" : `/orders/${encodeURIComponent(id)}`;
    const transport = (document.transport as Record<string, unknown> | undefined) ?? {};
    const commitOrderItems = async (nextItems: CartItem[]) => {
      if (mode === "order") await orderApi.update(id, { items: nextItems.map(itemPayload) });
      items = nextItems;
      render();
    };
    const render = () => {
      const total = items.reduce((sum, item) => sum + Number(item.pricing_preview.master_final_total ?? item.pricing_preview.line_total ?? 0), 0);
      body.innerHTML = `<section class="panel document-editor-header"><div><span class="eyebrow">${mode === "quotation" ? "Quotation" : "Working Order"}</span><h2>${escapeHtml(number)}</h2><p><strong>${escapeHtml(customerName)}</strong> · Customer locked for this edit · EUR master currency</p></div><div class="detail-hero-total"><span>Draft total</span><strong>${formatMoney(total, "EUR")}</strong></div></section><section class="panel"><div class="section-title"><div><span class="eyebrow">${mode === "quotation" ? "Quotation" : "Order"} items</span><h2>Current ${mode === "quotation" ? "quotation" : "order"} cart</h2></div><button class="button button-primary" data-add-item><i data-lucide="plus"></i>Add product</button></div><div class="data-table"><table><thead><tr><th>Product</th><th>Configuration</th><th>Qty</th><th>Unit price</th><th>Discount</th><th>Total</th><th>Actions</th></tr></thead><tbody>${itemRows(items)}</tbody></table></div></section>${mode === "quotation" ? `<section class="panel"><div class="section-title"><div><span class="eyebrow">Commercial details</span><h2>Quotation terms</h2></div></div><div class="form-grid"><label>Payment terms<input name="payment_terms" value="${escapeHtml(String(document.payment_terms ?? "Advance"))}"></label><label>Validity (days)<input name="validity_days" type="number" min="1" value="${Number(document.validity_days ?? 30)}"></label><label>Expiry date<input name="expiry_date" type="date" value="${escapeHtml(String(document.expiry_date ?? "").slice(0, 10))}"></label><label>Transport<select name="transport_mode"><option value="by_consignee"${transport.mode === "by_consignee" ? " selected" : ""}>By Consignee</option><option value="by_moneda_team"${transport.mode === "by_moneda_team" ? " selected" : ""}>By Moneda Team</option></select></label><label>Transport charges (EUR)<input name="transport_charges" type="number" min="0" step="0.01" value="${Number(transport.charges ?? 0)}"></label></div></section>` : ""}<div class="document-editor-actions"><a class="button button-secondary" href="${back}" data-route="${back}">Cancel</a><button class="button button-primary" data-save-document><i data-lucide="save"></i>Save ${mode === "quotation" ? "Quotation" : "Changes"}</button></div>`;
      body.querySelector<HTMLButtonElement>("[data-add-item]")?.addEventListener("click", () => void openDocumentItemEditor(customerId, undefined, async (saved) => { await commitOrderItems([...items, saved]); }));
      body.querySelectorAll<HTMLButtonElement>("[data-edit-item]").forEach((button) => button.addEventListener("click", () => {
        const item = items.find((row) => row._id === button.dataset.editItem); if (!item) return;
        void openDocumentItemEditor(customerId, item, async (saved) => { await commitOrderItems(items.map((row) => row._id === item._id ? saved : row)); });
      }));
      body.querySelectorAll<HTMLButtonElement>("[data-remove-item]").forEach((button) => button.addEventListener("click", () => { if (items.length <= 1) return toast("A document must contain at least one item", "error"); items = items.filter((row) => row._id !== button.dataset.removeItem); render(); }));
      body.querySelector<HTMLButtonElement>("[data-save-document]")?.addEventListener("click", async (event) => {
        const button = event.currentTarget as HTMLButtonElement; button.disabled = true;
        try {
          const payload: Record<string, unknown> = { items: items.map(itemPayload) };
          if (mode === "quotation") {
            payload.payment_terms = body.querySelector<HTMLInputElement>("[name=payment_terms]")?.value;
            payload.validity_days = Number(body.querySelector<HTMLInputElement>("[name=validity_days]")?.value ?? 30);
            payload.expiry_date = body.querySelector<HTMLInputElement>("[name=expiry_date]")?.value;
            payload.transport = {
              mode: body.querySelector<HTMLSelectElement>("[name=transport_mode]")?.value,
              charges: Number(body.querySelector<HTMLInputElement>("[name=transport_charges]")?.value ?? 0),
            };
            await quotationApi.update(id, payload);
          } else await orderApi.update(id, payload);
          toast(mode === "quotation" ? "Quotation saved" : "Working Order saved");
          window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: back }));
        } catch (error) { toast(error instanceof Error ? error.message : "Document could not be saved", "error"); button.disabled = false; }
      });
      refreshIcons(body);
    };
    render();
  } catch (error) { body.innerHTML = `<div class="notice error"><strong>${escapeHtml(error instanceof Error ? error.message : "Editor could not be opened")}</strong></div>`; }
  refreshIcons(page); return page;
}
