import { authApi } from "../api";
import { clearCustomerContextState } from "../guards/customer-context";
import { appStore } from "../state/store";

/** Single client-side logout boundary used by the header and profile page. */
export async function logout(): Promise<void> {
  try {
    await authApi.logout();
  } finally {
    clearCustomerContextState();
    appStore.set({ user: null, customers: [], customerCompanies: [], companies: [], notificationCount: 0 });
    window.dispatchEvent(new CustomEvent("moneda:auth-required"));
  }
}
