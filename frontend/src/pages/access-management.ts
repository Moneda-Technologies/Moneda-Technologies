import { adminApi, customerCompanyApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatDate, skeleton } from "../utils/dom";

type Row = Record<string, unknown>;

const permissionCategoryLabels: Record<string, string> = {
  audit_logs: "Audit / Activity", bank_details: "Bank Details", calculator: "Calculator", cart: "Cart",
  companies: "Customers", customers: "Customers", customer_pricing: "Customer Pricing", credit_notes: "Credit Notes",
  crm: "CRM", devices: "Login Devices", incentives: "Incentives", order_confirmations: "Order Confirmations",
  orders: "Orders", payments: "Payments / Finance", quotations: "Quotations", reports: "Reports",
  roles: "Roles & Permissions", settings: "Settings", users: "Users & Access",
};

function permissionCategory(permission: string): string {
  const namespace = permission.split(".")[0] || "other";
  return permissionCategoryLabels[namespace] ?? namespace.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function permissionLabel(permission: string): string {
  const action = permission.split(".").slice(1).join(" ") || permission;
  return action.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function permissionSelector(permissions: string[], selected: Set<string>): string {
  const groups = new Map<string, string[]>();
  permissions.forEach((permission) => { const category = permissionCategory(permission); groups.set(category, [...(groups.get(category) ?? []), permission]); });
  return `<section class="permission-selector"><div class="permission-toolbar"><label class="permission-search"><i data-lucide="search"></i><input type="search" data-permission-search placeholder="Search permissions" aria-label="Search permissions"></label><span data-permission-count></span><div><button class="text-button" type="button" data-permission-bulk="select">Select all</button><button class="text-button" type="button" data-permission-bulk="clear">Clear all</button></div></div><div class="permission-editor-list">${[...groups.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([category, items], index) => `<section class="permission-group" data-permission-group><label class="permission-group-head"><input type="checkbox" data-permission-category="${index}"><span>${escapeHtml(category)}</span><small>${items.length}</small></label><div>${items.sort().map((permission) => `<label class="permission-row" data-permission-row data-permission-search-value="${escapeHtml(`${category} ${permission} ${permissionLabel(permission)}`.toLowerCase())}"><input type="checkbox" name="permissions" value="${escapeHtml(permission)}" ${selected.has(permission) ? "checked" : ""}><span><strong>${escapeHtml(permissionLabel(permission))}</strong><small>${escapeHtml(permission)}</small></span></label>`).join("")}</div></section>`).join("")}</div></section>`;
}

function wirePermissionSelector(content: HTMLElement): void {
  const rows = [...content.querySelectorAll<HTMLElement>("[data-permission-row]")];
  const count = content.querySelector<HTMLElement>("[data-permission-count]");
  const update = () => {
    const checked = rows.filter((row) => row.querySelector<HTMLInputElement>('input[name="permissions"]')?.checked).length;
    if (count) count.textContent = `${checked} of ${rows.length} selected`;
    content.querySelectorAll<HTMLElement>("[data-permission-group]").forEach((group) => {
      const inputs = [...group.querySelectorAll<HTMLInputElement>('input[name="permissions"]')];
      const category = group.querySelector<HTMLInputElement>("[data-permission-category]");
      if (!category) return;
      const selected = inputs.filter((input) => input.checked).length;
      category.checked = selected === inputs.length && inputs.length > 0;
      category.indeterminate = selected > 0 && selected < inputs.length;
    });
  };
  content.querySelector<HTMLInputElement>("[data-permission-search]")?.addEventListener("input", (event) => {
    const search = (event.currentTarget as HTMLInputElement).value.trim().toLowerCase();
    rows.forEach((row) => { row.hidden = Boolean(search && !String(row.dataset.permissionSearchValue).includes(search)); });
    content.querySelectorAll<HTMLElement>("[data-permission-group]").forEach((group) => { group.hidden = !group.querySelector("[data-permission-row]:not([hidden])"); });
  });
  content.querySelectorAll<HTMLInputElement>("[data-permission-category]").forEach((category) => category.addEventListener("change", () => {
    category.closest("[data-permission-group]")?.querySelectorAll<HTMLInputElement>('input[name="permissions"]').forEach((input) => { input.checked = category.checked; });
    update();
  }));
  content.querySelectorAll<HTMLButtonElement>("[data-permission-bulk]").forEach((button) => button.addEventListener("click", () => {
    rows.filter((row) => !row.hidden).forEach((row) => { const input = row.querySelector<HTMLInputElement>('input[name="permissions"]'); if (input) input.checked = button.dataset.permissionBulk === "select"; });
    update();
  }));
  rows.forEach((row) => row.querySelector<HTMLInputElement>('input[name="permissions"]')?.addEventListener("change", update));
  update();
}

function forbiddenPage(title: string): HTMLElement {
  const page = pageScaffold("Management", title, "This workspace is restricted by server-authoritative access control.");
  page.querySelector<HTMLElement>(".page-body")!.innerHTML = '<div class="notice error"><i data-lucide="shield-x"></i><div><strong>Superadmin access required</strong><p>Role and permission definitions can only be viewed or changed by a Superadmin.</p></div></div>';
  refreshIcons(page);
  return page;
}

export async function rolesPermissionsPage(): Promise<HTMLElement> {
  if (appStore.state.user?.role_id !== "superadmin") return forbiddenPage("Roles & Permissions");
  const page = pageScaffold("Management", "Roles & Permissions", "Configure global role capabilities independently from user accounts and customer scope.", '<button class="button button-primary" type="button" id="add-role"><i data-lucide="plus"></i>Add Role</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  let availablePermissions: string[] = [];
  const load = async () => {
    const result = await adminApi.roles();
    const order = ["superadmin", "admin", "manager_sales_admin", "manager", "user"];
    const roles = [...result.items].sort((a, b) => order.indexOf(String(a._id)) - order.indexOf(String(b._id)));
    const allPermissions = [...new Set(roles.flatMap((role) => (role.permissions as unknown[] | undefined)?.map(String) ?? []))].sort();
    availablePermissions = allPermissions;
    body.innerHTML = `<div class="access-summary panel"><div><span class="eyebrow">Role hierarchy</span><h2>SUPERADMIN → ADMIN → MANAGER → USER</h2><p>A user has one role; the role owns permissions. Customer assignments remain a separate scope.</p></div></div><div class="role-management-grid">${roles.map((role) => { const permissions = (role.permissions as unknown[] | undefined)?.map(String) ?? []; return `<article class="panel role-management-card"><div class="section-title"><div><span class="eyebrow">${escapeHtml(String(role._id))}</span><h2>${escapeHtml(String(role.display_name ?? role._id))}</h2></div><button class="button button-secondary" type="button" data-edit-role="${escapeHtml(String(role._id))}"><i data-lucide="sliders-horizontal"></i>Edit permissions</button></div><p>${permissions.length} server-side permissions</p><div class="permission-chip-list">${permissions.slice(0, 12).map((permission) => `<span>${escapeHtml(permission)}</span>`).join("")}${permissions.length > 12 ? `<span>+${permissions.length - 12} more</span>` : ""}</div></article>`; }).join("")}</div>`;
    roles.forEach((role, index) => { const system = role.system === true || ["superadmin", "admin", "manager", "manager_sales_admin", "user"].includes(String(role._id)); const card = body.querySelectorAll<HTMLElement>(".role-management-card")[index]; if (!system && card) { const button = document.createElement("button"); button.className = "button button-danger"; button.type = "button"; button.textContent = "Delete role"; button.addEventListener("click", () => { const content = document.createElement("div"); const assigned = Number(role.assigned_user_count ?? 0); content.innerHTML = `<div class="confirmation-copy"><p>Delete <strong>${escapeHtml(String(role.display_name ?? role._id))}</strong>?</p>${assigned ? `<div class="notice warning">This role is assigned to ${assigned} user${assigned === 1 ? "" : "s"}. Reassign those users before deleting the role.</div>` : "<p>This cannot be undone.</p>"}<small class="field-error" data-delete-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-danger" type="button" data-confirm-delete ${assigned ? "disabled" : ""}>Delete role</button></div></div>`; const dialog = openModal("Delete role", content); content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close()); content.querySelector<HTMLButtonElement>("[data-confirm-delete]")?.addEventListener("click", async (event) => { const action = event.currentTarget as HTMLButtonElement; action.disabled = true; action.textContent = "Deleting…"; try { await adminApi.deleteRole(String(role._id)); dialog.close(); toast("Role deleted"); await load(); } catch (error) { action.disabled = false; action.textContent = "Delete role"; const node = content.querySelector<HTMLElement>("[data-delete-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Role could not be deleted"; } }); }); card.querySelector(".section-title")?.append(button); } });
    body.querySelectorAll<HTMLButtonElement>("[data-edit-role]").forEach((button) => button.addEventListener("click", () => {
      const role = roles.find((item) => String(item._id) === String(button.dataset.editRole));
      if (!role) return;
      const selected = new Set((role.permissions as unknown[] | undefined)?.map(String) ?? []);
      const content = document.createElement("div");
      content.innerHTML = `<form class="role-permission-editor"><p class="form-hint">Changes apply to every user assigned to ${escapeHtml(String(role.display_name ?? role._id))}. Backend authorization remains authoritative.</p>${permissionSelector(allPermissions, selected)}<small class="field-error" data-role-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">Save role</button></div></form>`;
      const dialog = openModal(`Edit ${String(role.display_name ?? role._id)}`, content, "wide");
      dialog.classList.add("role-permissions-dialog");
      wirePermissionSelector(content);
      content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
      content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget as HTMLFormElement;
        const permissions = [...content.querySelectorAll<HTMLInputElement>('input[name="permissions"]:checked')].map((input) => input.value);
        const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]'); if (submit) { submit.disabled = true; submit.textContent = "Saving…"; }
        try { await adminApi.updateRole(String(role._id), { permissions }); dialog.close(); toast("Role permissions updated"); await load(); }
        catch (error) { if (submit) { submit.disabled = false; submit.textContent = "Save role"; } const node = content.querySelector<HTMLElement>("[data-role-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Role could not be updated"; }
      });
      refreshIcons(content);
    }));
    refreshIcons(body);
  };
  page.querySelector<HTMLButtonElement>("#add-role")?.addEventListener("click", () => {
    const content = document.createElement("div");
    content.innerHTML = `<form class="role-permission-editor create-role-editor"><div class="role-fields"><label>Role name<input name="key" required pattern="[a-z][a-z0-9_-]{2,63}" placeholder="sales_operations"><small>Lowercase letters, numbers, underscores or hyphens.</small></label><label>Display name<input name="display_name" required maxlength="100" placeholder="Sales Operations"></label><label class="role-description">Description<textarea name="description" maxlength="500"></textarea></label></div>${permissionSelector(availablePermissions, new Set())}<small class="field-error" data-role-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">Create role</button></div></form>`;
    const dialog = openModal("Add custom role", content, "wide");
    dialog.classList.add("role-permissions-dialog");
    wirePermissionSelector(content);
    content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
    content.querySelector("form")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data: Record<string, unknown> = Object.fromEntries(new FormData(form).entries()); data.permissions = [...content.querySelectorAll<HTMLInputElement>('input[name="permissions"]:checked')].map((input) => input.value); const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]'); if (submit) { submit.disabled = true; submit.textContent = "Creating…"; } try { await adminApi.createRole(data); dialog.close(); toast("Role created"); await load(); } catch (error) { if (submit) { submit.disabled = false; submit.textContent = "Create role"; } const node = content.querySelector<HTMLElement>("[data-role-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Role could not be created"; } });
    refreshIcons(content);
  });
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Roles unavailable")}</div>`; }
  refreshIcons(page);
  return page;
}

export async function customerAssignmentsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "Customer Assignments", "Manage Manager → User and Manager/User → Customer scope relationships.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(5);
  const load = async () => {
    const [users, customers] = await Promise.all([adminApi.users(), customerCompanyApi.list()]);
    const requestedUser = new URLSearchParams(location.search).get("user_id");
    const managers = users.items.filter((user) => ["manager", "manager_sales_admin"].includes(String(user.role_id)));
    body.innerHTML = users.items.length ? `<div class="data-table panel"><table><thead><tr><th>User</th><th>Role</th><th>Manager</th><th>Assigned customers</th><th>Status</th><th>Actions</th></tr></thead><tbody>${users.items.map((user) => { const assigned = (user.assigned_customer_ids as unknown[] | undefined)?.map(String) ?? []; return `<tr class="${requestedUser === String(user._id) ? "is-highlighted" : ""}"><td><strong>${escapeHtml(String(user.name ?? "User"))}</strong><small>${escapeHtml(String(user.email ?? ""))}</small></td><td>${escapeHtml(String(user.role_id ?? "user"))}</td><td>${escapeHtml(String((user.manager as Row | undefined)?.name ?? "—"))}</td><td>${user.customer_access_global === true ? "All customers" : `${assigned.length} assigned`}</td><td>${statusBadge(user.active === false ? "Inactive" : "Active")}</td><td><button class="button button-secondary" type="button" data-edit-assignment="${escapeHtml(String(user._id))}">Manage assignments</button></td></tr>`; }).join("")}</tbody></table></div>` : emptyState("building-2", "No users", "Create a user before assigning customers.");
    body.querySelectorAll<HTMLButtonElement>("[data-edit-assignment]").forEach((button) => button.addEventListener("click", () => {
      const user = users.items.find((item) => String(item._id) === String(button.dataset.editAssignment));
      if (!user) return;
      const assigned = new Set((user.assigned_customer_ids as unknown[] | undefined)?.map(String) ?? []);
      const content = document.createElement("div");
      const managerField = String(user.role_id) === "user" ? `<label>Manager<select name="manager_id"><option value="">No manager</option>${managers.map((manager) => `<option value="${escapeHtml(String(manager._id))}" ${String(user.manager_id ?? "") === String(manager._id) ? "selected" : ""}>${escapeHtml(String(manager.name ?? manager.email))}</option>`).join("")}</select></label>` : "";
      content.innerHTML = `<form class="assignment-editor">${managerField}<label class="assignment-search-label">Search customers<span class="assignment-search"><i data-lucide="search"></i><input type="search" data-assignment-search placeholder="Search by name, code or country"></span></label><div class="assignment-list-head"><strong data-assignment-count></strong><div><button class="text-button" type="button" data-assignment-visible="select">Select all visible</button><button class="text-button" type="button" data-assignment-visible="clear">Clear visible</button></div></div><div class="assignment-customer-list">${customers.items.map((customer) => { const row = customer as unknown as Row; const name = String(customer.name ?? customer.company_name ?? customer._id); const details = [row.customer_code, customer.country].filter(Boolean).map(String).join(" · "); return `<label class="assignment-customer-row" data-customer-search="${escapeHtml(`${name} ${row.customer_code ?? ""} ${customer.country ?? ""}`.toLowerCase())}"><input type="checkbox" value="${escapeHtml(String(customer._id))}" ${assigned.has(String(customer._id)) ? "checked" : ""}><span><strong>${escapeHtml(name)}</strong>${details ? `<small>${escapeHtml(details)}</small>` : ""}</span></label>`; }).join("")}<div class="assignment-empty" data-assignment-empty hidden>No customers found</div></div><small class="field-error" data-assignment-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">Save assignments</button></div></form>`;
      const dialog = openModal(`Assignments · ${String(user.name ?? "User")}`, content, "wide");
      dialog.classList.add("assignment-dialog");
      const assignmentRows = [...content.querySelectorAll<HTMLElement>("[data-customer-search]")];
      const updateAssignmentState = () => { const selectedCount = assignmentRows.filter((row) => row.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked).length; const count = content.querySelector<HTMLElement>("[data-assignment-count]"); if (count) count.textContent = `${selectedCount} of ${assignmentRows.length} selected`; assignmentRows.forEach((row) => row.classList.toggle("is-selected", Boolean(row.querySelector<HTMLInputElement>('input[type="checkbox"]')?.checked))); const empty = content.querySelector<HTMLElement>("[data-assignment-empty]"); if (empty) empty.hidden = assignmentRows.some((row) => !row.hidden); };
      content.querySelector<HTMLInputElement>("[data-assignment-search]")?.addEventListener("input", (event) => { const value = (event.currentTarget as HTMLInputElement).value.toLowerCase().trim(); assignmentRows.forEach((row) => { row.hidden = Boolean(value && !String(row.dataset.customerSearch).includes(value)); }); updateAssignmentState(); });
      content.querySelectorAll<HTMLButtonElement>("[data-assignment-visible]").forEach((action) => action.addEventListener("click", () => { assignmentRows.filter((row) => !row.hidden).forEach((row) => { const input = row.querySelector<HTMLInputElement>('input[type="checkbox"]'); if (input) input.checked = action.dataset.assignmentVisible === "select"; }); updateAssignmentState(); }));
      assignmentRows.forEach((row) => row.querySelector<HTMLInputElement>('input[type="checkbox"]')?.addEventListener("change", updateAssignmentState));
      updateAssignmentState();
      content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
      content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget as HTMLFormElement;
        const customerIds = [...content.querySelectorAll<HTMLInputElement>('.assignment-customer-list input:checked')].map((input) => input.value);
        const managerId = new FormData(form).get("manager_id");
        const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]'); if (submit) { submit.disabled = true; submit.textContent = "Saving…"; }
        try { await adminApi.updateUser(String(user._id), { customer_ids: customerIds, ...(String(user.role_id) === "user" ? { manager_id: managerId || null } : {}) }); dialog.close(); toast("Customer assignments updated"); await load(); }
        catch (error) { if (submit) { submit.disabled = false; submit.textContent = "Save assignments"; } const node = content.querySelector<HTMLElement>("[data-assignment-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Assignments could not be updated"; }
      });
      refreshIcons(content);
    }));
    refreshIcons(body);
  };
  try { await load(); } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Assignments unavailable")}</div>`; }
  refreshIcons(page);
  return page;
}

function maskedIp(value: unknown): string {
  const text = String(value ?? "");
  if (!text) return "—";
  if (text.includes(":")) return `${text.split(":").slice(0, 3).join(":")}:…`;
  const parts = text.split(".");
  return parts.length === 4 ? `${parts[0]}.${parts[1]}.x.x` : "Masked";
}

export async function legacyActivityPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "Activity", "Audit events for user, security and business-resource actions.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  try {
    const result = await adminApi.auditLogs();
    body.innerHTML = result.items.length ? `<div class="data-table panel"><table><thead><tr><th>Timestamp</th><th>User</th><th>Action</th><th>Resource</th><th>Result</th><th>IP</th></tr></thead><tbody>${result.items.map((event) => { const metadata = (event.metadata as Row | undefined) ?? {}; return `<tr><td>${formatDate(String(event.created_at ?? event.timestamp ?? ""))}</td><td>${escapeHtml(String(event.actor_name ?? event.user_name ?? event.actor_id ?? event.user_id ?? "System"))}</td><td><strong>${escapeHtml(String(event.action ?? "Event"))}</strong></td><td>${escapeHtml(String(event.entity_type ?? event.resource_type ?? "—"))}${event.entity_id ? `<small>${escapeHtml(String(event.entity_id))}</small>` : ""}</td><td>${statusBadge(String(event.status ?? event.result ?? "RECORDED"))}</td><td>${escapeHtml(maskedIp(event.ip_address ?? metadata.ip_address))}</td></tr>`; }).join("")}</tbody></table></div>` : emptyState("history", "No activity events", "Audited actions will appear here.");
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Activity unavailable")}</div>`; }
  refreshIcons(page);
  return page;
}

export async function legacyLoginDevicesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "Login Devices", "Review trusted devices, login approvals and session status separately from audit activity.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  try {
    const users = await adminApi.users();
    const requestedUser = new URLSearchParams(location.search).get("user_id");
    const selectedUsers = requestedUser ? users.items.filter((user) => String(user._id) === requestedUser) : users.items;
    const results = await Promise.allSettled(selectedUsers.map(async (user) => ({ user, devices: (await adminApi.userDevices(String(user._id))).items })));
    const rows = results.flatMap((result) => result.status === "fulfilled" ? result.value.devices.map((device) => ({ user: result.value.user, device })) : []);
    body.innerHTML = rows.length ? `<div class="data-table panel"><table><thead><tr><th>User</th><th>Device</th><th>Platform</th><th>Location</th><th>First seen</th><th>Last seen</th><th>Status</th><th>Actions</th></tr></thead><tbody>${rows.map(({ user, device }) => `<tr><td><strong>${escapeHtml(String(user.name ?? user.email))}</strong><small>${escapeHtml(String(user.email ?? ""))}</small></td><td>${escapeHtml(String(device.browser_name ?? device.browser ?? "Browser"))}</td><td>${escapeHtml(String(device.os_name ?? device.operating_system ?? device.device_type ?? "Unknown"))}</td><td>${escapeHtml(String((device.location as Row | undefined)?.label ?? device.location_label ?? "Approx. unavailable"))}</td><td>${formatDate(String(device.registered_at ?? device.first_seen_at ?? ""))}</td><td>${formatDate(String(device.last_seen_at ?? device.last_login_at ?? ""))}</td><td>${statusBadge(String(device.device_status ?? "UNKNOWN"))}</td><td><div class="table-actions">${String(device.device_status) === "pending" ? `<button class="button button-primary" data-device-action="approve" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Approve</button><button class="button button-danger" data-device-action="reject" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Reject</button>` : String(device.device_status) === "approved" ? `<button class="button button-secondary" data-device-action="revoke" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Revoke</button>` : ""}</div></td></tr>`).join("")}</tbody></table></div>` : emptyState("monitor-smartphone", "No login devices", "Trusted and pending login devices will appear here.");
    body.querySelectorAll<HTMLButtonElement>("[data-device-action]").forEach((button) => button.addEventListener("click", async () => {
      const action = String(button.dataset.deviceAction); const userId = String(button.dataset.user); const deviceId = String(button.dataset.device);
      const reason = action === "approve" ? "" : window.prompt(`${action === "reject" ? "Rejection" : "Revocation"} reason:`, "")?.trim() ?? "";
      if (action !== "approve" && !reason) return;
      button.disabled = true;
      try { if (action === "approve") await adminApi.approveDevice(userId, deviceId); else if (action === "reject") await adminApi.rejectDevice(userId, deviceId, reason); else await adminApi.revokeDevice(userId, deviceId, reason); toast("Device status updated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: location.pathname + location.search })); }
      catch (error) { toast(error instanceof Error ? error.message : "Device could not be updated", "error"); button.disabled = false; }
    }));
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Login devices unavailable")}</div>`; }
  refreshIcons(page);
  return page;
}

function relativeActivity(value: unknown): string {
  const date = new Date(String(value ?? ""));
  if (Number.isNaN(date.getTime())) return "Never";
  const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
  return `${Math.floor(seconds / 86400)} days ago`;
}

function deviceLocation(device: Row): string {
  const location = device.location as Row | undefined;
  return String(location?.label ?? device.location_label ?? "Location unavailable").replace(/^Approx\.\s*/i, "");
}

function deviceClient(device: Row): string {
  const browser = String(device.browser_name ?? device.browser ?? "Unknown browser");
  const version = String(device.browser_version ?? "").trim();
  return `${browser}${version ? ` ${version}` : ""}`;
}

export async function activityPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "Activity", "Meaningful audit events for user, security and business-resource actions.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  try {
    const users = await adminApi.users();
    let requestSequence = 0;
    const render = async (filters: Record<string, string> = {}) => {
      const requestId = ++requestSequence;
      filters.page = filters.page || "1";
      filters.page_size = filters.page_size || "25";
      const currentTable = body.querySelector<HTMLElement>(".activity-table");
      currentTable?.classList.add("is-loading");
      currentTable?.setAttribute("aria-busy", "true");
      body.querySelectorAll<HTMLButtonElement>(".activity-pagination button").forEach((button) => { button.disabled = true; });
      const result = await adminApi.auditLogs(filters);
      if (requestId !== requestSequence) return;
      const actions = [...new Set(result.items.map((item) => String(item.action ?? "")).filter(Boolean))].sort();
      const resources = [...new Set(result.items.map((item) => String(item.entity_type ?? item.resource_type ?? "")).filter(Boolean))].sort();
      const options = (values: string[], selected = "") => values.map((value) => `<option value="${escapeHtml(value)}" ${selected === value ? "selected" : ""}>${escapeHtml(value)}</option>`).join("");
      const rows = result.items.map((event) => {
        const metadata = (event.metadata as Row | undefined) ?? {};
        const id = String(event.entity_id ?? "");
        const actor = users.items.find((user) => String(user._id) === String(event.user_id ?? event.actor_id));
        return `<tr><td>${formatDate(String(event.created_at ?? event.timestamp ?? ""))}</td><td><strong>${escapeHtml(String(actor?.name ?? event.actor_name ?? event.user_name ?? event.user_id ?? "System"))}</strong>${actor?.email ? `<small>${escapeHtml(String(actor.email))}</small>` : ""}</td><td><strong class="audit-action">${escapeHtml(String(event.action ?? "Event"))}</strong></td><td><strong>${escapeHtml(String(event.entity_type ?? event.resource_type ?? "Resource"))}</strong>${id ? `<small title="${escapeHtml(id)}">ID: ${escapeHtml(id.length > 18 ? `${id.slice(0, 18)}…` : id)}</small>` : ""}</td><td>${statusBadge(String(event.status ?? event.result ?? "RECORDED"))}</td><td>${escapeHtml(maskedIp(event.ip_address ?? metadata.ip_address))}</td></tr>`;
      }).join("");
      body.innerHTML = `<section class="panel activity-filter-panel"><form class="activity-filters"><label>User<select name="user"><option value="">All users</option>${users.items.map((user) => `<option value="${escapeHtml(String(user._id))}" ${filters.user === String(user._id) ? "selected" : ""}>${escapeHtml(String(user.name ?? user.email))}</option>`).join("")}</select></label><label>Action<select name="action"><option value="">All actions</option>${options(actions, filters.action)}</select></label><label>Resource<select name="resource"><option value="">All resources</option>${options(resources, filters.resource)}</select></label><label>Result<select name="result"><option value="">All results</option><option value="RECORDED" ${filters.result === "RECORDED" ? "selected" : ""}>Recorded</option></select></label><label>From date<input type="date" name="from_date" value="${escapeHtml(filters.from_date ?? "")}"></label><label>To date<input type="date" name="to_date" value="${escapeHtml(filters.to_date ?? "")}"></label><label class="activity-search">Search<input type="search" name="search" placeholder="Action, resource, ID or IP" value="${escapeHtml(filters.search ?? "")}"></label><label>Per page<select name="page_size"><option value="25" ${filters.page_size === "25" ? "selected" : ""}>25</option><option value="50" ${filters.page_size === "50" ? "selected" : ""}>50</option><option value="100" ${filters.page_size === "100" ? "selected" : ""}>100</option></select></label><div class="activity-filter-actions"><button class="button button-secondary" type="reset">Reset</button><button class="button button-primary" type="submit">Apply filters</button></div></form></section><div class="activity-result-count">${result.total} audit event${result.total === 1 ? "" : "s"}</div>${rows ? `<div class="data-table panel activity-table"><table><thead><tr><th>Timestamp</th><th>User</th><th>Action</th><th>Resource</th><th>Result</th><th>IP</th></tr></thead><tbody>${rows}</tbody></table></div><div class="activity-pagination"></div>` : emptyState("history", "No audit events found", "Try changing your filters or search criteria.")}`;
      const pagination = body.querySelector<HTMLElement>(".activity-pagination");
      if (pagination) {
        const pageSize = Number(result.page_size || filters.page_size || 25);
        const totalPages = Math.max(1, Number(result.pages) || Math.ceil(result.total / pageSize));
        const currentPage = Math.min(Math.max(Number(result.page) || 1, 1), totalPages);
        const start = result.total ? (currentPage - 1) * pageSize + 1 : 0;
        const end = result.total ? Math.min(currentPage * pageSize, result.total) : 0;
        const pages = Array.from({ length: totalPages }, (_, index) => index + 1).filter((value) => totalPages <= 7 || value === 1 || value === totalPages || Math.abs(value - currentPage) <= 1);
        let last = 0;
        const pageButtons = pages.map((value) => { const gap = value - last > 1 ? '<span class="pagination-ellipsis">…</span>' : ''; last = value; return `${gap}<button type="button" class="button pagination-page${value === currentPage ? " is-current" : ""}" data-page="${value}" aria-label="Page ${value}" ${value === currentPage ? 'aria-current="page"' : ""}>${value}</button>`; }).join("");
        pagination.innerHTML = `<span>Showing ${start}–${end} of ${result.total} · Page ${currentPage} of ${totalPages}</span><nav class="pagination-controls" aria-label="Audit log pages"><button type="button" class="button button-secondary" data-page="${currentPage - 1}" ${currentPage <= 1 ? "disabled" : ""} aria-label="Previous page">Previous</button>${pageButtons}<button type="button" class="button button-secondary" data-page="${currentPage + 1}" ${currentPage >= totalPages ? "disabled" : ""} aria-label="Next page">Next</button><label class="pagination-jump">Go to page <input type="number" min="1" max="${totalPages}" value="${currentPage}" data-page-jump aria-label="Go to page"><button type="button" class="button button-secondary" data-page-go>Go</button></label></nav>`;
        pagination.querySelectorAll<HTMLButtonElement>("[data-page]").forEach((button) => button.addEventListener("click", () => { const target = Number(button.dataset.page); if (target >= 1 && target <= totalPages && target !== currentPage) void render({ ...filters, page: String(target) }); }));
        const jump = pagination.querySelector<HTMLInputElement>("[data-page-jump]");
        const go = () => { const target = Math.min(Math.max(Number(jump?.value) || 1, 1), totalPages); if (jump) jump.value = String(target); if (target !== currentPage) void render({ ...filters, page: String(target) }); };
        pagination.querySelector("[data-page-go]")?.addEventListener("click", go);
        jump?.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); go(); } });
      }
      const form = body.querySelector<HTMLFormElement>(".activity-filters")!;
      form.addEventListener("submit", (event) => { event.preventDefault(); const values = Object.fromEntries([...new FormData(form).entries()].map(([key, value]) => [key, String(value)])); void render(values); });
      form.addEventListener("reset", () => setTimeout(() => void render(), 0));
      refreshIcons(body);
    };
    await render();
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Activity unavailable")}</div>`; }
  refreshIcons(page);
  return page;
}

export async function loginDevicesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "Login Devices", "Manage trusted devices, active sessions and login approvals. Trust and online activity are shown separately.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(6);
  try {
    const users = await adminApi.users();
    const requestedUser = new URLSearchParams(location.search).get("user_id");
    const selectedUsers = requestedUser ? users.items.filter((user) => String(user._id) === requestedUser) : users.items;
    const results = await Promise.allSettled(selectedUsers.map(async (user) => ({ user, devices: (await adminApi.userDevices(String(user._id))).items })));
    const rows = results.flatMap((result) => result.status === "fulfilled" ? result.value.devices.map((device) => ({ user: result.value.user, device })) : []);
    const counts = rows.reduce((total, { device }) => { const presence = String(device.presence_status ?? "offline"); const trust = String(device.device_status ?? "").toLowerCase(); if (presence === "online") total.online++; else if (presence === "recently_active") total.recent++; else total.offline++; if (trust === "approved") total.trusted++; if (trust === "pending") total.pending++; if (["revoked", "denied"].includes(trust)) total.revoked++; return total; }, { online: 0, recent: 0, offline: 0, trusted: 0, pending: 0, revoked: 0 });
    const summaries = [["Active now", counts.online, "circle-dot", "Currently online", "online"], ["Recently active", counts.recent, "clock-3", "Seen recently", "recent"], ["Offline", counts.offline, "cloud-off", "No recent activity", "offline"], ["Trusted devices", counts.trusted, "shield-check", "Approved access", "trusted"], ["Pending approval", counts.pending, "shield-alert", "Awaiting review", "pending"], ["Revoked", counts.revoked, "shield-x", "Access blocked", "revoked"]] as const;
    const tableRows = rows.map(({ user, device }) => {
      const trust = String(device.device_status ?? "unknown").toLowerCase();
      const presence = String(device.presence_status ?? "offline");
      const current = device.current_session === true;
      const presenceLabel = presence === "recently_active" ? "Recently active" : presence.charAt(0).toUpperCase() + presence.slice(1);
      const trustLabel = trust === "approved" ? "Trusted" : trust === "denied" ? "Blocked" : trust.charAt(0).toUpperCase() + trust.slice(1);
      const actions = trust === "pending" ? `<button class="button button-primary" data-device-action="approve" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Approve</button><button class="button button-danger" data-device-action="reject" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Reject</button>` : trust === "approved" && !current ? `<button class="button button-secondary" data-device-action="revoke" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Revoke</button>` : trust === "revoked" ? `<button class="button button-danger" data-device-action="delete" data-user="${escapeHtml(String(user._id))}" data-device="${escapeHtml(String(device.device_id))}">Delete</button>` : current ? '<button class="button button-secondary" disabled title="The current session cannot be revoked here">Current</button>' : "";
      return `<tr><td><strong>${escapeHtml(String(user.name ?? user.email))}</strong><small>${escapeHtml(String(user.email ?? ""))}</small></td><td><strong>${escapeHtml(deviceClient(device))}</strong>${current ? '<small class="current-device-label">This device</small>' : ""}</td><td>${escapeHtml(String(device.os_name ?? device.operating_system ?? "Unknown OS"))}${device.os_version ? `<small>${escapeHtml(String(device.os_version))}</small>` : ""}</td><td><span class="device-location"><i data-lucide="map-pin"></i>${escapeHtml(deviceLocation(device))}</span></td><td>${formatDate(String(device.registered_at ?? device.first_seen_at ?? ""))}</td><td><strong>${escapeHtml(relativeActivity(device.last_seen_at ?? device.last_activity_at ?? device.last_login_at))}</strong><small>${formatDate(String(device.last_seen_at ?? device.last_activity_at ?? device.last_login_at ?? ""))}</small></td><td><span class="presence-badge presence-${escapeHtml(presence)}"><i></i>${escapeHtml(presenceLabel)}</span></td><td><span class="trust-badge trust-${escapeHtml(trust)}">${escapeHtml(trustLabel)}</span></td><td><div class="table-actions">${actions}</div></td></tr>`;
    }).join("");
    body.innerHTML = `<div class="device-summary-grid">${summaries.map(([label, value, icon, copy, kind]) => `<article class="panel device-summary-card is-${kind}"><i data-lucide="${icon}"></i><div><span>${label}</span><strong>${value}</strong><small>${copy}</small></div></article>`).join("")}</div>${tableRows ? `<div class="data-table panel login-device-table"><table><thead><tr><th>User</th><th>Device / Browser</th><th>Platform</th><th>Location</th><th>First seen</th><th>Last seen</th><th>Presence</th><th>Trust status</th><th>Actions</th></tr></thead><tbody>${tableRows}</tbody></table></div>` : emptyState("monitor-smartphone", "No login devices", "Trusted and pending login devices will appear here.")}`;
    body.querySelectorAll<HTMLButtonElement>("[data-device-action]").forEach((button) => button.addEventListener("click", async () => {
      const action = String(button.dataset.deviceAction); const userId = String(button.dataset.user); const deviceId = String(button.dataset.device);
      let reason = action === "approve" ? "" : action === "delete" ? "Revoked device cleanup" : window.prompt(`${action === "reject" ? "Rejection" : "Revocation"} reason:`, "")?.trim() ?? "";
      if (action === "delete") {
        const confirmation = document.createElement("div");
        confirmation.innerHTML = '<p>This permanently removes this revoked device record. This action cannot be undone.</p><div class="modal-actions"><button type="button" class="button button-secondary" data-cancel>Cancel</button><button type="button" class="button button-danger" data-confirm>Delete device</button></div>';
        const confirmationDialog = openModal("Delete revoked device?", confirmation);
        const confirmed = await new Promise<boolean>((resolve) => { confirmation.querySelector("[data-cancel]")?.addEventListener("click", () => { confirmationDialog.close(); resolve(false); }); confirmation.querySelector("[data-confirm]")?.addEventListener("click", () => { confirmationDialog.close(); resolve(true); }); });
        if (!confirmed) { button.disabled = false; return; }
      }
      if (action !== "approve" && !reason) return;
      button.disabled = true;
      try { if (action === "approve") await adminApi.approveDevice(userId, deviceId); else if (action === "reject") await adminApi.rejectDevice(userId, deviceId, reason); else if (action === "delete") await adminApi.deleteDevice(userId, deviceId, reason); else await adminApi.revokeDevice(userId, deviceId, reason); toast(action === "delete" ? "Revoked device deleted" : "Device status updated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: location.pathname + location.search })); }
      catch (error) { toast(error instanceof Error ? error.message : "Device could not be updated", "error"); button.disabled = false; }
    }));
  } catch (error) { body.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Login devices unavailable")}</div>`; }
  refreshIcons(page);
  return page;
}
