export const escapeHtml = (value: unknown): string => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

/** Restrict server-configured assets to this application's HTTP(S) origin. */
export function safeAssetUrl(value: unknown, fallback = "/brand/image.png"): string {
  try {
    const raw = String(value ?? "").trim();
    if (!raw) return fallback;
    const url = new URL(raw, window.location.origin);
    if (url.origin !== window.location.origin || !["http:", "https:"].includes(url.protocol)) return fallback;
    if (url.pathname === "/" || !/\.(png|jpe?g|webp|gif|svg)$/i.test(url.pathname)) return fallback;
    return `${url.pathname}${url.search}${url.hash}`;
  } catch {
    return fallback;
  }
}

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

export function safeDate(value: unknown): Date | null {
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value : null;
  if (typeof value !== "string" && typeof value !== "number") return null;
  const raw = typeof value === "string" ? value.trim() : value;
  if (raw === "") return null;
  const parsed = new Date(raw);
  return Number.isFinite(parsed.getTime()) ? parsed : null;
}

export function formatDate(value: unknown, fallback = "\u2014"): string {
  const parsed = safeDate(value);
  if (!parsed) return fallback;
  try {
    return new Intl.DateTimeFormat("en", { day: "2-digit", month: "short", year: "numeric" }).format(parsed);
  } catch {
    return fallback;
  }
}

export function formatDateInput(value: unknown = new Date()): string {
  const parsed = safeDate(value);
  if (!parsed) return "";
  const year = parsed.getFullYear();
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
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
