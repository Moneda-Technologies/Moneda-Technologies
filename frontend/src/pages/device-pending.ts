import { authApi } from "../api";
import { logout } from "../auth/logout";
import { refreshIcons } from "../components/icons";
import { escapeHtml } from "../utils/dom";

export function devicePendingPage(config: { brand_name: string; brand_logo_path: string }, onApproved: () => Promise<void>): HTMLElement {
  const root = document.createElement("main");
  root.className = "device-pending-page";
  root.innerHTML = `<section class="device-pending-card"><img src="${escapeHtml(config.brand_logo_path)}" alt="${escapeHtml(config.brand_name)}"><span class="eyebrow">Secure access</span><h1 data-device-heading>Device approval pending</h1><p data-device-message>Your login was successful, but this device is waiting for administrator approval.</p><p class="device-pending-status" data-device-status>Waiting for approval...</p><button type="button" class="button button-primary" data-signout>Sign out</button></section>`;
  let timer: number | undefined;
  const check = async () => {
    try {
      const status = await authApi.deviceAccess();
      if (status.device_status === "approved") { if (timer) window.clearInterval(timer); await onApproved(); return; }
      const heading = root.querySelector<HTMLElement>("[data-device-heading]");
      if (heading) heading.textContent = status.device_status === "denied" ? "Device access denied" : status.device_status === "revoked" ? "Device access revoked" : "Device approval pending";
      const message = root.querySelector<HTMLElement>("[data-device-message]");
      if (message) message.textContent = status.device_status === "revoked" ? "Your Moneda Workspace access from this device has been revoked by an administrator. Please contact your Moneda administrator." : status.device_status === "denied" ? "This device is not currently approved for Moneda Workspace. Please contact your Moneda administrator." : status.reinstated_at ? "Your device has been reinstated and is waiting for administrator approval." : "Your login was successful, but this device is waiting for administrator approval. Please contact your Moneda administrator.";
      const node = root.querySelector<HTMLElement>("[data-device-status]");
      if (node) node.textContent = status.device_status === "revoked" ? "This device was revoked. Please contact your administrator." : status.device_status === "denied" ? "This device was not approved. Please contact your administrator." : status.reinstated_at ? "Your device has been reinstated and is waiting for administrator approval." : "Waiting for approval...";
      // Keep polling denied/revoked records so an authenticated reinstatement
      // changes this screen to the pending state without a page refresh.
    } catch { /* transient polling failures are retried on the next interval */ }
  };
  root.querySelector("[data-signout]")?.addEventListener("click", () => { if (timer) window.clearInterval(timer); void logout(); });
  timer = window.setInterval(() => void check(), 12000);
  void check();
  refreshIcons(root);
  return root;
}
