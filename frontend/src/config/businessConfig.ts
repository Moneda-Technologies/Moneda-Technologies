import type { ClientType } from "../types/domain";

/** Static business vocabulary shared by customer, pricing and incentive UIs. */
export const CUSTOMER_TYPES: ReadonlyArray<{ code: ClientType; label: string }> = [
  { code: "WHOLESALER", label: "Wholesaler" },
  { code: "DEALER", label: "Dealer" },
  { code: "CUSTOMER", label: "Customer" },
];

export const customerTypeLabel = (value: string | null | undefined): string =>
  CUSTOMER_TYPES.find((type) => type.code === value)?.label ?? "Unassigned";

export const customerTypeOptions = (selected = "", includeAll = false): string => [
  ...(includeAll ? ['<option value="">All Types</option>'] : ['<option value="">Select Customer Type</option>']),
  ...CUSTOMER_TYPES.map((type) => `<option value="${type.code}" ${type.code === selected ? "selected" : ""}>${type.label}</option>`),
].join("");
