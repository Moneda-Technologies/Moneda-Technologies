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
    return this.value.user?.permissions.includes(permission) ?? false;
  }
}

export const appStore = new Store();
