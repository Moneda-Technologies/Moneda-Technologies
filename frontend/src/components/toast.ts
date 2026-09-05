import { escapeHtml } from "../utils/dom";
import { refreshIcons } from "./icons";

export function toast(message: string, tone: "success" | "error" | "info" = "success"): void {
  let region = document.querySelector<HTMLElement>("#toast-region");
  if (!region) {
    region = document.createElement("div");
    region.id = "toast-region";
    region.className = "toast-region";
    region.setAttribute("aria-live", "polite");
    document.body.append(region);
  }
  const item = document.createElement("div");
  item.className = `toast toast-${tone}`;
  item.innerHTML = `<i data-lucide="${tone === "success" ? "circle-check" : tone === "error" ? "circle-alert" : "info"}"></i><span>${escapeHtml(message)}</span><button aria-label="Dismiss" title="Dismiss"><i data-lucide="x"></i></button>`;
  item.querySelector("button")?.addEventListener("click", () => item.remove());
  region.append(item);
  refreshIcons(item);
  window.setTimeout(() => item.remove(), 4500);
}
