import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, setOrganization, setSession } from "./api";

afterEach(() => { vi.restoreAllMocks(); setSession(null, null); setOrganization(null); });
describe("typed API client", () => {
  it("sends bearer and organization context and parses API validation errors", async () => {
    setSession({ access: "test-access", refresh: "test-refresh" }, "org-123");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ amount: ["Invalid amount"] }), { status: 400, headers: { "Content-Type": "application/json" } }));
    await expect(api("/payments/")).rejects.toMatchObject({ status: 400, message: "amount: Invalid amount" });
    const headers = new Headers(fetchMock.mock.calls[0][1]?.headers);
    expect(headers.get("Authorization")).toBe("Bearer test-access");
    expect(headers.get("X-Organization-ID")).toBe("org-123");
  });
  it("reports unauthorized responses as API errors if refresh is unavailable", async () => {
    setSession({ access: "old", refresh: "refresh" });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ detail: "Invalid token" }), { status: 401, headers: { "Content-Type": "application/json" } }));
    await expect(api("/auth/me/")).rejects.toBeInstanceOf(ApiError);
  });
  it("refreshes an expired access token once and retries the original organization-scoped request", async () => {
    setSession({ access: "expired-access", refresh: "refresh-token" }, "org-456");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (_input, init) => {
      const authorization = new Headers(init?.headers).get("Authorization");
      if (!authorization) return new Response(JSON.stringify({ access: "refreshed-access", refresh: "rotated-refresh" }), { status: 200 });
      if (authorization === "Bearer expired-access") return new Response(JSON.stringify({ detail: "Token expired" }), { status: 401 });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    });
    await expect(api("/payments/")).resolves.toEqual({ ok: true });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get("Authorization")).toBe("Bearer refreshed-access");
    expect(new Headers(fetchMock.mock.calls[2][1]?.headers).get("X-Organization-ID")).toBe("org-456");
  });
});
