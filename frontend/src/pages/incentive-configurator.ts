import { adminApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { customerTypeLabel } from "../config/businessConfig";
import { emptyState, escapeHtml } from "../utils/dom";

type BusinessRuleType = "user" | "manager_team" | "manager_creator";
interface ProductType { id: string; name: string }
interface ProductConfig { customer_type: string; product_type_id: string; product_type_name: string; user_rate: number | null; manager_team_rate: number | null; user_configured: boolean; manager_configured: boolean; team_rate: number | null; creator_rate: number | null; team_configured: boolean; creator_configured: boolean }
interface UserConfiguration { _id: string; name?: string; email?: string; manager_id?: string | null; manager?: { _id?: string; name?: string; email?: string } | null; configurations: ProductConfig[] }
interface ManagerConfiguration { _id: string; name?: string; email?: string; connected_users: Array<{ _id?: string; name?: string; email?: string }>; configurations: ProductConfig[] }
interface ProductRate { product_type_id: string; product_type_name: string; rate: number | null; configured: boolean; active?: boolean }
interface IncentiveRuleGroup { id: string; incentive_type: string; allocation_type: "creator" | "manager_override"; recipient_role: string; customer_type: string; customer_id?: string | null; active?: boolean; product_rates: ProductRate[] }
interface ConfiguratorData { customer_types: string[]; product_types: ProductType[]; users: UserConfiguration[]; managers: ManagerConfiguration[]; rule_groups?: IncentiveRuleGroup[] }

const nameOf = (r: { _id?: string; name?: string; email?: string } | null | undefined) => String(r?.name || r?.email || r?._id || "—");
const rateText = (rate: number | null | undefined) => rate == null || !Number.isFinite(Number(rate)) ? "—" : `${Number(rate)}%`;
const rateOptions = (selected: number | null | undefined) => Array.from({ length: 13 }, (_, i) => i / 2).map((rate) => `<option value="${rate}" ${Number(selected ?? 0) === rate ? "selected" : ""}>${rate}%</option>`).join("");

function groupFor(data: ConfiguratorData, type: BusinessRuleType, customerType: string): IncentiveRuleGroup | undefined {
  const allocation = type === "manager_team" ? "manager_override" : "creator";
  const role = type === "user" ? "user" : "manager_sales_admin";
  return (data.rule_groups || []).find((group) => group.allocation_type === allocation && group.recipient_role === role && group.customer_type === customerType && !group.customer_id);
}

function openRowEditor(data: ConfiguratorData, row: { view: "users" | "managers"; customerType: string; product: ProductConfig; managerId?: string; userName?: string; managerName?: string }): void {
  const product = data.product_types.find((item) => item.id === row.product.product_type_id);
  if (!product) return;
  const userRate = row.view === "users" ? row.product.user_rate : row.product.team_rate;
  const teamRate = row.view === "users" ? row.product.manager_team_rate : row.product.creator_rate;
  const title = row.view === "users" ? "Edit User Incentive" : "Edit Manager Incentive";
  const content = document.createElement("div");
  content.innerHTML = `<form class="incentive-config-form" data-incentive-row-form>
    <p class="form-hint">Changes apply to future Order Confirmations only. Historical incentive snapshots are preserved.</p>
    <div class="incentive-edit-context"><strong>${escapeHtml(product.name)}</strong><span>${escapeHtml(customerTypeLabel(row.customerType))}</span>${row.userName ? `<span>User: ${escapeHtml(row.userName)}</span>` : ""}${row.managerName ? `<span>Manager: ${escapeHtml(row.managerName)}</span>` : ""}</div>
    <div class="form-grid"><label>${row.view === "users" ? "User Incentive" : "Manager Team Incentive"}<select name="primary_rate">${rateOptions(userRate)}</select></label><label>${row.view === "users" ? "Manager Team Incentive" : "Manager Creator Incentive"}<select name="secondary_rate" ${row.view === "users" && !row.managerId ? "disabled" : ""}>${rateOptions(teamRate)}</select></label></div>
    <small class="field-error" data-incentive-row-error aria-live="polite"></small><div class="modal-actions"><button type="button" class="button button-quiet" data-cancel>Cancel</button><button type="submit" class="button button-primary">Save Changes</button></div>
  </form>`;
  const dialog = openModal(title, content, "wide");
  content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
  content.querySelector<HTMLFormElement>("[data-incentive-row-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    const error = content.querySelector<HTMLElement>("[data-incentive-row-error]");
    submit.disabled = true;
    try {
      const values = new FormData(form);
      const primary = Number(values.get("primary_rate"));
      const secondary = Number(values.get("secondary_rate"));
      const calls: Promise<unknown>[] = [];
      const primaryGroup = groupFor(data, row.view === "users" ? "user" : "manager_team", row.customerType);
      if (primaryGroup) calls.push(adminApi.configureProductIncentiveRules({ allocation_type: primaryGroup.allocation_type, recipient_role: primaryGroup.recipient_role, client_type: row.customerType, customer_id: null, rates: { [product.id]: primary } }));
      if (!(row.view === "users" && !row.managerId)) {
        const secondaryGroup = groupFor(data, row.view === "users" ? "manager_team" : "manager_creator", row.customerType);
        if (secondaryGroup) calls.push(adminApi.configureProductIncentiveRules({ allocation_type: secondaryGroup.allocation_type, recipient_role: secondaryGroup.recipient_role, client_type: row.customerType, customer_id: null, rates: { [product.id]: secondary } }));
      }
      if (!calls.length) throw new Error("No editable incentive rule is available for this row");
      await Promise.all(calls);
      dialog.close();
      toast("Incentive rates saved");
      window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/incentives/rules" }));
    } catch (reason) {
      if (error) error.textContent = reason instanceof Error ? reason.message : "Incentive rates could not be saved";
      submit.disabled = false;
    }
  });
  refreshIcons(dialog);
}

export async function renderIncentiveConfigurator(_page: HTMLElement, body: HTMLElement): Promise<void> {
  const data = await adminApi.incentiveConfigurator() as unknown as ConfiguratorData;
  const query = new URLSearchParams(window.location.search);
  const requestedType = String(query.get("client_type") || "WHOLESALER").toUpperCase();
  const initialType = data.customer_types.includes(requestedType) ? requestedType : (data.customer_types[0] || "WHOLESALER");
  let activeView: "users" | "managers" = query.get("tab") === "managers" ? "managers" : "users";
  body.innerHTML = `<section class="panel incentive-configurator">
    <div class="incentive-configurator-head"><div><span class="eyebrow">Incentives / Incentive Rules</span><h2>Incentive Rules</h2><p>Configure incentive rates for users and managers based on customer type and product type.</p></div></div>
    <div class="incentive-rule-tabs" role="tablist" aria-label="Incentive configuration view"><button type="button" class="button button-dark" data-rule-view="users" role="tab" aria-selected="true">User Incentives</button><button type="button" class="button button-quiet" data-rule-view="managers" role="tab" aria-selected="false">Manager Incentives</button></div>
    <div class="incentive-configurator-filters">
      <label data-user-filter>User<select data-rule-user><option value="">All Users</option>${data.users.map((user) => `<option value="${escapeHtml(user._id)}">${escapeHtml(nameOf(user))}</option>`).join("")}</select></label>
      <label data-manager-filter>Manager<select data-rule-manager><option value="">All Managers</option>${data.managers.map((manager) => `<option value="${escapeHtml(manager._id)}">${escapeHtml(nameOf(manager))}</option>`).join("")}</select></label>
      <label>Customer Type<select data-rule-client>${data.customer_types.map((type) => `<option value="${escapeHtml(type)}" ${type === initialType ? "selected" : ""}>${escapeHtml(customerTypeLabel(type))}</option>`).join("")}</select></label>
      <label>Incentive Type<select data-rule-type><option value="">All Types</option><option value="user">User Incentive</option><option value="manager_team">Manager Team Incentive</option><option value="manager_creator">Manager Creator Incentive</option></select></label>
      <label>Status<select data-rule-status><option value="active" selected>Active</option><option value="">All statuses</option><option value="inactive">Inactive</option></select></label>
      <label>Product Type<select data-rule-product><option value="">All Product Types</option>${data.product_types.map((product) => `<option value="${escapeHtml(product.id)}">${escapeHtml(product.name)}</option>`).join("")}</select></label>
      <label class="incentive-filter-search">Search<input type="search" data-rule-search placeholder="Search users or managers"></label>
    </div>
    <section class="incentive-product-configurations" aria-labelledby="incentive-rules-heading"><div data-product-rule-list></div></section>
  </section>`;

  const filters = () => ({ user: body.querySelector<HTMLSelectElement>("[data-rule-user]")?.value || "", manager: body.querySelector<HTMLSelectElement>("[data-rule-manager]")?.value || "", customerType: body.querySelector<HTMLSelectElement>("[data-rule-client]")?.value || initialType, incentiveType: body.querySelector<HTMLSelectElement>("[data-rule-type]")?.value || "", status: body.querySelector<HTMLSelectElement>("[data-rule-status]")?.value || "", product: body.querySelector<HTMLSelectElement>("[data-rule-product]")?.value || "", search: (body.querySelector<HTMLInputElement>("[data-rule-search]")?.value || "").trim().toLowerCase() });
  const matches = (values: string, filter: ReturnType<typeof filters>) => !filter.search || values.toLowerCase().includes(filter.search);
  const rowStatus = (active: boolean) => active ? "Active" : "Inactive";
  const rateActive = (type: BusinessRuleType, customerType: string, productId: string): boolean => {
    const group = groupFor(data, type, customerType);
    return group?.product_rates.find((item) => item.product_type_id === productId)?.active !== false;
  };

  const render = () => {
    const host = body.querySelector<HTMLElement>("[data-product-rule-list]"); if (!host) return;
    const filter = filters();
    body.querySelector<HTMLElement>("[data-user-filter]")?.toggleAttribute("hidden", activeView === "managers");
    body.querySelector<HTMLElement>("[data-manager-filter]")?.toggleAttribute("hidden", activeView === "users");
    body.querySelectorAll<HTMLButtonElement>("[data-rule-view]").forEach((button) => { const selected = button.dataset.ruleView === activeView; button.classList.toggle("button-dark", selected); button.classList.toggle("button-quiet", !selected); button.setAttribute("aria-selected", String(selected)); });
    type FlatRow = { customerType: string; product: ProductConfig; primary: number | null; secondary: number | null; status: string; user?: UserConfiguration; manager?: ManagerConfiguration };
    const rows: FlatRow[] = [];
    if (activeView === "users") {
      data.users.filter((user) => (!filter.user || user._id === filter.user) && (!filter.manager || String(user.manager_id || "") === filter.manager)).forEach((user) => user.configurations.filter((product) => product.customer_type === filter.customerType && (!filter.product || product.product_type_id === filter.product) && (!filter.incentiveType || filter.incentiveType === "user" || filter.incentiveType === "manager_team") && matches(`${nameOf(user)} ${nameOf(user.manager)} ${product.product_type_name} ${customerTypeLabel(product.customer_type)}`, filter)).forEach((product) => rows.push({ customerType: product.customer_type, product, primary: product.user_rate, secondary: product.manager_team_rate, status: rowStatus(rateActive("user", product.customer_type, product.product_type_id)), user })));
      const visibleRows = rows.filter((row) => !filter.status || (filter.status === "active" ? row.status === "Active" : row.status === "Inactive"));
      const heading = `User Incentive Rules (${customerTypeLabel(filter.customerType)})`;
      host.innerHTML = visibleRows.length ? `<div class="incentive-product-rule-heading"><div><span class="eyebrow">User Incentives</span><h3 id="incentive-rules-heading">${escapeHtml(heading)}</h3><p>Each user and their connected manager receive the rates shown for each product type.</p></div><span class="incentive-rule-count">${visibleRows.length} rules</span></div><div class="data-table incentive-product-rule-table"><table><thead><tr><th>User</th><th>Manager</th><th>Customer Type</th><th>Product Type</th><th>User Incentive</th><th>Manager Team Incentive</th><th>Status</th><th>Actions</th></tr></thead><tbody>${visibleRows.map((row) => `<tr><td><strong>${escapeHtml(nameOf(row.user))}</strong><small>${escapeHtml(row.user?.email || "")}</small></td><td>${escapeHtml(nameOf(row.user?.manager))}</td><td>${escapeHtml(customerTypeLabel(row.customerType))}</td><td>${escapeHtml(row.product.product_type_name)}</td><td>${escapeHtml(rateText(row.primary))}</td><td>${row.user?.manager ? escapeHtml(rateText(row.secondary)) : "—"}</td><td>${statusBadge(row.status)}</td><td><button type="button" class="button button-quiet incentive-product-edit-button" data-row-edit="user:${escapeHtml(row.user?._id || "")}:${escapeHtml(row.product.product_type_id)}:${escapeHtml(row.customerType)}"><i data-lucide="pencil"></i>Edit</button></td></tr>`).join("")}</tbody></table></div>` : emptyState("users", "No incentive rules match", "Adjust the filters to view active product-level rates.");
    } else {
      data.managers.filter((manager) => !filter.manager || manager._id === filter.manager).forEach((manager) => manager.configurations.filter((product) => product.customer_type === filter.customerType && (!filter.product || product.product_type_id === filter.product) && (!filter.incentiveType || filter.incentiveType === "manager_team" || filter.incentiveType === "manager_creator") && matches(`${nameOf(manager)} ${manager.connected_users.map(nameOf).join(" ")} ${product.product_type_name} ${customerTypeLabel(product.customer_type)}`, filter)).forEach((product) => rows.push({ customerType: product.customer_type, product, primary: product.team_rate, secondary: product.creator_rate, status: rowStatus(rateActive("manager_team", product.customer_type, product.product_type_id) || rateActive("manager_creator", product.customer_type, product.product_type_id)), manager })));
      const visibleRows = rows.filter((row) => !filter.status || (filter.status === "active" ? row.status === "Active" : row.status === "Inactive"));
      const heading = `Manager Incentive Rules (${customerTypeLabel(filter.customerType)})`;
      host.innerHTML = visibleRows.length ? `<div class="incentive-product-rule-heading"><div><span class="eyebrow">Manager Incentives</span><h3 id="incentive-rules-heading">${escapeHtml(heading)}</h3><p>Manager team and creator rates are shown independently for each product type.</p></div><span class="incentive-rule-count">${visibleRows.length} rules</span></div><div class="data-table incentive-product-rule-table"><table><thead><tr><th>Manager</th><th>Connected Users</th><th>Customer Type</th><th>Product Type</th><th>Manager Team Incentive</th><th>Manager Creator Incentive</th><th>Status</th><th>Actions</th></tr></thead><tbody>${visibleRows.map((row) => `<tr><td><strong>${escapeHtml(nameOf(row.manager))}</strong><small>${escapeHtml(row.manager?.email || "")}</small></td><td>${row.manager?.connected_users.length || 0}</td><td>${escapeHtml(customerTypeLabel(row.customerType))}</td><td>${escapeHtml(row.product.product_type_name)}</td><td>${escapeHtml(rateText(row.primary))}</td><td>${escapeHtml(rateText(row.secondary))}</td><td>${statusBadge(row.status)}</td><td><button type="button" class="button button-quiet incentive-product-edit-button" data-row-edit="manager:${escapeHtml(row.manager?._id || "")}:${escapeHtml(row.product.product_type_id)}:${escapeHtml(row.customerType)}"><i data-lucide="pencil"></i>Edit</button></td></tr>`).join("")}</tbody></table></div>` : emptyState("users", "No incentive rules match", "Adjust the filters to view active product-level rates.");
    }
    host.querySelectorAll<HTMLButtonElement>("[data-row-edit]").forEach((button) => button.addEventListener("click", () => {
      const [view, id, productId, customerType] = String(button.dataset.rowEdit || "").split(":");
      const product = view === "users" ? data.users.find((user) => user._id === id)?.configurations.find((item) => item.product_type_id === productId && item.customer_type === customerType) : data.managers.find((manager) => manager._id === id)?.configurations.find((item) => item.product_type_id === productId && item.customer_type === customerType);
      if (!product) return;
      const user = view === "users" ? data.users.find((item) => item._id === id) : undefined;
      const manager = view === "managers" ? data.managers.find((item) => item._id === id) : user?.manager;
      openRowEditor(data, { view: view as "users" | "managers", customerType, product, managerId: user?.manager_id || undefined, userName: user ? nameOf(user) : undefined, managerName: manager ? nameOf(manager) : undefined });
    }));
    refreshIcons(host);
  };
  body.querySelectorAll<HTMLButtonElement>("[data-rule-view]").forEach((button) => button.addEventListener("click", () => { activeView = button.dataset.ruleView === "managers" ? "managers" : "users"; render(); }));
  body.querySelectorAll<HTMLSelectElement>("[data-rule-user], [data-rule-manager], [data-rule-client], [data-rule-product], [data-rule-type], [data-rule-status]").forEach((control) => control.addEventListener("change", render));
  body.querySelector<HTMLInputElement>("[data-rule-search]")?.addEventListener("input", render);
  render();
  refreshIcons(body);
}
