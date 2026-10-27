import { defineConfig } from "@playwright/test";

// Real browser E2E against the actual Next.js app and the actual Django API.
// The Next.js app proxies /api/v1 to a disposable PostgreSQL-backed Django
// server. Bring the whole isolated lifecycle up/down with scripts/run_browser_e2e.sh
// (see docs/browser_e2e_runbook.md); nothing here is mocked at the financial
// layer; only browser confirmation/prompt dialogs are auto-accepted by the specs.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL || "http://127.0.0.1:3100",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 20_000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
