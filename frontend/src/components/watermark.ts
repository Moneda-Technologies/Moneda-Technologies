import type { User } from "../types/domain";
import { escapeHtml } from "../utils/dom";

/** Passive visual attribution for authenticated workspace pages. */
export function createWatermark(user: User | null, enabled = true): HTMLElement {
  const overlay = document.createElement("div");
  overlay.className = "app-watermark";
  overlay.setAttribute("aria-hidden", "true");
  overlay.dataset.enabled = String(enabled);
  const grid = document.createElement("div");
  grid.className = "app-watermark-grid";
  const identity = `${escapeHtml(user?.name ?? "User")} · ${escapeHtml(user?.email ?? "")}`;
  for (let index = 0; index < 28; index += 1) {
    const tile = document.createElement("div");
    tile.className = "app-watermark-tile";
    tile.innerHTML = `<strong>MONEDA TECHNOLOGIES</strong><span>${identity}</span><small>Protected View · <time data-watermark-time></time></small>`;
    grid.append(tile);
  }
  overlay.append(grid);
  const updateTime = () => {
    if (!overlay.isConnected) { window.clearInterval(timer); return; }
    const value = new Date().toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    overlay.querySelectorAll<HTMLElement>("[data-watermark-time]").forEach((node) => { node.textContent = value; });
  };
  const timer = window.setInterval(updateTime, 60_000);
  updateTime();
  return overlay;
}

export function setWatermarkEnabled(watermark: HTMLElement, enabled: boolean): void {
  watermark.dataset.enabled = String(enabled);
}
