import { cartApi, customerCompanyApi, profileApi, rateApi } from "../api";
import { api } from "../api/client";
import { appStore } from "../state/store";
import { escapeHtml } from "../utils/dom";
import { refreshIcons } from "../components/icons";
import { toast } from "../components/toast";
import { hasCustomerContext, customerGuardMessage } from "../guards/customer-context";
import { logout } from "../auth/logout";
import { createWatermark, setWatermarkEnabled } from "../components/watermark";

interface NavItem { id: string; label: string; path: string; icon: string; permission: string }
interface NavGroup { key: string; label: string; icon: string; items: NavItem[] }
type FxSnapshot = Awaited<ReturnType<typeof rateApi.get>>;

let fxSnapshot: FxSnapshot | null = null;
let fxRequest: Promise<FxSnapshot | null> | null = null;

function loadFxSnapshot(force = false): Promise<FxSnapshot | null> {
  if (fxSnapshot && !force) return Promise.resolve(fxSnapshot);
  if (!fxRequest) fxRequest = rateApi.get(force).then((value) => { fxSnapshot = value; return value; }).catch(() => fxSnapshot).finally(() => { fxRequest = null; });
  return fxRequest;
}

function fxDate(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fxValues(base: string, rates: Record<string, number>): Array<[string, number | null]> {
  const eurUsd = Number(rates.USD); const eurInr = Number(rates.INR);
  if (!(eurUsd > 0) || !(eurInr > 0)) return [];
  if (base === "USD") return [["INR", eurInr / eurUsd], ["EUR", 1 / eurUsd]];
  if (base === "INR") return [["USD", eurUsd / eurInr], ["EUR", 1 / eurInr]];
  return [["USD", eurUsd], ["INR", eurInr]];
}

function fxSymbol(currency: string): string { return currency === "USD" ? "$" : currency === "INR" ? "₹" : "€"; }

function renderFxPopover(popover: HTMLElement): void {
  const currency = appStore.state.currency;
  const snapshot = fxSnapshot;
  if (!snapshot) { popover.innerHTML = `<span class="eyebrow">FX Rates</span><strong>FX rates unavailable</strong><small>Try again when the rate service is available.</small>`; return; }
  const rows = fxValues(currency, snapshot.rates).map(([target, value]) => {
    return `<div class="fx-rate-row"><span>1 ${currency}</span><strong>= ${value === null ? "—" : `${fxSymbol(target)}${value.toFixed(4)}`}</strong><em>${target}</em></div>`;
  }).join("");
  const status = snapshot.status === "stored_fallback" || snapshot.stale ? "Using last successful ECB rate" : "Latest available";
  const providerDate = snapshot.provider_dates?.USD || snapshot.provider_dates?.INR;
  popover.innerHTML = `<div class="fx-popover-title"><span class="eyebrow">FX Rates</span><strong>Base: ${currency}</strong></div><div class="fx-rate-list">${rows}</div><div class="fx-meta"><span>Latest available ECB Reference Rate</span><span>Rate date: ${escapeHtml(providerDate ?? "Unavailable")}</span><span>Fetched: ${escapeHtml(fxDate(snapshot.fetched_at))}</span><strong class="fx-status ${status === "Latest available" ? "is-live" : "is-cached"}"><i></i>${status}</strong></div>`;
  refreshIcons(popover);
}

const directNav: NavItem[] = [
  { id: "dashboard", label: "Dashboard", path: "/dashboard", icon: "layout-dashboard", permission: "dashboard.view" },
  { id: "reports", label: "Reports", path: "/reports", icon: "chart-spline", permission: "reports.view" },
  { id: "crm", label: "CRM", path: "/crm", icon: "chart-no-axes-combined", permission: "crm.view" },
];

const navGroups: NavGroup[] = [
  { key: "sales", label: "Sales", icon: "shopping-cart", items: [
    { id: "customers", label: "Customers", path: "/customers", icon: "building-2", permission: "customers.view" },
    { id: "calculator", label: "Calculator", path: "/calculator", icon: "calculator", permission: "calculator.view" },
    { id: "cart", label: "Cart", path: "/cart", icon: "shopping-cart", permission: "cart.view" },
    { id: "quotations", label: "Quotations", path: "/quotations", icon: "file-text", permission: "quotations.view" },
    { id: "orders", label: "Orders", path: "/orders", icon: "shopping-bag", permission: "orders.view" },
  ] },
  { key: "users", label: "Users", icon: "users", items: [
    { id: "user-list", label: "User List", path: "/users", icon: "users", permission: "users.view" },
    { id: "roles-permissions", label: "Roles & Permissions", path: "/users?section=roles", icon: "shield-check", permission: "users.view" },
    { id: "incentives", label: "Incentives", path: "/incentives", icon: "badge-euro", permission: "incentives.view" },
    { id: "activity-devices", label: "Activity / Login Devices", path: "/users?section=devices", icon: "monitor-smartphone", permission: "users.view" },
  ] },
  { key: "settings", label: "Settings", icon: "settings", items: [
    { id: "settings-brand", label: "Settings", path: "/settings", icon: "palette", permission: "settings.view" },
    { id: "product-pricing", label: "Product & Pricing", path: "/admin", icon: "badge-euro", permission: "pricing.history" },
    { id: "currencies", label: "Currencies", path: "/settings/currencies", icon: "euro", permission: "settings.view" },
    { id: "communication", label: "Communication", path: "/settings/communication", icon: "mail", permission: "settings.view" },
    { id: "security", label: "Security", path: "/settings/security", icon: "shield-check", permission: "settings.view" },
  ] },
];

export function renderShell(): HTMLElement {
  const state = appStore.state;
  const customerOptions = state.customer && !state.customers.some((customer) => (customer.customer_id ?? customer._id) === (state.customer?.customer_id ?? state.customer?._id))
    ? [state.customer, ...state.customers]
    : state.customers;
  const root = document.createElement("div");
  root.className = `app-shell ${localStorage.getItem("moneda-sidebar") === "collapsed" ? "sidebar-collapsed" : ""}`;
  const watermark = createWatermark(state.user, state.watermarkEnabled);
  const renderNavLink = (item: NavItem, child = false, leaf = false) => {
    const customerLocked = ["/cart"].includes(item.path) && !hasCustomerContext();
    return `<a href="${item.path}" data-route="${item.path}" data-nav-id="${item.id}" class="nav-link${child ? " nav-child" : ""}${customerLocked ? " nav-link-locked" : ""}" aria-label="${item.label}"${leaf ? ` data-nav-leaf="true"` : ""}${customerLocked ? ` aria-disabled="true" data-customer-guard="true" title="Select a customer first"` : ""}><span class="nav-icon" aria-hidden="true"><i data-lucide="${customerLocked ? "lock-keyhole" : item.icon}"></i></span><span class="nav-label">${item.label}</span>${item.path === "/cart" ? `<b data-cart-count>${state.cartCount || ""}</b>` : ""}</a>`;
  };
  const renderDirect = (item: NavItem) => appStore.can(item.permission) ? renderNavLink(item, false, true) : "";
  const renderGroup = (group: NavGroup) => {
    const visible = group.items.filter((item) => appStore.can(item.permission));
    if (!visible.length) return "";
    const childLinks = visible.map((item) => renderNavLink(item, true)).join("");
    return `<section class="nav-group" data-nav-group="${group.key}"><button type="button" class="nav-parent" data-nav-parent="${group.key}" aria-expanded="false" aria-controls="nav-submenu-${group.key}" aria-label="${group.label}" title="${group.label}"><span class="nav-icon" aria-hidden="true"><i data-lucide="${group.icon}"></i></span><span class="nav-label">${group.label}</span><i class="nav-chevron" data-lucide="chevron-right" aria-hidden="true"></i></button><div class="nav-submenu" id="nav-submenu-${group.key}" hidden><strong class="nav-flyout-title">${group.label}</strong>${childLinks}</div></section>`;
  };
  const nav = `<div class="nav-section">Home</div>${renderDirect(directNav[0])}<div class="nav-section">Sales</div>${renderGroup(navGroups[0])}<div class="nav-section">Users</div>${renderGroup(navGroups[1])}<div class="nav-section">Reports</div>${renderDirect(directNav[1])}<div class="nav-section">CRM</div>${renderDirect(directNav[2])}<div class="nav-section">Settings</div>${renderGroup(navGroups[2])}`;
  root.innerHTML = `
    <aside class="sidebar" aria-label="Primary navigation">
      <div class="sidebar-header"><a class="brand-home" href="/dashboard" data-route="/dashboard" aria-label="${escapeHtml(state.brandName)} dashboard"><img src="${escapeHtml(state.brandLogoPath)}" alt="${escapeHtml(state.brandName)}"></a></div>
      <nav>${nav}</nav>
      <div class="sidebar-foot"><button class="icon-button sidebar-toggle" aria-label="Collapse navigation" title="Collapse navigation"><i data-lucide="panel-left-close"></i></button><div class="sidebar-account"><div class="avatar">${escapeHtml(state.user?.name?.slice(0, 2).toUpperCase() ?? "MT")}</div><div><strong>${escapeHtml(state.user?.name)}</strong><span>${escapeHtml(state.user?.role_display_name)}</span></div><a href="/profile" data-route="/profile" aria-label="Profile" title="Open profile"><i data-lucide="chevron-right"></i></a></div></div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <button class="icon-button mobile-menu" aria-label="Open navigation" title="Open navigation"><i data-lucide="menu"></i></button>
        <button class="search-trigger"><i data-lucide="search"></i><span>Search customers, quotes, products…</span><kbd>Ctrl K</kbd></button>
        <div class="top-actions">
          ${state.customers.length ? `<label class="compact-select customer-select"><span>Customer</span><select id="company-switcher" aria-label="Select customer"><option value="" ${!state.activeCustomerId ? "selected" : ""}>All Customers</option>${customerOptions.map((customer) => `<option value="${customer.customer_id ?? customer._id}" ${(customer.customer_id ?? customer._id) === state.activeCustomerId ? "selected" : ""}>${escapeHtml(customer.company_name ?? customer.name)}</option>`).join("")}</select></label>` : '<a class="select-company-action" href="/customer-selection" data-route="/customer-selection"><i data-lucide="building-2"></i>Select Customer</a>'}
          <div class="currency-fx-control" data-fx-control><label class="compact-select currency-select"><span>Display Currency</span><select id="currency-switcher" aria-label="Select display currency" aria-describedby="fx-popover">${["EUR", "USD", "INR"].map((currency) => `<option ${currency === state.currency ? "selected" : ""}>${currency}</option>`).join("")}</select></label><div id="fx-popover" class="fx-popover" role="tooltip" aria-label="Foreign exchange rates"></div></div>
          <div class="notification-control"><button class="icon-button" id="notification-button" aria-label="Notifications" title="Notifications" aria-expanded="false"><i data-lucide="bell"></i><span class="notification-dot" data-notification-count>${state.notificationCount || ""}</span></button><div id="notification-popover" class="notification-popover" role="dialog" aria-label="Notifications" hidden></div></div>
          <div class="user-menu-control"><button class="avatar avatar-button" id="user-menu-button" aria-label="Open user menu" aria-expanded="false">${escapeHtml(state.user?.name?.slice(0, 2).toUpperCase() ?? "MT")}</button><div id="user-menu" class="user-menu" role="menu" hidden><div class="user-menu-head"><strong>${escapeHtml(state.user?.name ?? "User")}</strong><span>${escapeHtml(state.user?.role_display_name ?? "User")}</span></div><a href="/profile" data-route="/profile" role="menuitem"><i data-lucide="user-round"></i>Profile</a><button type="button" data-open-notifications role="menuitem"><i data-lucide="bell"></i>Notifications</button><div class="user-menu-divider"></div><button type="button" data-sign-out role="menuitem"><i data-lucide="log-out"></i>Sign out</button></div></div>
        </div>
      </header>
      <main id="main-content" tabindex="-1"><div class="page-loading"><span></span><p>Loading workspace…</p></div></main>
    </div>
    <div class="mobile-scrim"></div>
    <dialog id="command-palette" class="command-palette"><form method="dialog"><div class="command-input"><i data-lucide="search"></i><input aria-label="Global search" placeholder="Search across Moneda" autocomplete="off"><button aria-label="Close">ESC</button></div><div class="command-results"><p>Start typing to search products, customers, quotations, orders and leads.</p></div></form></dialog>`;
  root.prepend(watermark);

  document.querySelectorAll<HTMLElement>(".nav-leaf-tooltip").forEach((node) => node.remove());
  const leafTooltip = document.createElement("div");
  leafTooltip.className = "nav-leaf-tooltip";
  leafTooltip.setAttribute("role", "tooltip");
  leafTooltip.hidden = true;
  document.body.appendChild(leafTooltip);
  let hoveredItemId: string | null = null;
  let leafTooltipTimer = 0;
  const hideLeafTooltip = () => {
    window.clearTimeout(leafTooltipTimer);
    hoveredItemId = null;
    leafTooltip.hidden = true;
  };
  const positionLeafTooltip = (link: HTMLElement) => {
    if (leafTooltip.hidden || !root.classList.contains("sidebar-collapsed")) return;
    const rect = link.getBoundingClientRect();
    const tooltipHeight = leafTooltip.getBoundingClientRect().height || 0;
    leafTooltip.style.left = `${rect.right + 8}px`;
    leafTooltip.style.top = `${Math.max(8, Math.min(Math.max(8, window.innerHeight - tooltipHeight - 8), rect.top + rect.height / 2))}px`;
  };
  const showLeafTooltip = (link: HTMLAnchorElement) => {
    if (!root.classList.contains("sidebar-collapsed")) return;
    const item = directNav.find((candidate) => candidate.id === link.dataset.navId);
    if (!item) return;
    hoveredItemId = item.id;
    window.clearTimeout(leafTooltipTimer);
    leafTooltipTimer = window.setTimeout(() => {
      if (hoveredItemId !== item.id || !root.isConnected) return;
      leafTooltip.textContent = item.label;
      leafTooltip.hidden = false;
      positionLeafTooltip(link);
    }, 70);
  };
  root.querySelectorAll<HTMLAnchorElement>("[data-nav-leaf]").forEach((link) => {
    link.addEventListener("pointerenter", () => showLeafTooltip(link));
    link.addEventListener("pointerleave", hideLeafTooltip);
    link.addEventListener("focusin", () => showLeafTooltip(link));
    link.addEventListener("focusout", hideLeafTooltip);
  });
  const repositionLeafTooltip = () => {
    const active = hoveredItemId ? [...root.querySelectorAll<HTMLElement>("[data-nav-id]")].find((node) => node.dataset.navId === hoveredItemId) ?? null : null;
    if (active) positionLeafTooltip(active);
    if (!root.isConnected) { hideLeafTooltip(); leafTooltip.remove(); window.removeEventListener("resize", repositionLeafTooltip); window.removeEventListener("scroll", repositionLeafTooltip, true); }
  };
  window.addEventListener("resize", repositionLeafTooltip);
  window.addEventListener("scroll", repositionLeafTooltip, true);

  const sidebarToggle = root.querySelector<HTMLButtonElement>(".sidebar-toggle");
  const updateSidebarToggle = () => {
    const collapsed = root.classList.contains("sidebar-collapsed");
    sidebarToggle?.setAttribute("aria-label", collapsed ? "Expand navigation" : "Collapse navigation");
    sidebarToggle?.setAttribute("title", collapsed ? "Expand navigation" : "Collapse navigation");
    if (sidebarToggle) { sidebarToggle.innerHTML = `<i data-lucide="${collapsed ? "panel-left-open" : "panel-left-close"}"></i>`; refreshIcons(sidebarToggle); }
  };
  updateSidebarToggle();
  sidebarToggle?.addEventListener("click", () => {
    root.classList.toggle("sidebar-collapsed");
    if (!root.classList.contains("sidebar-collapsed")) hideLeafTooltip();
    localStorage.setItem("moneda-sidebar", root.classList.contains("sidebar-collapsed") ? "collapsed" : "open");
    updateSidebarToggle();
    root.querySelectorAll<HTMLElement>(".nav-group.is-open").forEach((group) => {
      const submenu = group.querySelector<HTMLElement>(".nav-submenu");
      if (root.classList.contains("sidebar-collapsed") && submenu) positionFlyout(group);
      else if (submenu) { submenu.style.removeProperty("top"); submenu.style.removeProperty("left"); }
    });
  });
  const positionFlyout = (group: HTMLElement) => {
    const submenu = group.querySelector<HTMLElement>(".nav-submenu");
    if (!submenu || !root.classList.contains("sidebar-collapsed")) return;
    const bounds = group.getBoundingClientRect();
    const flyoutHeight = submenu.getBoundingClientRect().height || 0;
    submenu.style.top = `${Math.max(8, Math.min(Math.max(8, window.innerHeight - flyoutHeight - 8), bounds.top))}px`;
    submenu.style.left = `${bounds.right + 8}px`;
  };
  const setGroupOpen = (group: HTMLElement, open: boolean) => {
    const submenu = group.querySelector<HTMLElement>(".nav-submenu");
    const parent = group.querySelector<HTMLButtonElement>(".nav-parent");
    if (!submenu || !parent) return;
    group.classList.toggle("is-open", open);
    submenu.hidden = !open;
    parent.setAttribute("aria-expanded", String(open));
    if (open) positionFlyout(group);
    else { submenu.style.removeProperty("top"); submenu.style.removeProperty("left"); }
  };
  let navCloseTimer = 0;
  let openFlyoutId: string | null = null;
  root.querySelectorAll<HTMLElement>(".nav-group").forEach((group) => {
    const parent = group.querySelector<HTMLButtonElement>(".nav-parent");
    const groupId = group.dataset.navGroup ?? "";
    const openOnlyThisGroup = (open: boolean) => {
      if (open && root.classList.contains("sidebar-collapsed")) {
        root.querySelectorAll<HTMLElement>(".nav-group.is-open").forEach((other) => { if (other !== group) setGroupOpen(other, false); });
        openFlyoutId = groupId;
      } else if (!open && openFlyoutId === groupId) openFlyoutId = null;
      setGroupOpen(group, open);
    };
    parent?.addEventListener("click", () => openOnlyThisGroup(!group.classList.contains("is-open")));
    group.addEventListener("mouseenter", () => {
      if (!root.classList.contains("sidebar-collapsed")) return;
      window.clearTimeout(navCloseTimer); openOnlyThisGroup(true);
    });
    group.addEventListener("mouseleave", () => {
      if (!root.classList.contains("sidebar-collapsed") || group.matches(":focus-within")) return;
      navCloseTimer = window.setTimeout(() => openOnlyThisGroup(false), 180);
    });
    group.addEventListener("focusin", () => { if (root.classList.contains("sidebar-collapsed")) openOnlyThisGroup(true); });
    group.addEventListener("focusout", () => {
      if (root.classList.contains("sidebar-collapsed")) window.setTimeout(() => { if (!group.matches(":focus-within")) openOnlyThisGroup(false); }, 0);
    });
  });
  document.addEventListener("click", (event) => {
    if (!root.isConnected || root.contains(event.target as Node)) return;
    root.querySelectorAll<HTMLElement>(".nav-group.is-open").forEach((group) => setGroupOpen(group, false));
  });
  window.addEventListener("resize", () => root.querySelectorAll<HTMLElement>(".nav-group.is-open").forEach(positionFlyout));
  window.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    root.querySelectorAll<HTMLElement>(".nav-group.is-open").forEach((group) => setGroupOpen(group, false));
    openFlyoutId = null;
  });
  root.querySelector(".mobile-menu")?.addEventListener("click", () => root.classList.add("mobile-nav-open"));
  root.querySelector(".mobile-scrim")?.addEventListener("click", () => root.classList.remove("mobile-nav-open"));
  const userMenuButton = root.querySelector<HTMLButtonElement>("#user-menu-button");
  const userMenu = root.querySelector<HTMLElement>("#user-menu");
  const closeUserMenu = () => { if (userMenu) userMenu.hidden = true; userMenuButton?.setAttribute("aria-expanded", "false"); };
  userMenuButton?.addEventListener("click", (event) => { event.stopPropagation(); if (!userMenu) return; userMenu.hidden = !userMenu.hidden; userMenuButton.setAttribute("aria-expanded", String(!userMenu.hidden)); });
  userMenu?.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", closeUserMenu);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeUserMenu(); });
  root.querySelector<HTMLButtonElement>("[data-sign-out]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget as HTMLButtonElement;
    button.disabled = true; button.innerHTML = '<i data-lucide="loader-circle"></i>Signing out...'; refreshIcons(button);
    await logout();
  });
  root.querySelector<HTMLButtonElement>("[data-open-notifications]")?.addEventListener("click", () => {
    closeUserMenu(); root.querySelector<HTMLButtonElement>("#notification-button")?.click();
  });
  root.querySelectorAll<HTMLAnchorElement>("a[data-customer-guard]").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    toast(customerGuardMessage(link.getAttribute("href") ?? "/calculator"), "info");
  }));
  root.querySelector<HTMLSelectElement>("#company-switcher")?.addEventListener("change", async (event) => {
    const selected = state.customers.find((customer) => (customer.customer_id ?? customer._id) === (event.target as HTMLSelectElement).value) ?? null;
    const select = event.target as HTMLSelectElement;
    select.disabled = true;
    try {
      if (!selected) {
        await customerCompanyApi.clearSelection();
        localStorage.removeItem("moneda-active-customer-id");
        appStore.set({ customer: null, activeCustomerId: null, customerCompany: null, company: null, currency: "EUR", cartCount: 0 });
        root.replaceWith(renderShell());
        window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: location.pathname }));
        return;
      }
      if (state.customer && (state.customer.customer_id ?? state.customer._id) !== (selected.customer_id ?? selected._id)) {
        const currentCustomerId = state.customer.customer_id ?? state.customer._id;
        const currentCart = await cartApi.get(currentCustomerId, state.currency).catch(() => null);
        if (currentCart?.items.length) {
          const confirmed = window.confirm(`Switch customer?\n\nYour current cart belongs to ${state.customer.name}. ${selected.name} has a separate cart.\n\nCancel to stay, or OK to switch without deleting either cart.`);
          if (!confirmed) { select.value = state.activeCustomerId ?? ""; return; }
        }
      }
      const selectedId = selected.customer_id ?? selected._id;
      await customerCompanyApi.select(selectedId);
      localStorage.setItem("moneda-active-customer-id", selectedId);
      const nextCurrency = selected.preferred_currency ?? selected.default_currency ?? "EUR";
      const nextCart = await cartApi.get(selectedId, nextCurrency).catch(() => null);
      appStore.set({ customer: selected, activeCustomerId: selectedId, customerCompany: selected as unknown as import("../types/domain").Company, company: selected as unknown as import("../types/domain").Company, currency: nextCurrency, cartCount: nextCart?.items.length ?? 0 });
      window.dispatchEvent(new CustomEvent("moneda:navigate", { detail: location.pathname }));
    } catch (error) {
      select.value = state.activeCustomerId ?? "";
      toast(error instanceof Error ? error.message : "Customer change failed", "error");
    } finally { select.disabled = false; }
  });
  appStore.subscribe((nextState) => {
    if (!root.isConnected) return;
    const badge = root.querySelector<HTMLElement>("[data-cart-count]");
    if (badge) badge.textContent = nextState.cartCount ? String(nextState.cartCount) : "";
    setWatermarkEnabled(watermark, nextState.watermarkEnabled);
  });
  const fxControl = root.querySelector<HTMLElement>("[data-fx-control]");
  const fxPopover = root.querySelector<HTMLElement>("#fx-popover");
  const currencySelect = root.querySelector<HTMLSelectElement>("#currency-switcher");
  let closeTimer = 0;
  const setFxOpen = (open: boolean) => { window.clearTimeout(closeTimer); fxControl?.classList.toggle("is-open", open); };
  const scheduleFxClose = () => { window.clearTimeout(closeTimer); closeTimer = window.setTimeout(() => setFxOpen(false), 100); };
  const refreshFx = () => { setFxOpen(true); void loadFxSnapshot(false).then((snapshot) => { if (snapshot) appStore.set({ fxRates: snapshot.rates }); renderFx(); }); };
  fxControl?.addEventListener("mouseenter", refreshFx);
  fxControl?.addEventListener("mouseleave", scheduleFxClose);
  currencySelect?.addEventListener("focus", refreshFx);
  currencySelect?.addEventListener("click", refreshFx);
  currencySelect?.addEventListener("change", (event) => {
    const nextCurrency = (event.target as HTMLSelectElement).value as "USD" | "INR" | "EUR";
    const previousCurrency = appStore.state.currency;
    if (nextCurrency === previousCurrency) return;
    // Display currency is session/UI state only. Do not persist it on the
    // customer, select the customer again, or navigate/remount the route.
    appStore.set({ currency: nextCurrency });
    if (import.meta.env.DEV) console.debug("display_currency_changed", {
      previous_currency: previousCurrency, new_currency: nextCurrency,
      route_unchanged: true, calculator_state_preserved: true,
    });
  });
  currencySelect?.addEventListener("keydown", (event) => { if (event.key === "Escape") { setFxOpen(false); currencySelect.blur(); } });
  fxPopover?.addEventListener("mouseenter", () => setFxOpen(true));
  fxPopover?.addEventListener("mouseleave", scheduleFxClose);
  const closeFxOutside = (event: MouseEvent) => {
    if (!root.isConnected) { document.removeEventListener("click", closeFxOutside); return; }
    if (!fxControl?.contains(event.target as Node)) setFxOpen(false);
  };
  document.addEventListener("click", closeFxOutside);
  const renderFx = () => { if (fxPopover) renderFxPopover(fxPopover); };
  renderFx();
  void loadFxSnapshot(false).then((snapshot) => {
    if (snapshot) appStore.set({ fxRates: snapshot.rates });
    renderFx();
  });
  appStore.subscribe(() => { if (root.isConnected) renderFxPopover(fxPopover!); });
  void profileApi.notifications().then((result) => {
    appStore.set({ notificationCount: result.unread });
    const badge = root.querySelector<HTMLElement>("[data-notification-count]");
    if (badge) badge.textContent = result.unread ? String(result.unread) : "";
    const notificationPopover = root.querySelector<HTMLElement>("#notification-popover");
    const notificationButton = root.querySelector<HTMLButtonElement>("#notification-button");
    const deletingNotificationIds = new Set<string>();
    let clearNotificationsInFlight = false;
    const renderNotifications = () => {
      if (!notificationPopover) return;
      const rows = result.items;
      const controls = `${result.unread ? `<button type="button" data-mark-all>Mark all read</button>` : ""}${rows.length ? `<button type="button" data-clear-all>Clear all</button>` : ""}`;
      notificationPopover.innerHTML = `<div class="notification-popover-head"><strong>Notifications</strong><span class="notification-popover-actions">${controls}</span></div>${rows.length ? `<div class="notification-list">${rows.map((row) => { const id = String(row._id ?? row.id ?? ""); return `<div class="notification-item${row.read ? " is-read" : ""}"><button type="button" class="notification-item-main" data-notification-id="${escapeHtml(id)}"><span class="notification-item-dot"></span><span><strong>${escapeHtml(String(row.title ?? "Notification"))}</strong><small>${escapeHtml(String(row.message ?? ""))}</small><em>${row.created_at ? fxDate(String(row.created_at)) : ""}</em></span></button><button type="button" class="notification-delete" data-notification-delete="${escapeHtml(id)}" aria-label="Delete notification" title="Delete notification"><i data-lucide="trash-2"></i></button></div>`; }).join("")}</div>` : `<p class="notification-empty">No new notifications</p>`}`;
      refreshIcons(notificationPopover);
      notificationPopover.querySelectorAll<HTMLButtonElement>("[data-notification-id]").forEach((item) => item.addEventListener("click", async () => {
        const id = item.dataset.notificationId; if (!id) return;
        await profileApi.markNotificationRead(id).catch(() => undefined);
        const found = result.items.find((row) => String(row._id ?? row.id) === id); if (found) found.read = true;
        result.unread = result.items.filter((row) => !row.read).length;
        appStore.set({ notificationCount: result.unread });
        const badge = root.querySelector<HTMLElement>("[data-notification-count]"); if (badge) badge.textContent = result.unread ? String(result.unread) : "";
        renderNotifications();
      }));
      notificationPopover.querySelector<HTMLButtonElement>("[data-mark-all]")?.addEventListener("click", async () => {
        await Promise.all(result.items.filter((row) => !row.read).map((row) => profileApi.markNotificationRead(String(row._id ?? row.id)).catch(() => undefined)));
        result.items.forEach((row) => { row.read = true; }); result.unread = 0; appStore.set({ notificationCount: 0 });
        const badge = root.querySelector<HTMLElement>("[data-notification-count]"); if (badge) badge.textContent = ""; renderNotifications();
      });
      notificationPopover.querySelectorAll<HTMLButtonElement>("[data-notification-delete]").forEach((item) => item.addEventListener("click", async () => {
        const id = item.dataset.notificationDelete; if (!id) return;
        if (deletingNotificationIds.has(id)) return;
        deletingNotificationIds.add(id); item.disabled = true;
        try {
          await profileApi.deleteNotification(id);
          const index = result.items.findIndex((row) => String(row._id ?? row.id) === id);
          if (index >= 0) result.items.splice(index, 1);
          result.unread = result.items.filter((row) => !row.read).length;
          appStore.set({ notificationCount: result.unread });
          const badge = root.querySelector<HTMLElement>("[data-notification-count]"); if (badge) badge.textContent = result.unread ? String(result.unread) : "";
          renderNotifications();
        } catch {
          toast("Notification could not be deleted.", "error");
        } finally {
          deletingNotificationIds.delete(id); if (item.isConnected) item.disabled = false;
        }
      }));
      notificationPopover.querySelector<HTMLButtonElement>("[data-clear-all]")?.addEventListener("click", async () => {
        if (clearNotificationsInFlight) return;
        clearNotificationsInFlight = true;
        const clearButton = notificationPopover.querySelector<HTMLButtonElement>("[data-clear-all]");
        if (clearButton) clearButton.disabled = true;
        try {
          await profileApi.deleteNotifications();
          result.items.splice(0, result.items.length); result.unread = 0; appStore.set({ notificationCount: 0 });
          const badge = root.querySelector<HTMLElement>("[data-notification-count]"); if (badge) badge.textContent = "";
          renderNotifications();
        } catch {
          toast("Notifications could not be deleted.", "error");
        } finally {
          clearNotificationsInFlight = false; if (clearButton?.isConnected) clearButton.disabled = false;
        }
      });
    };
    renderNotifications();
    notificationButton?.addEventListener("click", () => { const open = !notificationPopover?.hidden; if (notificationPopover) notificationPopover.hidden = open; notificationButton.setAttribute("aria-expanded", String(!open)); });
    document.addEventListener("click", (event) => { if (!root.isConnected) return; const target = event.target as Node; if (!root.querySelector(".notification-control")?.contains(target) && notificationPopover) { notificationPopover.hidden = true; notificationButton?.setAttribute("aria-expanded", "false"); } });
  }).catch(() => {
    const notificationPopover = root.querySelector<HTMLElement>("#notification-popover");
    const notificationButton = root.querySelector<HTMLButtonElement>("#notification-button");
    if (notificationPopover) notificationPopover.innerHTML = '<div class="notification-popover-head"><strong>Notifications</strong></div><p class="notification-empty">Notifications are temporarily unavailable.</p>';
    notificationButton?.addEventListener("click", () => { if (notificationPopover) { notificationPopover.hidden = !notificationPopover.hidden; notificationButton.setAttribute("aria-expanded", String(!notificationPopover.hidden)); } });
  });
  const palette = root.querySelector<HTMLDialogElement>("#command-palette")!;
  const openSearch = () => { palette.showModal(); palette.querySelector("input")?.focus(); };
  root.querySelector(".search-trigger")?.addEventListener("click", openSearch);
  window.addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); openSearch(); } });
  let timer = 0;
  palette.querySelector<HTMLInputElement>("input")?.addEventListener("input", (event) => {
    window.clearTimeout(timer);
    timer = window.setTimeout(async () => {
      const term = (event.target as HTMLInputElement).value.trim();
      const resultsNode = palette.querySelector<HTMLElement>(".command-results")!;
      if (term.length < 2 || !state.customer) { resultsNode.innerHTML = "<p>Type at least two characters.</p>"; return; }
      try {
        const results = await api<{ type: string; id: string; title: string }[]>(`/search?q=${encodeURIComponent(term)}&customer_id=${encodeURIComponent(state.customer.customer_id ?? state.customer._id)}`);
        resultsNode.innerHTML = results.length ? results.map((result) => `<button type="button"><span class="result-type">${escapeHtml(result.type)}</span><strong>${escapeHtml(result.title)}</strong><i data-lucide="arrow-up-right"></i></button>`).join("") : "<p>No matching records.</p>";
        refreshIcons(resultsNode);
      } catch { resultsNode.innerHTML = "<p>Search is temporarily unavailable.</p>"; }
    }, 250);
  });
  refreshIcons(root);
  return root;
}

export function updateActiveNav(path: string): void {
  const routePath = path.split("?", 1)[0].replace(/\/$/, "") || "/";
  const matches = (href: string): boolean => {
    const [targetPath, targetQuery = ""] = href.split("?", 2);
    const target = targetPath.replace(/\/$/, "") || "/";
    const routeQuery = path.includes("?") ? path.slice(path.indexOf("?") + 1) : "";
    if (targetQuery) return routePath === target && routeQuery === targetQuery;
    if (target === "/users" && routeQuery) return false;
    if (target === "/settings") return routePath === target || routePath.startsWith(`${target}/`);
    if (target === "/calculator") return routePath === target || routePath === "/products" || routePath.startsWith("/products/");
    if (target === "/quotations") return routePath === target || routePath.startsWith("/quotations/") || routePath.startsWith("/quotation");
    return routePath === target || routePath.startsWith(`${target}/`);
  };
  document.querySelectorAll(".nav-link").forEach((node) => {
    const href = node.getAttribute("href") ?? "";
    node.classList.toggle("active", matches(href));
  });
  document.querySelectorAll<HTMLElement>(".nav-group").forEach((group) => {
    const active = [...group.querySelectorAll<HTMLAnchorElement>(".nav-child")].some((link) => matches(link.getAttribute("href") ?? ""));
    group.classList.toggle("active", active);
    const parent = group.querySelector<HTMLButtonElement>(".nav-parent");
    const submenu = group.querySelector<HTMLElement>(".nav-submenu");
    const shell = document.querySelector<HTMLElement>(".app-shell");
    if (active && !shell?.classList.contains("sidebar-collapsed")) {
      group.classList.add("is-open");
      if (submenu) submenu.hidden = false;
      parent?.setAttribute("aria-expanded", "true");
    }
  });
}
