import { refreshIcons } from "./components/icons";
import { updateActiveNav } from "./layouts/shell";
import { adminPage, companiesPage, profilePage, settingsPage, usersPage } from "./pages/management";
import { catalogPage } from "./pages/catalog";
import { crmPage, remindersWorkspacePage } from "./pages/crm";
import { customerDetailPage, customersPage } from "./pages/customers";
import { dashboardPage } from "./pages/dashboard";
import { orderDetailPage, ordersPage } from "./pages/orders";
import { cartPage, quotationDetailPage, quotationPreparationPage, quotationPreviewPage, quotationsPage } from "./pages/quotations";
import { reportDetailPage, reportsPage } from "./pages/reports";
import { companySelectionPage } from "./pages/company-selection";
import { element } from "./utils/dom";
import { clearCustomerContextState, customerGuardMessage, CUSTOMER_SELECTION_PATH, hasCustomerContext, isCustomerProtectedRoute } from "./guards/customer-context";
import { customerCompanyApi } from "./api";
import { toast } from "./components/toast";

type PageFactory = () => Promise<HTMLElement>;

const routes: Record<string, PageFactory> = {
  "/customer-selection": companySelectionPage,
  "/company-selection": companySelectionPage,
  "/calculator": () => catalogPage(),
  "/products": () => catalogPage(),
  "/products/blankets": () => catalogPage("blankets"),
  "/products/mpacks": () => catalogPage("mpacks"),
  "/products/chemicals": () => catalogPage("chemicals"),
  "/cart": cartPage,
  "/quotation": quotationPreparationPage,
  "/quotation/create": quotationPreparationPage,
  "/quotations/create": quotationPreparationPage,
  "/quotation-preview": quotationPreviewPage,
  "/dashboard": dashboardPage,
  "/catalog": catalogPage,
  "/customers": customersPage,
  "/quotations": quotationsPage,
  "/orders": ordersPage,
  "/crm": crmPage,
  "/reminders": remindersWorkspacePage,
  "/reports": reportsPage,
  "/reports/sales-performance": () => reportDetailPage("sales-performance"),
  "/reports/quotation-analysis": () => reportDetailPage("quotation-analysis"),
  "/reports/customer-growth": () => reportDetailPage("customer-growth"),
  "/reports/product-demand": () => reportDetailPage("product-demand"),
  "/reports/tax-summary": () => reportDetailPage("tax-summary"),
  "/reports/currency-exposure": () => reportDetailPage("currency-exposure"),
  "/companies": companiesPage,
  "/users": usersPage,
  "/admin": adminPage,
  "/settings": settingsPage,
  "/settings/currencies": () => settingsPage("currencies"),
  "/settings/communication": () => settingsPage("communication"),
  "/settings/security": () => settingsPage("security"),
  "/profile": profilePage,
};

export async function navigate(path: string, push = true): Promise<void> {
  const query = path.includes("?") ? path.slice(path.indexOf("?")) : "";
  const routePath = path.split("?", 1)[0];
  let resolved = routePath === "/" || routePath === "/login" || routePath === "/home" ? "/dashboard" : routePath.replace(/\/$/, "");
  const selectingCustomer = resolved === CUSTOMER_SELECTION_PATH || resolved === "/company-selection";
  if (selectingCustomer && hasCustomerContext()) {
    await customerCompanyApi.clearSelection().catch(() => undefined);
    clearCustomerContextState();
    window.dispatchEvent(new CustomEvent("moneda:customer-context-cleared"));
  }
  if (isCustomerProtectedRoute(resolved) && !hasCustomerContext()) {
    toast(customerGuardMessage(resolved), "info");
    resolved = CUSTOMER_SELECTION_PATH;
  }
  if (push && `${location.pathname}${location.search}` !== `${resolved}${query}`) history.pushState({}, "", `${resolved}${query}`);
  const main = document.querySelector<HTMLElement>("#main-content");
  if (!main) return;
  main.innerHTML = '<div class="page-loading"><span></span><p>Loading workspace…</p></div>';
  updateActiveNav(resolved);
  document.querySelector(".app-shell")?.classList.remove("mobile-nav-open");
  try {
    const factory = routes[resolved] ?? (resolved.startsWith("/customers/") ? () => customerDetailPage(resolved.split("/")[2]) : resolved.startsWith("/quotation/") ? () => quotationDetailPage(resolved.split("/")[2]) : resolved.startsWith("/quotations/") ? () => quotationDetailPage(resolved.split("/")[2]) : resolved.startsWith("/orders/") ? () => orderDetailPage(resolved.split("/")[2]) : undefined);
    const page = factory ? await factory() : element("section", "page not-found", '<span>404</span><h1>Page not found</h1><p>The requested workspace does not exist.</p><a class="button button-primary" href="/dashboard" data-route="/dashboard">Return to dashboard</a>');
    main.replaceChildren(page);
    main.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
    refreshIcons(main);
  } catch (error) {
    main.innerHTML = `<section class="page"><div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Page could not be loaded</strong><p>${error instanceof Error ? error.message : "Please try again."}</p></div></div></section>`;
    refreshIcons(main);
  }
}
