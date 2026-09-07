import type { Currency } from "../types/domain";

export type Continent = "Africa" | "Asia" | "Europe" | "North America" | "South America" | "Oceania" | "Antarctica";
export type CountryMeta = { code: string; name: string; default_currency: Currency; gst_applicable?: boolean };

export const PAYMENT_TERMS = ["Advance", "POD", "30 Days from receipt", "60 Days", "Custom"] as const;
export const CONTINENTS: Continent[] = ["Africa", "Asia", "Europe", "North America", "South America", "Oceania", "Antarctica"];
export const COUNTRIES_BY_CONTINENT: Record<Continent, CountryMeta[]> = {
  Africa: [{ code: "ZA", name: "South Africa", default_currency: "USD" }, { code: "NG", name: "Nigeria", default_currency: "USD" }, { code: "EG", name: "Egypt", default_currency: "USD" }, { code: "KE", name: "Kenya", default_currency: "USD" }],
  Asia: [{ code: "IN", name: "India", default_currency: "INR", gst_applicable: true }, { code: "CN", name: "China", default_currency: "USD" }, { code: "JP", name: "Japan", default_currency: "USD" }, { code: "SG", name: "Singapore", default_currency: "USD" }, { code: "AE", name: "United Arab Emirates", default_currency: "USD" }, { code: "SA", name: "Saudi Arabia", default_currency: "USD" }, { code: "TH", name: "Thailand", default_currency: "USD" }, { code: "MY", name: "Malaysia", default_currency: "USD" }],
  Europe: [{ code: "DE", name: "Germany", default_currency: "EUR" }, { code: "FR", name: "France", default_currency: "EUR" }, { code: "IT", name: "Italy", default_currency: "EUR" }, { code: "ES", name: "Spain", default_currency: "EUR" }, { code: "GB", name: "United Kingdom", default_currency: "USD" }, { code: "NL", name: "Netherlands", default_currency: "EUR" }],
  "North America": [{ code: "US", name: "United States", default_currency: "USD" }, { code: "CA", name: "Canada", default_currency: "USD" }, { code: "MX", name: "Mexico", default_currency: "USD" }],
  "South America": [{ code: "BR", name: "Brazil", default_currency: "USD" }, { code: "AR", name: "Argentina", default_currency: "USD" }, { code: "CL", name: "Chile", default_currency: "USD" }],
  Oceania: [{ code: "AU", name: "Australia", default_currency: "USD" }, { code: "NZ", name: "New Zealand", default_currency: "USD" }],
  Antarctica: [],
};

export const countryMeta = (continent: string, code: string) => COUNTRIES_BY_CONTINENT[continent as Continent]?.find((country) => country.code === code) ?? null;
