// Local acceptance run, not a CI job: it needs an archive with intraday
// bars, a built `dist` and a running API (`npm run build`, `uv run yfin api`,
// `npx playwright install chromium`, `npm run e2e`). `E2E_BASE_URL`
// overrides the target.
import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // One worker: the assertions are about one page against one archive,
  // and parallel runs would race each other's socket subscriptions.
  workers: 1,
  fullyParallel: false,
  // No retries. A flaky chart assertion is a bug in the chart or in the
  // test, and retrying it hides which.
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
