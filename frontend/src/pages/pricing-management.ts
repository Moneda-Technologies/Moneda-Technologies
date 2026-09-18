import { adminApi, catalogApi, rateApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import type { PriceHistoryEntry, PricingResource } from "../types/domain";
import { emptyState, escapeHtml, formatDate, skeleton } from "../utils/dom";

type MpackSourcePrice = { thickness_micron: number; sheets_per_box?: number | null };
type MpackSourceRow = {
  manufacturer: string;
  machine_model: string;
  width_mm: number;
  length_mm: number;
  prices: MpackSourcePrice[];
};

/** Normalize the structured catalogue exposed by the products endpoint. */
function mpackSourceRows(value: unknown): MpackSourceRow[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((candidate) => {
    if (!candidate || typeof candidate !== "object") return [];
    const row = candidate as Record<string, unknown>;
    const manufacturer = String(row.manufacturer ?? "").trim();
    const machineModel = String(row.machine_model ?? "").trim();
    const width = Number(row.width_mm);
    const length = Number(row.length_mm);
    if (!manufacturer || !machineModel || !Number.isFinite(width) || !Number.isFinite(length)) return [];
    const prices = Array.isArray(row.prices)
      ? row.prices.flatMap((entry) => {
        if (!entry || typeof entry !== "object") return [];
        const price = entry as Record<string, unknown>;
        const thickness = Number(price.thickness_micron);
        return Number.isFinite(thickness)
          ? [{ thickness_micron: thickness, sheets_per_box: price.sheets_per_box == null ? null : Number(price.sheets_per_box) }]
          : [];
      })
      : [];
    return [{ manufacturer, machine_model: machineModel, width_mm: width, length_mm: length, prices }];
  });
}

function fallbackManufacturers(rows: MpackSourceRow[]): Array<{ id: string; name: string; size_rows: number }> {
  const counts = new Map<string, { name: string; size_rows: number }>();
  rows.forEach((row) => {
    const name = row.manufacturer.trim();
    const key = name.toLocaleLowerCase();
    const current = counts.get(key);
    counts.set(key, { name: current?.name ?? name, size_rows: (current?.size_rows ?? 0) + 1 });
  });
  return [...counts.values()]
    .sort((left, right) => left.name.localeCompare(right.name))
    .map((value) => ({ id: value.name, ...value }));
}

type MpackMatrixRow = {
  size: string;
  width_mm: number;
  length_mm: number;
  sheets_per_box: Record<string, number | null>;
  prices_eur: Record<"DISTRIBUTOR" | "DEALER", Record<string, number | null>>;
  source_prices_eur: Record<"DISTRIBUTOR" | "DEALER", Record<string, number | null>>;
};

type MpackModel = {
  model?: string;
  machine_model?: string;
  rows?: MpackMatrixRow[];
  matrix?: MpackMatrixRow[];
  thicknesses?: number[];
  source?: { source?: string; source_document?: string; version?: string; valid_from?: string; valid_until?: string };
};

type MpackEditState = {
  values: Map<string, string>;
  originals: Map<string, string>;
  payloads: Map<string, { manufacturer: string; machine_model: string; width_mm: number; length_mm: number; thickness_micron: number }>;
};

function mpackCellKey(account: "DISTRIBUTOR" | "DEALER", manufacturer: string, model: string, width: number, length: number, thickness: number): string {
  return [account, manufacturer, model, width, length, thickness].join("|");
}

function normalizedSearch(value: string): string {
  return value.toLocaleLowerCase().replace(/\s+/g, "").trim();
}

function renderMpackModelSection(model: MpackModel, manufacturer: string, account: "DISTRIBUTOR" | "DEALER", canEdit: boolean): string {
  const rows = model.rows ?? model.matrix ?? [];
  const thicknesses = model.thicknesses ?? [];
  const modelName = model.model ?? model.machine_model ?? "Unnamed model";
  const sourceLabel = [model.source?.source, model.source?.version, model.source?.source_document].filter(Boolean).join(" · ");
  const headers = thicknesses.map((thickness) => {
    const key = String(thickness);
    const quantities = [...new Set(rows.map((row) => row.sheets_per_box?.[key]).filter((value): value is number => value !== null && value !== undefined))];
    const quantity = quantities.length === 1 ? `${quantities[0]} SHEETS/BOX` : quantities.length ? "SOURCE QUANTITY" : "";
    return `<th><strong>${escapeHtml(String(thickness))} MIC</strong>${quantity ? `<small>${escapeHtml(quantity)}</small>` : ""}</th>`;
  }).join("");
  const tableRows = rows.map((row, index) => `<tr><td>${index + 1}</td><td><strong>${escapeHtml(row.size)}</strong></td>${thicknesses.map((thickness) => {
    const key = String(thickness);
    const sheet = row.prices_eur?.[account]?.[key] ?? null;
    const sourceSheet = row.source_prices_eur?.[account]?.[key] ?? null;
    const sheets = row.sheets_per_box?.[key] ?? null;
    return `<td><div class="mpack-price-cell" data-manufacturer="${escapeHtml(manufacturer)}" data-machine-model="${escapeHtml(modelName)}" data-width="${row.width_mm}" data-length="${row.length_mm}" data-thickness="${thickness}">${canEdit ? `<label><span>EUR / sheet</span><input class="mpack-price-input" data-price-kind="sheet" data-original="${sheet ?? ""}" value="${sheet ?? ""}" placeholder="Not set" inputmode="decimal" /></label>` : `<strong>${sheet == null ? "Not set" : `€${Number(sheet).toFixed(3)} / sheet`}</strong>`}<small>${sheets ?? "—"} sheets / box</small>${sheet !== sourceSheet ? `<em title="Imported source: €${sourceSheet ?? "—"} / sheet">Edited</em>` : ""}</div></td>`;
  }).join("")}</tr>`).join("");
  return `<section class="panel mpack-model-section"><div class="mpack-model-heading"><div><h3>${escapeHtml(manufacturer)} — ${escapeHtml(modelName)}</h3>${sourceLabel ? `<p><i data-lucide="file-text"></i>${escapeHtml(sourceLabel)}</p>` : ""}</div></div><div class="data-table pricing-table mpack-matrix-table"><table><thead><tr><th>Sr. No.</th><th>Size</th>${headers}</tr></thead><tbody>${tableRows || `<tr><td colspan="${thicknesses.length + 2}">No persisted pricing rows found for this model.</td></tr>`}</tbody></table></div></section>`;
}

function renderMpackModelSectionWithState(
  model: MpackModel,
  manufacturer: string,
  account: "DISTRIBUTOR" | "DEALER",
  canEdit: boolean,
  editState: MpackEditState,
): string {
  const rows = model.rows ?? model.matrix ?? [];
  const thicknesses = model.thicknesses ?? [];
  const modelName = model.model ?? model.machine_model ?? "Unnamed model";
  const sourceLabel = [model.source?.source, model.source?.version, model.source?.source_document].filter(Boolean).join(" · ");
  const headers = thicknesses.map((thickness) => {
    const key = String(thickness);
    const quantities = [...new Set(rows.map((row) => row.sheets_per_box?.[key]).filter((value): value is number => value !== null && value !== undefined))];
    const quantity = quantities.length === 1 ? `${quantities[0]} SHEETS/BOX` : quantities.length ? "SOURCE QUANTITY" : "";
    return `<th><strong>${escapeHtml(String(thickness))} MIC</strong>${quantity ? `<small>${escapeHtml(quantity)}</small>` : ""}</th>`;
  }).join("");
  const tableRows = rows.map((row, index) => `<tr><td>${index + 1}</td><td><strong>${escapeHtml(row.size)}</strong></td>${thicknesses.map((thickness) => {
    const key = String(thickness);
    const sheet = row.prices_eur?.[account]?.[key] ?? null;
    const sourceSheet = row.source_prices_eur?.[account]?.[key] ?? null;
    const sheets = row.sheets_per_box?.[key] ?? null;
    const cellKey = mpackCellKey(account, manufacturer, modelName, row.width_mm, row.length_mm, thickness);
    const originalValue = sheet == null ? "" : String(sheet);
    const displayValue = editState.values.get(cellKey) ?? originalValue;
    editState.originals.set(cellKey, originalValue);
    editState.payloads.set(cellKey, { manufacturer, machine_model: modelName, width_mm: row.width_mm, length_mm: row.length_mm, thickness_micron: thickness });
    const changedFromSource = displayValue !== (sourceSheet == null ? "" : String(sourceSheet));
    return `<td><div class="mpack-price-cell" data-cell-key="${escapeHtml(cellKey)}" data-manufacturer="${escapeHtml(manufacturer)}" data-machine-model="${escapeHtml(modelName)}" data-width="${row.width_mm}" data-length="${row.length_mm}" data-thickness="${thickness}">${canEdit ? `<label><span>EUR / sheet</span><input class="mpack-price-input" data-price-kind="sheet" data-cell-key="${escapeHtml(cellKey)}" data-original="${escapeHtml(originalValue)}" value="${escapeHtml(displayValue)}" placeholder="Not set" inputmode="decimal" /></label>` : `<strong>${sheet == null ? "Not set" : `€${Number(sheet).toFixed(3)} / sheet`}</strong>`}<small>${sheets ?? "—"} sheets / box</small>${changedFromSource ? `<em title="Imported source: €${sourceSheet ?? "—"} / sheet">Edited</em>` : ""}</div></td>`;
  }).join("")}</tr>`).join("");
  return `<section class="panel mpack-model-section"><div class="mpack-model-heading"><div><h3>${escapeHtml(modelName)}</h3>${sourceLabel ? `<p><i data-lucide="file-text"></i>${escapeHtml(sourceLabel)}</p>` : ""}</div></div><div class="data-table pricing-table mpack-matrix-table"><table><thead><tr><th>Sr. No.</th><th>Size</th>${headers}</tr></thead><tbody>${tableRows || `<tr><td colspan="${thicknesses.length + 2}">No persisted pricing rows found for this model.</td></tr>`}</tbody></table></div></section>`;
}


export async function pricingAdminPage(): Promise<HTMLElement> {
  const page = pageScaffold("Settings", "Price Lists", "Manage EUR pricing by account type without duplicating the canonical catalogue.");
  page.classList.add("pricing-management-page");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  // Price Lists is an administrative surface, not the calculator's resolved
  // pricing view.  Keep the guard in the page as well as in the sidebar so a
  // manually entered /admin or /price-lists URL cannot render the management
  // UI for a non-Superadmin.  The API remains the authoritative enforcement.
  const isSuperadmin = String(appStore.state.user?.role_id ?? "") === "superadmin";
  const canView = isSuperadmin || (appStore.state.user?.permissions ?? []).includes("pricing.history");
  if (!canView) {
    body.innerHTML = '<div class="notice error"><i data-lucide="lock-keyhole"></i><div><strong>Access denied</strong><p>Only a Superadmin can access Price Lists administration.</p></div></div>';
    refreshIcons(body);
    return page;
  }
  const canEdit = isSuperadmin;
  const query = new URLSearchParams(location.search);
  // Read the old camelCase spelling for existing bookmarks; all newly
  // generated links use the backend's account_type contract.
  const requestedAccount = String(query.get("account_type") ?? query.get("accountType") ?? "").toUpperCase();
  const requestedCategory = query.get("category") ?? "";
  const state: { accountType: "" | "DISTRIBUTOR" | "DEALER"; category: string; manufacturer: string; machine: string; family: string; status: string; search: string } = {
    accountType: requestedAccount === "DISTRIBUTOR" || requestedAccount === "DEALER" ? requestedAccount : "",
    category: requestedCategory, manufacturer: query.get("manufacturer") ?? "",
    // MPack no longer has a machine/model navigation level.  Ignore the old
    // query parameter so bookmarks from the previous flow open the selected
    // manufacturer detail page instead of an intermediate selector.
    machine: requestedCategory === "mpacks" ? "" : (query.get("machine") ?? ""),
    family: "all", status: "all", search: "",
  };
  const mpackEditState: MpackEditState = { values: new Map(), originals: new Map(), payloads: new Map() };
  const navigateHierarchy = (accountType = "", category = "", machine = "", manufacturer = "") => {
    const params = new URLSearchParams();
    if (accountType) params.set("account_type", accountType);
    if (category) params.set("category", category);
    if (manufacturer) params.set("manufacturer", manufacturer);
    if (machine && category !== "mpacks") params.set("machine", machine);
    window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: `/price-lists${params.size ? `?${params}` : ""}` }));
  };

  const load = async () => {
    body.innerHTML = skeleton(7);
    try {
      const result = await adminApi.priceLists(state.accountType, state.category, state.machine, state.manufacturer);
      if (!state.accountType) {
        body.innerHTML = `<div class="admin-callout"><div><span class="eyebrow">Price Lists</span><h2>Account-type pricing</h2><p>Select an account type first. Customers select the corresponding list automatically.</p></div><div class="admin-flow"><span>Account Type</span><i data-lucide="chevron-right"></i><span>Category</span><i data-lucide="chevron-right"></i><strong>Price</strong></div></div><section class="price-list-choice-grid">${(result.account_types ?? []).map((item) => `<button type="button" class="price-list-choice panel" data-account-type="${item.code}"><i data-lucide="layers-3"></i><span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.description)}</small></span><i data-lucide="arrow-right"></i></button>`).join("")}</section><div class="notice compact"><i data-lucide="shield-check"></i><div><strong>EUR master pricing</strong><p>Distributor and Dealer are the only Price List account types. Customer remains a customer entity, not a third price list.</p></div></div>`;
        body.querySelectorAll<HTMLButtonElement>("[data-account-type]").forEach((button) => button.addEventListener("click", () => navigateHierarchy(button.dataset.accountType ?? "")));
        refreshIcons(body); return;
      }
      if (!state.category) {
        body.innerHTML = `<div class="section-title"><div><span class="eyebrow">${escapeHtml(state.accountType)}</span><h2>Select product category</h2></div><button type="button" class="button button-secondary" data-back-price-lists><i data-lucide="arrow-left"></i>Back to Price Lists</button></div><section class="price-list-category-grid">${(result.categories ?? []).map((item) => `<button type="button" class="price-list-category panel" data-category="${item.id}"><span><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.description)}</small></span><i data-lucide="arrow-right"></i></button>`).join("")}</section>`;
        body.querySelector("[data-back-price-lists]")?.addEventListener("click", () => navigateHierarchy());
        body.querySelectorAll<HTMLButtonElement>("[data-category]").forEach((button) => button.addEventListener("click", () => navigateHierarchy(state.accountType, button.dataset.category ?? "")));
        refreshIcons(body); return;
      }
      let sourceRows: MpackSourceRow[] = [];
      if (state.category === "mpacks" && ((!result.manufacturers?.length) || (Boolean(state.manufacturer) && !result.machines?.length))) {
        const sourceResult = await catalogApi.products("category=mpacks").catch(() => null);
        const sourceProduct = sourceResult?.items.find((item) => item._id === "mtech-mpack") ?? sourceResult?.items[0];
        sourceRows = mpackSourceRows(sourceProduct?.configuration?.machine_sizes);
      }
      const manufacturers = result.manufacturers?.length ? result.manufacturers : fallbackManufacturers(sourceRows);
      if (state.category === "mpacks" && !state.machine && !state.manufacturer) {
        const modelRows = result.machines ?? sourceRows.map((row) => ({ manufacturer: row.manufacturer, machine_model: row.machine_model }));
        const renderManufacturerChoices = (term: string) => {
          const needle = normalizedSearch(term);
          const visibleManufacturers = manufacturers.filter((item) => {
            if (!needle) return true;
            const modelsForManufacturer = modelRows.filter((row) => normalizedSearch(String(row.manufacturer ?? "")) === normalizedSearch(item.name));
            return normalizedSearch(item.name).includes(needle) || modelsForManufacturer.some((row) => normalizedSearch(String(row.machine_model ?? "")).includes(needle));
          });
          const list = body.querySelector<HTMLElement>("[data-mpack-manufacturers]");
          if (list) list.innerHTML = visibleManufacturers.length
            ? visibleManufacturers.map((manufacturer) => `<button type="button" class="price-list-choice panel" data-manufacturer="${escapeHtml(manufacturer.name)}"><i data-lucide="factory"></i><span><strong>${escapeHtml(manufacturer.name)}</strong></span><i data-lucide="arrow-right"></i></button>`).join("")
            : emptyState("factory", "No matching manufacturers found", "Try another manufacturer or model name.");
          const count = body.querySelector<HTMLElement>("[data-mpack-result-count]");
          if (count) count.textContent = `${visibleManufacturers.length} manufacturer${visibleManufacturers.length === 1 ? "" : "s"}`;
          list?.querySelectorAll<HTMLButtonElement>("[data-manufacturer]").forEach((button) => button.addEventListener("click", () => navigateHierarchy(state.accountType, state.category, "", button.dataset.manufacturer ?? "")));
          refreshIcons(list ?? body);
        };
        body.innerHTML = `<div class="section-title"><div><span class="eyebrow">${escapeHtml(state.accountType)} / Underpacking / MPack</span><h2>Select manufacturer</h2><p>Choose a source-defined manufacturer to view every model and its complete size × micron matrix.</p></div><button type="button" class="button button-secondary" data-back-price-category><i data-lucide="arrow-left"></i>Back to categories</button></div><section class="pricing-model-toolbar panel" aria-label="Manufacturer search"><label><span>Search manufacturer or model</span><input data-mpack-search type="search" placeholder="Search manufacturer or model name..." autocomplete="off" /></label><button type="button" class="button button-secondary" data-mpack-clear hidden>Clear</button><span class="pricing-result-count" data-mpack-result-count></span></section><section class="price-list-choice-grid" data-mpack-manufacturers></section>`;
        body.querySelector("[data-back-price-category]")?.addEventListener("click", () => navigateHierarchy(state.accountType));
        const search = body.querySelector<HTMLInputElement>("[data-mpack-search]")!;
        const clear = body.querySelector<HTMLButtonElement>("[data-mpack-clear]")!;
        search.addEventListener("input", () => { state.search = search.value; clear.hidden = !search.value.trim(); renderManufacturerChoices(state.search); });
        clear.addEventListener("click", () => { search.value = ""; state.search = ""; clear.hidden = true; renderManufacturerChoices(""); search.focus(); });
        renderManufacturerChoices(state.search);
        return;
      }
      if (state.category === "mpacks" && !state.machine && !state.manufacturer) {
        const choices = manufacturers.map((manufacturer) => `<button type="button" class="price-list-choice panel" data-manufacturer="${escapeHtml(manufacturer.name)}"><i data-lucide="factory"></i><span><strong>${escapeHtml(manufacturer.name)}</strong></span><i data-lucide="arrow-right"></i></button>`).join("");
        body.innerHTML = `<div class="section-title"><div><span class="eyebrow">${escapeHtml(state.accountType)} / Underpacking / MPack</span><h2>Select manufacturer</h2><p>Choose a source-defined manufacturer to view every model and its complete size × micron matrix.</p></div><button type="button" class="button button-secondary" data-back-price-category><i data-lucide="arrow-left"></i>Back to categories</button></div><section class="price-list-choice-grid">${choices || emptyState("factory", "No source-defined manufacturers found", "The persisted MPack source matrix has no rows.")}</section>`;
        body.querySelector("[data-back-price-category]")?.addEventListener("click", () => navigateHierarchy(state.accountType));
        body.querySelectorAll<HTMLButtonElement>("[data-manufacturer]").forEach((button) => button.addEventListener("click", () => navigateHierarchy(state.accountType, state.category, "", button.dataset.manufacturer ?? "")));
        refreshIcons(body); return;
      }
      if (state.category === "mpacks" && state.manufacturer && !state.machine) {
        const account = state.accountType as "DISTRIBUTOR" | "DEALER";
        const models = (result.models ?? []) as MpackModel[];
        const manufacturer = result.manufacturer ?? state.manufacturer;
        mpackEditState.values.clear();
        mpackEditState.originals.clear();
        mpackEditState.payloads.clear();
        // Register every editable cell before filtering so edits survive a
        // search that temporarily hides a model.
        models.forEach((model) => renderMpackModelSectionWithState(model, manufacturer, account, canEdit, mpackEditState));
        body.innerHTML = `<div class="section-title"><div><span class="eyebrow">${escapeHtml(state.accountType)} / Underpacking / MPack / ${escapeHtml(manufacturer)}</span><h2>${escapeHtml(manufacturer)} — MPack pricing</h2><p>All ${escapeHtml(manufacturer)} models and their size × micron EUR per-sheet prices.</p></div><button type="button" class="button button-secondary" data-back-manufacturers><i data-lucide="arrow-left"></i>Back to manufacturers</button></div><section class="pricing-model-toolbar panel" aria-label="Model search"><label><span>Search models</span><input data-mpack-model-search type="search" placeholder="Search model name..." autocomplete="off" /></label><button type="button" class="button button-secondary" data-mpack-model-clear hidden>Clear</button><span class="pricing-result-count" data-mpack-model-count></span></section><div class="mpack-model-sections" data-mpack-model-sections></div>${canEdit ? `<div class="modal-actions mpack-save-actions"><button type="button" class="button button-secondary mpack-save-button" data-save-mpack disabled><i data-lucide="save"></i>Save Changes</button></div>` : `<div class="notice compact"><i data-lucide="eye"></i><div><strong>View only</strong><p>Only Superadmin can change Price List values.</p></div></div>`}`;
        const sections = body.querySelector<HTMLElement>("[data-mpack-model-sections]")!;
        const saveButton = body.querySelector<HTMLButtonElement>("[data-save-mpack]");
        const updateSaveState = () => {
          if (!saveButton) return;
          const dirty = [...mpackEditState.values].some(([key, value]) => value !== mpackEditState.originals.get(key));
          saveButton.disabled = !dirty;
          saveButton.classList.toggle("button-primary", dirty);
          saveButton.classList.toggle("button-secondary", !dirty);
          saveButton.setAttribute("aria-disabled", String(!dirty));
        };
        const bindInputs = () => {
          sections.querySelectorAll<HTMLInputElement>(".mpack-price-input").forEach((input) => input.addEventListener("input", () => {
            const key = input.dataset.cellKey;
            if (!key) return;
            mpackEditState.values.set(key, input.value.trim());
            updateSaveState();
          }));
        };
        const renderModels = (term: string) => {
          const needle = normalizedSearch(term);
          const visibleModels = models.filter((model) => normalizedSearch(String(model.model ?? model.machine_model ?? "")).includes(needle));
          sections.innerHTML = visibleModels.length
            ? visibleModels.map((model) => renderMpackModelSectionWithState(model, manufacturer, account, canEdit, mpackEditState)).join("")
            : emptyState("search-x", "No matching models found", "Try another model name.");
          const count = body.querySelector<HTMLElement>("[data-mpack-model-count]");
          if (count) count.textContent = `Showing ${visibleModels.length} of ${models.length} models`;
          bindInputs();
          updateSaveState();
          refreshIcons(sections);
        };
        body.querySelector("[data-back-manufacturers]")?.addEventListener("click", () => navigateHierarchy(state.accountType, state.category));
        const search = body.querySelector<HTMLInputElement>("[data-mpack-model-search]")!;
        const clear = body.querySelector<HTMLButtonElement>("[data-mpack-model-clear]")!;
        search.addEventListener("input", () => { state.search = search.value; clear.hidden = !search.value.trim(); renderModels(state.search); });
        clear.addEventListener("click", () => { search.value = ""; state.search = ""; clear.hidden = true; renderModels(""); search.focus(); });
        saveButton?.addEventListener("click", async () => {
          const changes = [...mpackEditState.values].filter(([key, value]) => value !== mpackEditState.originals.get(key));
          if (!changes.length) return;
          try {
            for (const [key, value] of changes) {
              const payload = mpackEditState.payloads.get(key);
              if (!payload) continue;
              const parsed = value ? Number(value) : null;
              if (parsed !== null && !Number.isFinite(parsed)) throw new Error("Enter valid EUR per-sheet values before saving");
              await adminApi.updateMpackPrice({ account_type: account, machine: { manufacturer: payload.manufacturer, machine_model: payload.machine_model }, width_mm: payload.width_mm, length_mm: payload.length_mm, thickness_micron: payload.thickness_micron, price_per_sheet_eur: parsed });
            }
            toast("MPack price matrix updated", "success");
            await load();
          } catch (error) { toast(error instanceof Error ? error.message : "Price matrix could not be updated", "error"); }
        });
        renderModels(state.search);
        return;
      }
      if (state.category === "mpacks" && state.manufacturer && !state.machine) {
        const account = state.accountType as "DISTRIBUTOR" | "DEALER";
        const models = (result.models ?? []) as MpackModel[];
        const manufacturer = result.manufacturer ?? state.manufacturer;
        body.innerHTML = `<div class="section-title"><div><span class="eyebrow">${escapeHtml(state.accountType)} / Underpacking / MPack / ${escapeHtml(manufacturer)}</span><h2>${escapeHtml(manufacturer)} — MPack pricing</h2><p>All ${escapeHtml(manufacturer)} models and their size × micron EUR per-sheet prices.</p></div><button type="button" class="button button-secondary" data-back-manufacturers><i data-lucide="arrow-left"></i>Back to manufacturers</button></div><div class="mpack-model-sections">${models.length ? models.map((model) => renderMpackModelSection(model, manufacturer, account, canEdit)).join("") : emptyState("factory", "No source-defined models found", "The persisted MPack source matrix has no rows for this manufacturer.")}</div>${canEdit ? `<div class="modal-actions mpack-save-actions"><button type="button" class="button button-primary" data-save-mpack><i data-lucide="save"></i>Save Changes</button></div>` : `<div class="notice compact"><i data-lucide="eye"></i><div><strong>View only</strong><p>Only Superadmin can change Price List values.</p></div></div>`}`;
        body.querySelector("[data-back-manufacturers]")?.addEventListener("click", () => navigateHierarchy(state.accountType, state.category));
        body.querySelector("[data-save-mpack]")?.addEventListener("click", async () => {
          const cells = [...body.querySelectorAll<HTMLElement>(".mpack-price-cell")].filter((cell) => [...cell.querySelectorAll<HTMLInputElement>("input")].some((input) => input.value !== input.dataset.original));
          try {
            for (const cell of cells) {
              const sheet = cell.querySelector<HTMLInputElement>('[data-price-kind="sheet"]')!;
              const value = sheet.value.trim();
              await adminApi.updateMpackPrice({ account_type: account, machine: { manufacturer: cell.dataset.manufacturer, machine_model: cell.dataset.machineModel }, width_mm: Number(cell.dataset.width), length_mm: Number(cell.dataset.length), thickness_micron: Number(cell.dataset.thickness), price_per_sheet_eur: value ? Number(value) : null });
            }
            toast(cells.length ? "MPack price matrix updated" : "No price changes to save", cells.length ? "success" : "info"); await load();
          } catch (error) { toast(error instanceof Error ? error.message : "Price matrix could not be updated", "error"); }
        });
        refreshIcons(body); return;
      }
      // Legacy single-model response retained for non-MPack API clients. MPack
      // navigation normalizes machine query parameters away and uses `models`
      // above, so this branch is not part of the current UI flow.
      if (state.category === "mpacks" && state.machine && result.matrix) {
        const matrix = result.matrix ?? [];
        const account = state.accountType as "DISTRIBUTOR" | "DEALER";
        const sourceLabel = [result.source?.source, result.source?.version, result.source?.source_document].filter(Boolean).join(" · ");
        const matrixHeaders = (result.thicknesses ?? []).map((thickness) => {
          const key = String(thickness);
          const quantities = [...new Set(matrix.map((row) => row.sheets_per_box?.[key]).filter((value): value is number => value !== null && value !== undefined))];
          const quantity = quantities.length === 1 ? `${quantities[0]} sheets/box` : quantities.length ? "Source quantity" : "";
          return `<th><strong>${thickness} MIC</strong>${quantity ? `<small>${quantity}</small>` : ""}</th>`;
        }).join("");
        body.innerHTML = `<div class="section-title"><div><span class="eyebrow">${escapeHtml(state.accountType)} / Underpacking / MPack</span><h2>${escapeHtml(result.machine?.manufacturer ?? "")} - ${escapeHtml(result.machine?.machine_model ?? "")}</h2><p>Exact EUR per-sheet values. Box quantities are source-defined information only. ${escapeHtml(sourceLabel)}</p></div><div><button type="button" class="button button-secondary" data-back-machines><i data-lucide="arrow-left"></i>Back to machines</button></div></div><div class="data-table panel pricing-table mpack-matrix-table"><table><thead><tr><th>Sr. No.</th><th>Size</th>${matrixHeaders}</tr></thead><tbody>${matrix.map((row, index) => `<tr><td>${index + 1}</td><td><strong>${escapeHtml(row.size)}</strong></td>${(result.thicknesses ?? []).map((thickness) => { const key = String(thickness); const sheet = row.prices_eur?.[account]?.[key] ?? null; const sourceSheet = row.source_prices_eur?.[account]?.[key] ?? null; const sheets = row.sheets_per_box?.[key] ?? null; return `<td><div class="mpack-price-cell" data-width="${row.width_mm}" data-length="${row.length_mm}" data-thickness="${thickness}">${canEdit ? `<label><span>EUR / sheet</span><input class="mpack-price-input" data-price-kind="sheet" data-original="${sheet ?? ""}" value="${sheet ?? ""}" placeholder="Not set" inputmode="decimal" /></label>` : `<strong>${sheet == null ? "Not set" : `€${Number(sheet).toFixed(3)} / sheet`}</strong>`}<small>${sheets ?? "—"} sheets / box</small>${sheet !== sourceSheet ? `<em title="Imported source: €${sourceSheet ?? "—"} / sheet">Edited</em>` : ""}</div></td>`; }).join("")}</tr>`).join("")}</tbody></table></div>${canEdit ? `<div class="modal-actions"><button type="button" class="button button-primary" data-save-mpack><i data-lucide="save"></i>Save Changes</button></div>` : `<div class="notice compact"><i data-lucide="eye"></i><div><strong>View only</strong><p>Only Superadmin can change Price List values.</p></div></div>`}`;
        body.querySelector("[data-back-machines]")?.addEventListener("click", () => navigateHierarchy(state.accountType, state.category, "", state.manufacturer || result.machine?.manufacturer || ""));
        body.querySelector("[data-save-mpack]")?.addEventListener("click", async () => {
          const cells = [...body.querySelectorAll<HTMLElement>(".mpack-price-cell")].filter((cell) => [...cell.querySelectorAll<HTMLInputElement>("input")].some((input) => input.value !== input.dataset.original));
          try {
            for (const cell of cells) {
              const sheet = cell.querySelector<HTMLInputElement>('[data-price-kind="sheet"]')!;
              const value = sheet.value.trim();
              await adminApi.updateMpackPrice({ account_type: account, machine: result.machine, width_mm: Number(cell.dataset.width), length_mm: Number(cell.dataset.length), thickness_micron: Number(cell.dataset.thickness), price_per_sheet_eur: value ? Number(value) : null });
            }
            toast(cells.length ? "MPack price matrix updated" : "No price changes to save", cells.length ? "success" : "info"); await load();
          } catch (error) { toast(error instanceof Error ? error.message : "Price matrix could not be updated", "error"); }
        });
        refreshIcons(body); return;
      }
      const rates = await rateApi.get().catch(() => null);
      const visibleItems = result.items.filter((row) => {
        const term = state.search.toLocaleLowerCase();
        const matchesSearch = !term || [row.name, row.article_no, row.sku, row.family_name, row.category_name].join(" ").toLocaleLowerCase().includes(term);
        const matchesStatus = state.status === "all" || row.pricing_status === state.status;
        return matchesSearch && matchesStatus;
      });
      const pending = visibleItems.filter((row) => row.pricing_status !== "configured").length;
      const rateStatus = rates ? (rates.status === "stored_fallback" || rates.stale ? "Using last successful ECB rate" : "Latest available") : "Unavailable";
      const rateDate = rates?.provider_dates?.USD || rates?.provider_dates?.INR || "—";
      body.innerHTML = `<div class="admin-callout"><div><span class="eyebrow">${escapeHtml(state.accountType)} / ${escapeHtml(state.category)}</span><h2>One catalogue. One authoritative price.</h2><p>MongoDB is authoritative at runtime. Source prices remain EUR and historical documents retain their snapshots.</p></div><div class="admin-flow"><span>Account type</span><i data-lucide="chevron-right"></i><span>Category</span><i data-lucide="chevron-right"></i><strong>EUR master</strong></div></div>
        <div class="rate-source-strip panel"><div><span class="eyebrow">Currency engine</span><strong>${rateStatus} · Provider: ${escapeHtml(rates?.provider ?? "ECB")}</strong></div><div><span>EUR → USD</span><strong>${rates?.rates.USD?.toFixed(4) ?? "—"}</strong></div><div><span>EUR → INR</span><strong>${rates?.rates.INR?.toFixed(4) ?? "—"}</strong></div><div><span>Rate date</span><strong>${escapeHtml(rateDate)}</strong></div><div><span>Fetched time</span><strong>${rates?.fetched_at ? escapeHtml(formatDate(rates.fetched_at)) : "—"}</strong></div></div>
        <section class="pricing-toolbar panel" aria-label="Pricing filters"><button type="button" class="button button-secondary" data-back-price-category><i data-lucide="arrow-left"></i>Back to categories</button><label><span>Search</span><input id="pricing-search" value="${escapeHtml(state.search)}" placeholder="Product, article, SKU or category"></label><label><span>Status</span><select id="pricing-status">${["all", "configured", "pending", "on_request", "inactive"].map((value) => `<option value="${value}" ${state.status === value ? "selected" : ""}>${value === "all" ? "All statuses" : value.replace("_", " ")}</option>`).join("")}</select></label><div class="pricing-result-count"><strong>${result.total ?? result.items.length}</strong><span>records</span></div></section>
        <div class="section-title"><div><span class="eyebrow">Master catalogue</span><h2>EUR product pricing</h2></div><span class="count-badge">${pending} need attention</span></div>
        ${visibleItems.length ? `<div class="data-table panel pricing-table"><table><thead><tr><th>Product</th><th>Family / category</th><th>Pricing unit</th><th>Master EUR</th><th>Status</th><th>Last updated</th><th>Updated by</th><th>Actions</th></tr></thead><tbody>${visibleItems.map((row) => pricingTableRow(row, canEdit)).join("")}</tbody></table></div>` : emptyState("search-x", "No pricing records found", "Change the search or filter selection.")}`;

      const search = body.querySelector<HTMLInputElement>("#pricing-search")!;
      let timer = 0;
      search.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => { state.search = search.value.trim(); void load(); }, 300);
      });
      body.querySelector("[data-back-price-category]")?.addEventListener("click", () => navigateHierarchy(state.accountType));
      body.querySelector<HTMLSelectElement>("#pricing-status")?.addEventListener("change", (event) => { state.status = (event.target as HTMLSelectElement).value; void load(); });
      body.querySelectorAll<HTMLButtonElement>("[data-edit-price]").forEach((button) => button.addEventListener("click", () => {
        const row = visibleItems.find((item) => item.id === button.dataset.editPrice);
        if (row) void openPriceEditor(row, load);
      }));
      body.querySelectorAll<HTMLButtonElement>("[data-price-history]").forEach((button) => button.addEventListener("click", () => {
        const row = visibleItems.find((item) => item.id === button.dataset.priceHistory);
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
