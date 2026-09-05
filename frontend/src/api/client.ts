import type { ApiEnvelope } from "../types/domain";

export class ApiError extends Error {
  constructor(message: string, public status: number, public errors: unknown[] = []) {
    super(message);
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "include",
    ...options,
    headers: {
      ...(!(options.body instanceof FormData) && options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    if (!response.ok) throw new ApiError(`Request failed (${response.status})`, response.status);
    return response as T;
  }
  const envelope = (await response.json()) as ApiEnvelope<T>;
  if (!response.ok || !envelope.success) {
    if (response.status === 401) window.dispatchEvent(new CustomEvent("moneda:auth-required"));
    if (response.status === 403 && /customer|cart|active context/i.test(envelope.message ?? "")) window.dispatchEvent(new CustomEvent("moneda:customer-context-required"));
    throw new ApiError(envelope.message ?? "Request failed", response.status, envelope.errors);
  }
  return envelope.data;
}

export const jsonBody = (value: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(value) });
export const patchBody = (value: unknown): RequestInit => ({ method: "PATCH", body: JSON.stringify(value) });
