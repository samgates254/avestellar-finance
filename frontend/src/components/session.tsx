"use client";
import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, Organization, setOrganization, setSession, Tokens } from "@/lib/api";
type SessionValue = { tokens: Tokens | null; organizations: Organization[]; organization: Organization | null; user: { email: string; full_name: string } | null; loading: boolean; error: string; login: (email: string, password: string) => Promise<void>; logout: () => void; chooseOrganization: (id: string) => void; reload: () => Promise<void> };
const Context = createContext<SessionValue | null>(null);
export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [tokens, setTokens] = useState<Tokens | null>(null); const [organizations, setOrganizations] = useState<Organization[]>([]); const [organizationId, setOrganizationId] = useState(""); const [user, setUser] = useState<SessionValue["user"]>(null); const [loading, setLoading] = useState(false); const [error, setError] = useState("");
  const organization = organizations.find((item) => item.id === organizationId) ?? null;
  // Revoke the refresh token server-side (best effort) then purge all tokens
  // from memory. The network call captures the access token synchronously
  // before we clear state, so revocation stays authenticated.
  const logout = useCallback(() => { const refresh = tokens?.refresh; if (refresh) { void api("/auth/logout/", { method: "POST", body: JSON.stringify({ refresh }) }).catch(() => {}); } setTokens(null); setSession(null, null); setOrganization(null); setOrganizations([]); setOrganizationId(""); setUser(null); }, [tokens]);
  const reload = useCallback(async () => { if (!tokens) return; setLoading(true); setError(""); try { const [profile, orgs] = await Promise.all([api<{ email: string; full_name: string }>("/auth/me/"), api<Organization[] | { results: Organization[] }>("/organizations/")]); const items = Array.isArray(orgs) ? orgs : orgs.results; setUser(profile); setOrganizations(items); setOrganizationId((current) => items.some((item) => item.id === current) ? current : items[0]?.id ?? ""); } catch (e) { setError(e instanceof Error ? e.message : "Could not load your finance workspace."); } finally { setLoading(false); } }, [tokens]);
  useEffect(() => { void reload(); }, [reload]);
  useEffect(() => { const expired = () => logout(); window.addEventListener("session-expired", expired); return () => window.removeEventListener("session-expired", expired); }, [logout]);
  useEffect(() => { setOrganization(organizationId || null); }, [organizationId]);
  const login = useCallback(async (email: string, password: string) => { setError(""); const next = await api<Tokens>("/auth/token/", { method: "POST", body: JSON.stringify({ email, password }) }); setTokens(next); setSession(next, null); }, []);
  const chooseOrganization = useCallback((id: string) => { if (organizations.some((item) => item.id === id)) setOrganizationId(id); }, [organizations]);
  const value = useMemo(() => ({ tokens, organizations, organization, user, loading, error, login, logout, chooseOrganization, reload }), [tokens, organizations, organization, user, loading, error, login, logout, chooseOrganization, reload]);
  return <Context.Provider value={value}>{children}</Context.Provider>;
}
export function useSession() { const context = useContext(Context); if (!context) throw new Error("SessionProvider is missing"); return context; }
