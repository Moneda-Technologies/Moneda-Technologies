import { adminApi, authApi, companyApi, profileApi } from "../api";
import { apiEndpoint } from "../api/client";
import { logout } from "../auth/logout";
import { refreshIcons } from "../components/icons";
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
  const page = pageScaffold("Management", "Users & access", "Assign customer scope and permission-backed roles without email-based exceptions.", '<button class="button button-primary"><i data-lucide="user-plus"></i>Invite user</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  try { const [users, roles] = await Promise.all([adminApi.users(), adminApi.roles()]); body.innerHTML = `<div class="access-summary panel"><div><span class="eyebrow">Access model</span><h2>${users.total} users across ${roles.total} roles</h2><p>Server-side permissions remain authoritative for every customer-scoped action.</p></div><div class="role-pills">${roles.items.map((role) => `<span>${escapeHtml(role.display_name)}<b>${(role.permissions as unknown[])?.length ?? 0}</b></span>`).join("")}</div></div><div class="data-table panel"><table><thead><tr><th>User</th><th>Role</th><th>Customers</th><th>Status</th><th></th></tr></thead><tbody>${users.items.map((user) => `<tr><td><div class="table-identity"><span>${escapeHtml(String(user.name).slice(0, 2).toUpperCase())}</span><p><strong>${escapeHtml(user.name)}</strong><small>${escapeHtml(user.email)}</small></p></div></td><td>${escapeHtml(user.role_id)}</td><td>${(user.customer_ids as unknown[] ?? user.company_ids as unknown[])?.length ?? 0} assigned</td><td>${statusBadge(user.active ? "Active" : "Inactive")}</td><td><button class="icon-button" aria-label="More user actions" title="More user actions"><i data-lucide="more-horizontal"></i></button></td></tr>`).join("")}</tbody></table></div>`; }
  catch (error) { body.innerHTML = `<div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Users unavailable</strong><p>${escapeHtml(error instanceof Error ? error.message : "Please try again")}</p></div></div>`; }
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
    body.innerHTML = `<div class="admin-callout"><div><span class="eyebrow">EUR pricing control</span><h2>One master catalogue, live conversions.</h2><p>Review all ${result.pagination?.total ?? result.items.length} active products. ${pending} still require commercial pricing.</p></div><div class="admin-flow"><span>Catalog</span><i data-lucide="chevron-right"></i><span>EUR price</span><i data-lucide="chevron-right"></i><span>Live FX</span><i data-lucide="chevron-right"></i><strong>Product tax</strong></div></div><div class="section-title"><div><span class="eyebrow">Master catalogue</span><h2>Product pricing</h2></div><span class="count-badge">${pending} pending</span></div>${result.items.length ? `<div class="pricing-list panel">${result.items.map(pricingRow).join("")}</div>` : emptyState("circle-check-big", "Catalogue is empty", "Import products to begin pricing.")}`;
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

export async function settingsPage(): Promise<HTMLElement> {
  const page = pageScaffold("Management", "System settings", "Govern branding, tax options and commercial defaults from one place.", '<button class="button button-primary"><i data-lucide="save"></i>Save changes</button>');
  const body = page.querySelector<HTMLElement>(".page-body")!; body.innerHTML = skeleton(5);
  try {
    const settings = await adminApi.settings();
    let zohoMarkup = "";
    if (appStore.can("settings.manage")) {
      zohoMarkup = `<section class="panel settings-panel"><span class="eyebrow">Communication</span><h2>Zoho Mail</h2><p>Application email uses the server-side Zoho Mail API.</p><div class="notice compact"><i data-lucide="mail-check"></i><div><strong>Loading integration status…</strong></div></div></section>`;
      try {
        const status = await adminApi.zohoStatus();
        const state = status.connected ? "Connected" : status.status === "error" ? "Error" : "Not connected";
        const errorMarkup = status.last_error
          ? `<div class="notice error compact"><i data-lucide="circle-alert"></i><div><strong>${escapeHtml(status.last_error.code)}</strong><p>Stage: ${escapeHtml(status.last_error.stage)} · Reference: ${escapeHtml(status.last_error.diagnostic_id)}</p></div></div>`
          : "";
        zohoMarkup = `<section class="panel settings-panel zoho-integration"><span class="eyebrow">Communication</span><h2>Zoho Mail</h2><p>Transactional email is sent only through the server-side Zoho Mail API.</p><div class="integration-status"><span class="status-dot ${status.connected ? "is-live" : status.status === "error" ? "is-error" : ""}"></span><strong>${escapeHtml(state)}</strong><small>Provider: Zoho Mail API</small></div><dl class="integration-details"><div><dt>Account</dt><dd>${escapeHtml(status.account_email || "Unknown")}</dd></div><div><dt>OAuth</dt><dd>${escapeHtml(status.oauth === "connected" ? "Connected" : "Not connected")}</dd></div><div><dt>Account ID</dt><dd>${escapeHtml(status.account_id || "missing")}</dd></div><div><dt>API domain</dt><dd>${escapeHtml(status.api_domain || "missing")} <small>(${escapeHtml(status.api_domain_status)})</small></dd></div><div><dt>Scopes</dt><dd>${status.scopes.map((scope) => escapeHtml(scope)).join("<br>")}</dd></div></dl>${errorMarkup}<div class="settings-actions"><button class="button button-secondary" type="button" data-zoho-connect>${status.connected ? "Reconnect Zoho Mail" : "Connect Zoho Mail"}</button>${status.connected ? '<button class="button button-danger" type="button" data-zoho-disconnect>Disconnect</button>' : ""}</div><form class="integration-test-form" data-zoho-test-form><label>Test recipient email<input type="email" name="to" autocomplete="email" placeholder="recipient@example.com" required ${status.connected ? "" : "disabled"}></label><button class="button button-quiet" type="submit" ${status.connected ? "" : "disabled"}><i data-lucide="send"></i>Test Email</button></form><div class="integration-test-result" data-zoho-test-result></div><p class="form-hint">Refresh and access tokens remain encrypted or memory-only on the server and are never returned to the browser.</p></section>`;
        page.dataset.zohoConnected = String(status.connected);
      } catch (_error) { /* settings remains usable when the integration permission is absent */ }
    }
    body.innerHTML = `<div class="settings-layout"><nav class="settings-nav"><button class="active"><i data-lucide="palette"></i>Brand & company</button><button><i data-lucide="badge-percent"></i>Taxes</button><button><i data-lucide="euro"></i>Currencies</button><button><i data-lucide="mail"></i>Communication</button><button><i data-lucide="shield-check"></i>Security</button></nav><div class="settings-stack"><section class="panel settings-panel"><span class="eyebrow">Brand identity</span><h2>Moneda Technologies</h2><p>Logo paths stay configurable so the official artwork can be replaced without a frontend release.</p><div class="logo-preview"><img src="${escapeHtml(settings.brand_logo_path ?? "/brand/moneda-logo.svg")}" alt="Configured Moneda logo"></div><div class="form-grid"><label>Brand name<input value="${escapeHtml(String(settings.brand_name ?? "Moneda Technologies"))}"></label><label>Logo path<input value="${escapeHtml(String(settings.brand_logo_path ?? "/brand/moneda-logo.svg"))}"></label><label>Master currency<input value="EUR" disabled></label><label>Quotation prefix<input value="${escapeHtml(String(settings.quotation_prefix ?? "MON_Q"))}" disabled></label><label>Default tax<select><option>${escapeHtml(String(settings.default_tax_rate ?? 0))}%</option></select></label><label>Tax mode<select><option>${escapeHtml(String(settings.default_tax_mode ?? "exclusive"))}</option></select></label></div><div class="notice compact"><i data-lucide="lock-keyhole"></i><div><strong>Protected business constants</strong><p>EUR master pricing and the MON_Q numbering namespace are migration-controlled.</p></div></div></section>${zohoMarkup}</div></div>`;
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
  body.innerHTML = `<div class="profile-layout"><aside class="profile-card panel"><div class="profile-avatar">${escapeHtml(user.name.slice(0, 2).toUpperCase())}</div><h2>${escapeHtml(user.name)}</h2><p>${escapeHtml(user.email)}</p>${statusBadge(user.role_display_name)}<div class="profile-stat"><span>Customers</span><strong>${(user.customer_ids ?? user.company_ids ?? []).length}</strong></div><button type="button" class="profile-stat profile-stat-button" data-show-permissions><span>Permissions</span><strong>${permissions.length}</strong></button></aside><div class="profile-stack"><form class="panel settings-panel profile-form"><span class="eyebrow">Personal details</span><h2>Profile information</h2><div class="form-grid"><label>Full name<input name="name" value="${escapeHtml(user.name)}" required></label><label>Email<input value="${escapeHtml(user.email)}" disabled></label><label>Phone<input name="phone" value="${escapeHtml(user.phone ?? "")}"></label><label>Currency preference<select name="currency_preference"><option ${user.currency_preference === "EUR" ? "selected" : ""}>EUR</option><option ${user.currency_preference === "USD" ? "selected" : ""}>USD</option><option ${user.currency_preference === "INR" ? "selected" : ""}>INR</option></select></label></div><button class="button button-dark" type="submit">Update profile</button></form><form class="panel settings-panel password-form"><span class="eyebrow">Security</span><h2>Change password</h2><div class="form-grid"><label>Current password<input name="current_password" type="password" autocomplete="current-password" required></label><label>New password<input name="new_password" type="password" minlength="10" autocomplete="new-password" required></label></div><button class="button button-secondary" type="submit">Change password</button><button type="button" class="button button-danger profile-signout">Sign out</button></form></div></div>`;
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
      const updated = await profileApi.update({ name: String(data.get("name") ?? ""), phone: String(data.get("phone") ?? ""), currency_preference: String(data.get("currency_preference") ?? "EUR") });
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
