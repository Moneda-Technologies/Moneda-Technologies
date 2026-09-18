import type { ClientType } from "../types/domain";

export const PRICE_LIST_ACCOUNT_TYPES = [
  { code: "DISTRIBUTOR", label: "Distributor" },
  { code: "DEALER", label: "Dealer" },
] as const;

export type PriceListAccountType = typeof PRICE_LIST_ACCOUNT_TYPES[number]["code"];

/** Static business vocabulary shared by customer, pricing and incentive UIs. */
export const CUSTOMER_TYPES: ReadonlyArray<{ code: ClientType; label: string }> = [
  // WHOLESALER is the persisted/legacy enum.  Distributor is the business
  // term shown to users; keeping the code avoids changing pricing history.
  { code: "WHOLESALER", label: "Distributor" },
  { code: "DEALER", label: "Dealer" },
  { code: "CUSTOMER", label: "Customer" },
];

export const customerTypeLabel = (value: string | null | undefined): string =>
  CUSTOMER_TYPES.find((type) => type.code === value)?.label ?? "Unassigned";

export const customerTypeOptions = (selected = "", includeAll = false): string => [
  ...(includeAll ? ['<option value="">All Types</option>'] : ['<option value="">Select Customer Type</option>']),
  ...CUSTOMER_TYPES.map((type) => `<option value="${type.code}" ${type.code === selected ? "selected" : ""}>${type.label}</option>`),
].join("");

export const accountTypeLabel = (value: string | null | undefined): string =>
  PRICE_LIST_ACCOUNT_TYPES.find((type) => type.code === value)?.label ?? "Unassigned";

export const accountTypeOptions = (selected = ""): string => [
  '<option value="">Select Account Type</option>',
  ...PRICE_LIST_ACCOUNT_TYPES.map((type) => `<option value="${type.code}" ${type.code === selected ? "selected" : ""}>${type.label}</option>`),
].join("");
