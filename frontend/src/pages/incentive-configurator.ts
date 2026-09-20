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
  maximum_rules?: Record<string, unknown>[];
  individual_configurations?: Record<string, unknown>[];
}

const rateValue = (selected: number | null | undefined) => Number.isFinite(Number(selected)) ? String(Number(selected)) : "0";

function incentiveStatusLabel(value: unknown): string {
  return String(value || "INHERIT").trim().toUpperCase().replaceAll("_", " ");
}

function incentiveScopeLabel(row: Record<string, unknown>): string {
  const scope = String(row.scope || "person").toLowerCase();
  const dimensions = [
    scope === "customer" && row.customer_id ? `Customer ${row.customer_id}` : "",
    scope === "product" && row.product_id ? `Product ${row.product_id}` : "",
    scope === "category" && row.category_id ? `Category ${row.category_id}` : "",
    scope === "client_type" && row.client_type ? customerTypeLabel(String(row.client_type)) : "",
  ].filter(Boolean);
  return dimensions.length ? `${scope.replaceAll("_", " ")} · ${dimensions.join(" · ")}` : scope.replaceAll("_", " ");
}

function incentiveRateText(value: unknown, status: unknown): string {
  const normalized = String(status || "").toUpperCase();
  if (normalized !== "ENABLED" || value === null || value === undefined || value === "") return incentiveStatusLabel(status);
  const rate = Number(value);
  return Number.isFinite(rate) ? `${rate}%` : "Not configured";
}

function openIndividualEditor(configuration: Record<string, unknown>, onSaved: () => Promise<void>): void {
  const userId = String(configuration.user_id || configuration.recipient_user_id || "");
  if (!userId) {
    toast("This incentive configuration is missing its user reference");
    return;
  }
  const status = String(configuration.status || "INHERIT").toUpperCase();
  const content = document.createElement("div");
  const maximum = configuration.maximum_rate === null || configuration.maximum_rate === undefined ? null : Number(configuration.maximum_rate);
  content.innerHTML = `<form class="incentive-config-form" data-individual-form>
    <p class="form-hint">Changes apply to future Order Confirmations only. Historical incentive snapshots are preserved.</p>
    <div class="incentive-edit-context"><label>User<span class="field-readonly">${escapeHtml(String(configuration.user_name || configuration.user_email || userId))}</span></label><label>Configuration scope<span class="field-readonly">${escapeHtml(incentiveScopeLabel(configuration))}</span></label></div>
    <label>Status<select id="individual-incentive-status" name="status"><option value="INHERIT" ${status === "INHERIT" ? "selected" : ""}>Inherit default</option><option value="ENABLED" ${status === "ENABLED" ? "selected" : ""}>Enabled</option><option value="DISABLED" ${status === "DISABLED" ? "selected" : ""}>Disabled</option><option value="NOT_CONFIGURED" ${status === "NOT_CONFIGURED" ? "selected" : ""}>Not configured</option></select></label>
    <label>Individual rate (%)<input id="individual-incentive-rate" name="rate" type="number" min="0" max="100" step="0.5" value="${rateValue(configuration.rate as number | null | undefined)}" ${status !== "ENABLED" ? "disabled" : ""}></label>
    <small class="form-hint">Maximum allowed for this configuration: ${maximum === null || !Number.isFinite(maximum) ? "No ceiling configured" : `${maximum}%`}. The server validates the ceiling.</small>
    <small class="field-error" data-individual-error aria-live="polite"></small><div class="modal-actions"><button type="button" class="button button-quiet" data-cancel>Cancel</button><button type="submit" class="button button-primary">Save Changes</button></div>
  </form>`;
  const dialog = openModal("Edit Individual Incentive", content, "wide");
  const form = content.querySelector<HTMLFormElement>("[data-individual-form]");
  const statusField = content.querySelector<HTMLSelectElement>('[name="status"]');
  const rateField = content.querySelector<HTMLInputElement>('[name="rate"]');
  statusField?.addEventListener("change", () => {
    if (rateField) rateField.disabled = statusField.value !== "ENABLED";
  });
  content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]');
    const error = content.querySelector<HTMLElement>("[data-individual-error]");
    if (submit) submit.disabled = true;
    try {
      const nextStatus = statusField?.value || "INHERIT";
      const payload = {
        configurations: [{
          scope: configuration.scope || "person",
          allocation_type: configuration.allocation_type || "creator",
          customer_id: configuration.customer_id || null,
          product_id: configuration.product_id || null,
          category_id: configuration.category_id || null,
          client_type: configuration.client_type || "*",
          status: nextStatus,
          rate: nextStatus === "ENABLED" ? Number(rateField?.value || 0) : null,
        }],
        replace: false,
      };
      await adminApi.updateUserIncentiveConfiguration(userId, payload);
      dialog.close();
      toast("Individual incentive saved");
      await onSaved();
    } catch (reason) {
      if (error) error.textContent = reason instanceof Error ? reason.message : "Individual incentive could not be saved";
      if (submit) submit.disabled = false;
    }
  });
  refreshIcons(dialog);
}

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
    if (fields) fields.innerHTML = next.product_rates.map((product) => `<label>${escapeHtml(product.product_type_name)} (%)<input type="number" min="0" max="100" step="0.5" name="rate:${escapeHtml(product.product_type_id)}" value="${rateValue(product.rate)}"></label>`).join("");
    const empty = content.querySelector<HTMLElement>("[data-incentive-empty]");
    if (empty) empty.hidden = !next.id.startsWith("default:");
    const status = content.querySelector<HTMLSelectElement>('select[name="status"]');
    if (status) status.value = next.active === false ? "inactive" : "active";
  };
  const form = content.querySelector<HTMLFormElement>("[data-incentive-group-form]");
  const saveButton = form?.querySelector<HTMLButtonElement>('button[type="submit"]');
  let baseline = "";
  const formState = () => JSON.stringify(Array.from(form?.querySelectorAll<HTMLInputElement | HTMLSelectElement>("select, input") ?? []).map((field) => [field.name, field.value]));
  const resetBaseline = () => {
    baseline = formState();
    if (saveButton) saveButton.disabled = true;
  };
  const updateDirtyState = () => {
    if (saveButton) saveButton.disabled = formState() === baseline;
  };
  renderSelectedGroup(group);
  resetBaseline();
  form?.addEventListener("input", updateDirtyState);
  form?.addEventListener("change", updateDirtyState);
  content.querySelector<HTMLSelectElement>("[data-edit-customer-type]")?.addEventListener("change", (event) => {
    const customerType = (event.currentTarget as HTMLSelectElement).value;
    const next = logicalGroups(data, ruleType === "user" ? "users" : "managers", customerType).find((item) => item.incentive_type === selectedGroup.incentive_type) || defaultGroup(data, ruleType, customerType);
    renderSelectedGroup(next);
    resetBaseline();
  });
  form?.addEventListener("submit", async (event) => {
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

function openAddIncentiveRule(data: ConfiguratorData, onSaved: () => Promise<void>): void {
  const content = document.createElement("div");
  const people = [...data.users, ...data.managers];
  const personOptions = people.map((person) => `<option value="${escapeHtml(String(person._id ?? ""))}" data-role="${escapeHtml(String(person.role_id ?? ""))}">${escapeHtml(String(person.name ?? person.email ?? person._id ?? "Employee"))} · ${escapeHtml(String(person.role_id ?? "user").replaceAll("_", " "))}</option>`).join("");
  content.innerHTML = `<form class="stack-form incentive-rule-create-form" data-add-incentive-rule>
    <p class="form-hint">Rules apply only to future Order Confirmations. No matching rule means a 0% incentive.</p>
    <div class="form-grid">
      <label>Recipient type<select name="recipient_type"><option value="person">Specific employee</option><option value="role">Role / default</option></select></label>
      <label data-person-field>Specific employee<select name="user_id"><option value="">Select employee</option>${personOptions}</select></label>
      <label data-role-field hidden>Recipient role<select name="recipient_role"><option value="user">User</option><option value="manager_sales_admin">Manager</option><option value="admin">Admin</option><option value="*">All eligible roles</option></select></label>
      <label>Allocation<select name="allocation_type"><option value="creator">OC creator</option><option value="manager_override">Creator's manager</option></select></label>
      <label>Client type<select name="client_type"><option value="*">All client types</option>${data.customer_types.map((type) => `<option value="${escapeHtml(type)}">${escapeHtml(customerTypeLabel(type))}</option>`).join("")}</select></label>
      <label>Category<select name="category_id"><option value="*">All categories</option>${data.product_types.map((product) => `<option value="${escapeHtml(product.id)}">${escapeHtml(product.name)}</option>`).join("")}</select></label>
      <label>Eligible<select name="eligible"><option value="true">Yes</option><option value="false">No (0%)</option></select></label>
      <label>Rate type<select name="rate_type"><option value="percentage">Percentage</option></select></label>
      <label>Rate (%)<input name="rate" type="number" min="0" max="100" step="0.5" value="0" required></label>
      <label>Effective from<input name="effective_from" type="date"></label>
      <label>Effective until<input name="effective_to" type="date"></label>
      <label>Status<select name="active"><option value="true">Active</option><option value="false">Inactive</option></select></label>
    </div>
    <small class="field-error" data-add-rule-error aria-live="polite"></small>
    <div class="modal-actions"><button type="button" class="button button-quiet" data-cancel>Cancel</button><button type="submit" class="button button-primary"><i data-lucide="plus"></i>Add Incentive Rule</button></div>
  </form>`;
  const dialog = openModal("Add Incentive Rule", content, "wide");
  const form = content.querySelector<HTMLFormElement>("[data-add-incentive-rule]")!;
  const recipientType = form.elements.namedItem("recipient_type") as HTMLSelectElement;
  const eligible = form.elements.namedItem("eligible") as HTMLSelectElement;
  const rate = form.elements.namedItem("rate") as HTMLInputElement;
  const syncFields = () => {
    const roleRule = recipientType.value === "role";
    const personField = content.querySelector<HTMLElement>("[data-person-field]");
    const roleField = content.querySelector<HTMLElement>("[data-role-field]");
    if (personField) personField.hidden = roleRule;
    if (roleField) roleField.hidden = !roleRule;
    rate.disabled = eligible.value === "false";
    if (rate.disabled) rate.value = "0";
  };
  recipientType.addEventListener("change", syncFields);
  eligible.addEventListener("change", syncFields);
  syncFields();
  content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    const error = content.querySelector<HTMLElement>("[data-add-rule-error]");
    const values = new FormData(form);
    const isEligible = values.get("eligible") === "true";
    const isActive = values.get("active") === "true";
    const numericRate = isEligible ? Number(values.get("rate")) : 0;
    if (!Number.isFinite(numericRate) || numericRate < 0 || numericRate > 100 || Math.round(numericRate * 2) !== numericRate * 2) {
      if (error) error.textContent = "Rate must be between 0% and 100% in 0.5% steps.";
      return;
    }
    submit.disabled = true;
    try {
      const clientType = String(values.get("client_type") || "*");
      const categoryId = String(values.get("category_id") || "*");
      const allocationType = String(values.get("allocation_type") || "creator");
      const effectiveFrom = values.get("effective_from") || null;
      const effectiveTo = values.get("effective_to") || null;
      if (recipientType.value === "person") {
        const userId = String(values.get("user_id") || "");
        if (!userId) throw new Error("Select a specific employee");
        const scope = categoryId !== "*" ? "category" : clientType !== "*" ? "client_type" : "person";
        await adminApi.updateUserIncentiveConfiguration(userId, {
          configurations: [{
            scope, allocation_type: allocationType,
            category_id: categoryId === "*" ? null : categoryId,
            client_type: clientType,
            status: isActive && isEligible ? "ENABLED" : "DISABLED",
            rate: isActive && isEligible ? numericRate : null,
            effective_from: effectiveFrom, effective_to: effectiveTo,
          }],
          replace: false,
        });
      } else {
        await adminApi.createIncentiveRule({
          allocation_type: allocationType,
          recipient_role: values.get("recipient_role"),
          client_type: clientType,
          category_id: categoryId,
          rate: numericRate,
          active: isActive && isEligible,
          effective_from: effectiveFrom,
          effective_to: effectiveTo,
        });
      }
      dialog.close();
      toast("Incentive rule added");
      await onSaved();
    } catch (reason) {
      if (error) error.textContent = reason instanceof Error ? reason.message : "Incentive rule could not be added";
      submit.disabled = false;
    }
  });
  refreshIcons(dialog);
}

export async function renderIncentiveConfigurator(_page: HTMLElement, body: HTMLElement): Promise<void> {
  let data = await adminApi.incentiveConfigurator() as unknown as ConfiguratorData;
  let reload: (() => Promise<void>) | undefined;
  const query = new URLSearchParams(window.location.search);
  const requestedType = String(query.get("client_type") || "WHOLESALER").toUpperCase();
  const initialType = data.customer_types.includes(requestedType) ? requestedType : (data.customer_types[0] || "WHOLESALER");
  let activeView: "users" | "managers" = query.get("tab") === "managers" ? "managers" : "users";
  body.innerHTML = `<section class="panel incentive-configurator">
    <div class="incentive-configurator-head"><div><span class="eyebrow">Incentives / Incentive Rules</span><h2>Incentive Rules</h2><p>Configure optional incentive eligibility and rates for users, managers, or role defaults.</p></div><button type="button" class="button button-primary" data-add-incentive-rule><i data-lucide="plus"></i>Add Incentive Rule</button></div>
    <div class="incentive-rule-tabs" role="tablist" aria-label="Incentive configuration view"><button type="button" class="button button-dark" data-rule-view="users" role="tab" aria-selected="true">User Incentives</button><button type="button" class="button button-quiet" data-rule-view="managers" role="tab" aria-selected="false">Manager Incentives</button></div>
    <div class="incentive-configurator-filters">
      <label>Customer Type<select data-rule-client>${data.customer_types.map((type) => `<option value="${escapeHtml(type)}" ${type === initialType ? "selected" : ""}>${escapeHtml(customerTypeLabel(type))}</option>`).join("")}</select></label>
      <label>Incentive Type<select data-rule-type><option value="">All Types</option><option value="user">User Incentive</option><option value="manager_team">Manager Team Incentive</option><option value="manager_creator">Manager Creator Incentive</option></select></label>
      <label>Status<select data-rule-status><option value="" selected>All statuses</option><option value="active">Enabled</option><option value="inactive">Disabled / inherited</option></select></label>
      <label>Product Type<select data-rule-product><option value="">All Product Types</option>${data.product_types.map((product) => `<option value="${escapeHtml(product.id)}">${escapeHtml(product.name)}</option>`).join("")}</select></label>
      <label class="incentive-filter-search">Search<input type="search" data-rule-search placeholder="Search incentive configurations"></label>
    </div>
    <section class="incentive-configuration-panels" aria-label="Incentive ceilings and individual configurations"><div data-maximum-list></div><div data-individual-list></div></section>
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
    const maximumHost = body.querySelector<HTMLElement>("[data-maximum-list]");
    const maximumRows = (data.maximum_rules || []).filter((row) => {
      const rowType = String(row.client_type || "*").toUpperCase();
      const role = String(row.recipient_role || "*").toLowerCase();
      const allocation = String(row.allocation_type || "creator").toLowerCase();
      return (rowType === "*" || rowType === filter.customerType) && (!filter.incentiveType || (filter.incentiveType === "user" && role === "user" && allocation === "creator") || (filter.incentiveType === "manager_team" && allocation === "manager_override") || (filter.incentiveType === "manager_creator" && role === "manager_sales_admin" && allocation === "creator"));
    });
    if (maximumHost) {
      maximumHost.innerHTML = `<section class="incentive-config-subpanel"><div class="incentive-product-rule-heading"><div><span class="eyebrow">Maximum incentive rules</span><h3>Maximum allowed rates</h3><p>Ceilings are enforced by the server. Individual rates cannot exceed these values.</p></div><span class="incentive-rule-count">${maximumRows.length} ${maximumRows.length === 1 ? "ceiling" : "ceilings"}</span></div>${maximumRows.length ? `<div class="data-table incentive-configuration-table"><table><thead><tr><th>Allocation</th><th>Customer Type</th><th>Role</th><th>Maximum</th></tr></thead><tbody>${maximumRows.map((row) => `<tr><td>${escapeHtml(String(row.allocation_type || "creator").replaceAll("_", " "))}</td><td>${escapeHtml(row.client_type && row.client_type !== "*" ? customerTypeLabel(String(row.client_type)) : "All customer types")}</td><td>${escapeHtml(String(row.recipient_role || "All roles").replaceAll("_", " "))}</td><td><strong>${escapeHtml(String(row.maximum_rate ?? row.rate ?? "—"))}%</strong></td></tr>`).join("")}</tbody></table></div>` : emptyState("shield-check", "No maximum rules", "No incentive ceilings match the current filters.")}</section>`;
    }
    const individualRows = (data.individual_configurations || []).filter((row) => {
      const rowType = String(row.client_type || "*").toUpperCase();
      const rowStatus = String(row.status || "INHERIT").toUpperCase();
      const searchValues = `${row.user_name || ""} ${row.user_email || ""} ${row.user_id || ""} ${row.scope || ""} ${row.category_id || ""} ${row.product_id || ""}`.toLowerCase();
      return (rowType === "*" || rowType === filter.customerType) && (!filter.status || (filter.status === "active" ? rowStatus === "ENABLED" : rowStatus !== "ENABLED")) && (!filter.search || searchValues.includes(filter.search));
    });
    const individualHost = body.querySelector<HTMLElement>("[data-individual-list]");
    if (individualHost) {
      individualHost.innerHTML = `<section class="incentive-config-subpanel"><div class="incentive-product-rule-heading"><div><span class="eyebrow">Individual configurations</span><h3>Optional employee incentives</h3><p>Configure an individual override, inherit the applicable default, or disable the allocation.</p></div><span class="incentive-rule-count">${individualRows.length} ${individualRows.length === 1 ? "configuration" : "configurations"}</span></div>${individualRows.length ? `<div class="data-table incentive-configuration-table"><table><thead><tr><th>User</th><th>Role</th><th>Allocation</th><th>Scope</th><th>Rate</th><th>Maximum</th><th>Status</th><th>Actions</th></tr></thead><tbody>${individualRows.map((row, index) => `<tr><td><strong>${escapeHtml(String(row.user_name || row.user_email || row.user_id || "Unknown user"))}</strong><small>${escapeHtml(String(row.user_email || row.user_id || ""))}</small></td><td>${escapeHtml(String(row.recipient_role || row.role || "—").replaceAll("_", " "))}</td><td>${escapeHtml(String(row.allocation_type || "creator").replaceAll("_", " "))}</td><td>${escapeHtml(incentiveScopeLabel(row))}</td><td><strong>${escapeHtml(incentiveRateText(row.rate, row.status))}</strong></td><td>${row.maximum_rate === null || row.maximum_rate === undefined ? "—" : `${escapeHtml(String(row.maximum_rate))}%`}</td><td>${statusBadge(incentiveStatusLabel(row.status))}</td><td><button type="button" class="button button-quiet incentive-logical-edit-button" data-individual-edit="${index}"><i data-lucide="pencil"></i>Edit</button></td></tr>`).join("")}</tbody></table></div>` : emptyState("user-cog", "No individual configurations", "No employee-specific incentive overrides match the current filters.")}</section>`;
      individualHost.querySelectorAll<HTMLButtonElement>("[data-individual-edit]").forEach((button) => {
        const row = individualRows[Number(button.dataset.individualEdit)];
        if (row) button.addEventListener("click", () => openIndividualEditor(row, async () => { await reload?.(); }));
      });
    }
    refreshIcons(body);
  };

  reload = async () => {
    data = await adminApi.incentiveConfigurator() as unknown as ConfiguratorData;
    render();
  };
  body.querySelector<HTMLButtonElement>("[data-add-incentive-rule]")?.addEventListener("click", () => openAddIncentiveRule(data, async () => { await reload?.(); }));
  body.querySelectorAll<HTMLButtonElement>("[data-rule-view]").forEach((button) => button.addEventListener("click", () => { activeView = button.dataset.ruleView === "managers" ? "managers" : "users"; render(); }));
  body.querySelectorAll<HTMLSelectElement>("[data-rule-client], [data-rule-product], [data-rule-type], [data-rule-status]").forEach((control) => control.addEventListener("change", render));
  body.querySelector<HTMLInputElement>("[data-rule-search]")?.addEventListener("input", render);
  render();
  refreshIcons(body);
}
