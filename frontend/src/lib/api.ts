export type Tokens = { access: string; refresh: string };
export type Organization = { id: string; name: string; currency: string; currency_symbol: string; decimal_places: number; user_role: string };
export type Page<T> = { count: number; next: string | null; previous: string | null; results: T[] };
export type Metric = { metric_id: string; name: string; status: string; value: string | number | null; formatted_value: string; unit: string; currency: string | null; warnings?: string[]; assumptions?: string[] };

const base = (process.env.NEXT_PUBLIC_API_BASE_URL || "/api/v1").replace(/\/$/, "");
let activeTokens: Tokens | null = null;
let activeOrganization: string | null = null;
export function setSession(tokens: Tokens | null, organizationId: string | null = activeOrganization) { activeTokens = tokens; activeOrganization = organizationId; }
export function setOrganization(id: string | null) { activeOrganization = id; }
export function apiBase() { return base; }

export class ApiError extends Error {
  constructor(public status: number, message: string, public detail: unknown) { super(message); this.name = "ApiError"; }
}
function messageFrom(body: unknown): string {
  if (typeof body === "string") return body;
  if (body && typeof body === "object") {
    const data = body as Record<string, unknown>;
    if (typeof data.detail === "string") return data.detail;
    return Object.entries(data).map(([key, value]) => `${key}: ${Array.isArray(value) ? value.join(", ") : String(value)}`).join(" · ");
  }
  return "The request could not be completed.";
}
let refreshPromise: Promise<string | null> | null = null;
async function refreshAccess(): Promise<string | null> {
  if (!activeTokens?.refresh) return null;
  if (!refreshPromise) refreshPromise = fetch(`${base}/auth/token/refresh/`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh: activeTokens.refresh }) })
    .then(async (response) => { if (!response.ok) return null; const value = await response.json() as { access: string; refresh?: string }; activeTokens = { access: value.access, refresh: value.refresh ?? activeTokens!.refresh }; return value.access; })
    .catch(() => null).finally(() => { refreshPromise = null; });
  return refreshPromise;
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData) && options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (activeTokens?.access) headers.set("Authorization", `Bearer ${activeTokens.access}`);
  if (activeOrganization) headers.set("X-Organization-ID", activeOrganization);
  const signal = options.signal ? AbortSignal.any([options.signal, AbortSignal.timeout(20_000)]) : AbortSignal.timeout(20_000);
  let response = await fetch(`${base}${path.startsWith("/") ? path : `/${path}`}`, { ...options, headers, signal, cache: "no-store" });
  if (response.status === 401 && activeTokens?.refresh && !path.includes("/auth/token/")) {
    const access = await refreshAccess();
    if (access) { headers.set("Authorization", `Bearer ${access}`); response = await fetch(`${base}${path.startsWith("/") ? path : `/${path}`}`, { ...options, headers, signal, cache: "no-store" }); }
    else { activeTokens = null; window.dispatchEvent(new Event("session-expired")); }
  }
  if (!response.ok) { const body = await response.json().catch(() => response.statusText); throw new ApiError(response.status, messageFrom(body), body); }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export function jsonBody(value: unknown) { return JSON.stringify(value); }
export function listOf<T>(value: T[] | Page<T>): T[] { return Array.isArray(value) ? value : value.results; }
