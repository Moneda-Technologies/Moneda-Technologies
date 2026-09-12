import { adminApi, authApi, companyApi, customerCompanyApi, profileApi, rateApi } from "../api";
import { apiEndpoint } from "../api/client";
import { logout } from "../auth/logout";
import { refreshIcons } from "../components/icons";
import { enhancePasswordFields } from "../components/password";
import { openModal } from "../components/modal";
import { pageScaffold, statusBadge } from "../components/page";
import { toast } from "../components/toast";
import { appStore } from "../state/store";
import { emptyState, escapeHtml, skeleton } from "../utils/dom";
import { pricingAdminPage } from "./pricing-management";

export async function companiesPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "Customer Directory", "Manage customer businesses, default currencies and commercial defaults.", '<a class="button button-primary" href="/customers" data-route="/customers"><i data-lucide="plus"></i>Add Customer</a>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(4);
  try { const data = await companyApi.list(); body.innerHTML = data.items.length ? `<div class="company-grid">${data.items.map((company) => `<article class="company-card"><div class="company-card-head"><span class="company-logo-mini">${escapeHtml(company.name.slice(0, 2).toUpperCase())}</span>${statusBadge(company.active ? "Active" : "Inactive")}</div><h3>${escapeHtml(company.name)}</h3><p>${escapeHtml(company.legal_name ?? "Legal name not configured")}</p><div class="company-details"><span><i data-lucide="map-pin"></i>${escapeHtml(company.country ?? "Region pending")}</span><span><i data-lucide="euro"></i>${company.default_currency} · EUR master</span><span><i data-lucide="badge-percent"></i>${company.default_tax_rate}% ${company.default_tax_mode}</span></div><a class="button button-quiet" href="/customers/${encodeURIComponent(company._id)}" data-route="/customers/${encodeURIComponent(company._id)}">View customer<i data-lucide="arrow-right"></i></a></article>`).join("")}</div>` : emptyState("building-2", "No customers configured", "Create a customer business to prepare a quotation."); }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Customers unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function usersPage(): Promise<HTMLElement> {
  const canInviteUsers = appStore.state.user?.role_id === "superadmin";
  const page = pageScaffold("Management", "Users & access", "Assign customer scope and permission-backed roles without email-based exceptions.", canInviteUsers ? '<button class="button button-primary" id="invite-user" type="button"><i data-lucide="user-plus"></i>Invite user</button>' : "");
  page.classList.add("users-access-page");
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  const actorRole = appStore.state.user?.role_id;
  // The server treats Admin and Superadmin as global customer-scope roles even
  // when an older session has not yet refreshed the derived permission list.
  // Keep the users.update check so the UI never advertises mutation access to
  // an actor who cannot call the protected update endpoint.
  const canManageCustomerAccess = appStore.can("users.update") && (appStore.can("customers.view_all") || actorRole === "admin" || actorRole === "superadmin");
  const openDevices = async (user: Record<string, unknown>) => {
    const content = document.createElement("div");
    content.innerHTML = '<div class="skeleton-stack"><div class="skeleton-line"></div><div class="skeleton-line"></div></div>';
    const dialog = openModal(`${String(user.name ?? "User")} · Trusted devices`, content, "wide");
    try {
      const result = await adminApi.userDevices(String(user._id));
      const historyText = (device: Record<string, unknown>) => {
        const history = (device.history as Record<string, unknown>[] | undefined) ?? [];
        const labels: Record<string, string> = { DEVICE_LOGIN_ATTEMPT: "Login attempt", DEVICE_APPROVAL_REQUESTED: "Approval requested", DEVICE_APPROVED: "Device approved", DEVICE_DENIED: "Device denied", DEVICE_REVOKED: "Device revoked", DEVICE_REINSTATED: "Device reinstated", DEVICE_DELETED: "Device deleted" };
        const dateText = (value: unknown) => { const date = value ? new Date(String(value)) : null; return date && !Number.isNaN(date.getTime()) ? new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date) : String(value ?? ""); };
        return history.slice(-6).reverse().map((entry) => `<small class="device-history-entry">${escapeHtml(labels[String(entry.action ?? "")] || String(entry.action ?? "Event"))} · ${escapeHtml(dateText(entry.timestamp))}${entry.reason ? ` · ${escapeHtml(String(entry.reason))}` : ""}</small>`).join("");
      };
      content.innerHTML = result.items.length ? `<div class="device-admin-list">${result.items.map((device) => { const status = String(device.device_status); const browser = String(device.browser || "Browser"); const operatingSystem = String(device.operating_system || "Unknown OS"); const deviceType = String(device.device_type || "Desktop"); const label = `${browser} · ${operatingSystem} · ${deviceType}`; const action = status === "pending" ? `<button class="button button-primary" data-device-action="approve">Approve</button><button class="button button-quiet" data-device-action="reject">Reject</button>` : status === "approved" ? `<button class="button button-quiet" data-device-action="revoke">Revoke</button>` : status === "denied" || status === "revoked" ? `<button class="button button-primary" data-device-action="reinstate">Reinstate</button>` : `<span class="muted">${escapeHtml(status)}</span>`; const statusLabel = status === "pending" && device.reinstated_at ? "Pending approval (reinstated)" : status; return `<article class="device-admin-row"><div class="device-admin-details"><strong>${escapeHtml(label)}</strong><small>Location: ${escapeHtml(String(device.location || "Unavailable"))}</small><small>Added: ${escapeHtml(String(device.registered_at ?? ""))}</small><small>Last activity: ${escapeHtml(String(device.last_seen_at ?? ""))}</small><span class="device-admin-status device-status-${escapeHtml(status)}">Status: ${escapeHtml(statusLabel)}</span>${status === "revoked" ? `<small>Revoked: ${escapeHtml(String(device.revoked_at ?? ""))}${device.revoked_by_name ? ` by ${escapeHtml(String(device.revoked_by_name))}` : ""}</small>${device.revoke_reason ? `<small>Reason: ${escapeHtml(String(device.revoke_reason))}</small>` : ""}` : ""}${status === "denied" ? `${device.denied_by_name ? `<small>Denied by: ${escapeHtml(String(device.denied_by_name))}</small>` : ""}${device.denial_reason ? `<small>Reason: ${escapeHtml(String(device.denial_reason))}</small>` : ""}` : ""}${device.reinstatement_reason ? `<small>Reinstated: ${escapeHtml(String(device.reinstated_at ?? ""))}${device.reinstated_by_name ? ` by ${escapeHtml(String(device.reinstated_by_name))}` : ""} · Reason: ${escapeHtml(String(device.reinstatement_reason))}</small>` : ""}${historyText(device)}</div><div>${action}</div><span hidden data-device-id="${escapeHtml(String(device.device_id ?? ""))}"></span></article>`; }).join("")}</div>` : '<p class="form-hint">No device requests for this user.</p>';
       const counts = result.items.reduce<{ total: number; approved: number; pending: number; denied: number; revoked: number }>((summary, device) => { const status = String(device.device_status ?? "").toLowerCase(); if (["approved", "pending", "denied", "revoked"].includes(status)) summary[status as "approved" | "pending" | "denied" | "revoked"] += 1; summary.total += 1; return summary; }, { total: 0, approved: 0, pending: 0, denied: 0, revoked: 0 });
       content.insertAdjacentHTML("afterbegin", `<div class="device-admin-summary"><strong>${counts.total} trusted device${counts.total === 1 ? "" : "s"}</strong><span>${counts.approved} approved · ${counts.pending} pending · ${counts.denied} denied · ${counts.revoked} revoked</span></div><div class="device-admin-filters"><input type="search" data-device-search placeholder="Search browser, OS or location" aria-label="Search trusted devices"><select data-device-status><option value="all">All statuses</option><option value="approved">Approved</option><option value="pending">Pending</option><option value="denied">Denied</option><option value="revoked">Revoked</option></select><select data-device-type><option value="all">All device types</option><option value="Desktop">Desktop</option><option value="Tablet">Tablet</option><option value="Mobile">Mobile</option></select></div>`);
       const renderedRows = [...content.querySelectorAll<HTMLElement>(".device-admin-row")];
       result.items.forEach((device, index) => {
         const row = renderedRows[index];
         const details = row?.querySelector<HTMLElement>(".device-admin-details");
         if (!row || !details) return;
         const location = device.location as Record<string, unknown> | undefined;
         const locationLabel = String(location?.label || [location?.city, location?.state, location?.country].filter(Boolean).join(", ") || "Approx. location unavailable");
         details.querySelectorAll("small").forEach((node) => { if (node.textContent?.startsWith("Location:")) node.remove(); });
         details.insertAdjacentHTML("afterbegin", `<small>Browser: ${escapeHtml(String(device.browser_name || device.browser || "Unknown"))}${device.browser_version ? ` ${escapeHtml(String(device.browser_version))}` : ""}</small><small>OS: ${escapeHtml(String(device.os_name || device.operating_system || "Unknown"))}${device.os_version ? ` ${escapeHtml(String(device.os_version))}` : ""}</small><small>Type: ${escapeHtml(String(device.device_type || "Desktop"))} · Location: ${escapeHtml(locationLabel)} (Approx.)</small>${device.public_ip ? `<small>Public IP: ${escapeHtml(String(device.public_ip))}</small>` : ""}${device.current_session ? '<span class="device-current-session">Current session</span>' : ""}${device.last_login_at ? `<small>Last login: ${escapeHtml(String(device.last_login_at))}${device.last_login_result ? ` · ${escapeHtml(String(device.last_login_result))}` : ""}</small>` : ""}`);
         row.dataset.deviceSearch = `${device.browser_name || device.browser || ""} ${device.os_name || device.operating_system || ""} ${device.device_type || ""} ${locationLabel}`.toLowerCase();
         row.dataset.deviceStatus = String(device.device_status || "").toLowerCase(); row.dataset.deviceType = String(device.device_type || "Desktop");
       });
       const applyDeviceFilters = () => { const term = (content.querySelector<HTMLInputElement>("[data-device-search]")?.value || "").toLowerCase().trim(); const status = content.querySelector<HTMLSelectElement>("[data-device-status]")?.value || "all"; const type = content.querySelector<HTMLSelectElement>("[data-device-type]")?.value || "all"; renderedRows.forEach((row) => { row.hidden = Boolean((term && !String(row.dataset.deviceSearch).includes(term)) || (status !== "all" && row.dataset.deviceStatus !== status) || (type !== "all" && row.dataset.deviceType !== type)); }); };
       content.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-device-search], [data-device-status], [data-device-type]").forEach((control) => control.addEventListener("input", applyDeviceFilters));
       content.querySelectorAll<HTMLSelectElement>("[data-device-status], [data-device-type]").forEach((control) => control.addEventListener("change", applyDeviceFilters));
      if (!result.items.length) content.innerHTML = '<section class="device-empty-state"><strong>No trusted devices</strong><span>No approved, pending, denied, or revoked devices are currently associated with this account.</span></section>';
      content.querySelectorAll<HTMLElement>(".device-admin-details").forEach((details) => {
        const history = [...details.querySelectorAll<HTMLElement>(".device-history-entry")];
        if (!history.length) return;
        const toggle = document.createElement("button"); toggle.type = "button"; toggle.className = "button button-quiet device-history-toggle"; toggle.textContent = "View history";
        toggle.addEventListener("click", () => { const show = history[0].style.display !== "block"; history.forEach((entry) => { entry.style.display = show ? "block" : "none"; }); toggle.textContent = show ? "Hide history" : "View history"; });
        details.append(toggle);
      });
      result.items.forEach((device, index) => {
        if (String(device.device_status) !== "denied") return;
        const actions = renderedRows[index]?.querySelector<HTMLElement>(":scope > div:nth-of-type(2)");
        if (!actions) return;
        const deleteButton = document.createElement("button"); deleteButton.className = "button button-danger"; deleteButton.type = "button"; deleteButton.dataset.deviceAction = "delete"; deleteButton.textContent = "Delete"; actions.append(deleteButton);
      });
      content.querySelectorAll<HTMLButtonElement>("[data-device-action]").forEach((button) => button.addEventListener("click", async () => {
        button.disabled = true;
        const row = button.closest<HTMLElement>(".device-admin-row");
        const deviceId = row?.querySelector<HTMLElement>("[data-device-id]")?.dataset.deviceId;
        if (!deviceId) return;
        const action = String(button.dataset.deviceAction);
        const deviceLabel = row?.querySelector("strong")?.textContent || "this device";
        if (!window.confirm(action === "delete" ? `Delete trusted device?\n\nThis will permanently remove this rejected device from the user's trusted-device history.\n\nDevice: ${deviceLabel}` : action === "reinstate" ? "Reinstate this denied device?" : `${action[0].toUpperCase()}${action.slice(1)} this device?`)) { button.disabled = false; return; }
        let reason = "";
        if (action === "reject" || action === "reinstate" || action === "revoke" || action === "delete") {
          reason = window.prompt(action === "reject" ? "Deny reason (required):" : action === "revoke" ? "Revocation reason (required):" : action === "delete" ? "Reason for deletion (required):" : "Reinstatement reason (required):", "")?.trim() ?? "";
          if (!reason) { toast("A reason is required", "error"); button.disabled = false; return; }
        }
        try { if (action === "approve") await adminApi.approveDevice(String(user._id), deviceId); else if (action === "reject") await adminApi.rejectDevice(String(user._id), deviceId, reason); else if (action === "reinstate") await adminApi.reinstateDevice(String(user._id), deviceId, reason); else if (action === "delete") await adminApi.deleteDevice(String(user._id), deviceId, reason); else await adminApi.revokeDevice(String(user._id), deviceId, reason); toast(action === "reinstate" ? "Device reinstated; approval required" : action === "delete" ? "Denied device deleted" : "Device updated"); dialog.close(); await openDevices(user); } catch (error) { toast(error instanceof Error ? error.message : "Device could not be updated", "error"); button.disabled = false; }
      }));
    } catch (error) { content.innerHTML = `<div class="notice error">${escapeHtml(error instanceof Error ? error.message : "Devices unavailable")}</div>`; }
    refreshIcons(content);
  };
  const openCustomerAccess = (user: Record<string, unknown>, customers: Record<string, unknown>[], reload: () => Promise<void>) => {
    const name = String(user.name ?? "User");
    const email = String(user.email ?? "");
    const content = document.createElement("div");
    if (user.customer_access_global === true) {
      content.innerHTML = `<section class="customer-access-readonly"><span class="eyebrow">Customer access</span><h3>ALL CUSTOMERS</h3><p>This user has access to all customers.</p></section>`;
      openModal(`Customer access Â· ${escapeHtml(name)}`, content, "normal");
      return;
    }
    const assigned = new Set((user.assigned_customer_ids as unknown[] ?? []).map(String));
    if (!canManageCustomerAccess) {
      const assignedNames = [...assigned]
        .map((id) => customers.find((customer) => String(customer._id) === id))
        .filter(Boolean)
        .map((customer) => String(customer?.name ?? customer?.company_name ?? "Customer"));
      content.innerHTML = `<section class="customer-access-readonly"><span class="eyebrow">Customer access</span><h3>${escapeHtml(name)}</h3><p class="form-hint">${escapeHtml(email)}</p><strong>${assigned.size} assigned customer${assigned.size === 1 ? "" : "s"}</strong><p>${assignedNames.length ? escapeHtml(assignedNames.join(", ")) : "No customers assigned."}</p><small class="form-hint">You have read-only access to this scope.</small></section>`;
      openModal(`Customer access - ${escapeHtml(name)}`, content, "normal");
      return;
    }
    content.innerHTML = `<section class="customer-access-quick"><span class="eyebrow">Customer access</span><h3>${escapeHtml(name)}</h3><p class="form-hint">${escapeHtml(email)}</p><div class="customer-access-modal-heading"><strong>Assigned customers</strong><span data-assigned-count>${assigned.size} selected</span></div><input class="customer-access-search" data-quick-customer-search type="search" placeholder="Search customers..." aria-label="Search customers"><div class="customer-access-options customer-access-modal-options" data-quick-customer-options role="listbox" aria-label="Customers"></div><div class="customer-access-modal-selected"><strong>Selected customers</strong><div class="customer-access-selected" data-quick-selected></div></div><small class="field-error" data-quick-customer-error aria-live="polite"></small><div class="modal-actions"><button type="button" class="button button-quiet" data-quick-customer-cancel>Cancel</button><button type="button" class="button button-primary" data-quick-customer-save>Save assignments</button></div></section>`;
    const dialog = openModal(`Customer access Â· ${escapeHtml(name)}`, content, "wide");
    const options = content.querySelector<HTMLElement>("[data-quick-customer-options]");
    const selected = content.querySelector<HTMLElement>("[data-quick-selected]");
    const search = content.querySelector<HTMLInputElement>("[data-quick-customer-search]");
    const render = () => {
      const term = (search?.value ?? "").trim().toLowerCase();
      if (options) options.innerHTML = customers.filter((customer) => !term || [customer.name, customer.company_name, customer.customer_code, customer.contact_name, customer.email, customer.country, customer.country_name, (customer.region as Record<string, unknown> | undefined)?.country_name].some((value) => String(value ?? "").toLowerCase().includes(term))).map((customer) => { const id = String(customer._id); const isSelected = assigned.has(id); const country = customer.country_name ?? customer.country ?? (customer.region as Record<string, unknown> | undefined)?.country_name; const currency = customer.preferred_currency ?? customer.default_currency; return `<button type="button" class="customer-access-option${isSelected ? " is-selected" : ""}" data-quick-customer-id="${escapeHtml(id)}" role="option" aria-selected="${isSelected}"><span><b>${isSelected ? "✓ " : "☐ "}${escapeHtml(String(customer.name ?? customer.company_name ?? "Customer"))}</b><small>${escapeHtml([country, currency].filter(Boolean).join(" Â· "))}</small></span></button>`; }).join("") || '<span class="muted">No matching customers</span>';
      if (selected) selected.innerHTML = [...assigned].map((id) => { const customer = customers.find((item) => String(item._id) === id); return customer ? `<span class="customer-access-chip"><span>${escapeHtml(String(customer.name ?? customer.company_name ?? "Customer"))}</span><button type="button" data-quick-remove-customer="${escapeHtml(id)}" aria-label="Remove ${escapeHtml(String(customer.name ?? "customer"))}">×</button></span>` : ""; }).join("") || '<span class="muted">No customers assigned</span>';
      const count = content.querySelector<HTMLElement>("[data-assigned-count]");
      if (count) count.textContent = `${assigned.size} selected`;
    };
    options?.addEventListener("click", (event) => { const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-quick-customer-id]"); if (!button) return; const id = String(button.dataset.quickCustomerId); if (assigned.has(id)) assigned.delete(id); else assigned.add(id); render(); });
    selected?.addEventListener("click", (event) => { const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-quick-remove-customer]"); if (!button) return; assigned.delete(String(button.dataset.quickRemoveCustomer)); render(); });
    search?.addEventListener("input", render);
    content.querySelector<HTMLButtonElement>("[data-quick-customer-cancel]")?.addEventListener("click", () => dialog.close());
    content.querySelector<HTMLButtonElement>("[data-quick-customer-save]")?.addEventListener("click", async (event) => { const button = event.currentTarget as HTMLButtonElement; button.disabled = true; try { await adminApi.updateUser(String(user._id), { customer_ids: [...assigned] }); dialog.close(); toast("Customer assignments saved"); await reload(); } catch (error) { const node = content.querySelector<HTMLElement>("[data-quick-customer-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Customer assignments could not be saved"; button.disabled = false; } });
    render();
    refreshIcons(content);
  };
  const editUser = (user: Record<string, unknown>, roles: Record<string, unknown>[], customers: Record<string, unknown>[], reload: () => Promise<void>) => {
    const content = document.createElement("div");
    const assigned = new Set((user.assigned_customer_ids as unknown[] ?? []).map(String));
    const roleHasGlobalCustomerAccess = (roleId: string) => {
      const role = roles.find((candidate) => String(candidate._id) === roleId);
      return ["admin", "superadmin"].includes(roleId) || (role?.permissions as unknown[] | undefined)?.includes("customers.view_all") === true;
    };
    const globalRole = () => roleHasGlobalCustomerAccess(String((content.querySelector('[name="role_id"]') as HTMLSelectElement | null)?.value ?? user.role_id));
    const customerOptions = (term = "") => customers.filter((customer) => !assigned.has(String(customer._id))).filter((customer) => !term || [customer.name, customer.company_name, customer.customer_code, customer.contact_name, customer.email, customer.country, customer.country_name, (customer.region as Record<string, unknown> | undefined)?.country_name].some((value) => String(value ?? "").toLowerCase().includes(term))).slice(0, 50).map((customer) => `<button type="button" class="customer-access-option" data-customer-id="${escapeHtml(String(customer._id))}"><span>${escapeHtml(String(customer.name ?? customer.company_name ?? "Customer"))}</span><small>${escapeHtml([customer.customer_code, customer.country_name ?? customer.country, customer.preferred_currency ?? customer.default_currency].filter(Boolean).join(" Â· "))}</small></button>`).join("");
    const selectedRows = () => [...assigned].map((id) => {
      const customer = customers.find((item) => String(item._id) === id);
      return customer ? `<span class="customer-access-chip"><span>${escapeHtml(String(customer.name ?? customer.company_name ?? "Customer"))}</span><button type="button" data-remove-customer="${escapeHtml(id)}" aria-label="Remove ${escapeHtml(String(customer.name ?? "customer"))}">×</button></span>` : "";
    }).join("");
     const canConfigureIncentive = String(appStore.state.user?.role_id ?? "") === "superadmin";
     const incentiveRoles = new Set(["admin", "manager_sales_admin", "user"]);
     const initialRoleId = String(user.role_id ?? "");
     const incentiveOptions = [1, 2, 3, 4, 5, 6];
     const incentiveCategories = (user.incentive_categories as Record<string, unknown>[] | undefined) ?? [{ _id: "blankets", name: "Blanket" }, { _id: "mpacks", name: "Underpacking" }, { _id: "chemicals", name: "Chemical" }];
     const incentiveProducts = incentiveCategories;
     const incentiveRates = (user.incentive_rates as Record<string, unknown> | undefined) ?? {};
     const incentiveField = canConfigureIncentive ? `<section class="admin-user-section incentive-configuration" data-incentive-field ${incentiveRoles.has(initialRoleId) ? "" : 'style="display:none"'}><span class="eyebrow">Incentive configuration</span><p class="form-hint">Set the incentive percentage for each category. These rates are snapshotted when an Order Confirmation is created.</p><div class="incentive-category-grid">${incentiveProducts.length ? incentiveProducts.map((category) => { const id = String(category._id); const selected = Number(incentiveRates[id] ?? 0); return `<label>${escapeHtml(String(category.name ?? id))}<select data-incentive-category="${escapeHtml(id)}"><option value="">Not configured</option>${incentiveOptions.map((rate) => `<option value="${rate}" ${rate === selected ? "selected" : ""}>${rate}%</option>`).join("")}</select></label>`; }).join("") : '<span class="muted">No incentive categories available.</span>'}</div></section>` : "";
     content.innerHTML = `<form class="stack-form admin-user-form"><section class="admin-user-section"><span class="eyebrow">User details</span><div class="form-grid"><label>Full name<input name="name" required value="${escapeHtml(String(user.name ?? ""))}"></label><label>Email<input value="${escapeHtml(String(user.email ?? ""))}" disabled></label><label>Phone<input name="phone" type="tel" inputmode="tel" value="${escapeHtml(String(user.phone ?? ""))}"></label></div></section><section class="admin-user-section"><span class="eyebrow">Access</span><div class="form-grid"><label>Role<select name="role_id">${roles.map((role) => `<option value="${escapeHtml(String(role._id))}" ${String(role._id) === String(user.role_id) ? "selected" : ""}>${escapeHtml(String(role.display_name ?? role._id))}</option>`).join("")}</select></label><label>Device policy<select name="device_access_mode"><option value="approved_devices_only" ${user.device_access_mode !== "any_authorized_device" ? "selected" : ""}>Approved devices only</option><option value="any_authorized_device" ${user.device_access_mode === "any_authorized_device" ? "selected" : ""}>Any authorized device</option></select></label><label class="check-row"><input name="active" type="checkbox" ${user.active !== false ? "checked" : ""}><span>Account active</span></label></div></section>${incentiveField}<section class="admin-user-section admin-user-security" data-device-security><span class="eyebrow">Security &amp; devices</span><div class="device-security-summary"><span class="form-hint">Loading device information...</span></div></section>${canManageCustomerAccess ? `<section class="admin-user-section customer-access-editor" data-customer-access-section><span class="eyebrow">Customer access</span><p class="form-hint" data-customer-access-note>${globalRole() ? "This role has global access to all customers." : "Only selected customers are accessible to this user."}</p><div class="customer-access-actions"><button type="button" class="button button-quiet" data-select-all>Select all</button><button type="button" class="button button-quiet" data-clear-all>Clear all</button></div><input class="customer-access-search" type="search" placeholder="Search name, code, contact or email" aria-label="Search customers"><div class="customer-access-selected" data-selected-customers>${selectedRows() || '<span class="muted">No customers assigned</span>'}</div><div class="customer-access-options" data-customer-options>${customerOptions() || '<span class="muted">No customers found</span>'}</div></section>` : ""}<small class="field-error" data-admin-user-error></small><button class="button button-primary button-full" type="submit">Save user</button></form>`;
     const dialog = openModal("Edit user", content, "wide");
     const incentiveHint = content.querySelector<HTMLElement>("[data-incentive-field] .form-hint");
     if (incentiveHint) incentiveHint.textContent = "Set the incentive percentage for each category. These rates are snapshotted when an Order Confirmation is created.";
     dialog.addEventListener("close", () => selectorCleanup?.(), { once: true });
     void adminApi.userDevices(String(user._id)).then((result) => {
       const target = content.querySelector<HTMLElement>("[data-device-security]");
       if (!target) return;
       const items = result.items;
       const counts = items.reduce<{ approved: number; pending: number; denied: number; revoked: number }>((summary, device) => { const status = String(device.device_status || device.status || "").toLowerCase(); if (status in summary) summary[status as keyof typeof summary] += 1; return summary; }, { approved: 0, pending: 0, denied: 0, revoked: 0 });
       if (!items.length) { target.querySelector<HTMLElement>(".device-security-summary")!.innerHTML = '<strong>NO TRUSTED DEVICES</strong><span>This user has not registered a device yet.</span>'; return; }
       const current = items.find((device) => device.current_session === true);
       const recent = current || items[0];
       const location = recent.location as Record<string, unknown> | undefined;
       const locationLabel = String(location?.label || [location?.city, location?.state, location?.country].filter(Boolean).join(", ") || "Location unavailable");
       const status = String(recent.device_status || recent.status || "unknown").toLowerCase();
       const client = `${recent.browser_name || recent.browser || "Unknown browser"}${recent.browser_version ? ` ${recent.browser_version}` : ""} · ${recent.os_name || recent.operating_system || "Unknown OS"}${recent.os_version ? ` ${recent.os_version}` : ""} · ${recent.device_type || "Unknown device"}`;
       target.querySelector<HTMLElement>(".device-security-summary")!.innerHTML = `<strong>${items.length} trusted device${items.length === 1 ? "" : "s"}</strong><span>${counts.approved} approved · ${counts.pending} pending · ${counts.denied} denied · ${counts.revoked} revoked</span><article class="device-security-card"><b>${current ? "CURRENT DEVICE" : "RECENT DEVICE"}</b><strong>${escapeHtml(client)}</strong><span>${escapeHtml(locationLabel)}</span><small>Approximate network location</small>${recent.last_activity_at || recent.last_seen_at ? `<small>Last activity: ${escapeHtml(String(recent.last_activity_at || recent.last_seen_at))}</small>` : ""}<span class="device-security-status device-status-${escapeHtml(status)}">STATUS: ${escapeHtml(status.toUpperCase())}</span></article><button class="button button-quiet" type="button" data-view-user-devices>View trusted devices</button>`;
       target.querySelector<HTMLButtonElement>("[data-view-user-devices]")?.addEventListener("click", () => void openDevices(user));
     }).catch(() => { const target = content.querySelector<HTMLElement>("[data-device-security] .device-security-summary"); if (target) target.innerHTML = '<span class="form-hint">Device information unavailable.</span>'; });
      const renderAssignments = () => {
       const selected = content.querySelector<HTMLElement>("[data-selected-customers]");
       const options = content.querySelector<HTMLElement>("[data-customer-options]");
       const isGlobal = globalRole();
       if (selected) selected.innerHTML = isGlobal ? '<span class="customer-access-global">ALL CUSTOMERS</span>' : (selectedRows() || '<span class="muted">No customers assigned</span>');
       if (options) {
         const term = (content.querySelector<HTMLInputElement>(".customer-access-search")?.value ?? "").trim().toLowerCase();
         options.innerHTML = customerOptions(term) || '<span class="muted">No matching customers</span>';
       }
       const note = content.querySelector<HTMLElement>("[data-customer-access-note]");
       if (note) note.textContent = isGlobal ? "This role has global access to all customers. Specific assignments are not required." : "Only selected customers are accessible to this user.";
       const label = content.querySelector<HTMLElement>("[data-customer-selector-label]");
       if (label) label.textContent = isGlobal ? "All customers" : assigned.size ? `${assigned.size} customer${assigned.size === 1 ? "" : "s"} selected` : "Search or select customers...";
       const trigger = content.querySelector<HTMLButtonElement>("[data-customer-selector-trigger]");
       if (trigger) trigger.disabled = isGlobal;
       content.querySelectorAll<HTMLButtonElement>("[data-select-all], [data-clear-all]").forEach((button) => { button.disabled = isGlobal; });
      };
    const customerSection = content.querySelector<HTMLElement>("[data-customer-access-section]");
    const securitySection = content.querySelector<HTMLElement>("[data-device-security]");
    const customerSearch = content.querySelector<HTMLInputElement>(".customer-access-search");
    const customerOptionsNode = content.querySelector<HTMLElement>("[data-customer-options]");
    let selectorCleanup: (() => void) | undefined;
    if (customerSection && securitySection && securitySection.parentElement === customerSection.parentElement) securitySection.before(customerSection);
    if (customerSection && customerSearch && customerOptionsNode) {
      const selector = document.createElement("div");
      selector.className = "customer-access-selector";
      selector.dataset.customerSelector = "true";
      const trigger = document.createElement("button");
      trigger.type = "button";
      trigger.className = "customer-access-trigger";
      trigger.dataset.customerSelectorTrigger = "true";
      trigger.setAttribute("aria-haspopup", "listbox");
      trigger.setAttribute("aria-expanded", "false");
      trigger.innerHTML = '<span data-customer-selector-label>Search or select customers...</span><i data-lucide="chevron-down" aria-hidden="true"></i>';
      const dropdown = document.createElement("div");
      dropdown.className = "customer-access-dropdown";
      dropdown.dataset.customerSelectorDropdown = "true";
      dropdown.hidden = true;
      customerSearch.dataset.customerAccessSearch = "true";
      customerOptionsNode.setAttribute("role", "listbox");
      customerOptionsNode.setAttribute("aria-label", "Available customers");
      selector.append(trigger, dropdown);
      dropdown.append(customerSearch, customerOptionsNode);
      customerSection.append(selector);
      const closeSelector = () => { dropdown.hidden = true; trigger.setAttribute("aria-expanded", "false"); };
      const openSelector = () => { dropdown.hidden = false; trigger.setAttribute("aria-expanded", "true"); window.setTimeout(() => customerSearch.focus(), 0); };
      trigger.addEventListener("click", () => { if (dropdown.hidden) openSelector(); else closeSelector(); });
      const outsidePointerDown = (event: PointerEvent) => { if (!selector.contains(event.target as Node)) closeSelector(); };
      const escapeKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !dropdown.hidden) { closeSelector(); trigger.focus(); } };
      document.addEventListener("pointerdown", outsidePointerDown);
      content.addEventListener("keydown", escapeKey);
      selectorCleanup = () => { document.removeEventListener("pointerdown", outsidePointerDown); content.removeEventListener("keydown", escapeKey); };
      (customerSection as HTMLElement).dataset.customerSelectorReady = "true";
    }
    content.querySelector("[data-customer-options]")?.addEventListener("click", (event) => { const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-customer-id]"); if (!button) return; assigned.add(String(button.dataset.customerId)); renderAssignments(); });
    content.querySelector("[data-selected-customers]")?.addEventListener("click", (event) => { const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-remove-customer]"); if (!button) return; assigned.delete(String(button.dataset.removeCustomer)); renderAssignments(); });
    content.querySelector("[data-select-all]")?.addEventListener("click", () => { customers.forEach((customer) => assigned.add(String(customer._id))); renderAssignments(); });
    content.querySelector("[data-clear-all]")?.addEventListener("click", () => { assigned.clear(); renderAssignments(); });
    const syncIncentiveField = () => {
      const roleId = String((content.querySelector('[name="role_id"]') as HTMLSelectElement | null)?.value ?? "");
      const field = content.querySelector<HTMLElement>("[data-incentive-field]");
      if (field) field.style.display = incentiveRoles.has(roleId) ? "" : "none";
    };
     content.querySelector<HTMLInputElement>(".customer-access-search")?.addEventListener("input", renderAssignments);
     content.querySelector<HTMLSelectElement>('[name="role_id"]')?.addEventListener("change", () => { renderAssignments(); syncIncentiveField(); });
     syncIncentiveField();
      renderAssignments();
     content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); const selectedRoleId = String(data.get("role_id") ?? ""); const incentiveRates: Record<string, number> = {}; content.querySelectorAll<HTMLSelectElement>("[data-incentive-category]").forEach((select) => { if (select.value) incentiveRates[String(select.dataset.incentiveCategory)] = Number(select.value); }); try { await adminApi.updateUser(String(user._id), { name: data.get("name"), phone: data.get("phone"), role_id: selectedRoleId, device_access_mode: data.get("device_access_mode"), ...(canConfigureIncentive ? { incentive_rates: incentiveRoles.has(selectedRoleId) ? incentiveRates : {} } : {}), active: data.get("active") === "on", ...(canManageCustomerAccess ? { customer_ids: [...assigned] } : {}) }); dialog.close(); toast("User updated"); await reload(); } catch (error) { const node = content.querySelector<HTMLElement>("[data-admin-user-error]"); if (node) node.textContent = error instanceof Error ? error.message : "User could not be updated"; } });
     refreshIcons(content);
  };
  let availableRoles: Record<string, unknown>[] = [];
  const inviteUser = (roles: Record<string, unknown>[], reload: () => Promise<void>) => {
    const content = document.createElement("div");
    content.innerHTML = `<form class="stack-form admin-user-form invite-user-form"><section class="admin-user-section"><span class="eyebrow">Account details</span><div class="form-grid"><label>Name<input name="name" maxlength="100" autocomplete="name" required></label><label>Username / User ID<input name="username" minlength="3" maxlength="80" pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,79}" autocomplete="username" required></label><label>Email<input name="email" type="email" maxlength="254" autocomplete="email" required></label><label>Role<select name="role_id" required><option value="">Select role</option>${roles.map((role) => `<option value="${escapeHtml(String(role._id))}">${escapeHtml(String(role.display_name ?? role._id))}</option>`).join("")}</select></label></div></section><section class="admin-user-section"><span class="eyebrow">Initial password</span><p class="form-hint">Use at least 8 characters with 1 uppercase letter, 1 lowercase letter, and 1 number.</p><div class="form-grid"><label>Password<input name="password" type="password" minlength="8" maxlength="200" pattern="(?=.*[a-z])(?=.*[A-Z])(?=.*[0-9]).{8,}" autocomplete="new-password" required></label><label>Confirm Password<input name="confirm_password" type="password" minlength="8" maxlength="200" autocomplete="new-password" required></label></div></section><small class="field-error" data-invite-error aria-live="polite"></small><div class="modal-actions"><button class="button button-quiet" type="button" data-cancel-invite>Cancel</button><button class="button button-primary" type="submit"><i data-lucide="send"></i>Create &amp; invite</button></div></form>`;
    enhancePasswordFields(content);
    const dialog = openModal("Invite user", content, "wide");
    content.querySelector<HTMLButtonElement>("[data-cancel-invite]")?.addEventListener("click", () => dialog.close());
    content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget as HTMLFormElement;
      const errorNode = content.querySelector<HTMLElement>("[data-invite-error]");
      const passwordInput = form.elements.namedItem("password") as HTMLInputElement;
      const confirmationInput = form.elements.namedItem("confirm_password") as HTMLInputElement;
      confirmationInput.setCustomValidity(passwordInput.value === confirmationInput.value ? "" : "Passwords do not match.");
      if (!form.reportValidity()) {
        if (errorNode) errorNode.textContent = confirmationInput.validationMessage === "Passwords do not match." ? "Passwords do not match." : "Check the highlighted fields and try again.";
        return;
      }
      const submit = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
      submit.disabled = true;
      if (errorNode) errorNode.textContent = "";
      const data = new FormData(form);
      try {
        const result = await adminApi.createUser({
          name: String(data.get("name") ?? "").trim(),
          username: String(data.get("username") ?? "").trim(),
          email: String(data.get("email") ?? "").trim(),
          password: passwordInput.value,
          confirm_password: confirmationInput.value,
          role_id: data.get("role_id"),
          customer_ids: [],
        });
        passwordInput.value = "";
        confirmationInput.value = "";
        dialog.close();
        await reload();
        if (result.invitation.email_sent) toast("User invited successfully.");
        else toast(`User account created, but the invitation email could not be sent. Reference: ${result.invitation.diagnostic_id}`, "error");
      } catch (error) {
        if (errorNode) errorNode.textContent = error instanceof Error ? error.message : "User could not be invited.";
        submit.disabled = false;
      }
    });
    refreshIcons(content);
  };
  const load = async () => {
    const [users, roles, customerResult] = await Promise.all([adminApi.users(), adminApi.roles(), customerCompanyApi.list()]);
    availableRoles = roles.items;
    const customers = customerResult.items as unknown as Record<string, unknown>[];
     body.innerHTML = `<div class="access-summary panel"><div><span class="eyebrow">Access model</span><h2>${users.total} users across ${roles.total} roles</h2><p>Server-side permissions remain authoritative for every customer-scoped action.</p></div><div class="role-pills">${roles.items.map((role) => `<span>${escapeHtml(String(role.display_name))}<b>${(role.permissions as unknown[])?.length ?? 0}</b></span>`).join("")}</div></div><div class="data-table panel"><table><thead><tr><th>User</th><th>Role</th><th>Customers</th><th>Devices</th><th>Status</th><th></th></tr></thead><tbody>${users.items.map((user) => { const global = user.customer_access_global === true; const count = Number(user.customer_access_count ?? ((user.assigned_customer_ids as unknown[]) ?? []).length); const devices = (user.device_counts as { total?: number; approved?: number; pending?: number; denied?: number; revoked?: number } | undefined) ?? {}; const total = Number(devices.total ?? 0); const deviceLabel = `${total} device${total === 1 ? "" : "s"} · ${Number(devices.approved ?? 0)} approved${Number(devices.pending ?? 0) ? ` · ${Number(devices.pending)} pending` : ""}${Number(devices.revoked ?? 0) ? ` · ${Number(devices.revoked)} revoked` : ""}${Number(devices.denied ?? 0) ? ` · ${Number(devices.denied)} denied` : ""}`; return `<tr><td><div class="table-identity"><span>${escapeHtml(String(user.name ?? "User").replace(/\s+/g, "").slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(String(user.name ?? "User"))}</strong><small>${escapeHtml(String(user.email ?? ""))}</small></p></div></td><td>${escapeHtml(String(user.role_id ?? "user"))}</td><td><button class="text-button customer-count-button" data-id="${escapeHtml(String(user._id))}">${global ? "All customers" : `${count} assigned`}</button></td><td><button class="text-button device-count-button" data-id="${escapeHtml(String(user._id))}">${deviceLabel}</button></td><td>${statusBadge(user.active === false ? "Inactive" : "Active")}</td><td><button class="icon-button edit-user" data-id="${escapeHtml(String(user._id))}" aria-label="Edit user" title="Edit user"><i data-lucide="pencil"></i></button></td></tr>`; }).join("")}</tbody></table></div>`;
    body.querySelectorAll<HTMLButtonElement>(".edit-user, .customer-count-button, .device-count-button").forEach((button) => {
      const user = users.items.find((item) => String(item._id) === button.dataset.id);
      if (!user) return;
      button.type = "button";
      if (button.classList.contains("device-count-button")) button.addEventListener("click", () => void openDevices(user));
      else if (button.classList.contains("customer-count-button")) {
        button.setAttribute("aria-label", `View customer access for ${String(user.name ?? "user")}`);
        button.title = "View customer access";
        button.addEventListener("click", () => openCustomerAccess(user, customers, load));
      } else button.addEventListener("click", () => editUser(user, roles.items, customers, load));
    });
    refreshIcons(body);
  };
  try { await load(); }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Users unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector<HTMLButtonElement>("#invite-user")?.addEventListener("click", () => inviteUser(availableRoles, load));
  refreshIcons(page); return page;
}

export async function adminPage(): Promise<HTMLElement> {
  return pricingAdminPage();
  /* Removed legacy inline price-and-tax editor.
  const page = pageScaffold("Management", "Pricing administration", "Approve EUR master prices and govern product tax without touching application code.", '<button class="button button-secondary" id="validate-import"><i data-lucide="upload"></i>Validate import</button><input id="product-import-file" type="file" accept=".csv,.json,.xlsx" hidden>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(7);
  try {
    const result = await catalogApi.products();
    const pending = result.items.filter((product) => product.pricing_status !== "configured").length;
    body.innerHTML = `<div class="admin-callout"><div><span class="eyebrow">EUR pricing control</span><h2>One master catalogue, reference conversions.</h2><p>Review all ${result.pagination?.total ?? result.items.length} active products. ${pending} still require commercial pricing.</p></div><div class="admin-flow"><span>Catalog</span><i data-lucide="chevron-right"></i><span>EUR price</span><i data-lucide="chevron-right"></i><span>ECB reference rate</span><i data-lucide="chevron-right"></i><strong>Product tax</strong></div></div><div class="section-title"><div><span class="eyebrow">Master catalogue</span><h2>Product pricing</h2></div><span class="count-badge">${pending} pending</span></div>${result.items.length ? `<div class="pricing-list panel">${result.items.map(pricingRow).join("")}</div>` : emptyState("circle-check-big", "Catalogue is empty", "Import products to begin pricing.")}`;
    body.querySelectorAll<HTMLFormElement>(".inline-price-form").forEach((form) => form.addEventListener("submit", async (event) => {
      event.preventDefault(); const data = new FormData(form); const button = form.querySelector<HTMLButtonElement>("button")!; button.disabled = true;
      const variantInputs = Array.from(form.querySelectorAll<HTMLInputElement>("[data-thickness]"));
      const variantPrices = Object.fromEntries(variantInputs.map((input) => [input.dataset.thickness!, input.value === "" ? null : Number(input.value)]));
      const pricing = variantInputs.length ? { variant_prices: variantPrices } : { price: Number(data.get("price")) };
      try { await catalogApi.updatePricing(form.dataset.id!, { ...pricing, tax_override_enabled: data.get("tax_rate") !== "", tax_mode: data.get("tax_rate") === "" ? undefined : data.get("tax_mode"), tax_rate: data.get("tax_rate") === "" ? null : Number(data.get("tax_rate")), reason: "EUR master price update from admin catalogue" }); toast("EUR price saved with history"); button.disabled = false; }
      catch (error) { toast(error instanceof Error ? error.message : "Price could not be updated", "error"); button.disabled = false; }
    }));
  } catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Pricing administration unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  page.querySelector<HTMLButtonElement>("#validate-import")?.addEventListener("click", () => page.querySelector<HTMLInputElement>("#product-import-file")?.click());
  page.querySelector<HTMLInputElement>("#product-import-file")?.addEventListener("change", async (event) => {
    const file = (event.target as HTMLInputElement).files?.[0];
    if (!file) return;
    try {
      const result = await adminApi.validateImport(file);
      if (result.invalid_rows.length) toast(`${result.summary.invalid} rows need correction`, "error");
      else if (confirm(`${result.summary.valid} rows validated. Commit this import?`)) { await adminApi.commitImport(file); toast("Product import committed"); }
      else toast(`${result.summary.valid} rows validated`, "info");
    } catch (error) { toast(error instanceof Error ? error.message : "Import could not be validated", "error"); }
  });
  refreshIcons(page); return page;
}

function pricingRow(product: Product): string {
  const thicknesses = (product.configuration.thicknesses_mm as number[] | undefined) ?? [];
  const priceFields = thicknesses.length > 1 ? thicknesses.map((thickness) => {
    const value = Object.entries(product.pricing.variant_prices ?? {}).find(([key]) => Number(key) === thickness)?.[1];
    return `<label><span>${thickness.toFixed(2)} mm · EUR/m²</span><input data-thickness="${thickness}" type="number" min="0" step="0.01" value="${value ?? ""}" placeholder="0.00" required></label>`;
  }).join("") : `<label><span>EUR price</span><input name="price" type="number" min="0" step="0.01" value="${product.pricing.price ?? ""}" placeholder="0.00" required></label>`;
  return `<div class="pricing-row"><div class="pricing-product"><span><i data-lucide="package"></i></span><p><strong>${escapeHtml(product.name)}</strong><small>Art. ${escapeHtml(product.article_no ?? product.sku)} · ${escapeHtml(product.category_id)} · ${escapeHtml(product.pricing.pricing_type)} / ${escapeHtml(product.pricing.unit)}</small></p></div><form class="inline-price-form" data-id="${product._id}">${priceFields}<label><span>Tax mode</span><select name="tax_mode"><option value="exclusive" ${product.tax.mode === "exclusive" ? "selected" : ""}>Exclusive</option><option value="inclusive" ${product.tax.mode === "inclusive" ? "selected" : ""}>Inclusive</option><option value="no_tax" ${product.tax.mode === "no_tax" ? "selected" : ""}>No tax</option></select></label><label><span>Tax rate</span><select name="tax_rate"><option value="" ${!product.tax.override_enabled ? "selected" : ""}>Company default</option><option value="0" ${product.tax.override_enabled && product.tax.rate === 0 ? "selected" : ""}>0%</option><option value="5" ${product.tax.rate === 5 ? "selected" : ""}>5%</option><option value="12" ${product.tax.rate === 12 ? "selected" : ""}>12%</option><option value="18" ${product.tax.rate === 18 ? "selected" : ""}>18%</option></select></label><button class="button button-dark"><i data-lucide="save"></i>Save Price</button></form></div>`;
}

  */
}

export async function settingsPage(section: "brand" | "currencies" | "communication" | "security" = "brand"): Promise<HTMLElement> {
  const page = pageScaffold("Management", "System settings", "Govern branding, EUR quotation policy, and communication settings.", '<button class="button button-primary"><i data-lucide="save"></i>Save changes</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  try {
    const settings = await adminApi.settings();
    const watermarkEnabled = settings.watermark_enabled !== false;
    let zohoMarkup = "";
    let routingPolicy: { cc: Array<{ address: string; email?: string; enabled: boolean; source?: string; display_name?: string | null }>; bcc: Array<{ address: string; email?: string; enabled: boolean; source?: string; display_name?: string | null }> } | null = null;
    const securityMarkup = appStore.state.user?.role_id === "superadmin"
      ? `<section class="panel settings-panel watermark-settings"><span class="eyebrow">Security</span><h2>Protected workspace view</h2><p>Show a light, non-interactive watermark on authenticated workspace pages. It never appears in quotation PDFs or emails.</p><label class="setting-toggle"><input type="checkbox" data-watermark-toggle ${watermarkEnabled ? "checked" : ""}><span><strong>Workspace watermark</strong><small>Include the current user and local date/time.</small></span></label><button type="button" class="button button-secondary" data-watermark-save>Save watermark setting</button></section>`
      : `<section class="panel settings-panel watermark-settings"><span class="eyebrow">Security</span><h2>Protected workspace view</h2><p>Workspace watermark is managed by a Superadmin.</p><div class="setting-toggle is-readonly"><span><strong>Workspace watermark</strong><small>${watermarkEnabled ? "Enabled" : "Disabled"}</small></span></div></section>`;
    if (appStore.can("settings.manage")) {
      zohoMarkup = `<section class="panel settings-panel zoho-integration"><span class="eyebrow">Communication</span><h2>Zoho Mail</h2><p>Application email uses the server-side Zoho Mail API.</p><div class="notice compact"><i data-lucide="mail-check"></i><div><strong>Loading integration status…</strong></div></div></section>`;
      try {
        const status = await adminApi.zohoStatus();
        const state = status.connected ? "Connected" : status.status === "error" ? "Error" : "Not connected";
        const errorMarkup = status.last_error
          ? `<div class="notice error compact"><i data-lucide="circle-alert"></i><div><strong>${escapeHtml(status.last_error.code)}</strong><p>Stage: ${escapeHtml(status.last_error.stage)} · Reference: ${escapeHtml(status.last_error.diagnostic_id)}</p></div></div>`
          : "";
        const purposeLabels: Record<string, string> = { general: "Primary mailbox", otp: "OTP", quotation: "Quotations", order: "Orders" };
        const senderOrder: Record<string, number> = { general: 0, otp: 1, quotation: 2, order: 3 };
        const senderItems = [...(status.email_senders ?? [])].sort((left, right) => senderOrder[left.purpose] - senderOrder[right.purpose]).map((identity) => {
          const availability = identity.available === true ? "Available" : identity.available === false ? "Unavailable" : "Not verified";
          const stateClass = identity.available === true ? "is-available" : identity.available === false ? "is-unavailable" : "";
          return `<div class="email-identity"><div><dt>${escapeHtml(identity.from_name)} · ${escapeHtml(purposeLabels[identity.purpose] ?? identity.purpose)}</dt><dd>${escapeHtml(identity.address)}</dd></div><span class="identity-state ${stateClass}">${escapeHtml(availability)}</span></div>`;
        }).join("");
        const missingSenders = (status.email_senders ?? []).filter((identity) => identity.available === false);
        const aliasWarning = missingSenders.length
          ? `<div class="notice error compact"><i data-lucide="circle-alert"></i><div><strong>Sender identity unavailable</strong><p>${missingSenders.map((identity) => escapeHtml(`${purposeLabels[identity.purpose] ?? identity.purpose}: ${identity.address}`)).join("<br>")}</p></div></div>`
          : status.sender_validation_error
            ? `<div class="notice error compact"><i data-lucide="circle-alert"></i><div><strong>Sender identities could not be verified</strong><p>Reference: ${escapeHtml(status.sender_validation_error.diagnostic_id)}</p></div></div>`
            : "";
        const policy = status.customer_recipient_policy ?? { cc: [], bcc: [] };
        routingPolicy = policy;
        const policyRows = (items: Array<{ address: string; enabled: boolean }>) => items.map((item) => `<div class="routing-identity"><span>${escapeHtml(item.address)}</span><strong class="${item.enabled ? "is-active" : "is-disabled"}">${item.enabled ? "✓ Active" : "○ Disabled"}</strong></div>`).join("");
        const routingMarkup = `<div class="email-routing"><h3>Customer-facing quotation/order routing</h3><strong>CC</strong>${policyRows(policy.cc)}<strong>BCC</strong>${policyRows(policy.bcc)}</div>`;
        zohoMarkup = `<section class="panel settings-panel zoho-integration"><span class="eyebrow">Communication</span><h2>Zoho Mail</h2><p>Transactional email is sent only through the server-side Zoho Mail API.</p><div class="integration-status"><span class="status-dot ${status.connected ? "is-live" : status.status === "error" ? "is-error" : ""}"></span><strong>${escapeHtml(state)}</strong><small>Provider: Zoho Mail API</small></div><dl class="integration-details"><div><dt>Account</dt><dd>${escapeHtml(status.account_email || "Unknown")}</dd></div><div><dt>OAuth</dt><dd>${escapeHtml(status.oauth === "connected" ? "Connected" : "Not connected")}</dd></div><div><dt>Account ID</dt><dd>${escapeHtml(status.account_id || "missing")}</dd></div><div><dt>API domain</dt><dd>${escapeHtml(status.api_domain || "missing")} <small>(${escapeHtml(status.api_domain_status)})</small></dd></div><div><dt>Scopes</dt><dd>${status.scopes.map((scope) => escapeHtml(scope)).join("<br>")}</dd></div></dl><div class="email-identities"><h3>Email identities</h3><dl>${senderItems || '<p class="form-hint">Sender identities are not available until Zoho Mail is connected.</p>'}</dl></div>${routingMarkup}${aliasWarning}${errorMarkup}<div class="settings-actions"><button class="button button-secondary" type="button" data-zoho-connect>${status.connected ? "Reconnect Zoho Mail" : "Connect Zoho Mail"}</button>${status.connected ? '<button class="button button-danger" type="button" data-zoho-disconnect>Disconnect</button>' : ""}</div><form class="integration-test-form" data-zoho-test-form><label>Test recipient email<input type="email" name="to" autocomplete="email" placeholder="recipient@example.com" required ${status.connected ? "" : "disabled"}></label><button class="button button-quiet" type="submit" ${status.connected ? "" : "disabled"}><i data-lucide="send"></i>Test Email</button></form><div class="integration-test-result" data-zoho-test-result></div><p class="form-hint">Test email uses the primary mailbox. All aliases share this one OAuth connection; tokens remain server-side.</p></section>`;
        page.dataset.zohoConnected = String(status.connected);
      } catch (_error) { /* settings remains usable when the integration permission is absent */ }
    }
    body.innerHTML = `<div class="settings-layout"><nav class="settings-nav"><a class="${section === "brand" ? "active" : ""}" href="/settings" data-route="/settings"><i data-lucide="palette"></i>Brand & company</a><a class="${section === "currencies" ? "active" : ""}" href="/settings/currencies" data-route="/settings/currencies"><i data-lucide="euro"></i>Currencies</a><a class="${section === "communication" ? "active" : ""}" href="/settings/communication" data-route="/settings/communication"><i data-lucide="mail"></i>Communication</a><a class="${section === "security" ? "active" : ""}" href="/settings/security" data-route="/settings/security"><i data-lucide="shield-check"></i>Security</a></nav><div class="settings-stack"><section class="panel settings-panel" data-settings-section="brand"><span class="eyebrow">Brand identity</span><h2>Moneda Technologies</h2><p>Logo paths stay configurable so the official artwork can be replaced without a frontend release.</p><div class="logo-preview"><img src="${escapeHtml(settings.brand_logo_path ?? "/brand/moneda-logo.svg")}" alt="Configured Moneda logo"></div><div class="form-grid"><label>Brand name<input value="${escapeHtml(String(settings.brand_name ?? "Moneda Technologies"))}"></label><label>Logo path<input value="${escapeHtml(String(settings.brand_logo_path ?? "/brand/moneda-logo.svg"))}"></label><label>Master currency<input value="EUR" disabled></label><label>Quotation prefix<input value="${escapeHtml(String(settings.quotation_prefix ?? "MON_Q"))}" disabled></label></div><div class="notice compact"><i data-lucide="lock-keyhole"></i><div><strong>Protected business constants</strong><p>EUR master pricing and the MON_Q numbering namespace are migration-controlled.</p></div></div></section>${securityMarkup}${zohoMarkup}<section class="panel settings-panel" data-settings-section="currencies"><span class="eyebrow">Currencies</span><h2>EUR master pricing</h2><p>All catalogue and quotation values remain in EUR. USD and INR are display/reference currencies only.</p><div class="master-currency-card"><strong>Master currency</strong><b>EUR</b><span>Locked · quotations always EUR</span></div><div class="currency-rates" data-currency-rates><p class="form-hint">Loading latest reference rates…</p></div></section></div></div>`;
    [...body.querySelectorAll<HTMLElement>(".settings-nav button")].find((button) => button.textContent?.trim() === "Taxes")?.remove();
    [...body.querySelectorAll<HTMLElement>(".settings-panel label")].filter((label) => ["Default tax", "Tax mode"].some((text) => label.textContent?.trim().startsWith(text))).forEach((label) => label.remove());
    const policyGrid = body.querySelector<HTMLElement>(".settings-panel .form-grid");
    policyGrid?.insertAdjacentHTML("beforeend", '<label>Quotation currency<input value="EUR" disabled></label><label>Quotation tax<input value="None" disabled></label>');
    // Keep each settings destination focused: the navigation is a single entry
    // point, while each route renders only its own configuration section.
    const brandPanel = body.querySelector<HTMLElement>('[data-settings-section="brand"]');
    const currencyPanel = body.querySelector<HTMLElement>('[data-settings-section="currencies"]');
    const securityPanel = body.querySelector<HTMLElement>(".watermark-settings");
    const communicationPanel = body.querySelector<HTMLElement>(".zoho-integration");
    [brandPanel, currencyPanel, securityPanel, communicationPanel].forEach((panel) => { if (panel) panel.hidden = true; });
    ({ brand: brandPanel, currencies: currencyPanel, communication: communicationPanel, security: securityPanel }[section])?.removeAttribute("hidden");
    if (section === "currencies") {
      const rates = body.querySelector<HTMLElement>("[data-currency-rates]");
      currencyPanel?.insertAdjacentHTML("afterbegin", '<fieldset class="currency-preferences"><legend>Display/reference currencies</legend><label><input type="checkbox" checked disabled> EUR</label><label><input type="checkbox" checked disabled> USD</label><label><input type="checkbox" checked disabled> INR</label><small>Display preference is controlled from the workspace header.</small></fieldset>');
      try {
        const data = await rateApi.get(false);
        if (rates) rates.innerHTML = `<div class="currency-rate-grid"><div><span>EUR</span><strong>1.0000</strong></div><div><span>USD</span><strong>${Number(data.rates.USD ?? 0).toFixed(4)}</strong></div><div><span>INR</span><strong>${Number(data.rates.INR ?? 0).toFixed(4)}</strong></div></div><p class="form-hint">Source: ${escapeHtml(data.provider_source ?? data.provider ?? "ECB / Frankfurter")} · Rate date: ${escapeHtml(data.rate_date ?? "latest")} · ${escapeHtml(data.status === "stored_fallback" || data.stale ? "Using last successful rate" : "Latest available")}</p>`;
      } catch { if (rates) rates.innerHTML = '<p class="form-hint">Reference rates are temporarily unavailable; existing EUR pricing remains usable.</p>'; }
    }
    if (section === "security" && securityPanel) {
      securityPanel.insertAdjacentHTML("beforeend", '<div class="settings-security-links"><section><h3>Trusted devices</h3><p>Review pending, approved, denied and revoked devices in Users & access.</p><a class="button button-quiet" href="/users" data-route="/users">Manage trusted devices</a></section><section><h3>Login notifications</h3><p>Superadmins receive server-generated approval notifications for new-device access.</p></section><section><h3>Audit / security history</h3><p>Destructive actions and security changes are retained in the server audit trail.</p><a class="button button-quiet" href="/users" data-route="/users">Open access history</a></section></div>');
    }
    const routingPanel = page.querySelector<HTMLElement>(".email-routing");
    if (routingPanel && routingPolicy) {
      const canEditRouting = appStore.state.user?.role_id === "superadmin";
      const rows = (group: "cc" | "bcc", items: typeof routingPolicy.cc) => items.map((item) => {
        const email = String(item.email || item.address || "").toLowerCase();
        const source = item.source === "system" ? "System recipient" : "Custom recipient";
        return `<div class="routing-identity"><label><input type="checkbox" data-routing-toggle data-routing-group="${group}" data-routing-email="${escapeHtml(email)}" ${item.enabled ? "checked" : ""} ${canEditRouting ? "" : "disabled"}><span><strong>${escapeHtml(String(item.display_name || email))}</strong><small>${escapeHtml(email)} Â· ${source}</small></span></label><strong class="${item.enabled ? "is-active" : "is-disabled"}">${item.enabled ? "Active" : "Disabled"}</strong>${canEditRouting && item.source !== "system" ? `<button type="button" class="icon-button" data-routing-remove data-routing-group="${group}" data-routing-email="${escapeHtml(email)}" aria-label="Remove ${escapeHtml(email)}" title="Remove recipient"><i data-lucide="trash-2"></i></button>` : ""}</div>`;
      }).join("");
      routingPanel.innerHTML = `<h3>Customer-facing email routing</h3><p class="form-hint">Automatically included recipients for quotation and order emails.</p><h4>CC recipients</h4><div>${rows("cc", routingPolicy.cc)}</div>${canEditRouting ? '<button type="button" class="button button-quiet" data-routing-add="cc">+ Add CC recipient</button>' : ""}<h4>BCC recipients</h4><div>${rows("bcc", routingPolicy.bcc)}</div>${canEditRouting ? '<button type="button" class="button button-quiet" data-routing-add="bcc">+ Add BCC recipient</button>' : ""}`;
      routingPanel.querySelectorAll<HTMLInputElement>("[data-routing-toggle]").forEach((toggle) => toggle.addEventListener("change", async () => {
        const previous = !toggle.checked;
        const reason = window.prompt("Reason for routing change (required):", "")?.trim() ?? "";
        if (!reason) { toggle.checked = previous; toast("A reason is required", "error"); return; }
        toggle.disabled = true;
        try { await adminApi.updateRouting({ group: toggle.dataset.routingGroup, email: toggle.dataset.routingEmail, enabled: toggle.checked, reason }); toast("Email routing updated"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/settings" })); }
        catch (error) { toggle.checked = previous; toast(error instanceof Error ? error.message : "Email routing update failed", "error"); toggle.disabled = false; }
      }));
      routingPanel.querySelectorAll<HTMLButtonElement>("[data-routing-add]").forEach((button) => button.addEventListener("click", () => {
        const group = button.dataset.routingAdd as "cc" | "bcc";
        const content = document.createElement("div");
        content.innerHTML = `<form class="stack-form"><label>Email address<input name="email" type="email" required autocomplete="email"></label><label>Display name (optional)<input name="display_name" autocomplete="organization"></label><small class="field-error" data-routing-form-error></small><div class="modal-actions"><button type="button" class="button button-quiet" data-cancel>Cancel</button><button type="submit" class="button button-primary">Add recipient</button></div></form>`;
        const dialog = openModal(`Add ${group.toUpperCase()} recipient`, content);
        content.querySelector("[data-cancel]")?.addEventListener("click", () => dialog.close());
        content.querySelector<HTMLFormElement>("form")?.addEventListener("submit", async (event) => {
          event.preventDefault(); const form = event.currentTarget as HTMLFormElement; const data = new FormData(form); const submit = form.querySelector<HTMLButtonElement>("[type=submit]")!;
          submit.disabled = true;
          try { await adminApi.addRouting({ group, email: data.get("email"), display_name: data.get("display_name") }); dialog.close(); toast("Recipient added"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/settings" })); }
          catch (error) { const node = content.querySelector<HTMLElement>("[data-routing-form-error]"); if (node) node.textContent = error instanceof Error ? error.message : "Recipient could not be added"; submit.disabled = false; }
        });
        refreshIcons(content);
      }));
      routingPanel.querySelectorAll<HTMLButtonElement>("[data-routing-remove]").forEach((button) => button.addEventListener("click", async () => {
        const email = button.dataset.routingEmail || ""; if (!window.confirm("Remove recipient?\n\nThis email will no longer receive automatic quotation/order routing emails.")) return;
        const reason = window.prompt("Reason for removal (required):", "")?.trim() ?? ""; if (!reason) { toast("A reason is required", "error"); return; }
        button.disabled = true;
        try { await adminApi.removeRouting({ group: button.dataset.routingGroup, email, reason }); toast("Recipient removed"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/settings" })); }
        catch (error) { toast(error instanceof Error ? error.message : "Recipient removal failed", "error"); button.disabled = false; }
      }));
      refreshIcons(routingPanel);
      const testForm = page.querySelector<HTMLFormElement>("[data-zoho-test-form]");
      if (testForm) {
        const cc = routingPolicy.cc.filter((item) => item.enabled).map((item) => item.email || item.address).join(", ") || "None";
        const bcc = routingPolicy.bcc.filter((item) => item.enabled && !routingPolicy.cc.some((ccItem) => ccItem.enabled && (ccItem.email || ccItem.address) === (item.email || item.address))).map((item) => item.email || item.address).join(", ") || "None";
        testForm.insertAdjacentHTML("afterend", `<div class="test-routing-preview"><strong>Resolved routing</strong><span>CC: ${escapeHtml(cc)}</span><span>BCC: ${escapeHtml(bcc)}</span></div>`);
      }
    }
    page.querySelector<HTMLButtonElement>("[data-watermark-save]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget as HTMLButtonElement;
      const toggle = page.querySelector<HTMLInputElement>("[data-watermark-toggle]");
      if (!toggle) return;
      const reason = window.prompt("Reason for changing screenshot protection (required):", "")?.trim() ?? "";
      if (!reason) { toast("A reason is required", "error"); return; }
      button.disabled = true;
      try {
        await adminApi.updateSettings({ watermark_enabled: toggle.checked, reason });
        appStore.set({ watermarkEnabled: toggle.checked });
        window.dispatchEvent(new CustomEvent("moneda:watermark-setting", { detail: toggle.checked }));
        toast("Workspace watermark setting saved");
      } catch (error) { toast(error instanceof Error ? error.message : "Could not save watermark setting", "error"); }
      finally { button.disabled = false; }
    });
    if (section === "brand" && brandPanel) {
      const save = page.querySelector<HTMLButtonElement>(".page-actions button");
      save?.addEventListener("click", async () => {
        const inputs = brandPanel.querySelectorAll<HTMLInputElement>("input");
        try { await adminApi.updateSettings({ brand_name: inputs[0]?.value.trim(), brand_logo_path: inputs[1]?.value.trim() }); toast("Brand settings saved"); }
        catch (error) { toast(error instanceof Error ? error.message : "Could not save brand settings", "error"); }
      });
    }
    page.querySelector<HTMLButtonElement>("[data-zoho-connect]")?.addEventListener("click", () => { window.location.href = apiEndpoint("/integrations/zoho/connect"); });
    page.querySelector<HTMLButtonElement>("[data-zoho-disconnect]")?.addEventListener("click", async (event) => {
      if (!window.confirm("Disconnect Zoho Mail? Application email will stop until it is reconnected.")) return;
      const button = event.currentTarget as HTMLButtonElement; button.disabled = true;
      try { await adminApi.zohoDisconnect(); toast("Zoho Mail disconnected"); window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: "/settings" })); }
      catch (error) { toast(error instanceof Error ? error.message : "Zoho Mail could not be disconnected", "error"); button.disabled = false; }
    });
    page.querySelector<HTMLFormElement>("[data-zoho-test-form]")?.addEventListener("submit", async (event) => {
      event.preventDefault(); const form = event.currentTarget as HTMLFormElement;
      const button = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
      const target = page.querySelector<HTMLElement>("[data-zoho-test-result]")!;
      const to = String(new FormData(form).get("to") ?? ""); button.disabled = true; target.innerHTML = "";
      try {
        const result = await adminApi.zohoTest(to);
        target.innerHTML = `<div class="notice compact"><i data-lucide="circle-check"></i><div><strong>Test email submitted</strong><p>${result.checks.map((check) => `${escapeHtml(check.stage)} ${escapeHtml(check.result)}`).join(" · ")} · Reference ${escapeHtml(result.diagnostic_id ?? "recorded")}</p></div></div>`;
        toast("Zoho test email submitted"); refreshIcons(target);
      } catch (error) { toast(error instanceof Error ? error.message : "Zoho test failed", "error"); }
      finally { button.disabled = false; }
    });
    const query = new URLSearchParams(window.location.search);
    if (query.get("zoho") === "connected") toast("Zoho Mail connected successfully.");
    if (query.get("zoho") === "error") toast(`Zoho Mail connection failed (${query.get("error_code") ?? "OAUTH_CALLBACK_ERROR"}) at ${query.get("stage") ?? "callback"}. Reference ${query.get("diagnostic_id") ?? "unavailable"}.`, "error");
    if (query.has("zoho")) history.replaceState({}, "", window.location.pathname);
  }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Settings unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
  refreshIcons(page); return page;
}

export async function profilePage(): Promise<HTMLElement> {
  const user = appStore.state.user;
  const page = pageScaffold("Account", "Your profile", "Manage personal details, assigned customers and notification preferences.");
  const body = page.querySelector<HTMLElement>(".page-body")!;
  if (!user) { body.innerHTML = emptyState("user-x", "Profile unavailable", "Sign in to view your profile."); return page; }
  const permissions = Array.from(new Set(user.permissions ?? []));
  const permissionLabels: Record<string, string> = { view: "View", create: "Create", update: "Edit", edit: "Edit", delete: "Delete", manage: "Manage", history: "View history", send: "Send", download: "Download", print: "Print", confirm: "Confirm", pack: "Pack", ship: "Ship", receive: "Receive", complete: "Complete", override: "Override" };
  const permissionGroups = permissions.reduce<Record<string, string[]>>((groups, permission) => { const [module, action] = permission.split("."); (groups[module] ??= []).push(permissionLabels[action] ? `${permissionLabels[action]} ${module}` : permission); return groups; }, {});
    const customerCount = user.customer_access_global ? "All" : String(user.customer_access_count ?? user.assigned_customer_ids?.length ?? user.customer_ids?.length ?? user.company_ids?.length ?? 0);
    body.innerHTML = `<div class="profile-layout"><aside class="profile-card panel"><div class="profile-avatar">${escapeHtml(user.name.replace(/\s+/g, "").slice(0, 2).toUpperCase())}</div><h2>${escapeHtml(user.name)}</h2><p data-profile-email>${escapeHtml(user.email)}</p>${statusBadge(user.role_display_name)}<div class="profile-stat"><span>Customers</span><strong>${customerCount}</strong></div><button type="button" class="profile-stat profile-stat-button" data-show-permissions><span>Permissions</span><strong>${permissions.length}</strong></button></aside><div class="profile-stack"><form class="panel settings-panel profile-form"><span class="eyebrow">Profile</span><h2>Personal details</h2><div class="form-grid"><label>Full name<input name="name" value="${escapeHtml(user.name)}" required></label><label>Username<input value="${escapeHtml(user.username ?? "—")}" disabled></label><label class="profile-email-field">Email<div class="profile-email-control"><input name="email" value="${escapeHtml(user.email)}" disabled autocomplete="email"><button type="button" class="icon-button profile-email-edit" data-email-edit aria-label="Edit email" title="Edit email"><i data-lucide="pencil"></i></button></div><span class="profile-email-actions hidden"><button type="button" class="button button-secondary" data-email-cancel>Cancel</button><button type="button" class="button button-dark" data-email-save>Change email</button></span><small class="field-error" data-email-error></small></label><label>Phone<input name="phone" type="tel" inputmode="tel" value="${escapeHtml(user.phone ?? "")}"></label><label>Role<input value="${escapeHtml(user.role_display_name)}" disabled></label></div><div class="profile-email-verify hidden" data-email-verify><p>We&apos;ve sent a verification code to <strong data-email-pending></strong></p><label>Verification code<input data-email-otp class="otp-input" inputmode="numeric" pattern="[0-9]{6}" maxlength="6" autocomplete="one-time-code" placeholder="000000"></label><div class="profile-email-actions"><button type="button" class="button button-secondary" data-email-cancel-pending>Keep current email</button><button type="button" class="button button-dark" data-email-verify-submit>Verify email</button></div><button type="button" class="button button-quiet" data-email-resend>Resend code</button><small class="field-error" data-email-verify-error></small></div><p class="muted">Username, role, permissions and email identity are protected account fields.</p><button class="button button-dark" type="submit">Save profile</button></form><section class="panel settings-panel profile-account"><span class="eyebrow">Account</span><h2>Account status</h2><div class="detail-grid"><div><span>Email verification</span><strong data-email-verification>${user.email_verified ? "Verified" : "Not verified"}</strong></div><div><span>Account status</span><strong>${user.active === false ? "Inactive" : "Active"}</strong></div><div><span>Created</span><strong>${escapeHtml(String(user.created_at ?? "—"))}</strong></div></div></section><form class="panel settings-panel password-form"><span class="eyebrow">Security</span><h2>Change password</h2><div class="form-grid"><label>Current password<input name="current_password" type="password" autocomplete="current-password" required></label><label>New password<input name="new_password" type="password" minlength="10" autocomplete="new-password" required></label></div><button class="button button-secondary" type="submit">Change password</button><button type="button" class="button button-danger profile-signout">Sign out</button></form></div></div>`;
  enhancePasswordFields(body);
  const emailField = body.querySelector<HTMLInputElement>('[name="email"]');
  const emailEdit = body.querySelector<HTMLButtonElement>('[data-email-edit]');
  const emailEditActions = body.querySelector<HTMLElement>('.profile-email-field .profile-email-actions');
  const emailVerify = body.querySelector<HTMLElement>('[data-email-verify]');
  const emailError = body.querySelector<HTMLElement>('[data-email-error]');
  const emailVerifyError = body.querySelector<HTMLElement>('[data-email-verify-error]');
  const currentEmail = () => appStore.state.user?.email ?? user.email;
  const showEmailError = (node: HTMLElement | null, message = "") => { if (node) node.textContent = message; };
  const showEmailEdit = () => {
    if (!emailField || !emailEdit || !emailEditActions) return;
    emailField.disabled = false; emailField.focus(); emailEdit.hidden = true; emailEditActions.classList.remove("hidden"); showEmailError(emailError);
  };
  const resetEmailEdit = () => {
    if (!emailField || !emailEdit || !emailEditActions) return;
    emailField.value = currentEmail(); emailField.disabled = true; emailEdit.hidden = false; emailEditActions.classList.add("hidden"); showEmailError(emailError);
  };
  const showEmailVerification = (pendingEmail: string) => {
    if (!emailVerify) return;
    emailVerify.querySelector<HTMLElement>('[data-email-pending]')!.textContent = pendingEmail;
    emailVerify.classList.remove("hidden"); emailEdit?.setAttribute("hidden", "true"); emailEditActions?.classList.add("hidden");
    emailField?.setAttribute("disabled", "true"); showEmailError(emailVerifyError); emailVerify.querySelector<HTMLInputElement>('[data-email-otp]')?.focus();
  };
  const hideEmailVerification = () => { emailVerify?.classList.add("hidden"); emailEdit?.removeAttribute("hidden"); if (emailField) { emailField.value = currentEmail(); emailField.disabled = true; } };
  if (user.pending_email) showEmailVerification(user.pending_email);
  emailEdit?.addEventListener("click", showEmailEdit);
  body.querySelector<HTMLButtonElement>('[data-email-cancel]')?.addEventListener("click", resetEmailEdit);
  body.querySelector<HTMLButtonElement>('[data-email-save]')?.addEventListener("click", async (event) => {
    if (!emailField) return;
    const button = event.currentTarget as HTMLButtonElement;
    if (!emailField.checkValidity()) { emailField.reportValidity(); return; }
    button.disabled = true; showEmailError(emailError);
    try {
      const result = await profileApi.requestEmailChange(emailField.value.trim());
      appStore.set({ user: { ...appStore.state.user!, pending_email: result.pending_email, pending_email_verification_expires_at: result.expires_at } });
      showEmailVerification(result.pending_email); toast("Verification code sent to the new email", "info");
    } catch (error) { showEmailError(emailError, error instanceof Error ? error.message : "Email change could not be started"); }
    finally { button.disabled = false; }
  });
  body.querySelector<HTMLButtonElement>('[data-email-verify-submit]')?.addEventListener("click", async (event) => {
    const code = emailVerify?.querySelector<HTMLInputElement>('[data-email-otp]')?.value.trim() ?? "";
    const button = event.currentTarget as HTMLButtonElement;
    if (!/^\d{6}$/.test(code)) { showEmailError(emailVerifyError, "Enter the six-digit verification code."); return; }
    button.disabled = true; showEmailError(emailVerifyError);
    try {
      const updated = await profileApi.verifyEmailChange(code);
      appStore.set({ user: updated });
      body.querySelector<HTMLElement>('[data-profile-email]')!.textContent = updated.email;
      body.querySelector<HTMLElement>('[data-email-verification]')!.textContent = updated.email_verified ? "Verified" : "Not verified";
      hideEmailVerification(); toast("Email address updated");
    } catch (error) { showEmailError(emailVerifyError, error instanceof Error ? error.message : "Email could not be verified"); }
    finally { button.disabled = false; }
  });
  body.querySelector<HTMLButtonElement>('[data-email-resend]')?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    button.disabled = true; showEmailError(emailVerifyError);
    try { const result = await profileApi.resendEmailChange(); appStore.set({ user: { ...appStore.state.user!, pending_email: result.pending_email, pending_email_verification_expires_at: result.expires_at } }); showEmailVerification(result.pending_email); toast("A new verification code was sent", "info"); }
    catch (error) { showEmailError(emailVerifyError, error instanceof Error ? error.message : "Verification code could not be resent"); }
    finally { button.disabled = false; }
  });
  body.querySelector<HTMLButtonElement>('[data-email-cancel-pending]')?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    button.disabled = true; showEmailError(emailVerifyError);
    try { const updated = await profileApi.cancelEmailChange(); appStore.set({ user: updated }); hideEmailVerification(); toast("Email change cancelled", "info"); }
    catch (error) { showEmailError(emailVerifyError, error instanceof Error ? error.message : "Email change could not be cancelled"); }
    finally { button.disabled = false; }
  });
  body.querySelector<HTMLButtonElement>('[data-show-permissions]')?.addEventListener('click', () => {
    const content = document.createElement('div');
    content.innerHTML = `<p class="permission-modal-count">${permissions.length} permissions assigned to ${escapeHtml(user.name)}.</p><div class="permission-groups">${Object.entries(permissionGroups).sort(([a], [b]) => a.localeCompare(b)).map(([module, values]) => `<section><h3>${escapeHtml(module.replace(/_/g, ' '))}</h3><ul>${values.sort().map((value) => `<li><i data-lucide="check"></i>${escapeHtml(value)}</li>`).join('')}</ul></section>`).join('')}</div>`;
    openModal('Your Permissions', content, 'wide'); refreshIcons(content);
  });
  body.querySelector<HTMLFormElement>(".profile-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    try {
      const updated = await profileApi.update({ name: String(data.get("name") ?? ""), phone: String(data.get("phone") ?? "") });
      appStore.set({ user: updated });
      toast("Profile updated");
    } catch (error) { toast(error instanceof Error ? error.message : "Profile could not be updated", "error"); }
  });
  body.querySelector<HTMLFormElement>(".password-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const data = new FormData(form);
    try { await authApi.changePassword(String(data.get("current_password") ?? ""), String(data.get("new_password") ?? "")); form.reset(); toast("Password changed successfully"); }
    catch (error) { toast(error instanceof Error ? error.message : "Password could not be changed", "error"); }
  });
  body.querySelector<HTMLButtonElement>(".profile-signout")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    button.disabled = true; button.textContent = "Signing out...";
    await logout();
  });
  refreshIcons(page); return page;
}
