import { adminApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { customerTypeLabel } from "../config/businessConfig";
import { emptyState, escapeHtml } from "../utils/dom";

type BusinessRuleType = "user" | "manager_team" | "manager_creator";
interface ProductType { id: string; name: string }
interface ProductRate { product_type_id: string; product_type_name: string; rate: number | null; configured: boolean; active?: boolean; rule_id?: string | null }
interface IncentiveRuleGroup {
  id: string;
  incentive_type: string;
  allocation_type: "creator" | "manager_override";
  recipient_role: string;
  customer_type: string;
  customer_id?: string | null;
  customer_name?: string | null;
  active?: boolean;
  configured_count?: number;
  product_type_label?: string;
  product_rates: ProductRate[];
}
interface ConfiguratorData {
  customer_types: string[];
  product_types: ProductType[];
  users: Record<string, unknown>[];
  managers: Record<string, unknown>[];
  rule_groups?: IncentiveRuleGroup[];
}

const rateOptions = (selected: number | null | undefined) => Array.from({ length: 13 }, (_, i) => i / 2).map((rate) => `<option value="${rate}" ${Number(selected ?? 0) === rate ? "selected" : ""}>${rate}%</option>`).join("");

function groupFor(data: ConfiguratorData, type: BusinessRuleType, customerType: string): IncentiveRuleGroup | undefined {
  const allocation = type === "manager_team" ? "manager_override" : "creator";
  const role = type === "user" ? "user" : "manager_sales_admin";
  return (data.rule_groups || []).find((group) => group.allocation_type === allocation && group.recipient_role === role && group.customer_type === customerType && !group.customer_id);
}

function defaultGroup(data: ConfiguratorData, type: BusinessRuleType, customerType: string): IncentiveRuleGroup {
  const allocation = type === "manager_team" ? "manager_override" : "creator";
  const role = type === "user" ? "user" : "manager_sales_admin";
  const incentiveType = type === "user" ? "User Incentive" : type === "manager_team" ? "Manager Team Incentive" : "Manager Creator Incentive";
  return {
    id: `default:${allocation}:${customerType}:${role}`,
    incentive_type: incentiveType,
    allocation_type: allocation,
    recipient_role: role,
    customer_type: customerType,
    customer_id: null,
    active: true,
    product_rates: data.product_types.map((product) => ({ product_type_id: product.id, product_type_name: product.name, rate: null, configured: false, active: true })),
  };
}

function logicalGroups(data: ConfiguratorData, activeView: "users" | "managers", customerType: string): IncentiveRuleGroup[] {
  const types: BusinessRuleType[] = activeView === "users" ? ["user"] : ["manager_team", "manager_creator"];
  return types.map((type) => groupFor(data, type, customerType) || defaultGroup(data, type, customerType));
}

function openGroupEditor(data: ConfiguratorData, group: IncentiveRuleGroup): void {
  const content = document.createElement("div");
  const ruleType: BusinessRuleType = group.incentive_type === "User Incentive" ? "user" : group.incentive_type === "Manager Team Incentive" ? "manager_team" : "manager_creator";
  let selectedGroup = group;
  content.innerHTML = `<form class="incentive-config-form" data-incentive-group-form>
    <p class="form-hint">Changes apply to future Order Confirmations only. Historical incentive snapshots are preserved.</p>
    <div class="incentive-edit-context"><label>Incentive Type<span class="field-readonly">${escapeHtml(group.incentive_type)}</span></label><label>Customer Type<select name="customer_type" data-edit-customer-type>${data.customer_types.map((type) => `<option value="${escapeHtml(type)}" ${type === group.customer_type ? "selected" : ""}>${escapeHtml(customerTypeLabel(type))}</option>`).join("")}</select></label></div>
    <div class="notice compact" data-incentive-empty hidden>No configuration exists yet for this Customer Type. Set the product rates below to create it.</div>
    <span class="eyebrow">Product rates</span><div class="incentive-product-rate-fields"></div>
    <label>Status<select name="status"><option value="active">Active</option><option value="inactive">Inactive</option></select></label>
    <small class="field-error" data-incentive-row-error aria-live="polite"></small><div class="modal-actions"><button type="button" class="button button-quiet" data-cancel>Cancel</button><button type="submit" class="button button-primary">Save Changes</button></div>
  </form>`;
  const dialog = openModal(`Edit ${group.incentive_type}`, content, "wide");
  content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
  const renderSelectedGroup = (next: IncentiveRuleGroup) => {
    selectedGroup = next;
    const fields = content.querySelector<HTMLElement>(".incentive-product-rate-fields");
    if (fields) fields.innerHTML = next.product_rates.map((product) => `<label>${escapeHtml(product.product_type_name)}<select name="rate:${escapeHtml(product.product_type_id)}">${rateOptions(product.rate)}</select></label>`).join("");
    const empty = content.querySelector<HTMLElement>("[data-incentive-empty]");
    if (empty) empty.hidden = !next.id.startsWith("default:");
    const status = content.querySelector<HTMLSelectElement>('select[name="status"]');
    if (status) status.value = next.active === false ? "inactive" : "active";
  };
  renderSelectedGroup(group);
  content.querySelector<HTMLSelectElement>("[data-edit-customer-type]")?.addEventListener("change", (event) => {
    const customerType = (event.currentTarget as HTMLSelectElement).value;
    const next = logicalGroups(data, ruleType === "user" ? "users" : "managers", customerType).find((item) => item.incentive_type === selectedGroup.incentive_type) || defaultGroup(data, ruleType, customerType);
    renderSelectedGroup(next);
  });
  content.querySelector<HTMLFormElement>("[data-incentive-group-form]")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    const error = content.querySelector<HTMLElement>("[data-incentive-row-error]");
    submit.disabled = true;
    try {
      const values = new FormData(form);
      const rates: Record<string, number> = {};
      selectedGroup.product_rates.forEach((product) => { rates[product.product_type_id] = Number(values.get(`rate:${product.product_type_id}`)); });
      const selectedCustomerType = String(values.get("customer_type") || "").toUpperCase();
      if (!data.customer_types.includes(selectedCustomerType)) throw new Error("Select a valid Customer Type");
      const result = await adminApi.configureProductIncentiveRules({ allocation_type: selectedGroup.allocation_type, recipient_role: selectedGroup.recipient_role, client_type: selectedCustomerType, customer_id: selectedGroup.customer_id ?? null, rates });
      if (String(values.get("status")) === "inactive") {
        const savedItems = (result.items || []) as Record<string, unknown>[];
        await Promise.all(savedItems.map((item) => item._id ? adminApi.updateIncentiveRule(String(item._id), { active: false }) : Promise.resolve()));
      }
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
      <label>Customer Type<select data-rule-client>${data.customer_types.map((type) => `<option value="${escapeHtml(type)}" ${type === initialType ? "selected" : ""}>${escapeHtml(customerTypeLabel(type))}</option>`).join("")}</select></label>
      <label>Incentive Type<select data-rule-type><option value="">All Types</option><option value="user">User Incentive</option><option value="manager_team">Manager Team Incentive</option><option value="manager_creator">Manager Creator Incentive</option></select></label>
      <label>Status<select data-rule-status><option value="active" selected>Active</option><option value="">All statuses</option><option value="inactive">Inactive</option></select></label>
      <label>Product Type<select data-rule-product><option value="">All Product Types</option>${data.product_types.map((product) => `<option value="${escapeHtml(product.id)}">${escapeHtml(product.name)}</option>`).join("")}</select></label>
      <label class="incentive-filter-search">Search<input type="search" data-rule-search placeholder="Search incentive configurations"></label>
    </div>
    <section class="incentive-product-configurations" aria-labelledby="incentive-rules-heading"><div data-product-rule-list></div></section>
  </section>`;
  const filterPanel = body.querySelector<HTMLElement>(".incentive-configurator-filters");
  if (filterPanel) {
    filterPanel.querySelector("select[data-rule-client]")?.parentElement?.classList.add("incentive-mobile-primary-filter");
    const filterBar = document.createElement("div");
    filterBar.className = "incentive-mobile-filter-bar";
    filterBar.innerHTML = `<strong>Filters</strong><button type="button" class="button button-quiet" data-mobile-filter-toggle aria-expanded="false">Show filters</button>`;
    filterPanel.before(filterBar);
    filterBar.querySelector<HTMLButtonElement>("[data-mobile-filter-toggle]")?.addEventListener("click", (event) => {
      const button = event.currentTarget as HTMLButtonElement;
      const open = filterPanel.classList.toggle("is-mobile-open");
      button.setAttribute("aria-expanded", String(open));
      button.textContent = open ? "Hide filters" : "Show filters";
    });
  }

  const filters = () => ({
    customerType: body.querySelector<HTMLSelectElement>("[data-rule-client]")?.value || initialType,
    incentiveType: body.querySelector<HTMLSelectElement>("[data-rule-type]")?.value || "",
    status: body.querySelector<HTMLSelectElement>("[data-rule-status]")?.value || "",
    product: body.querySelector<HTMLSelectElement>("[data-rule-product]")?.value || "",
    search: (body.querySelector<HTMLInputElement>("[data-rule-search]")?.value || "").trim().toLowerCase(),
  });

  const render = () => {
    const host = body.querySelector<HTMLElement>("[data-product-rule-list]");
    if (!host) return;
    const filter = filters();
    body.querySelectorAll<HTMLButtonElement>("[data-rule-view]").forEach((button) => {
      const selected = button.dataset.ruleView === activeView;
      button.classList.toggle("button-dark", selected);
      button.classList.toggle("button-quiet", !selected);
      button.setAttribute("aria-selected", String(selected));
    });
    const groups = logicalGroups(data, activeView, filter.customerType).filter((group) => {
      const typeMatches = !filter.incentiveType || (filter.incentiveType === "user" && group.incentive_type === "User Incentive") || (filter.incentiveType === "manager_team" && group.incentive_type === "Manager Team Incentive") || (filter.incentiveType === "manager_creator" && group.incentive_type === "Manager Creator Incentive");
      const statusMatches = !filter.status || (filter.status === "active" ? group.active !== false : group.active === false);
      const products = filter.product ? group.product_rates.filter((product) => product.product_type_id === filter.product) : group.product_rates;
      const searchValues = `${group.incentive_type} ${customerTypeLabel(group.customer_type)} ${products.map((product) => product.product_type_name).join(" ")}`.toLowerCase();
      return typeMatches && statusMatches && (!filter.search || searchValues.includes(filter.search));
    });
    const heading = `${activeView === "users" ? "User" : "Manager"} Incentive Rules (${customerTypeLabel(filter.customerType)})`;
    const description = activeView === "users" ? "Each user incentive configuration contains an independent rate for every product type." : "Manager team and creator incentives are separate configurations with independent product rates.";
    const countLabel = `${groups.length} ${groups.length === 1 ? "rule" : "rules"}`;
    const rows = groups.map((group) => {
      const products = filter.product ? group.product_rates.filter((product) => product.product_type_id === filter.product) : group.product_rates;
      const productNames = products.map((product) => product.product_type_name).join(" · ");
      return `<tr><td data-label="Incentive Type"><strong>${escapeHtml(group.incentive_type)}</strong></td><td data-label="Customer Type">${escapeHtml(customerTypeLabel(group.customer_type))}</td><td data-label="Product Types"><strong>${products.length} product ${products.length === 1 ? "rate" : "rates"}</strong><small>${escapeHtml(productNames || "No product rates")}</small></td><td data-label="Status">${statusBadge(group.active === false ? "Inactive" : "Active")}</td><td data-label="Actions"><button type="button" class="button button-quiet incentive-logical-edit-button" data-group-edit="${escapeHtml(encodeURIComponent(group.id))}"><i data-lucide="pencil"></i>Edit</button></td></tr>`;
    }).join("");
    host.innerHTML = groups.length ? `<div class="incentive-product-rule-heading"><div><span class="eyebrow">${activeView === "users" ? "User Incentives" : "Manager Incentives"}</span><h3 id="incentive-rules-heading">${escapeHtml(heading)}</h3><p>${escapeHtml(description)}</p></div><span class="incentive-rule-count">${countLabel}</span></div><div class="data-table incentive-logical-rule-table"><table><thead><tr><th>Incentive Type</th><th>Customer Type</th><th>Product Types</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyState("users", "No incentive configurations match", "Adjust the filters to view the available configurations.");
    const groupById = new Map(groups.map((group) => [group.id, group]));
    host.querySelectorAll<HTMLButtonElement>("[data-group-edit]").forEach((button) => button.addEventListener("click", () => {
      const group = groupById.get(decodeURIComponent(button.dataset.groupEdit || ""));
      if (group) openGroupEditor(data, group);
    }));
    refreshIcons(host);
  };

  body.querySelectorAll<HTMLButtonElement>("[data-rule-view]").forEach((button) => button.addEventListener("click", () => { activeView = button.dataset.ruleView === "managers" ? "managers" : "users"; render(); }));
  body.querySelectorAll<HTMLSelectElement>("[data-rule-client], [data-rule-product], [data-rule-type], [data-rule-status]").forEach((control) => control.addEventListener("change", render));
  body.querySelector<HTMLInputElement>("[data-rule-search]")?.addEventListener("input", render);
  render();
  refreshIcons(body);
}
