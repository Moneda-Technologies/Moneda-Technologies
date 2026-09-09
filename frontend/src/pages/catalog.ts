import { cartApi, catalogApi, machineApi, rateApi } from "../api";
import { ApiError } from "../api/client";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { CartItem, CatalogOption, Currency, PriceLine, Product } from "../types/domain";
import { emptyState, escapeHtml, formatMoney, skeleton } from "../utils/dom";

const familyCopy: Record<string, { icon: string; label: string; detail: string }> = {
  blankets: { icon: "rectangle-horizontal", label: "Printing Blankets", detail: "Select a blanket group, product, thickness, dimensions and format." },
  mpacks: { icon: "layers-3", label: "Underpacking", detail: "Configure thickness, press size and underpacking type." },
  chemicals: { icon: "flask-conical", label: "Chemicals & Maintenance", detail: "Select a chemical and calculate complete container quantities." },
};

type ConfigValue = string | number | boolean;
type Config = Record<string, unknown>;
interface MpackPrice {
  thickness_mm: number;
  thickness_micron: number;
  sheets_per_box: number;
  price_per_sheet_eur: number;
  price_per_box_eur: number;
}
interface MpackMachineSize {
  manufacturer: string;
  machine_model: string;
  width_mm: number;
  length_mm: number;
  prices: MpackPrice[];
}

interface BlanketMachineOption {
  _id?: string;
  id?: string;
  manufacturer?: string;
  manufacturer_id?: string;
  machine_model?: string;
  model?: string;
  model_id?: string;
  name?: string;
  models?: Array<{ id?: string; name?: string; model?: string }>;
}

function blanketMachineOptions(raw: unknown): BlanketMachineOption[] {
  if (Array.isArray(raw)) return raw as BlanketMachineOption[];
  if (!raw || typeof raw !== "object") return [];
  const manufacturers = (raw as { manufacturers?: Array<BlanketMachineOption & { models?: Array<{ id?: string; name?: string; model?: string }> }> }).manufacturers ?? [];
  return manufacturers.flatMap((manufacturer) => (manufacturer.models?.length
    ? manufacturer.models.map((model) => ({
      manufacturer: manufacturer.name ?? manufacturer.manufacturer,
      manufacturer_id: manufacturer.id ?? manufacturer.manufacturer_id,
      machine_model: model.name ?? model.model,
      model_id: model.id,
      id: model.id,
    }))
    : [manufacturer]));
}

const configuratorSubscriptions = new WeakMap<HTMLElement, () => void>();

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

function machineField(initial: Config): string {
  const initialMachine = String(initial.machine ?? initial.machine_name ?? "");
  return `<label data-machine-name-field>Machine Name <small data-machine-required-copy>(Optional for Cut Format)</small><input name="machine" data-blanket-machine value="${valueAttr(initialMachine)}" placeholder="Search or select machine name" autocomplete="off"><input type="hidden" name="machine_id" value="${valueAttr(initial.machine_id)}"><div class="machine-search-results" data-blanket-machines role="listbox" hidden></div><small>Required when Format is Bar Format.</small></label>`;
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
  return `<div class="form-grid">${machineField(initial)}<label>Thickness<select name="thickness_mm" required>${thicknesses.map((value) => `<option value="${value}" ${selected(initial.thickness_mm ?? thicknesses[0], value)}>${value.toFixed(2)} mm</option>`).join("")}</select></label><label>Dimension Unit<select name="dimension_unit"><option value="mm" ${selected(initial.dimension_unit ?? "mm", "mm")}>Millimetres</option><option value="inch" ${selected(initial.dimension_unit, "inch")}>Inches</option><option value="m" ${selected(initial.dimension_unit, "m")}>Metres</option></select></label><label>Length<input name="length" type="number" min="0.001" step="any" value="${valueAttr(initial.length)}" placeholder="Enter length" required></label><label>Width<input name="width" type="number" min="0.001" step="any" value="${valueAttr(initial.width)}" placeholder="Enter or select width" list="standard-widths" required><datalist id="standard-widths">${widths.map((width) => `<option value="${width}"></option>`).join("")}</datalist><small class="width-guidance">${widths.length ? `Standard: ${widths.join(", ")} mm` : "Custom width"}</small></label><label>Format<select name="format_type" required>${formats.map((format) => `<option value="${format}" ${selected(initialFormat, format)}>${format === "bar_format" ? "Bar Format" : "Cut Format"}</option>`).join("")}</select></label></div><div class="form-grid bar-fields" ${initialFormat === "bar_format" ? "" : "hidden"}><label>Bar 1<select name="bar_1_id" ${initialFormat === "bar_format" ? "required" : ""}>${barOptions(initial.bar_1_id, defaults[0])}</select></label><label class="check-row different-second-bar"><input name="use_second_bar" type="checkbox" ${initialUseSecondBar ? "checked" : ""}><span>Different second bar</span></label><label class="second-bar-field" ${initialUseSecondBar ? "" : "hidden"}>Bar 2<select name="bar_2_id" ${initialUseSecondBar ? "required" : ""}>${barOptions(initial.bar_2_id, defaults[1])}</select></label></div>`;
}

function mpackFields(product: Product, initial: Config): string {
  const rows = (product.configuration.machine_sizes as MpackMachineSize[] | undefined) ?? [];
  if (!rows.length) {
    return `<div class="notice warning"><i data-lucide="clock-3"></i><div><strong>Structured machine pricing is not available</strong><p>This Underpacking product cannot be configured until an approved machine price list is loaded.</p></div></div>`;
  }
  const manufacturers = [...new Set(rows.map((row) => row.manufacturer))];
  const initialManufacturer = String(initial.manufacturer ?? "");
  const initialModel = String(initial.machine_model ?? "");
  const initialRows = rows.filter((row) => row.manufacturer === initialManufacturer && row.machine_model === initialModel);
  const sizeValue = initial.width_mm && initial.length_mm ? `${initial.width_mm}x${initial.length_mm}` : "";
  const initialSize = initialRows.find((row) => `${row.width_mm}x${row.length_mm}` === sizeValue);
  return `<div class="form-grid mpack-dependent-fields"><label>Machine Manufacturer<select name="manufacturer" required><option value="">Select manufacturer</option>${manufacturers.map((manufacturer) => `<option value="${escapeHtml(manufacturer)}" ${selected(initialManufacturer, manufacturer)}>${escapeHtml(manufacturer)}</option>`).join("")}</select></label><label>Machine Model<select name="machine_model" required ${initialManufacturer ? "" : "disabled"}><option value="">Select model</option>${[...new Set(rows.filter((row) => row.manufacturer === initialManufacturer).map((row) => row.machine_model))].map((model) => `<option value="${escapeHtml(model)}" ${selected(initialModel, model)}>${escapeHtml(model)}</option>`).join("")}</select></label><label>Size<select name="machine_size" required ${initialModel ? "" : "disabled"}><option value="">Select size</option>${initialRows.map((row) => `<option value="${row.width_mm}x${row.length_mm}" data-width-mm="${row.width_mm}" data-length-mm="${row.length_mm}" ${selected(sizeValue, `${row.width_mm}x${row.length_mm}`)}>${row.width_mm} mm Across (W) × ${row.length_mm} mm Around (L)</option>`).join("")}</select></label><label>Thickness<select name="thickness_mm" required ${initialSize ? "" : "disabled"}><option value="">Select thickness</option>${(initialSize?.prices ?? []).map((price) => `<option value="${price.thickness_mm}" ${selected(initial.thickness_mm, price.thickness_mm)}>${price.thickness_mm.toFixed(3)} mm (${price.thickness_micron} µ)</option>`).join("")}</select></label></div>`;
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
  const stringFields = new Set(["machine", "machine_id", "manufacturer", "machine_model", "dimension_unit", "format_type", "bar_1_id", "bar_2_id", "format_id"]);
  ["machine", "machine_id", "manufacturer", "machine_model", "thickness_mm", "length", "width", "dimension_unit", "thickness_micron", "format_type", "bar_1_id", "bar_2_id", "requested_litres", "format_id"].forEach((key) => {
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
  // A machine is a canonical selection, never free text.  Optional cut-format
  // text is ignored unless it resolved to a machine ID; bar format validity is
  // enforced by the configurator's custom validity check below.
  if (product.category_id === "blankets" && !result.machine_id) delete result.machine;
  const packageSelect = form.querySelector<HTMLSelectElement>("[name=format_id]");
  if (packageSelect?.value) result.size_litre = Number(packageSelect.selectedOptions[0]?.dataset.size ?? 0);
  const machineSize = form.querySelector<HTMLSelectElement>("[name=machine_size]")?.selectedOptions[0];
  if (machineSize?.value) {
    result.width_mm = Number(machineSize.dataset.widthMm);
    result.length_mm = Number(machineSize.dataset.lengthMm);
    result.dimension_unit = "mm";
  }
  if (product.configuration.underpacking_type) result.underpacking_type = String(product.configuration.underpacking_type);
  return result;
}

function roundDisplay(value: number): number { return Number(value.toFixed(2)); }

function lineForDisplay(line: PriceLine, currency: Currency, rates: Record<string, number> | null): PriceLine {
  const masterUnit = Number(line.master_unit_price ?? line.master_price_eur ?? 0);
  const masterSubtotal = Number(line.master_subtotal ?? 0);
  const masterDiscount = Number(line.master_discount_amount ?? 0);
  const masterTotal = Number(line.master_final_total ?? line.master_total ?? 0);
  const rate = currency === "EUR" ? 1 : Number(rates?.[currency]);
  const convert = (value: number) => roundDisplay(value * rate);
  if (!(rate > 0)) {
    return {
      ...line,
      currency,
      display_currency: currency,
      exchange_rate: 0,
      converted_price: undefined,
      display_unit_price: undefined,
      display_subtotal: undefined,
      display_discount_amount: undefined,
      display_total: undefined,
      display_final_total: undefined,
      unit_price: masterUnit,
      subtotal: masterSubtotal,
      discount_amount: masterDiscount,
      total: masterTotal,
      line_total: masterTotal,
    };
  }
  const displayUnit = convert(masterUnit);
  const displaySubtotal = convert(masterSubtotal);
  const displayDiscount = convert(masterDiscount);
  const displayTotal = convert(masterTotal);
  return {
    ...line,
    currency,
    display_currency: currency,
    exchange_rate: rate,
    converted_price: displayUnit,
    display_unit_price: displayUnit,
    display_subtotal: displaySubtotal,
    display_discount_amount: displayDiscount,
    display_total: displayTotal,
    display_final_total: displayTotal,
    unit_price: displayUnit,
    subtotal: displaySubtotal,
    discount_amount: displayDiscount,
    total: displayTotal,
    line_total: displayTotal,
  };
}

function priceListDate(value?: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value ?? "");
  if (!match) return "—";
  const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${match[3]} ${months[Number(match[2]) - 1]} ${match[1]}`;
}

function mpackSummaryPlaceholder(product: Product, form?: HTMLFormElement): string {
  const data = form ? configurationFromForm(product, form) : {};
  const priceList = (product.configuration.machine_price_list as { valid_from?: string; valid_until?: string } | undefined) ?? {};
  const rows = (product.configuration.machine_sizes as MpackMachineSize[] | undefined) ?? [];
  const selectedRow = rows.find((row) => row.manufacturer === data.manufacturer && row.machine_model === data.machine_model
    && row.width_mm === Number(data.width_mm) && row.length_mm === Number(data.length_mm));
  const selectedPrice = selectedRow?.prices.find((price) => price.thickness_mm === Number(data.thickness_mm));
  const quantity = Number(form?.querySelector<HTMLInputElement>("[name=quantity]")?.value ?? 1);
  const value = (field: string) => escapeHtml(String(data[field] || "—"));
  const size = data.width_mm && data.length_mm ? `${data.width_mm} × ${data.length_mm} mm` : "—";
  const thickness = data.thickness_mm ? `${Number(data.thickness_mm).toFixed(3)} mm (${Math.round(Number(data.thickness_mm) * 1000)} µ)` : "—";
  const total = selectedPrice && quantity > 0 ? formatMoney(selectedPrice.price_per_box_eur * quantity, "EUR") : "—";
  return `<div class="live-price-head"><span><i data-lucide="list-checks"></i>Underpacking</span><span class="rate-live">EUR commercial</span></div><p class="configuration-summary-title">Selected configuration</p><div class="live-price-rows configuration-summary-rows"><div><span>Manufacturer</span><strong>${value("manufacturer")}</strong></div><div><span>Machine</span><strong>${value("machine_model")}</strong></div><div><span>Size</span><strong>${size}</strong></div><div><span>Thickness</span><strong>${thickness}</strong></div><div><span>Price / sheet</span><strong>${selectedPrice ? formatMoney(selectedPrice.price_per_sheet_eur, "EUR") : "—"}</strong></div><div><span>Sheets / box</span><strong>${selectedPrice?.sheets_per_box ?? "—"}</strong></div><div><span>Price / box</span><strong>${selectedPrice ? formatMoney(selectedPrice.price_per_box_eur, "EUR") : "—"}</strong></div></div><div class="live-price-total"><span>Total price</span><strong>${total}</strong></div><p class="rate-caption">Price list: ${priceListDate(priceList.valid_from)} – ${priceListDate(priceList.valid_until)}</p>`;
}

function pricingMarkup(line: PriceLine, _rate: { provider: string; provider_source?: string; source?: string; fetched_at: string; stale: boolean; warning?: string }): string {
  const displayCurrency = line.display_currency ?? line.currency;
  const masterSubtotal = line.master_subtotal ?? 0;
  const masterDiscount = line.master_discount_amount ?? 0;
  const masterTotalEur = line.master_final_total ?? line.master_total ?? 0;
  const displayTotal = line.display_final_total ?? line.display_total;
  const adjustmentRows = line.adjustments.map((item) => `<div><span>${escapeHtml(item.label)}${item.quantity ? ` × ${item.quantity}` : ""}</span><strong>${formatMoney(item.amount_master, "EUR")}</strong></div>`).join("");
  const reference = displayCurrency !== "EUR"
    ? `<div class="live-price-reference"><span>${escapeHtml(displayCurrency)} reference</span><strong>${displayTotal === undefined ? "Unavailable" : formatMoney(displayTotal, displayCurrency)}</strong></div>`
    : "";
  const configuration = line.configuration ?? {};
  const mpackRows = line.price_per_box_eur !== undefined
    ? `<div><span>Manufacturer</span><strong>${escapeHtml(String(configuration.manufacturer ?? "—"))}</strong></div><div><span>Machine model</span><strong>${escapeHtml(String(configuration.machine_model ?? "—"))}</strong></div><div><span>Size</span><strong>${Number(configuration.width_mm)} × ${Number(configuration.length_mm)} mm</strong></div><div><span>Thickness</span><strong>${Number(configuration.thickness_mm).toFixed(3)} mm (${Number(configuration.thickness_micron)} µ)</strong></div><div><span>Price / sheet</span><strong>${formatMoney(line.price_per_sheet_eur ?? 0, "EUR")}</strong></div><div><span>Sheets / box</span><strong>${line.sheets_per_box}</strong></div><div><span>Price / box</span><strong>${formatMoney(line.price_per_box_eur, "EUR")}</strong></div>`
    : "";
  const validity = line.price_list?.valid_from && line.price_list?.valid_until
    ? `<p class="rate-caption">Price list: ${priceListDate(line.price_list.valid_from)} – ${priceListDate(line.price_list.valid_until)}</p>`
    : "";
  return `<div class="live-price-head"><span><i data-lucide="shield-check"></i>${mpackRows ? "Configuration & Pricing" : "Pricing Summary"}</span><span class="rate-live">EUR commercial</span></div><div class="live-price-rows ${mpackRows ? "configuration-summary-rows" : ""}">${mpackRows}${line.area_sqm !== undefined ? `<div><span>Area</span><strong>${line.area_sqm.toFixed(4)} m²</strong></div>` : ""}${adjustmentRows}<div><span>Subtotal</span><strong>${formatMoney(masterSubtotal, "EUR")}</strong></div><div><span>Discount ${line.discount_percent}%</span><strong>${masterDiscount ? `- ${formatMoney(masterDiscount, "EUR")}` : "—"}</strong></div></div><div class="live-price-total"><span>Total</span><strong>${formatMoney(masterTotalEur, "EUR")}</strong></div>${reference}${validity}`;
}

function discountOptions(product: Product, initialDiscount = 0): string {
  const rules = product.discount_rules;
  const maximum = appStore.can("pricing.discount.override") ? rules.privileged_max_percent : rules.default_max_percent;
  const step = Number(rules.step) > 0 ? Number(rules.step) : 0.5;
  const allowed = rules.enabled ? Array.from({ length: Math.floor(maximum / step) + 1 }, (_, index) => Number((index * step).toFixed(4))) : [0];
  if (!allowed.includes(initialDiscount)) allowed.push(initialDiscount);
  return allowed.sort((a, b) => a - b).map((discount) => `<option value="${discount}" ${selected(initialDiscount, discount)}>${discount.toFixed(1)}%</option>`).join("");
}

interface ConfiguratorOptions {
  mode: "add" | "edit";
  initial?: CartItem;
  onSaved?: (item: CartItem) => void | Promise<void>;
}

function bindMpackDependencies(form: HTMLFormElement, product: Product): void {
  const rows = (product.configuration.machine_sizes as MpackMachineSize[] | undefined) ?? [];
  if (!rows.length) return;
  const manufacturer = form.querySelector<HTMLSelectElement>("[name=manufacturer]");
  const model = form.querySelector<HTMLSelectElement>("[name=machine_model]");
  const size = form.querySelector<HTMLSelectElement>("[name=machine_size]");
  const thickness = form.querySelector<HTMLSelectElement>("[name=thickness_mm]");
  if (!manufacturer || !model || !size || !thickness) return;

  const option = (value: string, label: string) => {
    const row = document.createElement("option");
    row.value = value;
    row.textContent = label;
    return row;
  };
  const populateThicknesses = (preserve = "") => {
    const selectedRow = rows.find((row) => row.manufacturer === manufacturer.value
      && row.machine_model === model.value && `${row.width_mm}x${row.length_mm}` === size.value);
    thickness.replaceChildren(option("", "Select thickness"));
    selectedRow?.prices.forEach((price) => thickness.append(option(
      String(price.thickness_mm), `${price.thickness_mm.toFixed(3)} mm (${price.thickness_micron} µ)`,
    )));
    thickness.disabled = !selectedRow;
    if (selectedRow?.prices.some((price) => String(price.thickness_mm) === preserve)) thickness.value = preserve;
  };
  const populateSizes = (preserve = "", preserveThickness = "") => {
    const matching = rows.filter((row) => row.manufacturer === manufacturer.value && row.machine_model === model.value);
    size.replaceChildren(option("", "Select size"));
    matching.forEach((row) => {
      const sizeOption = option(`${row.width_mm}x${row.length_mm}`, `${row.width_mm} mm Across (W) × ${row.length_mm} mm Around (L)`);
      sizeOption.dataset.widthMm = String(row.width_mm);
      sizeOption.dataset.lengthMm = String(row.length_mm);
      size.append(sizeOption);
    });
    size.disabled = !matching.length;
    if (matching.some((row) => `${row.width_mm}x${row.length_mm}` === preserve)) size.value = preserve;
    populateThicknesses(preserveThickness);
  };
  const populateModels = (preserve = "", preserveSize = "", preserveThickness = "") => {
    const models = [...new Set(rows.filter((row) => row.manufacturer === manufacturer.value).map((row) => row.machine_model))];
    model.replaceChildren(option("", "Select model"));
    models.forEach((name) => model.append(option(name, name)));
    model.disabled = !models.length;
    if (models.includes(preserve)) model.value = preserve;
    populateSizes(preserveSize, preserveThickness);
  };

  const initialModel = model.value;
  const initialSize = size.value;
  const initialThickness = thickness.value;
  populateModels(initialModel, initialSize, initialThickness);
  manufacturer.addEventListener("change", () => populateModels());
  model.addEventListener("change", () => populateSizes());
  size.addEventListener("change", () => populateThicknesses());
}

function renderConfigurator(host: HTMLElement, product: Product, options: ConfiguratorOptions): void {
  configuratorSubscriptions.get(host)?.();
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  if (!customerCompany) return;
  const initial = options.initial?.configuration ?? {};
  const initialDiscount = Number(options.initial?.discount_percent ?? 0);
  const structuredMpack = ((product.configuration.machine_sizes as MpackMachineSize[] | undefined) ?? []).length > 0;
  // A valid structured MPack matrix is itself the authoritative price source;
  // do not block the configurator because a stale generic status says pending.
  const configured = product.pricing_status === "configured" || structuredMpack;
  const commercialUnit = String(product.commercial_unit ?? (structuredMpack ? "box" : product.category_id === "blankets" ? "pc" : ""));
  const quantityLabel = commercialUnit ? `Quantity (${commercialUnit === "box" ? "Box" : commercialUnit === "pc" ? "Pc" : commercialUnit})` : "Quantity";
  host.innerHTML = `<section class="inline-configurator ${structuredMpack ? "structured-machine-configurator" : ""}"><div class="selected-product"><span class="product-dialog-icon"><i data-lucide="${familyCopy[product.category_id]?.icon ?? "package"}"></i></span><div><span class="eyebrow">Art. ${escapeHtml(product.article_no ?? product.sku)}</span><h3>${escapeHtml(product.name)}</h3><p>${escapeHtml(product.description)}</p></div></div>${configured ? "" : `<div class="notice warning"><i data-lucide="clock-3"></i><div><strong>EUR price ${product.pricing_status === "on_request" ? "is on request" : "is pending"}</strong><p>An administrator must configure the master price before this product can be calculated.</p></div></div>`}<form class="stack-form configure-form"><div class="form-section"><div class="section-number">01</div><div><h4>Product Configuration</h4><p>Required fields are checked and priced by the Moneda API.</p></div></div>${configurationFields(product, initial)}<div class="form-section"><div class="section-number">02</div><div><h4>Commercial Details</h4><p>Discount permissions and tax rules are validated on the server.</p></div></div><div class="form-grid"><label>${quantityLabel}<input name="quantity" type="number" min="1" max="100000" value="${valueAttr(options.initial?.quantity ?? 1)}" required></label><label>Discount<select name="discount_percent">${discountOptions(product, initialDiscount)}</select></label></div><button class="button button-primary button-full" type="submit" ${configured ? "" : "disabled"}><i data-lucide="${options.mode === "edit" ? "save" : "shopping-cart"}"></i>${options.mode === "edit" ? "Save Changes" : "Add Configured Item to Cart"}</button></form><aside class="live-price inline-price-summary panel">${structuredMpack ? mpackSummaryPlaceholder(product) : `<div class="live-price-placeholder"><i data-lucide="calculator"></i><h4>Pricing Summary</h4><p>Complete the required configuration to see the server-calculated amount.</p></div>`}</aside></section>`;
  const commercialCopy = [...host.querySelectorAll(".form-section h4")].find((heading) => heading.textContent === "Commercial Details")?.nextElementSibling;
  if (commercialCopy) commercialCopy.textContent = "Discount permissions are validated on the server. Quotations are issued in EUR.";
  const form = host.querySelector<HTMLFormElement>(".configure-form")!;
  const blanketMachine = product.category_id === "blankets" ? form.querySelector<HTMLInputElement>("[data-blanket-machine]") : null;
  const machineIdInput = form.querySelector<HTMLInputElement>("[name=machine_id]");
  const machineList = form.querySelector<HTMLElement>("[data-blanket-machines]");
  const configuredMachines = product.category_id === "blankets" ? blanketMachineOptions(product.configuration.machine_options) : [];
  let savedMachines: BlanketMachineOption[] = [];
  const machineName = (machine: BlanketMachineOption) => String(machine.name || (
    `${machine.manufacturer ?? ""} - ${machine.machine_model ?? machine.model ?? ""}`
  )).replace(/^\s*[-]\s*|\s*[-]\s*$/g, "").trim();
  const refreshBlanketMachineOptions = () => {
    if (!machineList) return;
    const names = [...new Set([...configuredMachines, ...savedMachines].map(machineName).filter(Boolean))].sort((a, b) => a.localeCompare(b));
    machineList.innerHTML = names.map((name) => {
      const machine = [...configuredMachines, ...savedMachines].find((item) => machineName(item) === name);
      const id = machine?.id ?? machine?._id ?? "";
      return `<button type="button" class="machine-search-option" role="option" data-machine-id="${escapeHtml(String(id))}" data-machine-name="${escapeHtml(name)}"><strong>${escapeHtml(name)}</strong></button>`;
    }).join("");
    machineList.hidden = !names.length;
  };
  const syncMachineIdentity = () => {
    if (!blanketMachine || !machineIdInput) return;
    const selected = [...configuredMachines, ...savedMachines].find((machine) => machineName(machine).toLowerCase() === blanketMachine.value.trim().toLowerCase());
    machineIdInput.value = String(selected?.id ?? selected?._id ?? "");
    const isBar = form.querySelector<HTMLSelectElement>("[name=format_type]")?.value === "bar_format";
    blanketMachine.setCustomValidity(isBar && blanketMachine.value.trim() && !machineIdInput.value ? "Select a machine from the list." : "");
  };
  const loadMachines = async () => {
    if (product.category_id !== "blankets") return;
    try {
      const result = await machineApi.list();
      savedMachines = result.items;
      refreshBlanketMachineOptions();
      syncMachineIdentity();
    } catch { /* Product-provided machine options remain available offline. */ }
  };
  refreshBlanketMachineOptions();
  void loadMachines();
  syncMachineIdentity();
  blanketMachine?.addEventListener("input", () => {
    syncMachineIdentity();
    if (machineList) machineList.hidden = false;
    calculatePreview();
  });
  blanketMachine?.addEventListener("focus", () => { if (machineList) machineList.hidden = false; });
  machineList?.addEventListener("mousedown", (event) => {
    const option = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-machine-id]");
    if (!option || !blanketMachine || !machineIdInput) return;
    event.preventDefault();
    blanketMachine.value = option.dataset.machineName ?? "";
    machineIdInput.value = option.dataset.machineId ?? "";
    machineList.hidden = true;
    syncMachineIdentity();
    calculatePreview();
  });
  const commercialNote = form.querySelectorAll<HTMLElement>(".form-section")[1]?.querySelector("p");
  if (commercialNote) commercialNote.textContent = "Discount is applied to the EUR master price; USD/INR are display references only.";
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
    const machineInput = form.querySelector<HTMLInputElement>("[data-blanket-machine]");
    const machineHint = form.querySelector<HTMLElement>("[data-machine-required-copy]");
    if (machineInput && product.category_id === "blankets") {
      machineInput.required = barFormat;
      machineInput.setCustomValidity(barFormat && machineInput.value.trim() && !machineIdInput?.value ? "Select a machine from the list." : "");
      if (machineHint) machineHint.textContent = barFormat ? "Required for Bar Format" : "Optional for Cut Format";
    }
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
  let previewSequence = 0;
  let latestPreviewLine: PriceLine | null = null;
  const calculatePreview = () => {
    const sequence = ++previewSequence;
    window.clearTimeout(timer);
    if (structuredMpack) {
      preview.innerHTML = mpackSummaryPlaceholder(product, form);
      refreshIcons(preview);
    }
    timer = window.setTimeout(async () => {
      if (sequence !== previewSequence) return;
      if (!configured || !form.checkValidity()) {
        preview.innerHTML = structuredMpack
          ? mpackSummaryPlaceholder(product, form)
          : '<div class="live-price-placeholder"><i data-lucide="calculator"></i><h4>Pricing Summary</h4><p>Complete the required configuration to calculate.</p></div>';
        refreshIcons(preview); return;
      }
      try {
        preview.classList.add("loading");
        const data = new FormData(form);
        const selectedDiscount = Number(data.get("discount_percent"));
        console.debug("discount_debug", { item_id: options.initial?._id ?? "new", ui_selected_discount: selectedDiscount, price_preview_requested_discount: selectedDiscount });
        const result = await catalogApi.preview(product._id, {
          item_id: options.initial?._id, customer_id: customerCompany._id, display_currency: appStore.state.currency,
          configuration: configurationFromForm(product, form),
          quantity: Number(data.get("quantity")), discount_percent: selectedDiscount,
        });
        if (sequence !== previewSequence) return;
        latestPreviewLine = result.line;
        console.debug("discount_debug", { item_id: options.initial?._id ?? "new", response_discount: result.line.discount_percent });
        console.debug("add_to_cart_ui", { step: "PRICE_PREVIEW_COMPLETE", item_id: options.initial?._id ?? "new" });
        preview.innerHTML = pricingMarkup(result.line, result.rate); refreshIcons(preview);
      } catch (error) {
        if (sequence !== previewSequence) return;
        preview.innerHTML = `<div class="notice warning"><i data-lucide="circle-alert"></i><div><strong>Preview unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Check the configuration")}</p></div></div>`;
        refreshIcons(preview);
      } finally { if (sequence === previewSequence) preview.classList.remove("loading"); }
    }, 220);
  };
  let lastDisplayCurrency = appStore.state.currency;
  let displayCurrencySequence = 0;
  const renderLocalDisplay = (currency: Currency, rates: Record<string, number> | null) => {
    if (!latestPreviewLine) return;
    preview.innerHTML = pricingMarkup(lineForDisplay(latestPreviewLine, currency, rates), {
      provider: "cached", fetched_at: new Date().toISOString(), stale: true,
    });
    refreshIcons(preview);
  };
  const unsubscribeCurrency = appStore.subscribe((nextState) => {
    if (!host.isConnected) { unsubscribeCurrency(); return; }
    if (nextState.currency === lastDisplayCurrency) return;
    lastDisplayCurrency = nextState.currency;
    // Display currency is derived locally from the latest EUR master line.
    // Never re-run the product preview merely because the selector changed.
    const sequence = ++displayCurrencySequence;
    renderLocalDisplay(nextState.currency, nextState.fxRates);
    if (nextState.currency !== "EUR" && !(Number(nextState.fxRates?.[nextState.currency]) > 0)) {
      void rateApi.get().then((snapshot) => {
        if (sequence !== displayCurrencySequence || !host.isConnected || appStore.state.currency !== nextState.currency) return;
        appStore.set({ fxRates: snapshot.rates });
        renderLocalDisplay(nextState.currency, snapshot.rates);
      }).catch(() => {
        if (sequence === displayCurrencySequence && host.isConnected && appStore.state.currency === nextState.currency) renderLocalDisplay(nextState.currency, null);
      });
    }
  });
  configuratorSubscriptions.set(host, unsubscribeCurrency);
  bindMpackDependencies(form, product);
  form.querySelector<HTMLSelectElement>("[name=format_type]")?.addEventListener("change", () => { toggleBars(); calculatePreview(); });
  form.querySelector<HTMLInputElement>("[name=use_second_bar]")?.addEventListener("change", () => { toggleBars(); calculatePreview(); });
  form.querySelector<HTMLSelectElement>("[name=thickness_mm]")?.addEventListener("change", () => { updateWidthGuidance(); calculatePreview(); });
  form.querySelector<HTMLSelectElement>("[name=size_preset]")?.addEventListener("change", () => { applyPreset(); calculatePreview(); });
  form.addEventListener("input", calculatePreview);
  form.addEventListener("change", calculatePreview);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form); const button = form.querySelector<HTMLButtonElement>("button[type=submit]")!;
    button.disabled = true; button.textContent = options.mode === "edit" ? "Saving…" : "Adding to Cart…";
    console.debug("add_to_cart_ui", { step: "START", item_id: options.initial?._id ?? "new" });
    const payload = { customer_id: customerCompany._id, product_id: product._id, display_currency: appStore.state.currency, configuration: configurationFromForm(product, form), quantity: Number(data.get("quantity")), discount_percent: Number(data.get("discount_percent")) };
    try {
      const item = options.mode === "edit" && options.initial ? await cartApi.update(options.initial._id, payload) : await cartApi.add(payload);
      console.debug("add_to_cart_ui", { step: "CART_POST_COMPLETE", status: 201, item_id: item._id });
      if (options.mode === "add") appStore.set({ cartCount: appStore.state.cartCount + 1 });
      toast(options.mode === "edit" ? "Cart item updated." : `${product.name} added · ${formatMoney(item.pricing_preview.master_final_total ?? 0, "EUR")}`);
      try {
        console.debug("add_to_cart_ui", { step: "CART_REFRESH_START", item_id: item._id });
        await options.onSaved?.(item);
        console.debug("add_to_cart_ui", { step: "CART_REFRESH_COMPLETE", item_id: item._id });
      } catch (refreshError) {
        // The cart write already succeeded; a secondary refresh failure must
        // not turn a successful add into a stuck or misleading error state.
        toast(refreshError instanceof ApiError ? `Item saved, but cart refresh failed: ${refreshError.message}` : "Item saved, but the cart could not be refreshed.", "error");
      }
    } catch (error) {
      toast(error instanceof ApiError ? error.message : "Product could not be saved", "error");
    } finally {
      button.disabled = false;
      button.innerHTML = `<i data-lucide="${options.mode === "edit" ? "save" : "shopping-cart"}"></i>${options.mode === "edit" ? "Save Changes" : "Add Configured Item to Cart"}`;
      console.debug("add_to_cart_ui", { step: "LOADING_RESET", item_id: options.initial?._id ?? "new" });
      refreshIcons(button);
    }
  });
  toggleBars(); updateWidthGuidance(); calculatePreview(); refreshIcons(host);
}

async function loadFamilyProducts(family: string): Promise<Product[]> {
  if (family === "blankets") return (await catalogApi.blanketProducts()).items;
  return (await catalogApi.products(`category=${encodeURIComponent(family)}`)).items;
}

function bindProductCombobox(input: HTMLInputElement, products: Product[] | (() => Product[]), onSelect: (product: Product) => void, onClear?: () => void, initialProduct?: Product): void {
  input.removeAttribute("list");
  const field = input.parentElement;
  if (!field) return;
  field.classList.add("product-combobox-field");
  const results = document.createElement("div");
  results.className = "product-search-results";
  results.setAttribute("role", "listbox");
  results.hidden = true;
  field.append(results);
  let selectedId = initialProduct?._id ?? null;
  const rows = () => typeof products === "function" ? products() : products;
  const render = () => {
    const term = input.value.trim().toLowerCase();
    const visible = rows().filter((item) => !term || [item.name, item.article_no, item.sku, item.description].some((value) => String(value ?? "").toLowerCase().includes(term))).slice(0, 50);
    results.innerHTML = visible.map((item) => `<button type="button" class="product-search-option" role="option" data-product-id="${escapeHtml(item._id)}"><strong>${escapeHtml(item.name)}</strong><small>Art. ${escapeHtml(item.article_no ?? item.sku)}</small><span>${escapeHtml(item.description ?? "")}</span></button>`).join("");
    results.hidden = !visible.length;
  };
  input.addEventListener("input", () => {
    if (selectedId && input.value !== productChoice(rows().find((item) => item._id === selectedId) ?? initialProduct ?? ({} as Product))) {
      selectedId = null;
      onClear?.();
    }
    render();
  });
  input.addEventListener("focus", render);
  results.addEventListener("mousedown", (event) => {
    const option = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-product-id]");
    if (!option) return;
    event.preventDefault();
    const product = rows().find((item) => item._id === option.dataset.productId);
    if (!product) return;
    selectedId = product._id;
    input.value = productChoice(product);
    results.hidden = true;
    onSelect(product);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { results.hidden = true; return; }
    if (event.key === "Enter" && !selectedId) { event.preventDefault(); }
  });
  document.addEventListener("mousedown", (event) => { if (!field.contains(event.target as Node)) results.hidden = true; });
}

export async function openCartItemEditor(item: CartItem, onSaved: () => void | Promise<void>): Promise<void> {
  const current = await catalogApi.product(item.product_id);
  const products = await loadFamilyProducts(current.category_id);
  const mpackOnly = current.category_id === "mpacks";
  const wrapper = document.createElement("div");
  wrapper.innerHTML = `<div class="stack-form">${mpackOnly ? `<div class="selected-product-fixed"><strong>Mtech Mpack</strong><span>Art. MTECH-MPACK</span></div>` : `<label>Product<input class="product-combobox" value="${valueAttr(productChoice(current))}" autocomplete="off" placeholder="Search or select product"></label>`}<div class="edit-configurator-host"></div></div>`;
  const dialog = openModal("Edit Product", wrapper, "wide");
  const host = wrapper.querySelector<HTMLElement>(".edit-configurator-host")!;
  const render = (product: Product) => renderConfigurator(host, product, { mode: "edit", initial: item, onSaved: async () => { dialog.close(); await onSaved(); } });
  render(current);
  if (!mpackOnly) bindProductCombobox(wrapper.querySelector<HTMLInputElement>(".product-combobox")!, products, render, () => {
    host.innerHTML = '<div class="selection-summary"><i data-lucide="mouse-pointer-2"></i><span>Select a product to continue.</span></div>';
    refreshIcons(host);
  }, current);
  refreshIcons(wrapper);
}

export async function catalogPage(selectedFamily = ""): Promise<HTMLElement> {
  const customerCompany = appStore.state.customer ?? appStore.state.customerCompany ?? appStore.state.company;
  const copy = familyCopy[selectedFamily];
  const page = pageScaffold("Calculator", copy?.label ?? "Calculator", customerCompany ? `Quotation For: ${customerCompany.name}` : "Select a customer before configuring products.", '<a class="button button-primary" href="/cart" data-route="/cart"><i data-lucide="shopping-cart"></i>Quotation Cart</a>');
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
    const subcategories = selectedFamily === "blankets"
      ? [{ id: "all", name: "All" }, ...blanketCategories]
      : chemicalCategories;
    const mpackOnly = selectedFamily === "mpacks";
    body.innerHTML = `<div class="calculator-toolbar"><a href="/calculator" data-route="/calculator" class="back-link"><i data-lucide="arrow-left"></i>All Product Families</a><span><strong class="product-count">${products.length}</strong> products available</span></div><div class="workspace-steps compact"><span class="done"><b>1</b>Customer</span><i data-lucide="chevron-right"></i><span class="done"><b>2</b>Products</span><i data-lucide="chevron-right"></i><span class="active"><b>3</b>Configure</span><i data-lucide="chevron-right"></i><span><b>4</b>Quotation</span></div><section class="family-selector panel"><div class="section-title"><div><span class="eyebrow">${escapeHtml(copy?.label ?? selectedFamily)}</span><h2>${mpackOnly ? "Mtech Mpack" : "Choose a category and product"}</h2><p>${mpackOnly ? "Art. MTECH-MPACK · Configure the machine, size and thickness." : escapeHtml(copy?.detail ?? "Select a product to continue.")}</p></div><i data-lucide="${mpackOnly ? "layers-3" : "list-filter"}"></i></div>${mpackOnly ? `<div class="selected-product-fixed"><strong>Mtech Mpack</strong><span>Art. MTECH-MPACK</span></div>` : `<div class="form-grid">${subcategories.length ? `<label>Category<select class="subcategory-select" required><option value="">Select category</option>${subcategories.map((category) => `<option value="${escapeHtml(category.id)}">${escapeHtml(category.name)}</option>`).join("")}</select></label>` : ""}<label>Product<input class="product-combobox" autocomplete="off" placeholder="Search products by name or article" ${subcategories.length ? "disabled" : ""}></label></div>`}</section><div class="configurator-host"><div class="selection-summary"><i data-lucide="mouse-pointer-2"></i><span>${mpackOnly ? "Loading Mtech Mpack configuration…" : subcategories.length ? "Select a category, then choose a product." : "Choose a product to continue."}</span></div></div>`;
    const input = body.querySelector<HTMLInputElement>(".product-combobox")!;
    const host = body.querySelector<HTMLElement>(".configurator-host")!;
    if (mpackOnly) {
      const product = products.find((item) => item._id === "mtech-mpack") ?? products[0];
      if (product) renderConfigurator(host, product, { mode: "add" });
      else host.innerHTML = '<div class="notice error"><strong>Mtech Mpack is not available</strong><p>The Underpacking catalog returned no canonical MPack product.</p></div>';
      refreshIcons(page);
      return page;
    }
    let available = subcategories.length ? (selectedFamily === "blankets" ? products : []) : products;
    body.querySelector<HTMLSelectElement>(".subcategory-select")?.addEventListener("change", async (event) => {
      const categoryId = (event.currentTarget as HTMLSelectElement).value;
      input.value = ""; input.disabled = !categoryId;
      host.innerHTML = '<div class="selection-summary"><i data-lucide="mouse-pointer-2"></i><span>Choose a product to begin configuration.</span></div>';
      if (selectedFamily === "blankets") available = categoryId === "all" ? products : categoryId ? (await catalogApi.blanketProducts(categoryId)).items : [];
      else available = products.filter((product) => String(product.configuration.sub_category_id) === categoryId);
      body.querySelector<HTMLElement>(".product-count")!.textContent = String(available.length); refreshIcons(host);
    });
    const renderSelection = (product: Product) => {
      host.innerHTML = `<article class="selected-product-card"><div><span class="eyebrow">Art. ${escapeHtml(product.article_no ?? product.sku)}</span><h3>${escapeHtml(product.name)}</h3><p>${escapeHtml(product.description)}</p><small>${escapeHtml(String(product.configuration.thickness_mm ?? product.configuration.thickness ?? ""))}${product.configuration.thickness_mm ? " mm" : ""} · EUR master pricing</small></div><button type="button" class="button button-dark" data-configure-product><i data-lucide="settings-2"></i>Configure Product</button></article>`;
      host.querySelector<HTMLButtonElement>("[data-configure-product]")?.addEventListener("click", () => renderConfigurator(host, product, { mode: "add" }));
      refreshIcons(host);
    };
    bindProductCombobox(input, () => available, renderSelection, () => {
      host.innerHTML = '<div class="selection-summary"><i data-lucide="mouse-pointer-2"></i><span>Select a product to continue.</span></div>';
      refreshIcons(host);
    });
  } catch (error) {
    body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Calculator unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`;
  }
  refreshIcons(page); return page;
}
