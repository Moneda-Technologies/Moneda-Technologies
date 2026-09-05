import { cartApi, customerCompanyApi, profileApi, rateApi } from "../api";
import { api } from "../api/client";
import { appStore } from "../state/store";
import { escapeHtml } from "../utils/dom";
import { refreshIcons } from "../components/icons";
import { toast } from "../components/toast";
import { hasCustomerContext, customerGuardMessage } from "../guards/customer-context";

interface NavItem { label: string; path: string; icon: string; permission: string; section?: string }
type FxSnapshot = Awaited<ReturnType<typeof rateApi.get>>;

let fxSnapshot: FxSnapshot | null = null;
let fxRequest: Promise<FxSnapshot | null> | null = null;

function loadFxSnapshot(): Promise<FxSnapshot | null> {
  if (fxSnapshot) return Promise.resolve(fxSnapshot);
  if (!fxRequest) fxRequest = rateApi.get().then((value) => { fxSnapshot = value; return value; }).catch(() => { fxRequest = null; return null; });
  return fxRequest;
}

function fxDate(value: string | null | undefined): string {
  if (!value) return "Unavailable";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(undefined, { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
}

function fxValue(base: string, target: string, rates: Record<string, number>): number | null {
  const eurUsd = Number(rates.USD); const eurInr = Number(rates.INR);
  if (!(eurUsd > 0) || !(eurInr > 0)) return null;
  if (base === "EUR") return target === "USD" ? eurUsd : eurInr;
  if (base === "USD") return target === "EUR" ? 1 / eurUsd : eurInr / eurUsd;
  return target === "EUR" ? 1 / eurInr : eurUsd / eurInr;
}

function fxSymbol(currency: string): string { return currency === "USD" ? "$" : currency === "INR" ? "₹" : "€"; }

function renderFxPopover(popover: HTMLElement, currency: string): void {
  const snapshot = fxSnapshot;
  if (!snapshot) { popover.innerHTML = `<span class="eyebrow">FX Rates</span><strong>FX rates unavailable</strong><small>Try again when the rate service is available.</small>`; return; }
  const targets = currency === "EUR" ? ["INR", "USD"] : currency === "USD" ? ["INR", "EUR"] : ["EUR", "USD"];
  const rows = targets.map((target) => {
    const value = fxValue(currency, target, snapshot.rates);
    return `<div class="fx-rate-row"><span>1 ${currency}</span><strong>= ${value === null ? "—" : `${fxSymbol(target)}${value.toFixed(4)}`}</strong><em>${target}</em></div>`;
  }).join("");
  const status = snapshot.stale || snapshot.source === "cached" ? "Cached" : "Live";
  const providerDate = snapshot.provider_dates?.USD || snapshot.provider_dates?.INR;
  popover.innerHTML = `<div class="fx-popover-title"><span class="eyebrow">FX Rates</span><strong>Base: ${currency}</strong></div><div class="fx-rate-list">${rows}</div><div class="fx-meta"><span>ECB Reference Rate</span><span>Rate date: ${escapeHtml(providerDate ?? "Unavailable")}</span><span>Fetched: ${escapeHtml(fxDate(snapshot.fetched_at))}</span><strong class="fx-status ${status === "Live" ? "is-live" : "is-cached"}"><i></i>${status}</strong></div>`;
  refreshIcons(popover);
}

const navItems: NavItem[] = [
  { label: "Select Customer", path: "/customer-selection", icon: "building-2", permission: "companies.view", section: "Workspace" },
  { label: "Calculator", path: "/calculator", icon: "calculator", permission: "calculator.view" },
  { label: "Cart", path: "/cart", icon: "shopping-cart", permission: "cart.view" },
  { label: "Dashboard", path: "/dashboard", icon: "layout-dashboard", permission: "dashboard.view" },
  { label: "Customers", path: "/customers", icon: "building-2", permission: "customers.view" },
  { label: "Quotations", path: "/quotations", icon: "file-text", permission: "quotations.view" },
  { label: "Orders", path: "/orders", icon: "shopping-bag", permission: "orders.view" },
  { label: "CRM", path: "/crm", icon: "chart-no-axes-combined", permission: "crm.view", section: "Sales" },
  { label: "Reminders", path: "/reminders", icon: "bell-ring", permission: "reminders.view" },
  { label: "Reports", path: "/reports", icon: "chart-spline", permission: "reports.view" },
  { label: "Users", path: "/users", icon: "users", permission: "users.view" },
  { label: "Product & Pricing", path: "/admin", icon: "badge-euro", permission: "pricing.history" },
  { label: "Settings", path: "/settings", icon: "settings", permission: "settings.view" },
];

export function renderShell(): HTMLElement {
  const state = appStore.state;
  const root = document.createElement("div");
  root.className = `app-shell ${localStorage.getItem("moneda-sidebar") === "collapsed" ? "sidebar-collapsed" : ""}`;
  let currentSection = "";
  const nav = navItems.filter((item) => appStore.can(item.permission)).map((item) => {
    const section = item.section && item.section !== currentSection ? `<div class="nav-section">${item.section}</div>` : "";
    if (item.section) currentSection = item.section;
    const customerLocked = ["/calculator", "/cart"].includes(item.path) && !hasCustomerContext();
    return `${section}<a href="${item.path}" data-route="${item.path}" class="nav-link${customerLocked ? " nav-link-locked" : ""}" aria-label="${item.label}"${customerLocked ? ` aria-disabled="true" data-customer-guard="true" title="Select a customer first"` : ""}><i data-lucide="${customerLocked ? "lock-keyhole" : item.icon}"></i><span>${item.label}</span>${item.path === "/cart" ? `<b data-cart-count>${state.cartCount || ""}</b>` : ""}</a>`;
  }).join("");
  root.innerHTML = `
    <aside class="sidebar" aria-label="Primary navigation">
      <div class="brand-block"><img src="${escapeHtml(state.brandLogoPath)}" alt="${escapeHtml(state.brandName)}"><button class="icon-button sidebar-toggle" aria-label="Collapse navigation" title="Collapse navigation"><i data-lucide="panel-left-close"></i></button></div>
      <nav>${nav}</nav>
      <div class="sidebar-foot"><div class="avatar">${escapeHtml(state.user?.name?.slice(0, 2).toUpperCase() ?? "MT")}</div><div><strong>${escapeHtml(state.user?.name)}</strong><span>${escapeHtml(state.user?.role_display_name)}</span></div><a href="/profile" data-route="/profile" aria-label="Profile" title="Open profile"><i data-lucide="chevron-right"></i></a></div>
    </aside>
    <div class="workspace">
      <header class="topbar">
        <button class="icon-button mobile-menu" aria-label="Open navigation" title="Open navigation"><i data-lucide="menu"></i></button>
        <button class="search-trigger"><i data-lucide="search"></i><span>Search customers, quotes, products…</span><kbd>Ctrl K</kbd></button>
        <div class="top-actions">
          ${state.customer ? `<label class="compact-select"><span>Customer</span><select id="company-switcher" aria-label="Select customer">${state.customers.map((customer) => `<option value="${customer.customer_id ?? customer._id}" ${(customer.customer_id ?? customer._id) === state.activeCustomerId ? "selected" : ""}>${escapeHtml(customer.company_name ?? customer.name)}</option>`).join("")}</select></label>` : '<a class="select-company-action" href="/customer-selection" data-route="/customer-selection"><i data-lucide="building-2"></i>Select Customer</a>'}
          <div class="currency-fx-control" data-fx-control><label class="compact-select currency-select"><span>Currency</span><select id="currency-switcher" aria-label="Select quotation currency" aria-describedby="fx-popover">${["EUR", "USD", "INR"].map((currency) => `<option ${currency === state.currency ? "selected" : ""}>${currency}</option>`).join("")}</select></label><div id="fx-popover" class="fx-popover" role="tooltip" aria-label="Foreign exchange rates"></div></div>
          <div class="notification-control"><button class="icon-button" id="notification-button" aria-label="Notifications" title="Notifications" aria-expanded="false"><i data-lucide="bell"></i><span class="notification-dot" data-notification-count>${state.notificationCount || ""}</span></button><div id="notification-popover" class="notification-popover" role="dialog" aria-label="Notifications" hidden></div></div>
          <button class="avatar avatar-button" aria-label="Open user menu">${escapeHtml(state.user?.name?.slice(0, 2).toUpperCase() ?? "MT")}</button>
        </div>
      </header>
      <main id="main-content" tabindex="-1"><div class="page-loading"><span></span><p>Loading workspace…</p></div></main>
    </div>
    <div class="mobile-scrim"></div>
    <dialog id="command-palette" class="command-palette"><form method="dialog"><div class="command-input"><i data-lucide="search"></i><input aria-label="Global search" placeholder="Search across Moneda" autocomplete="off"><button aria-label="Close">ESC</button></div><div class="command-results"><p>Start typing to search products, customers, quotations, orders and leads.</p></div></form></dialog>`;

  root.querySelector(".sidebar-toggle")?.addEventListener("click", () => {
    root.classList.toggle("sidebar-collapsed");
    localStorage.setItem("moneda-sidebar", root.classList.contains("sidebar-collapsed") ? "collapsed" : "open");
  });
  root.querySelector(".mobile-menu")?.addEventListener("click", () => root.classList.add("mobile-nav-open"));
  root.querySelector(".mobile-scrim")?.addEventListener("click", () => root.classList.remove("mobile-nav-open"));
  root.querySelectorAll<HTMLAnchorElement>("a[data-customer-guard]").forEach((link) => link.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    toast(customerGuardMessage(link.getAttribute("href") ?? "/calculator"), "info");
  }));
  root.querySelector<HTMLSelectElement>("#company-switcher")?.addEventListener("change", async (event) => {
    const selected = state.customers.find((customer) => (customer.customer_id ?? customer._id) === (event.target as HTMLSelectElement).value) ?? null;
    if (!selected) return;
    const select = event.target as HTMLSelectElement;
    select.disabled = true;
    try {
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
      const nextCurrency = selected.default_currency ?? selected.preferred_currency ?? "EUR";
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
  });
  const fxControl = root.querySelector<HTMLElement>("[data-fx-control]");
  const fxPopover = root.querySelector<HTMLElement>("#fx-popover");
  const currencySelect = root.querySelector<HTMLSelectElement>("#currency-switcher");
  let closeTimer = 0;
  const setFxOpen = (open: boolean) => { window.clearTimeout(closeTimer); fxControl?.classList.toggle("is-open", open); };
  const scheduleFxClose = () => { window.clearTimeout(closeTimer); closeTimer = window.setTimeout(() => setFxOpen(false), 100); };
  fxControl?.addEventListener("mouseenter", () => setFxOpen(true));
  fxControl?.addEventListener("mouseleave", scheduleFxClose);
  currencySelect?.addEventListener("focus", () => setFxOpen(true));
  currencySelect?.addEventListener("click", () => setFxOpen(true));
  currencySelect?.addEventListener("change", (event) => appStore.set({ currency: (event.target as HTMLSelectElement).value as "USD" | "INR" | "EUR" }));
  currencySelect?.addEventListener("keydown", (event) => { if (event.key === "Escape") { setFxOpen(false); currencySelect.blur(); } });
  fxPopover?.addEventListener("mouseenter", () => setFxOpen(true));
  fxPopover?.addEventListener("mouseleave", scheduleFxClose);
  const closeFxOutside = (event: MouseEvent) => {
    if (!root.isConnected) { document.removeEventListener("click", closeFxOutside); return; }
    if (!fxControl?.contains(event.target as Node)) setFxOpen(false);
  };
  document.addEventListener("click", closeFxOutside);
  const renderFx = () => { if (fxPopover) renderFxPopover(fxPopover, appStore.state.currency); };
  renderFx();
  void loadFxSnapshot().then(renderFx);
  appStore.subscribe((nextState) => { if (root.isConnected) renderFxPopover(fxPopover!, nextState.currency); });
  void profileApi.notifications().then((result) => {
    appStore.set({ notificationCount: result.unread });
    const badge = root.querySelector<HTMLElement>("[data-notification-count]");
    if (badge) badge.textContent = result.unread ? String(result.unread) : "";
    const notificationPopover = root.querySelector<HTMLElement>("#notification-popover");
    const notificationButton = root.querySelector<HTMLButtonElement>("#notification-button");
    const renderNotifications = () => {
      if (!notificationPopover) return;
      const rows = result.items;
      notificationPopover.innerHTML = `<div class="notification-popover-head"><strong>Notifications</strong>${result.unread ? `<button type="button" data-mark-all>Mark all read</button>` : ""}</div>${rows.length ? `<div class="notification-list">${rows.map((row) => `<button type="button" class="notification-item${row.read ? " is-read" : ""}" data-notification-id="${escapeHtml(String(row._id ?? row.id ?? ""))}"><span class="notification-item-dot"></span><span><strong>${escapeHtml(String(row.title ?? "Notification"))}</strong><small>${escapeHtml(String(row.message ?? ""))}</small><em>${row.created_at ? fxDate(String(row.created_at)) : ""}</em></span></button>`).join("")}</div>` : `<p class="notification-empty">No new notifications</p>`}`;
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
  document.querySelectorAll(".nav-link").forEach((node) => node.classList.toggle("active", node.getAttribute("href") === path));
}
