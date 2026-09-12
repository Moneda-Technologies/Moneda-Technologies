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
