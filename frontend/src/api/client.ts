import type { ApiEnvelope } from "../types/domain";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "/api/v1").replace(/\/$/, "");
const PUBLIC_AUTH_PATHS = new Set(["/", "/login", "/signup", "/forgot-password", "/reset-password"]);

type ApiBehavior = {
  /** A 401 from the initial session probe means anonymous, not failed startup. */
  on401?: "anonymous";
};

export class ApiError extends Error {
  constructor(message: string, public status: number, public errors: unknown[] = [], public reference?: string, public stage?: string) {
    super(message);
  }
}

export const apiEndpoint = (path: string): string => `${API_BASE_URL}${path}`;

export function api<T>(path: string, options?: RequestInit): Promise<T>;
export function api<T>(path: string, options: RequestInit, behavior: { on401: "anonymous" }): Promise<T | null>;
export async function api<T>(path: string, options: RequestInit = {}, behavior: ApiBehavior = {}): Promise<T | null> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    credentials: "include",
    ...options,
    headers: {
      ...(!(options.body instanceof FormData) && options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });

  // Session bootstrap intentionally treats an anonymous 401 as a normal state.
  // Other callers keep the existing error/event behavior below.
  if (response.status === 401 && behavior.on401 === "anonymous") return null;

  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    if (!response.ok) throw new ApiError(`Request failed (${response.status})`, response.status);
    return response as T;
  }
  const envelope = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || !envelope.success) {
    if (response.status === 401 && !PUBLIC_AUTH_PATHS.has(window.location.pathname)) window.dispatchEvent(new CustomEvent("moneda:auth-required"));
    if (response.status === 403 && /customer|cart|active context/i.test(envelope.message ?? "")) window.dispatchEvent(new CustomEvent("moneda:customer-context-required"));
    const first = envelope.errors?.[0] as { message?: string } | undefined;
    throw new ApiError(first?.message ?? envelope.message ?? "Request failed", response.status, envelope.errors, envelope.diagnostic_id ?? envelope.request_id, envelope.stage);
  }
  return envelope.data;
}

export const jsonBody = (value: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(value) });
export const patchBody = (value: unknown): RequestInit => ({ method: "PATCH", body: JSON.stringify(value) });
