import { cartApi, catalogApi, machineApi, rateApi } from "../api";
import { ApiError } from "../api/client";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { CartItem, CatalogOption, PriceLine, Product } from "../types/domain";
import { emptyState, escapeHtml, formatDate, formatMoney, skeleton } from "../utils/dom";

const familyCopy: Record<string, { icon: string; label: string; detail: string }> = {
  blankets: { icon: "rectangle-horizontal", label: "Printing Blankets", detail: "Select a blanket group, product, thickness, dimensions and format." },
  mpacks: { icon: "layers-3", label: "Underpacking", detail: "Configure thickness, press size and underpacking type." },
  chemicals: { icon: "flask-conical", label: "Chemicals & Maintenance", detail: "Select a chemical and calculate complete container quantities." },
};

type ConfigValue = string | number | boolean;
type Config = Record<string, unknown>;

function productChoice(product: Product): string {
  return `${product.name} — Art. ${product.article_no ?? product.sku}`;
}

function selected(value: unknown, expected: unknown): string {
  return String(value ?? "") === String(expected) ? "selected" : "";
}

function valueAttr(value: unknown): string {
  return escapeHtml(value === undefined || value === null ? "" : String(value));
}

function familyCards(families: CatalogOption[]): string {
  return `<div class="calculator-category-grid">${families.map((family, index) => {
    const copy = familyCopy[family.id];
    return `<a class="calculator-category-card category-${escapeHtml(family.id)}" href="/products/${escapeHtml(family.id)}" data-route="/products/${escapeHtml(family.id)}"><span class="category-index">0${index + 1}</span><span class="category-card-icon"><i data-lucide="${copy?.icon ?? "package"}"></i></span><div><span class="eyebrow">${escapeHtml(copy?.label ?? family.name)}</span><h2>${escapeHtml(family.name)}</h2><p>${escapeHtml(copy?.detail ?? "Open calculator")}</p></div><span class="category-open">Open calculator<i data-lucide="arrow-up-right"></i></span></a>`;
  }).join("")}</div>`;
}

function machineField(product: Product, initial: Config): string {
  const machines = (product.configuration.machine_options as Array<{ id?: string; name?: string } | string> | undefined) ?? [];
  const datalistId = `machine-options-${product._id.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
  const initialMachine = String(initial.machine ?? "");
  const initialMachineName = machines.reduce((value, item) => {
    if (value) return value;
    if (typeof item === "string") return item === initialMachine ? item : "";
    return String(item.id ?? "") === initialMachine ? String(item.name ?? item.id ?? "") : "";
  }, "");
  const options = machines.map((item) => {
    const name = typeof item === "string" ? item : String(item.name ?? item.id ?? "");
    return `<option value="${escapeHtml(name)}"></option>`;
  }).join("");
  return `<label>Machine / Model <small>(Optional)</small><input name="machine" list="${datalistId}" value="${valueAttr(initialMachineName || initialMachine)}" placeholder="Type or select machine" autocomplete="off"><datalist id="${datalistId}" data-machine-options>${options}</datalist><small>New machine names are saved for future selections.</small></label>`;
}

function blanketFields(product: Product, initial: Config): string {
  const thicknesses = (product.configuration.thicknesses_mm as number[] | undefined) ?? [];
  const widths = (product.configuration.standard_widths_mm as number[] | undefined) ?? [];
  const declaredFormats = (product.configuration.format_types as string[] | undefined) ?? [];
  const formats = declaredFormats.length ? declaredFormats : [
    ...(product.configuration.supports_cut_format !== false ? ["cut_format"] : []),
    ...(product.configuration.supports_bar_format !== false ? ["bar_format"] : []),
  ];
  const bars = (product.configuration.bar_options as Array<{ id: string; article_no: string; name: string; pricing_status: string }> | undefined) ?? [];
  const defaults = (product.configuration.default_bar_ids as string[] | undefined) ?? ["aluminium", "aluminium"];
  const initialFormat = formats.includes(String(initial.format_type)) ? String(initial.format_type) : String(formats[0] ?? "cut_format");
  const initialUseSecondBar = initial.use_second_bar === true || Boolean(initial.bar_2_id && initial.bar_1_id && initial.bar_2_id !== initial.bar_1_id);
  const barOptions = (value: unknown, fallback: string) => bars.map((bar) => `<option value="${escapeHtml(bar.id)}" ${selected(value ?? fallback, bar.id)} ${bar.pricing_status !== "configured" ? "disabled" : ""}>${escapeHtml(bar.name)} · Art. ${escapeHtml(bar.article_no)}${bar.pricing_status !== "configured" ? " · On request" : ""}</option>`).join("");
  return `<div class="form-grid">${machineField(product, initial)}<label>Thickness<select name="thickness_mm" required>${thicknesses.map((value) => `<option value="${value}" ${selected(initial.thickness_mm ?? thicknesses[0], value)}>${value.toFixed(2)} mm</option>`).join("")}</select></label><label>Dimension Unit<select name="dimension_unit"><option value="mm" ${selected(initial.dimension_unit ?? "mm", "mm")}>Millimetres</option><option value="inch" ${selected(initial.dimension_unit, "inch")}>Inches</option><option value="m" ${selected(initial.dimension_unit, "m")}>Metres</option></select></label><label>Length<input name="length" type="number" min="0.001" step="any" value="${valueAttr(initial.length)}" placeholder="Enter length" required></label><label>Width<input name="width" type="number" min="0.001" step="any" value="${valueAttr(initial.width)}" placeholder="Enter or select width" list="standard-widths" required><datalist id="standard-widths">${widths.map((width) => `<option value="${width}"></option>`).join("")}</datalist><small class="width-guidance">${widths.length ? `Standard: ${widths.join(", ")} mm` : "Custom width"}</small></label><label>Format<select name="format_type" required>${formats.map((format) => `<option value="${format}" ${selected(initialFormat, format)}>${format === "bar_format" ? "Bar Format" : "Cut Format"}</option>`).join("")}</select></label></div><div class="form-grid bar-fields" ${initialFormat === "bar_format" ? "" : "hidden"}><label>Bar 1<select name="bar_1_id" ${initialFormat === "bar_format" ? "required" : ""}>${barOptions(initial.bar_1_id, defaults[0])}</select></label><label class="check-row different-second-bar"><input name="use_second_bar" type="checkbox" ${initialUseSecondBar ? "checked" : ""}><span>Different second bar</span></label><label class="second-bar-field" ${initialUseSecondBar ? "" : "hidden"}>Bar 2<select name="bar_2_id" ${initialUseSecondBar ? "required" : ""}>${barOptions(initial.bar_2_id, defaults[1])}</select></label></div>`;
}

function mpackFields(product: Product, initial: Config): string {
  const thicknesses = (product.configuration.thicknesses as Array<{ label: string; value: number }> | undefined) ?? [];
  const presets = (product.configuration.size_presets as Array<{ micron: number; sizes_mm: Array<{ id: number; width: number; length: number }> }> | undefined) ?? [];
  const presetOptions = presets.flatMap((row) => row.sizes_mm.map((size) => ({ ...size, micron: row.micron })));
  return `<div class="form-grid">${machineField(product, initial)}<label>Standard Size <small>(Optional)</small><select name="size_preset"><option value="">Custom dimensions</option>${presetOptions.map((size) => `<option value="${size.id}" data-thickness="${size.micron}" data-length="${size.length}" data-width="${size.width}">${size.length} × ${size.width} mm · ${size.micron} micron</option>`).join("")}</select></label><label>Thickness<select name="thickness_micron" required>${thicknesses.map((item) => `<option value="${item.value}" ${selected(initial.thickness_micron ?? thicknesses[0]?.value, item.value)}>${escapeHtml(item.label)}</option>`).join("")}</select></label><label>Dimension Unit<select name="dimension_unit"><option value="mm" ${selected(initial.dimension_unit ?? "mm", "mm")}>Millimetres</option><option value="inch" ${selected(initial.dimension_unit, "inch")}>Inches</option><option value="m" ${selected(initial.dimension_unit, "m")}>Metres</option></select></label><label>Length<input name="length" type="number" min="0.001" step="any" value="${valueAttr(initial.length)}" placeholder="Enter length" required></label><label>Width<input name="width" type="number" min="0.001" step="any" value="${valueAttr(initial.width)}" placeholder="Enter width" required></label></div>`;
}

function chemicalFields(product: Product, initial: Config): string {
  const formats = (product.configuration.formats as Array<{ id: string; label: string; size_litre: number }> | undefined) ?? [];
  return `<div class="form-grid"><label>Chemical Category<input value="${valueAttr(product.configuration.sub_category)}" disabled></label><label>Container / Package<select name="format_id" required>${formats.map((format) => `<option value="${escapeHtml(format.id)}" data-size="${format.size_litre}" ${selected(initial.format_id ?? formats[0]?.id, format.id)}>${escapeHtml(format.label)}</option>`).join("")}</select></label><label>Requested Litres<input name="requested_litres" type="number" min="0.01" step="0.01" value="${valueAttr(initial.requested_litres)}" placeholder="Enter required volume" required></label></div><div class="notice compact"><i data-lucide="package-check"></i><div><strong>Complete containers only</strong><p>The backend rounds the requested volume up to the selected package size.</p></div></div>`;
}

function configurationFields(product: Product, initial: Config): string {
  const configurator = String(product.configuration.configurator ?? "unit");
  if (product.category_id === "blankets") return blanketFields(product, initial);
  if (configurator === "mpack") return mpackFields(product, initial);
  if (configurator === "chemical") return chemicalFields(product, initial);
  return `<div class="notice compact"><i data-lucide="package-check"></i><div><strong>Unit product</strong><p>Price is calculated per ${escapeHtml(product.pricing.unit)}.</p></div></div>`;
}

function configurationFromForm(product: Product, form: HTMLFormElement): Record<string, ConfigValue> {
  const data = new FormData(form);
  const result: Record<string, ConfigValue> = {};
  const stringFields = new Set(["machine", "dimension_unit", "format_type", "bar_1_id", "bar_2_id", "format_id"]);
  ["machine", "thickness_mm", "length", "width", "dimension_unit", "thickness_micron", "format_type", "bar_1_id", "bar_2_id", "requested_litres", "format_id"].forEach((key) => {
    if ((key === "bar_1_id" || key === "bar_2_id") && form.querySelector<HTMLSelectElement>("[name=format_type]")?.value !== "bar_format") return;
    const value = data.get(key);
    if (value === null || value === "") return;
    result[key] = stringFields.has(key) ? String(value) : Number(value);
  });
  if (form.querySelector<HTMLSelectElement>("[name=format_type]")?.value === "bar_format") {
    const useSecondBar = form.querySelector<HTMLInputElement>("[name=use_second_bar]")?.checked === true;
    result.use_second_bar = useSecondBar;
    // The unchecked state is still a two-bar configuration: send Bar 1 as
    // both IDs so older API clients also calculate the selected bar ×2.
    if (!useSecondBar && result.bar_1_id) result.bar_2_id = result.bar_1_id;
  }
  const packageSelect = form.querySelector<HTMLSelectElement>("[name=format_id]");
  if (packageSelect?.value) result.size_litre = Number(packageSelect.selectedOptions[0]?.dataset.size ?? 0);
  if (product.configuration.underpacking_type) result.underpacking_type = String(product.configuration.underpacking_type);
  return result;
}

function pricingMarkup(line: PriceLine, rate: { provider: string; provider_source?: string; source?: string; fetched_at: string; stale: boolean; warning?: string }): string {
  const showMaster = appStore.can("pricing.history");
  const showTax = line.currency === "INR" && line.tax_amount > 0;
  const netTotal = showTax ? line.line_total : line.subtotal;
  if (showTax) {
    line = { ...line, adjustments: [...line.adjustments, { type: "tax", label: `Tax ${line.tax_rate}% (${line.tax_mode})`, amount_master: line.tax_amount / line.exchange_rate }] };
  }
  return `<div class="live-price-head"><span><i data-lucide="shield-check"></i>Pricing Summary</span>${rate.stale ? '<span class="rate-stale">Cached rate</span>' : '<span class="rate-live">Rate verified</span>'}</div><div class="live-price-rows">${showMaster ? `<div><span>Master price</span><strong>${formatMoney(line.master_unit_price, "EUR")} / ${escapeHtml(line.pricing_unit)}</strong></div>` : ""}${line.area_sqm !== undefined ? `<div><span>Area</span><strong>${line.area_sqm.toFixed(4)} m²</strong></div>` : ""}${line.adjustments.map((item) => `<div><span>${escapeHtml(item.label)}${item.quantity ? ` × ${item.quantity}` : ""}</span><strong>${formatMoney(item.amount_master * line.exchange_rate, line.currency)}</strong></div>`).join("")}<div><span>Subtotal</span><strong>${formatMoney(line.subtotal, line.currency)}</strong></div>${line.discount_amount ? `<div><span>Discount ${line.discount_percent}%</span><strong>- ${formatMoney(line.discount_amount, line.currency)}</strong></div>` : ""}</div><div class="live-price-total"><span>Total</span><strong>${formatMoney(netTotal, line.currency)}</strong></div><p class="rate-caption">1 EUR = ${line.exchange_rate.toFixed(4)} ${line.currency} · ${escapeHtml(rate.provider_source ?? rate.provider)} · ${rate.source === "cached" || rate.stale ? "cached" : "live"} · ${formatDate(rate.fetched_at)}</p>${rate.warning ? `<p class="rate-warning">${escapeHtml(rate.warning)}</p>` : ""}`;
}

interface ConfiguratorOptions {
  mode: "add" | "edit";
  initial?: CartItem;
  onSaved?: (item: CartItem) => void | Promise<void>;
}

function renderConfigurator(host: HTMLElement, product: Product, options: ConfiguratorOptions): void {
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  if (!customerCompany) return;
  const initial = options.initial?.configuration ?? {};
  const configured = product.pricing_status === "configured";
  host.innerHTML = `<section class="inline-configurator"><div class="selected-product"><span class="product-dialog-icon"><i data-lucide="${familyCopy[product.category_id]?.icon ?? "package"}"></i></span><div><span class="eyebrow">Art. ${escapeHtml(product.article_no ?? product.sku)}</span><h3>${escapeHtml(product.name)}</h3><p>${escapeHtml(product.description)}</p></div></div>${configured ? "" : `<div class="notice warning"><i data-lucide="clock-3"></i><div><strong>EUR price ${product.pricing_status === "on_request" ? "is on request" : "is pending"}</strong><p>An administrator must configure the master price before this product can be calculated.</p></div></div>`}<form class="stack-form configure-form"><div class="form-section"><div class="section-number">01</div><div><h4>Product Configuration</h4><p>Required fields are checked and priced by the Moneda API.</p></div></div>${configurationFields(product, initial)}<div class="form-section"><div class="section-number">02</div><div><h4>Commercial Details</h4><p>Discount permissions and tax rules are validated on the server.</p></div></div><div class="form-grid"><label>Quantity<input name="quantity" type="number" min="1" max="100000" value="${valueAttr(options.initial?.quantity ?? 1)}" required></label><label>Discount<select name="discount_percent">${Array.from({ length: 21 }, (_, index) => index * 0.5).map((discount) => `<option value="${discount}" ${selected(options.initial?.discount_percent ?? 0, discount)}>${discount.toFixed(1)}%</option>`).join("")}</select></label></div><button class="button button-primary button-full" type="submit" ${configured ? "" : "disabled"}><i data-lucide="${options.mode === "edit" ? "save" : "shopping-cart"}"></i>${options.mode === "edit" ? "Save Changes" : "Add Configured Item to Cart"}</button></form><aside class="live-price inline-price-summary panel"><div class="live-price-placeholder"><i data-lucide="calculator"></i><h4>Pricing Summary</h4><p>Complete the required configuration to see the server-calculated amount.</p></div></aside></section>`;
  const form = host.querySelector<HTMLFormElement>(".configure-form")!;
  const machineInput = form.querySelector<HTMLInputElement>("[name=machine]");
  const machineList = form.querySelector<HTMLDataListElement>("[data-machine-options]");
  const knownMachines = new Set(Array.from(machineList?.options ?? []).map((option) => option.value.trim().toLowerCase()).filter(Boolean));
  const addMachineOption = (name: string) => {
    if (!machineList || knownMachines.has(name.toLowerCase())) return;
    const option = document.createElement("option");
    option.value = name;
    machineList.append(option);
    knownMachines.add(name.toLowerCase());
  };
  const loadMachines = async () => {
    try {
      const result = await machineApi.list();
      result.items.forEach((machine) => addMachineOption(machine.name));
    } catch { /* Product-provided machine options remain available offline. */ }
  };
  const ensureMachineStored = async () => {
    const name = machineInput?.value.trim() ?? "";
    if (!name || knownMachines.has(name.toLowerCase())) return;
    const machine = await machineApi.create(name);
    addMachineOption(machine.name);
    if (machineInput) machineInput.value = machine.name;
  };
  void loadMachines();
  machineInput?.addEventListener("blur", () => { void ensureMachineStored().catch((error) => toast(error instanceof Error ? error.message : "Machine could not be saved", "error")); });
  const commercialNote = form.querySelectorAll<HTMLElement>(".form-section")[1]?.querySelector("p");
  if (commercialNote) commercialNote.textContent = "Discount permissions are validated on the server. INR tax is optional.";
  if (appStore.state.currency === "INR") {
    const controls = document.createElement("div");
    controls.className = "tax-controls product-tax-controls";
    controls.innerHTML = `<label class="check-row"><input name="tax_enabled" type="checkbox" ${options.initial?.tax_enabled ? "checked" : ""}><span>Apply INR tax to this product</span></label><label>Tax mode<select name="tax_mode"><option value="exclusive" ${options.initial?.tax_mode !== "inclusive" ? "selected" : ""}>Tax exclusive</option><option value="inclusive" ${options.initial?.tax_mode === "inclusive" ? "selected" : ""}>Tax inclusive</option></select></label>`;
    form.querySelector<HTMLButtonElement>("button[type=submit]")?.before(controls);
  }
  const preview = host.querySelector<HTMLElement>(".live-price")!;
  const toggleBars = () => {
    const barFields = form.querySelector<HTMLElement>(".bar-fields");
    const barFormat = form.querySelector<HTMLSelectElement>("[name=format_type]")?.value === "bar_format";
    if (barFields) barFields.hidden = !barFormat;
    const useSecond = form.querySelector<HTMLInputElement>("[name=use_second_bar]")?.checked === true;
    barFields?.querySelectorAll<HTMLSelectElement>("select").forEach((select) => {
      const second = select.name === "bar_2_id";
      select.required = barFormat && (!second || useSecond);
      select.disabled = !barFormat || (second && !useSecond);
    });
    const secondBar = form.querySelector<HTMLElement>(".second-bar-field");
    if (secondBar) secondBar.hidden = !barFormat || !useSecond;
  };
  const updateWidthGuidance = () => {
    const thickness = Number(form.querySelector<HTMLSelectElement>("[name=thickness_mm]")?.value);
    const variants = (product.configuration.thickness_variants as Array<{ thickness_mm: number; standard_widths_mm: number[] }> | undefined) ?? [];
    if (!variants.length) return;
    const widths = variants.find((item) => Number(item.thickness_mm) === thickness)?.standard_widths_mm ?? [];
    const list = form.querySelector<HTMLDataListElement>("#standard-widths");
    if (list) list.innerHTML = widths.map((width) => `<option value="${width}"></option>`).join("");
    const guidance = form.querySelector<HTMLElement>(".width-guidance");
    if (guidance) guidance.textContent = widths.length ? `Standard: ${widths.join(", ")} mm` : "Custom width";
  };
  const applyPreset = () => {
    const select = form.querySelector<HTMLSelectElement>("[name=size_preset]");
    if (!select?.value) return;
    const option = select.selectedOptions[0];
    const length = form.querySelector<HTMLInputElement>("[name=length]");
    const width = form.querySelector<HTMLInputElement>("[name=width]");
    const thickness = form.querySelector<HTMLSelectElement>("[name=thickness_micron]");
    const unit = form.querySelector<HTMLSelectElement>("[name=dimension_unit]");
    if (length) length.value = option.dataset.length ?? "";
    if (width) width.value = option.dataset.width ?? "";
    if (thickness) thickness.value = option.dataset.thickness ?? thickness.value;
    if (unit) unit.value = "mm";
  };
  let timer = 0;
  const calculatePreview = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      if (!configured || !form.checkValidity()) {
        preview.innerHTML = '<div class="live-price-placeholder"><i data-lucide="calculator"></i><h4>Pricing Summary</h4><p>Complete the required configuration to calculate.</p></div>';
        refreshIcons(preview); return;
      }
      try {
        preview.classList.add("loading");
        const data = new FormData(form);
        const taxEnabled = appStore.state.currency === "INR" && form.querySelector<HTMLInputElement>("[name=tax_enabled]")?.checked === true;
        const taxMode = form.querySelector<HTMLSelectElement>("[name=tax_mode]")?.value ?? "exclusive";
        const result = await catalogApi.preview(product._id, {
          customer_id: customerCompany._id, currency: appStore.state.currency,
          configuration: configurationFromForm(product, form),
          quantity: Number(data.get("quantity")), discount_percent: Number(data.get("discount_percent")),
          tax_enabled: taxEnabled, tax_mode: taxMode,
        });
        preview.innerHTML = pricingMarkup(result.line, result.rate); refreshIcons(preview);
      } catch (error) {
        preview.innerHTML = `<div class="notice warning"><i data-lucide="circle-alert"></i><div><strong>Preview unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Check the configuration")}</p></div></div>`;
        refreshIcons(preview);
      } finally { preview.classList.remove("loading"); }
    }, 220);
  };
  form.querySelector<HTMLSelectElement>("[name=format_type]")?.addEventListener("change", () => { toggleBars(); calculatePreview(); });
  form.querySelector<HTMLInputElement>("[name=use_second_bar]")?.addEventListener("change", () => { toggleBars(); calculatePreview(); });
  form.querySelector<HTMLSelectElement>("[name=thickness_mm]")?.addEventListener("change", () => { updateWidthGuidance(); calculatePreview(); });
  form.querySelector<HTMLSelectElement>("[name=size_preset]")?.addEventListener("change", () => { applyPreset(); calculatePreview(); });
  form.addEventListener("input", calculatePreview);
  form.addEventListener("change", calculatePreview);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form); const button = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
    button.disabled = true; button.textContent = options.mode === "edit" ? "Saving…" : "Calculating…";
    const taxEnabled = appStore.state.currency === "INR" && form.querySelector<HTMLInputElement>("[name=tax_enabled]")?.checked === true;
    const taxMode = form.querySelector<HTMLSelectElement>("[name=tax_mode]")?.value ?? "exclusive";
    const payload = { customer_id: customerCompany._id, product_id: product._id, currency: appStore.state.currency, configuration: configurationFromForm(product, form), quantity: Number(data.get("quantity")), discount_percent: Number(data.get("discount_percent")), tax_enabled: taxEnabled, tax_mode: taxMode };
    try {
      await ensureMachineStored();
      const item = options.mode === "edit" && options.initial ? await cartApi.update(options.initial._id, payload) : await cartApi.add(payload);
      if (options.mode === "add") appStore.set({ cartCount: appStore.state.cartCount + 1 });
      toast(options.mode === "edit" ? "Cart item updated." : `${product.name} added · ${formatMoney(item.pricing_preview.line_total, item.currency)}`);
      await options.onSaved?.(item);
    } catch (error) {
      toast(error instanceof ApiError ? error.message : "Product could not be saved", "error");
      button.disabled = false; button.innerHTML = `<i data-lucide="${options.mode === "edit" ? "save" : "shopping-cart"}"></i>${options.mode === "edit" ? "Save Changes" : "Add Configured Item to Cart"}`; refreshIcons(button);
    }
  });
  toggleBars(); updateWidthGuidance(); calculatePreview(); refreshIcons(host);
}

async function loadFamilyProducts(family: string): Promise<Product[]> {
  if (family === "blankets") return (await catalogApi.blanketProducts()).items;
  return (await catalogApi.products(`category=${encodeURIComponent(family)}`)).items;
}

function bindProductCombobox(input: HTMLInputElement, products: Product[] | (() => Product[]), onSelect: (product: Product) => void): void {
  input.addEventListener("change", () => {
    const normalized = input.value.trim().toLowerCase();
    const rows = typeof products === "function" ? products() : products;
    const product = rows.find((item) => productChoice(item).toLowerCase() === normalized || item.name.toLowerCase() === normalized || item._id.toLowerCase() === normalized);
    if (product) { input.value = productChoice(product); onSelect(product); }
  });
}

export async function openCartItemEditor(item: CartItem, onSaved: () => void | Promise<void>): Promise<void> {
  const current = await catalogApi.product(item.product_id);
  const products = await loadFamilyProducts(current.category_id);
  const wrapper = document.createElement("div");
  wrapper.innerHTML = `<div class="stack-form"><label>Product<input class="product-combobox" list="edit-product-options" value="${valueAttr(productChoice(current))}" autocomplete="off" placeholder="Search or select product"><datalist id="edit-product-options">${products.map((product) => `<option value="${valueAttr(productChoice(product))}"></option>`).join("")}</datalist></label><div class="edit-configurator-host"></div></div>`;
  const dialog = openModal("Edit Product", wrapper, "wide");
  const host = wrapper.querySelector<HTMLElement>(".edit-configurator-host")!;
  const render = (product: Product) => renderConfigurator(host, product, { mode: "edit", initial: item, onSaved: async () => { dialog.close(); await onSaved(); } });
  render(current);
  bindProductCombobox(wrapper.querySelector<HTMLInputElement>(".product-combobox")!, products, render);
  refreshIcons(wrapper);
}

export async function catalogPage(selectedFamily = ""): Promise<HTMLElement> {
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  const copy = familyCopy[selectedFamily];
  const page = pageScaffold("Calculator", copy?.label ?? "Product Calculator", customerCompany ? `Quotation For: ${customerCompany.name}` : "Select a customer before configuring products.", '<a class="button button-primary" href="/cart" data-route="/cart"><i data-lucide="shopping-cart"></i>Quotation Cart</a>');
  page.classList.add("calculator-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  if (!customerCompany) { body.innerHTML = emptyState("building-2", "Customer selection required", "Choose a customer to establish pricing and currency context."); refreshIcons(page); return page; }
  body.innerHTML = skeleton(5);
  try {
    if (!selectedFamily) {
      const families = await catalogApi.families();
      let rateText = "EUR master pricing";
      try {
        const rate = await rateApi.get();
        if (appStore.state.currency !== "EUR") rateText = `1 EUR = ${Number(rate.rates[appStore.state.currency]).toFixed(4)} ${appStore.state.currency}`;
      } catch { /* Server resolves the rate during calculation. */ }
      body.innerHTML = `<section class="calculator-welcome"><div class="workspace-steps"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="active"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span><b>3</b>Configure</span><i data-lucide="chevron-right"></i><span><b>4</b>Quotation</span></div><div class="calculator-context"><div><span class="eyebrow">Quotation For</span><h2>${escapeHtml(customerCompany.name)}</h2><p>Choose one of the three active Moneda product families.</p></div><div class="context-pills"><span><i data-lucide="building-2"></i>Customer: ${escapeHtml(customerCompany.name)}</span><span><i data-lucide="euro"></i>${escapeHtml(rateText)}</span><span><i data-lucide="shield-check"></i>Server-calculated</span></div></div></section>${familyCards(families)}`;
      refreshIcons(page); return page;
    }
    let products = await loadFamilyProducts(selectedFamily);
    const blanketCategories = selectedFamily === "blankets" ? await catalogApi.blanketCategories() : [];
    const chemicalCategories = selectedFamily === "chemicals" ? Array.from(new Map(products.map((product) => [String(product.configuration.sub_category_id), String(product.configuration.sub_category)])).entries()).map(([id, name]) => ({ id, name })) : [];
    const subcategories = blanketCategories.length ? blanketCategories : chemicalCategories;
    body.innerHTML = `<div class="calculator-toolbar"><a href="/calculator" data-route="/calculator" class="back-link"><i data-lucide="arrow-left"></i>All Product Families</a><span><strong class="product-count">${products.length}</strong> products available</span></div><div class="workspace-steps compact"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="done"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span class="active"><b>3</b>Configure</span><i data-lucide="chevron-right"></i><span><b>4</b>Quotation</span></div><section class="family-selector panel"><div class="section-title"><div><span class="eyebrow">${escapeHtml(copy?.label ?? selectedFamily)}</span><h2>Choose a category and product</h2><p>${escapeHtml(copy?.detail ?? "Select a product to continue.")}</p></div><i data-lucide="list-filter"></i></div><div class="form-grid">${subcategories.length ? `<label>Category<select class="subcategory-select" required><option value="">Select category</option>${subcategories.map((category) => `<option value="${escapeHtml(category.id)}">${escapeHtml(category.name)}</option>`).join("")}</select></label>` : ""}<label>Product<input class="product-combobox" list="product-options" autocomplete="off" placeholder="Search products by name or article" ${subcategories.length ? "disabled" : ""}><datalist id="product-options"></datalist></label></div></section><div class="configurator-host"><div class="selection-summary"><i data-lucide="mouse-pointer-2"></i><span>${subcategories.length ? "Select a category, then choose a product." : "Choose a product to continue."}</span></div></div>`;
    const input = body.querySelector<HTMLInputElement>(".product-combobox")!;
    const datalist = body.querySelector<HTMLDataListElement>("#product-options")!;
    const host = body.querySelector<HTMLElement>(".configurator-host")!;
    let available = subcategories.length ? [] : products;
    const renderOptions = () => {
      datalist.innerHTML = available.map((product) => `<option value="${valueAttr(productChoice(product))}">${escapeHtml(product.description)}</option>`).join("");
      body.querySelector<HTMLElement>(".product-count")!.textContent = String(available.length);
    };
    renderOptions();
    body.querySelector<HTMLSelectElement>(".subcategory-select")?.addEventListener("change", async (event) => {
      const categoryId = (event.currentTarget as HTMLSelectElement).value;
      input.value = ""; input.disabled = !categoryId;
      host.innerHTML = '<div class="selection-summary"><i data-lucide="mouse-pointer-2"></i><span>Choose a product to begin configuration.</span></div>';
      if (selectedFamily === "blankets") available = categoryId ? (await catalogApi.blanketProducts(categoryId)).items : [];
      else available = products.filter((product) => String(product.configuration.sub_category_id) === categoryId);
      renderOptions(); refreshIcons(host);
    });
    const renderSelection = (product: Product) => {
      host.innerHTML = `<article class="selected-product-card"><div><span class="eyebrow">Art. ${escapeHtml(product.article_no ?? product.sku)}</span><h3>${escapeHtml(product.name)}</h3><p>${escapeHtml(product.description)}</p><small>${escapeHtml(String(product.configuration.thickness_mm ?? product.configuration.thickness ?? ""))}${product.configuration.thickness_mm ? " mm" : ""} · EUR master pricing</small></div><button type="button" class="button button-dark" data-configure-product><i data-lucide="settings-2"></i>Configure Product</button></article>`;
      host.querySelector<HTMLButtonElement>("[data-configure-product]")?.addEventListener("click", () => renderConfigurator(host, product, { mode: "add" }));
      refreshIcons(host);
    };
    bindProductCombobox(input, () => available, renderSelection);
  } catch (error) {
    body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Calculator unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
  }
  refreshIcons(page); return page;
}
