import { element, escapeHtml } from "../utils/dom";

export function pageScaffold(kicker: string, title: string, description: string, actions = ""): HTMLElement {
  return element("section", "page", `<div class="page-header"><div><div class="breadcrumb">Moneda / ${escapeHtml(kicker)}</div><h1>${escapeHtml(title)}</h1><p>${escapeHtml(description)}</p></div><div class="page-actions">${actions}</div></div><div class="page-body"></div>`);
}

export function statusBadge(status: string): string {
  const key = status.toLowerCase().replaceAll(" ", "-");
  return `<span class="status-badge status-${key}"><span></span>${escapeHtml(status)}</span>`;
}

