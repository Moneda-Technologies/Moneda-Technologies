export const escapeHtml = (value: unknown): string => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

export function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = "", html = ""): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  node.innerHTML = html;
  return node;
}

export function skeleton(rows = 4): string {
  return `<div class="skeleton-stack">${Array.from({ length: rows }, () => '<div class="skeleton-line"></div>').join("")}</div>`;
}

export function formatMoney(amount: number, currency = "EUR"): string {
  const locale = currency === "INR" ? "en-IN" : currency === "EUR" ? "en-IE" : "en-US";
  return new Intl.NumberFormat(locale, { style: "currency", currency, maximumFractionDigits: 2 }).format(amount || 0);
}

export function formatDate(value: string | undefined): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en", { day: "2-digit", month: "short", year: "numeric" }).format(new Date(value));
}

/** Return a greeting from the browser's local clock (never the server clock).
 * 06:00-11:59 morning, 12:00-17:59 afternoon, and 18:00-05:59 evening.
 * Tests may provide an IANA timezone; normal callers omit it to use the
 * device timezone, including its DST rules.
 */
export function getUserLocalGreeting(date: Date = new Date(), timeZone?: string): string {
  const hour = timeZone
    ? Number(new Intl.DateTimeFormat("en-US", { hour: "numeric", hour12: false, timeZone }).format(date)) % 24
    : date.getHours();
  return hour >= 6 && hour < 12
    ? "Good morning"
    : hour >= 12 && hour < 18
      ? "Good afternoon"
      : "Good evening";
}

export function emptyState(icon: string, title: string, copy: string): string {
  return `<div class="empty-state"><div class="empty-icon"><i data-lucide="${icon}"></i></div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(copy)}</p></div>`;
}
