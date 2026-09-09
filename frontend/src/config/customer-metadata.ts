import type { Currency } from "../types/domain";

export type Continent = "Africa" | "Asia" | "Europe" | "North America" | "South America" | "Oceania" | "Antarctica";
export type CountryMeta = {
  code: string;
  name: string;
  region: Continent;
  currency_code: string;
  default_display_currency: Currency;
  phone_country_code: string;
};

export const PAYMENT_TERMS = ["Advance", "POD", "30 Days from receipt", "60 Days", "Custom"] as const;
