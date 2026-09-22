import { appStore } from "../state/store";
import { escapeHtml, safeAssetUrl } from "../utils/dom";

export function authPageShell(content: string, footerLinks = false): HTMLElement {
  const root = document.createElement("main");
  root.className = "auth-page";
  root.id = "main-content";
  root.tabIndex = -1;
  const logoPath = escapeHtml(safeAssetUrl(appStore.state.brandLogoPath));
  const brandName = escapeHtml(appStore.state.brandName);
  root.innerHTML = `<header class="auth-brand"><div class="auth-logo-wrap"><img class="auth-logo" src="${logoPath}" alt="${brandName}"></div><div class="auth-tagline">Know Your Alternative</div></header><div class="auth-stripe" aria-hidden="true"></div><section class="auth-content"><div class="auth-card"><div class="auth-form-wrap">${content}</div></div></section><footer class="auth-footer"><span>© ${new Date().getFullYear()} Moneda Technologies</span>${footerLinks ? "<span>Privacy · Coming soon</span><span>Support · Coming soon</span>" : ""}</footer>`;
  return root;
}
