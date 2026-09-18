import { catalogPage } from "../pages/catalog";
import { companySelectionPage } from "../pages/company-selection";
import { crmPage, remindersWorkspacePage } from "../pages/crm";
import { customersPage } from "../pages/customers";
import { dashboardPage } from "../pages/dashboard";
import { bankingPage, paymentsPage, incentivesPage, customerIncentivesPage, creditNotesPage, customerCreditsPage } from "../pages/finance";
import { companiesPage, usersPage, adminPage, settingsPage, profilePage, myBankDetailsPage } from "../pages/management";
import { ordersPage } from "../pages/orders";
import { cartPage, quotationPreparationPage, quotationPreviewPage, quotationsPage } from "../pages/quotations";
import { reportsPage, reportDetailPage } from "../pages/reports";
import { PageFactory } from "../router";
import type { Company, Currency, Customer, User } from "../types/domain";

export interface AppState {
  brandName: string;
  brandLogoPath: string;
  user: User | null;
  customers: Customer[];
  customer: Customer | null;
  activeCustomerId: string | null;
  customerCompanies: Company[];
  customerCompany: Company | null;
  /** @deprecated compatibility aliases; these always mirror customer context. */
  companies: Company[];
  company: Company | null;
  currency: Currency;
  fxRates: Record<string, number> | null;
  cartCount: number;
  notificationCount: number;
  watermarkEnabled: boolean;
  customerIncentiveVisible: boolean;
}

type Listener = (state: AppState) => void;

const initial: AppState = {
  brandName: "Moneda Technologies",
  brandLogoPath: "/brand/moneda-logo.svg",
  user: null,
  customers: [],
  customer: null,
  activeCustomerId: null,
  customerCompanies: [],
  customerCompany: null,
  companies: [],
  company: null,
  currency: "EUR",
  fxRates: null,
  cartCount: 0,
  notificationCount: 0,
  watermarkEnabled: true,
  customerIncentiveVisible: false,
};

class Store {
  private value: AppState = initial;
  private listeners = new Set<Listener>();

  get state(): AppState { return this.value; }

  set(patch: Partial<AppState>): void {
    this.value = { ...this.value, ...patch };
    this.listeners.forEach((listener) => listener(this.value));
  }

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  can(permission: string): boolean {
    const user = this.value.user;
    if (!user) return false;
    if (user.permissions.includes(permission)) return true;
    // These modules are part of every authenticated sales role's read scope.
    // Keep this UI fallback for older sessions whose role document predates
    // the finance-navigation migration; the backend remains authoritative for
    // every request and still enforces customer/ownership scope.
    const financeReadPermissions = new Set(["payments.view", "incentives.view", "credit_notes.view"]);
    return financeReadPermissions.has(permission) && ["superadmin", "admin", "manager_sales_admin", "user"].includes(user.role_id);
  }
}

export const appStore = new Store();
export const routes: Record<string, PageFactory> = {
  "/customer-selection": companySelectionPage,
  "/company-selection": companySelectionPage,
  "/calculator": () => companySelectionPage({ preserveCurrent: true, nextPath: "/products" }),
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
  "/order-confirmations": ordersPage,
  "/banking": bankingPage,
  "/payments": paymentsPage,
  "/incentives": incentivesPage,
  "/incentives/overview": incentivesPage,
  "/incentives/rules": incentivesPage,
  "/incentives/user": incentivesPage,
  "/incentives/payouts": incentivesPage,
  "/incentives/customer": customerIncentivesPage,
  "/credit-notes": creditNotesPage,
  "/customer-credits": customerCreditsPage,
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
  "/price-lists": adminPage,
  "/settings": settingsPage,
  "/settings/currencies": () => settingsPage("currencies"),
  "/settings/communication": () => settingsPage("communication"),
  "/settings/security": () => settingsPage("security"),
  "/profile": profilePage,
  "/my-bank-details": myBankDetailsPage,
};
