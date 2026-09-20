import { adminApi, customerCompanyApi } from "../api";
import { refreshIcons } from "../components/icons";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, formatDate, skeleton } from "../utils/dom";

type Row = Record<string, unknown>;

function forbiddenPage(title: string): HTMLElement {
  const page = pageScaffold("Management", title, "This workspace is restricted by server-authoritative access control.");
  page.querySelector<HTMLElement>(".page-body")!.innerHTML = '<div class="notice error"><i data-lucide="shield-x"></i><div><strong>Superadmin access required</strong><p>Role and permission definitions can only be viewed or changed by a Superadmin.</p></div></div>';
  refreshIcons(page);
  return page;
}

export async function rolesPermissionsPage(): Promise<HTMLElement> {
  if (appStore.state.user?.role_id !== "superadmin") return forbiddenPage("Roles & Permissions");
  const page = pageScaffold("Management", "Roles & Permissions", "Configure global role capabilities independently from user accounts and customer scope.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  body.innerHTML = skeleton(4);
  const load = async () => {
    const result = await adminApi.roles();
    const order = ["superadmin", "admin", "manager_sales_admin", "manager", "user"];
    const roles = [...result.items].sort((a, b) => order.indexOf(String(a._id)) - order.indexOf(String(b._id)));
    const allPermissions = [...new Set(roles.flatMap((role) => (role.permissions as unknown[] | undefined)?.map(String) ?? []))].sort();
    body.innerHTML = `<div class="access-summary panel"><div><span class="eyebrow">Role hierarchy</span><h2>SUPERADMIN → ADMIN → MANAGER → USER</h2><p>A user has one role; the role owns permissions. Customer assignments remain a separate scope.</p></div></div><div class="role-management-grid">${roles.map((role) => { const permissions = (role.permissions as unknown[] | undefined)?.map(String) ?? []; return `<article class="panel role-management-card"><div class="section-title"><div><span class="eyebrow">${escapeHtml(String(role._id))}</span><h2>${escapeHtml(String(role.display_name ?? role._id))}</h2></div><button class="button button-secondary" type="button" data-edit-role="${escapeHtml(String(role._id))}"><i data-lucide="sliders-horizontal"></i>Edit permissions</button></div><p>${permissions.length} server-side permissions</p><div class="permission-chip-list">${permissions.slice(0, 12).map((permission) => `<span>${escapeHtml(permission)}</span>`).join("")}${permissions.length > 12 ? `<span>+${permissions.length - 12} more</span>` : ""}</div></article>`; }).join("")}</div>`;
    body.querySelectorAll<HTMLButtonElement>("[data-edit-role]").forEach((button) => button.addEventListener("click", () => {
      const role = roles.find((item) => String(item._id) === String(button.dataset.editRole));
      if (!role) return;
      const selected = new Set((role.permissions as unknown[] | undefined)?.map(String) ?? []);
      const content = document.createElement("div");
      content.innerHTML = `<form class="stack-form role-permission-editor"><p class="form-hint">Changes apply to every user assigned to ${escapeHtml(String(role.display_name ?? role._id))}. Backend authorization remains authoritative.</p><div class="permission-editor-list">${allPermissions.map((permission) => `<label class="check-row"><input type="checkbox" value="${escapeHtml(permission)}" ${selected.has(permission) ? "checked" : ""}><span>${escapeHtml(permission)}</span></label>`).join("")}</div><small class="field-error" data-role-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">Save role</button></div></form>`;
      const dialog = openModal(`Edit ${String(role.display_name ?? role._id)}`, content, "wide");
      content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
      content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const permissions = [...content.querySelectorAll<HTMLInputElement>('input[type="checkbox"]:checked')].map((input) => input.value);
        try { await adminApi.updateRole(String(role._id), { permissions }); dialog.close(); toast("Role permissions updated"); await load(); }
        catch (error) { const node = content.querySelector<HTMLElement>("[data-role-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Role could not be updated"; }
      });
      refreshIcons(content);
    }));
    refreshIcons(body);
  };
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
      content.innerHTML = `<form class="stack-form assignment-editor">${managerField}<label>Search customers<input type="search" data-assignment-search placeholder="Name, code or country"></label><div class="assignment-customer-list">${customers.items.map((customer) => { const row = customer as unknown as Row; return `<label class="check-row" data-customer-search="${escapeHtml(`${customer.name ?? ""} ${row.customer_code ?? ""} ${customer.country ?? ""}`.toLowerCase())}"><input type="checkbox" value="${escapeHtml(String(customer._id))}" ${assigned.has(String(customer._id)) ? "checked" : ""}><span>${escapeHtml(String(customer.name ?? customer.company_name ?? customer._id))}</span></label>`; }).join("")}</div><small class="field-error" data-assignment-error></small><div class="modal-actions"><button class="button button-secondary" type="button" data-cancel>Cancel</button><button class="button button-primary" type="submit">Save assignments</button></div></form>`;
      const dialog = openModal(`Assignments · ${String(user.name ?? "User")}`, content, "wide");
      content.querySelector<HTMLInputElement>("[data-assignment-search]")?.addEventListener("input", (event) => { const value = (event.currentTarget as HTMLInputElement).value.toLowerCase().trim(); content.querySelectorAll<HTMLElement>("[data-customer-search]").forEach((row) => { row.hidden = Boolean(value && !String(row.dataset.customerSearch).includes(value)); }); });
      content.querySelector<HTMLButtonElement>("[data-cancel]")?.addEventListener("click", () => dialog.close());
      content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.currentTarget as HTMLFormElement;
        const customerIds = [...content.querySelectorAll<HTMLInputElement>('.assignment-customer-list input:checked')].map((input) => input.value);
        const managerId = new FormData(form).get("manager_id");
        try { await adminApi.updateUser(String(user._id), { customer_ids: customerIds, ...(String(user.role_id) === "user" ? { manager_id: managerId || null } : {}) }); dialog.close(); toast("Customer assignments updated"); await load(); }
        catch (error) { const node = content.querySelector<HTMLElement>("[data-assignment-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Assignments could not be updated"; }
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

export async function activityPage(): Promise<HTMLElement> {
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

export async function loginDevicesPage(): Promise<HTMLElement> {
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
