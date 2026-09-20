import { refreshIcons } from "./components/icons";
import { updateActiveNav } from "./layouts/shell";
import { customerDetailPage } from "./pages/customers";
import { orderDetailPage } from "./pages/orders";
import { quotationDetailPage } from "./pages/quotations";
import { element } from "./utils/dom";
import { clearCustomerContextState, customerGuardMessage, CUSTOMER_SELECTION_PATH, hasCustomerContext, isCustomerProtectedRoute } from "./guards/customer-context";
import { customerCompanyApi } from "./api";
import { toast } from "./components/toast";
import { customerContextSignal } from "./state/customer-context";
import { routes } from "./state/store";
import { closeViewportMenus, enhanceViewportMenus } from "./components/viewport-menu";

export type PageFactory = () => Promise<HTMLElement>;
let navigationRevision = 0;

export async function navigate(path: string, push = true): Promise<void> {
  closeViewportMenus();
  const requestRevision = ++navigationRevision;
  const query = path.includes("?") ? path.slice(path.indexOf("?")) : "";
  const routePath = path.split("?", 1)[0];
  let resolved = routePath === "/" || routePath === "/login" || routePath === "/home" ? "/dashboard" : routePath.replace(/\/$/, "");
  const selectingCustomer = resolved === CUSTOMER_SELECTION_PATH || resolved === "/company-selection";
  if (selectingCustomer && hasCustomerContext()) {
    clearCustomerContextState();
    await customerCompanyApi.clearSelection(customerContextSignal()).catch(() => undefined);
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
  updateActiveNav(`${resolved}${query}`);
  document.querySelector(".app-shell")?.classList.remove("mobile-nav-open");
  try {
    const factory = routes[resolved] ?? (resolved.startsWith("/customers/") ? () => customerDetailPage(resolved.split("/")[2]) : resolved.startsWith("/quotation/") ? () => quotationDetailPage(resolved.split("/")[2]) : resolved.startsWith("/quotations/") ? () => quotationDetailPage(resolved.split("/")[2]) : resolved.startsWith("/orders/") || resolved.startsWith("/order-confirmations/") ? () => orderDetailPage(resolved.split("/")[2]) : undefined);
    const page = factory ? await factory() : element("section", "page not-found", '<span>404</span><h1>Page not found</h1><p>The requested workspace does not exist.</p><a class="button button-primary" href="/dashboard" data-route="/dashboard">Return to dashboard</a>');
    if (requestRevision !== navigationRevision) return;
    main.replaceChildren(page);
    enhanceViewportMenus(page);
    main.focus({ preventScroll: true });
    window.scrollTo({ top: 0, behavior: "instant" });
    refreshIcons(main);
  } catch (error) {
    if (requestRevision !== navigationRevision) return;
    main.innerHTML = `<section class="page"><div class="notice error"><i data-lucide="circle-alert"></i><div><strong>Page could not be loaded</strong><p>${error instanceof Error ? error.message : "Please try again."}</p></div></div></section>`;
    refreshIcons(main);
  }
}
