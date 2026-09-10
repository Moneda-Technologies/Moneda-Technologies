import { adminApi, rateApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { PriceHistoryEntry, PricingResource } from "../types/domain";
import { emptyState, escapeHtml, formatDate, skeleton } from "../utils/dom";


export async function pricingAdminPage(): Promise<HTMLElement> {
  const canEdit = appStore.can("pricing.edit");
  const page = pageScaffold("Management", "Product & Pricing", "Review the canonical catalogue, edit EUR master prices, and inspect every price change.");
  page.classList.add("pricing-management-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  const state = { family: "all", status: "all", search: "" };

  const load = async () => {
    body.innerHTML = skeleton(7);
    try {
      const query = new URLSearchParams({ family: state.family, status: state.status, search: state.search }).toString();
      const [result, rates] = await Promise.all([
        adminApi.pricingProducts(query),
        rateApi.get().catch(() => null),
      ]);
      const pending = result.items.filter((row) => row.pricing_status !== "configured").length;
      const rateStatus = rates ? (rates.status === "stored_fallback" || rates.stale ? "Using last successful ECB rate" : "Latest available") : "Unavailable";
      const rateDate = rates?.provider_dates?.USD || rates?.provider_dates?.INR || "—";
      body.innerHTML = `<div class="admin-callout"><div><span class="eyebrow">EUR master pricing</span><h2>One catalogue. One authoritative price.</h2><p>MongoDB is authoritative at runtime. JSON is used only for controlled bootstrap and migration.</p></div><div class="admin-flow"><span>Product identity</span><i data-lucide="chevron-right"></i><span>EUR master</span><i data-lucide="chevron-right"></i><strong>Reference conversion</strong></div></div>
        <div class="rate-source-strip panel"><div><span class="eyebrow">Currency engine</span><strong>${rateStatus} · Provider: ${escapeHtml(rates?.provider ?? "ECB")}</strong></div><div><span>EUR → USD</span><strong>${rates?.rates.USD?.toFixed(4) ?? "—"}</strong></div><div><span>EUR → INR</span><strong>${rates?.rates.INR?.toFixed(4) ?? "—"}</strong></div><div><span>Rate date</span><strong>${escapeHtml(rateDate)}</strong></div><div><span>Fetched time</span><strong>${rates?.fetched_at ? escapeHtml(formatDate(rates.fetched_at)) : "—"}</strong></div></div>
        <section class="pricing-toolbar panel" aria-label="Pricing filters"><label><span>Search</span><input id="pricing-search" value="${escapeHtml(state.search)}" placeholder="Product, article, SKU or category"></label><label><span>Family</span><select id="pricing-family">${["all", "blankets", "mpacks", "chemicals", "bars"].map((value) => `<option value="${value}" ${state.family === value ? "selected" : ""}>${({ all: "All families", blankets: "Blankets", mpacks: "Underpacking", chemicals: "Chemicals", bars: "Bars" } as Record<string, string>)[value]}</option>`).join("")}</select></label><label><span>Status</span><select id="pricing-status">${["all", "configured", "pending", "on_request", "inactive"].map((value) => `<option value="${value}" ${state.status === value ? "selected" : ""}>${value === "all" ? "All statuses" : value.replace("_", " ")}</option>`).join("")}</select></label><div class="pricing-result-count"><strong>${result.total}</strong><span>records</span></div></section>
        <div class="section-title"><div><span class="eyebrow">Master catalogue</span><h2>EUR product pricing</h2></div><span class="count-badge">${pending} need attention</span></div>
        ${result.items.length ? `<div class="data-table panel pricing-table"><table><thead><tr><th>Product</th><th>Family / category</th><th>Pricing unit</th><th>Master EUR</th><th>Status</th><th>Last updated</th><th>Updated by</th><th>Actions</th></tr></thead><tbody>${result.items.map((row) => pricingTableRow(row, canEdit)).join("")}</tbody></table></div>` : emptyState("search-x", "No pricing records found", "Change the search or filter selection.")}`;

      const search = body.querySelector<HTMLInputElement>("#pricing-search")!;
      let timer = 0;
      search.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => { state.search = search.value.trim(); void load(); }, 300);
      });
      body.querySelector<HTMLSelectElement>("#pricing-family")?.addEventListener("change", (event) => { state.family = (event.target as HTMLSelectElement).value; void load(); });
      body.querySelector<HTMLSelectElement>("#pricing-status")?.addEventListener("change", (event) => { state.status = (event.target as HTMLSelectElement).value; void load(); });
      body.querySelectorAll<HTMLButtonElement>("[data-edit-price]").forEach((button) => button.addEventListener("click", () => {
        const row = result.items.find((item) => item.id === button.dataset.editPrice);
        if (row) void openPriceEditor(row, load);
      }));
      body.querySelectorAll<HTMLButtonElement>("[data-price-history]").forEach((button) => button.addEventListener("click", () => {
        const row = result.items.find((item) => item.id === button.dataset.priceHistory);
        if (row) void openPriceHistory(row);
      }));
      refreshIcons(body);
    } catch (error) {
      body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Product pricing unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
      refreshIcons(body);
    }
  };
  await load();
  return page;
}


function pricingTableRow(row: PricingResource, canEdit: boolean): string {
  const maps = [row.pricing.variant_prices, row.pricing.dimension_prices, row.pricing.package_prices].filter(Boolean) as Record<string, number | null>[];
  const configuredValues = maps.flatMap((value) => Object.values(value)).filter((value): value is number => value !== null);
  const price = row.pricing.price_eur !== null
    ? `€${row.pricing.price_eur.toFixed(2)}`
    : configuredValues.length ? `${configuredValues.length} variant${configuredValues.length === 1 ? "" : "s"}` : "Not set";
  const updatedAt = row.price_updated_at ? new Date(row.price_updated_at).toLocaleDateString() : "—";
  return `<tr><td><div class="table-product"><span><i data-lucide="${row.entity_type === "bar" ? "minus" : "package"}"></i></span><p><strong>${escapeHtml(row.name)}</strong><small>Art. ${escapeHtml(row.article_no || row.sku || "—")}</small></p></div></td><td><strong>${escapeHtml(row.family_name)}</strong><small>${escapeHtml(row.category_name)}</small></td><td>${escapeHtml(row.pricing.pricing_type || "—")}<small>per ${escapeHtml(row.pricing.unit || "unit")}</small></td><td><strong>${price}</strong><small>EUR / ${escapeHtml(row.pricing.unit || "unit")}</small></td><td>${statusBadge(row.pricing_status.replace("_", " "))}</td><td>${escapeHtml(updatedAt)}</td><td>${escapeHtml(row.price_updated_by || "Seed data")}</td><td><div class="table-actions">${canEdit ? `<button class="button button-small button-dark" data-edit-price="${escapeHtml(row.id)}"><i data-lucide="pencil"></i>Edit</button>` : ""}<button class="button button-small button-quiet" data-price-history="${escapeHtml(row.id)}"><i data-lucide="history"></i>History</button></div></td></tr>`;
}


function priceChoices(row: PricingResource): Array<{ map: string; key: string; label: string; value: number | null }> {
  const groups: Array<[string, string, Record<string, number | null> | undefined]> = [
    ["variant_prices", "Thickness", row.pricing.variant_prices],
    ["dimension_prices", "Dimension", row.pricing.dimension_prices],
    ["package_prices", "Package", row.pricing.package_prices],
  ];
  const choices = groups.flatMap(([map, label, values]) => Object.entries(values ?? {}).map(([key, value]) => ({ map, key, label: `${label}: ${key}`, value })));
  return choices.length ? choices : [{ map: "", key: "", label: "Base price", value: row.pricing.price_eur }];
}


async function openPriceEditor(row: PricingResource, reload: () => Promise<void>): Promise<void> {
  const choices = priceChoices(row);
  const content = document.createElement("div");
  content.innerHTML = `<div class="price-editor-summary"><span><i data-lucide="package"></i></span><div><strong>${escapeHtml(row.name)}</strong><p>Art. ${escapeHtml(row.article_no || row.sku || "—")} · ${escapeHtml(row.family_name)} · EUR/${escapeHtml(row.pricing.unit)}</p></div></div><div class="price-editor-preview" data-live-preview><span class="eyebrow">Reference equivalents</span><strong>Loading current FX…</strong><small>USD and INR are read-only conversions from the EUR master.</small></div><form class="stack-form price-editor-form">${choices.length > 1 ? `<label>Price variant<select name="choice">${choices.map((choice, index) => `<option value="${index}">${escapeHtml(choice.label)}</option>`).join("")}</select></label>` : `<div class="field-readonly"><span>Price scope</span><strong>${escapeHtml(choices[0].label)}</strong></div>`}<label>New EUR master price<input name="price_eur" type="number" min="0" step="0.01" value="${choices[0].value ?? ""}" placeholder="Leave empty for unavailable"></label><label>Pricing status<select name="pricing_status">${["configured", "pending", "on_request", "inactive"].map((status) => `<option value="${status}" ${row.pricing_status === status ? "selected" : ""}>${status.replace("_", " ")}</option>`).join("")}</select></label><label>Reason for price change <span class="optional">optional</span><textarea name="reason" rows="3" maxlength="500" placeholder="Why is this EUR master price changing?"></textarea></label><div class="notice compact"><i data-lucide="shield-check"></i><div><strong>EUR master only</strong><p>USD and INR use the latest ECB reference conversion. Existing quotations remain unchanged.</p></div></div><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit"><i data-lucide="save"></i>Save Price</button></div></form>`;
  const dialog = openModal("Edit EUR Master Price", content, "normal");
  const form = content.querySelector<HTMLFormElement>("form")!;
  const priceInput = form.elements.namedItem("price_eur") as HTMLInputElement;
  const choiceSelect = form.elements.namedItem("choice") as HTMLSelectElement | null;
  const saveButton = form.querySelector<HTMLButtonElement>("[type=submit]")!;
  const initialPrice = priceInput.value;
  const initialStatus = String((form.elements.namedItem("pricing_status") as HTMLSelectElement)?.value ?? "");
  const updateDirtyState = () => { saveButton.disabled = priceInput.value === initialPrice && String((form.elements.namedItem("pricing_status") as HTMLSelectElement)?.value ?? "") === initialStatus && !String((form.elements.namedItem("reason") as HTMLTextAreaElement)?.value ?? "").trim(); };
  priceInput.addEventListener("input", updateDirtyState);
  (form.elements.namedItem("pricing_status") as HTMLSelectElement | null)?.addEventListener("change", updateDirtyState);
  (form.elements.namedItem("reason") as HTMLTextAreaElement | null)?.addEventListener("input", updateDirtyState);
  updateDirtyState();
  const preview = content.querySelector<HTMLElement>("[data-live-preview]");
  try {
    const rates = await rateApi.get();
    const renderPreview = () => {
      const price = Number(priceInput.value);
      if (!preview) return;
      if (!(price >= 0) || !rates.rates.USD || !rates.rates.INR) { preview.innerHTML = `<span class="eyebrow">Reference equivalents</span><strong>Unavailable</strong><small>Enter a EUR master price to preview conversions.</small>`; return; }
      preview.innerHTML = `<span class="eyebrow">Reference equivalents · ${rates.status === "stored_fallback" || rates.stale ? "Using last successful ECB rate" : "Latest available ECB Reference Rate"}</span><div><strong>USD $${(price * rates.rates.USD).toFixed(2)}</strong><strong>INR ₹${(price * rates.rates.INR).toFixed(2)}</strong></div><small>Read-only conversion from EUR master.</small>`;
    };
    priceInput.addEventListener("input", renderPreview);
    renderPreview();
  } catch { if (preview) preview.innerHTML = `<span class="eyebrow">Reference equivalents</span><strong>FX rates unavailable</strong><small>EUR master editing remains available.</small>`; }
  choiceSelect?.addEventListener("change", () => { priceInput.value = choices[Number(choiceSelect.value)].value?.toString() ?? ""; updateDirtyState(); });
  content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const choice = choices[Number(choiceSelect?.value ?? 0)];
    const priceValue = String(data.get("price_eur") ?? "").trim();
    const nextPrice = priceValue === "" ? null : Number(priceValue);
    if (priceValue !== "" && (!Number.isFinite(nextPrice) || Number(nextPrice) < 0)) {
      priceInput.setCustomValidity("Price cannot be negative.");
      priceInput.reportValidity();
      return;
    }
    priceInput.setCustomValidity("");
    const previousLabel = choice.value === null ? "unavailable" : `€${choice.value.toFixed(2)}`;
    const nextLabel = nextPrice === null ? "unavailable" : `€${nextPrice.toFixed(2)}`;
    if (!window.confirm(`Update ${choice.label} from ${previousLabel} to ${nextLabel}?`)) return;
    const submit = form.querySelector<HTMLButtonElement>("[type=submit]")!; submit.disabled = true;
    try {
      await adminApi.updatePrice(row.id, { currency: "EUR", price_eur: nextPrice, price_map: choice.map || undefined, price_key: choice.key || undefined, pricing_status: data.get("pricing_status"), reason: data.get("reason") });
      dialog.close(); toast("EUR master price updated"); await reload();
    } catch (error) { toast(error instanceof Error ? error.message : "Price could not be updated", "error"); submit.disabled = false; }
  });
}


async function openPriceHistory(row: PricingResource): Promise<void> {
  const content = document.createElement("div"); content.innerHTML = skeleton(4);
  openModal(`Price history · ${row.name}`, content, "wide");
  try {
    const result = await adminApi.priceHistory(row.id);
    content.innerHTML = result.items.length ? `<div class="data-table price-history-table"><table><thead><tr><th>Effective date</th><th>Scope</th><th>Previous</th><th>New EUR</th><th>Changed by</th><th>Reason</th></tr></thead><tbody>${result.items.map(priceHistoryRow).join("")}</tbody></table></div>` : emptyState("history", "No price changes yet", "This record still uses its controlled seed value.");
  } catch (error) { content.innerHTML = `<div class="notice error"><strong>History unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div>`; }
  refreshIcons(content);
}


function priceHistoryRow(row: PriceHistoryEntry): string {
  const oldValue = row.old_price_eur ?? row.old_price;
  const newValue = row.new_price_eur ?? row.new_price;
  const timestamp = row.changed_at ?? row.created_at;
  return `<tr><td>${timestamp ? escapeHtml(new Date(timestamp).toLocaleString()) : "—"}</td><td>${escapeHtml(row.price_key ? `${row.price_map?.replace("_prices", "")}: ${row.price_key}` : "Base price")}</td><td>${oldValue === null || oldValue === undefined ? "Unavailable" : `€${Number(oldValue).toFixed(2)}`}</td><td><strong>${newValue === null || newValue === undefined ? "Unavailable" : `€${Number(newValue).toFixed(2)}`}</strong></td><td>${escapeHtml(row.changed_by_name || row.changed_by || "System")}</td><td>${escapeHtml(row.reason || "—")}</td></tr>`;
}
