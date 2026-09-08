import { appStore } from "../state/store";

export const CUSTOMER_SELECTION_PATH = "/customer-selection";

const protectedPrefixes = [
  "/calculator",
  "/products",
  "/cart",
];

export function hasCustomerContext(): boolean {
  const state = appStore.state;
  return Boolean(state.user && state.activeCustomerId && state.customer);
}

export function clearCustomerContextState(): void {
  localStorage.removeItem("moneda-active-customer-id");
  localStorage.removeItem("moneda-selected-customer-company");
  localStorage.removeItem("moneda-selected-company");
  appStore.set({ customer: null, activeCustomerId: null, customerCompany: null, company: null, cartCount: 0 });
}

export function isCustomerProtectedRoute(path: string): boolean {
  const normalized = path.replace(/\/$/, "") || "/";
  if (normalized === "/quotation" || normalized === "/quotation/create" || normalized === "/quotations/create") return true;
  return protectedPrefixes.some((prefix) => normalized === prefix || normalized.startsWith(`${prefix}/`));
}

export function customerGuardMessage(path: string): string {
  return path === "/cart" || path.startsWith("/cart/")
    ? "Please select a customer before accessing the cart."
    : path === "/quotation" || path.startsWith("/quotation/create") || path.startsWith("/quotations/create")
      ? "Please select a customer before preparing a quotation."
    : "Please select a customer before using the calculator.";
}
