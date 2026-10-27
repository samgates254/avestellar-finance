import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, setOrganization, setSession } from "@/lib/api";
import { SessionProvider, useSession } from "./session";

function TestSession() {
  const session = useSession();
  return <div><button onClick={() => void session.login("finance@example.test", "long-test-password")}>Sign in</button><label>Active organization<select aria-label="Active organization" value={session.organization?.id || ""} onChange={(event) => session.chooseOrganization(event.target.value)}>{session.organizations.map((org) => <option key={org.id} value={org.id}>{org.name}</option>)}</select></label><output>{session.user?.email}</output></div>;
}
afterEach(() => { vi.restoreAllMocks(); setSession(null, null); setOrganization(null); });
describe("authentication and organization selection", () => {
  it("obtains JWTs, loads memberships and scopes subsequent calls to the selected organization", async () => {
    const organization = { id: "org-1", name: "Synthetic Finance", currency: "KES", currency_symbol: "KSh", decimal_places: 2, user_role: "ACCOUNTANT" };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.endsWith("/auth/token/")) return new Response(JSON.stringify({ access: "access-test", refresh: "refresh-test" }), { status: 200 });
      if (url.endsWith("/auth/me/")) return new Response(JSON.stringify({ email: "finance@example.test", full_name: "Finance User" }), { status: 200 });
      if (url.endsWith("/organizations/")) return new Response(JSON.stringify({ count: 1, results: [organization] }), { status: 200 });
      return new Response(JSON.stringify([]), { status: 200 });
    });
    render(<SessionProvider><TestSession /></SessionProvider>);
    fireEvent.click(screen.getByText("Sign in"));
    await waitFor(() => expect(screen.getByText("finance@example.test")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("option", { name: "Synthetic Finance" })).toBeInTheDocument());
    await api("/payments/");
    const scopedCall = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/payments/"));
    expect(new Headers(scopedCall?.[1]?.headers).get("Authorization")).toBe("Bearer access-test");
    expect(new Headers(scopedCall?.[1]?.headers).get("X-Organization-ID")).toBe("org-1");
  });
});
