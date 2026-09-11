import "./styles/main.css";
import { authApi, customerCompanyApi } from "./api";
import { api, ApiError } from "./api/client";
import { refreshIcons } from "./components/icons";
import { renderShell } from "./layouts/shell";
import { loginPage } from "./pages/login";
import { passwordResetPage, signupPage } from "./pages/auth";
import { quotationPreviewPage } from "./pages/quotations";
import { navigate } from "./router";
import { appStore } from "./state/store";
import type { Company, Customer } from "./types/domain";
import { clearCustomerContextState, CUSTOMER_SELECTION_PATH } from "./guards/customer-context";
import { devicePendingPage } from "./pages/device-pending";

interface PublicConfig { brand_name: string; brand_logo_path: string; demo_mode: boolean; master_currency: "EUR"; signup_email_domains?: string[] }

const app = document.querySelector<HTMLElement>("#app")!;
const skipLink = document.querySelector<HTMLAnchorElement>(".skip-link");
skipLink?.addEventListener("click", () => {
  window.requestAnimationFrame(() => document.querySelector<HTMLElement>("#main-content")?.focus({ preventScroll: true }));
});

async function enterWorkspace(forceCompanySelection = false, existingSession?: Awaited<ReturnType<typeof authApi.me>>): Promise<void> {
  const session = existingSession ?? await authApi.me();
  if (session.application_access === false || session.device_access?.application_access === false) {
    document.body.classList.remove("print-preview-mode");
    app.replaceChildren(devicePendingPage(configForPending(), async () => {
      const refreshed = await authApi.bootstrapSession();
      if (refreshed) await enterWorkspace(false, refreshed);
    }));
    return;
  }
  const customers = session.customers ?? (session.customer_companies ?? session.companies) as unknown as Customer[];
  const customerCompanies = customers as unknown as Company[];
  const selectionPage = location.pathname === CUSTOMER_SELECTION_PATH || location.pathname === "/company-selection";
  const mustSelectCustomer = forceCompanySelection || selectionPage;
  if (mustSelectCustomer) {
    const hadServerContext = Boolean(session.active_customer_id || session.selected_customer_id || session.selected_customer_company_id || session.active_company_id);
    clearCustomerContextState();
    if (hadServerContext) await customerCompanyApi.clearSelection().catch(() => undefined);
  }
  // The Flask session is authoritative. Local storage is only a migration
  // fallback for sessions that predate the active_customer_id field.
  const serverSelectedId = session.active_customer_id ?? session.selected_customer_id ?? session.selected_customer_company_id ?? session.active_company_id ?? null;
  const selectedId = mustSelectCustomer ? null : (serverSelectedId ?? localStorage.getItem("moneda-active-customer-id") ?? localStorage.getItem("moneda-selected-customer-company") ?? localStorage.getItem("moneda-selected-company") ?? null);
  const customer = customers.find((item) => item._id === selectedId || item.customer_id === selectedId) ?? null;
  const customerCompany = customer as unknown as Company | null;
  if (customer && session.active_customer_id !== (customer.customer_id ?? customer._id)) await customerCompanyApi.select(customer.customer_id ?? customer._id);
  appStore.set({ user: session.user, customers, customer, activeCustomerId: customer?.customer_id ?? customer?._id ?? null, customerCompanies, customerCompany, companies: customerCompanies, company: customerCompany, currency: customer?.preferred_currency ?? customer?.default_currency ?? "EUR", watermarkEnabled: session.watermark_enabled !== false });
  if (location.pathname === "/quotation-preview") {
    document.body.classList.add("print-preview-mode");
    app.replaceChildren(await quotationPreviewPage());
    return;
  }
  document.body.classList.remove("print-preview-mode");
  app.replaceChildren(renderShell());
  const authEntry = ["/", "/login", "/home", "/signup"].includes(location.pathname);
  const destination = forceCompanySelection
    ? "/customer-selection"
    : authEntry
      ? "/dashboard"
      : location.pathname;
  const customerOptional = ["/crm", "/dashboard", "/customers", "/quotations", "/orders", "/payments", "/incentives", "/credit-notes", "/reminders", "/reports", "/users", "/settings", "/profile"].includes(destination.split("?", 1)[0]) || destination.startsWith("/settings/");
  await navigate(!customer && !customerOptional && destination !== "/customer-selection" && destination !== "/company-selection" ? "/customer-selection" : destination, authEntry || forceCompanySelection);
}

let bootstrapConfig: PublicConfig = { brand_name: "Moneda Technologies", brand_logo_path: "/brand/moneda-logo.svg", demo_mode: false, master_currency: "EUR" };
function configForPending(): PublicConfig { return bootstrapConfig; }

function renderPublicAuthentication(config: PublicConfig): void {
  if (location.pathname === "/signup") app.replaceChildren(signupPage(config.signup_email_domains, () => enterWorkspace(false)));
  else if (["/forgot-password", "/reset-password"].includes(location.pathname)) app.replaceChildren(passwordResetPage());
  else {
    if (["/", "/home"].includes(location.pathname)) history.replaceState({}, "", "/login");
    app.replaceChildren(loginPage(config.demo_mode, () => enterWorkspace(false)));
  }
}

async function bootstrap(): Promise<void> {
  let config: PublicConfig = { brand_name: "Moneda Technologies", brand_logo_path: import.meta.env.VITE_BRAND_LOGO_PATH ?? "/brand/moneda-logo.svg", demo_mode: false, master_currency: "EUR", signup_email_domains: ["monedatechnologies.com", "chemo.in"] };
  try { config = await api<PublicConfig>("/config"); }
  catch (error) {
    const message = error instanceof ApiError ? `${error.message} (${error.status})` : "The API did not return a valid response.";
    app.innerHTML = `<div class="fatal-state"><img src="${config.brand_logo_path}" alt="${config.brand_name}"><h1>Moneda configuration could not be loaded.</h1><p>${message}</p><button class="button button-primary" onclick="location.reload()">Try again</button></div>`;
    return;
  }
  appStore.set({ brandName: config.brand_name, brandLogoPath: config.brand_logo_path });
  bootstrapConfig = config;
  try {
    const session = await authApi.bootstrapSession();
    if (!session) {
      renderPublicAuthentication(config);
      return;
    }
    await enterWorkspace(false, session);
  }
  catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      renderPublicAuthentication(config);
      return;
    }
    console.error("Session bootstrap failed", error);
    const message = error instanceof ApiError
      ? `${error.message} (${error.status})`
      : "The Moneda API could not be reached. Check the connection and try again.";
    app.innerHTML = `<div class="fatal-state"><img src="${config.brand_logo_path}" alt="${config.brand_name}"><h1>Workspace unavailable</h1><p>${message}</p><button class="button button-primary" onclick="location.reload()">Try again</button></div>`;
  }
}

document.addEventListener("click", (event) => {
  const target = (event.target as HTMLElement).closest<HTMLAnchorElement>("a[data-route]");
  if (!target || event.defaultPrevented || target.dataset.customerGuard === "true") return;
  event.preventDefault(); void navigate(target.getAttribute("href") ?? "/dashboard");
});
window.addEventListener("popstate", () => void navigate(`${location.pathname}${location.search}`, false));
window.addEventListener("moneda:navigate", (event) => void navigate((event as CustomEvent<string>).detail));
window.addEventListener("moneda:company-selected", () => app.replaceChildren(renderShell()));
window.addEventListener("moneda:customer-context-cleared", () => app.replaceChildren(renderShell()));
window.addEventListener("moneda:customer-context-required", () => {
  clearCustomerContextState();
  if (location.pathname !== CUSTOMER_SELECTION_PATH) void navigate(CUSTOMER_SELECTION_PATH);
});
window.addEventListener("moneda:device-access-required", () => {
  document.body.classList.remove("print-preview-mode");
  app.replaceChildren(devicePendingPage(configForPending(), async () => {
    const refreshed = await authApi.bootstrapSession();
    if (refreshed) await enterWorkspace(false, refreshed);
  }));
});
window.addEventListener("moneda:auth-required", () => {
  localStorage.removeItem("moneda-active-customer-id");
  localStorage.removeItem("moneda-selected-customer-company");
  appStore.set({ user: null, customer: null, activeCustomerId: null, customerCompany: null, company: null, cartCount: 0 });
  history.replaceState({}, "", "/login");
  app.replaceChildren(loginPage(false, () => enterWorkspace(false)));
});

void bootstrap().then(() => refreshIcons());
