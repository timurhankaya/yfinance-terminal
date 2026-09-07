// The end-to-end run, and it does NOT run in CI.
//
// It needs three things CI does not have: a PostgreSQL with an archive
// in it, a built `dist`, and a symbol with intraday bars. A suite that
// can only pass on a developer's machine is a suite that goes red in CI
// for reasons nobody can act on, so this is the local acceptance
// criterion for the charts instead:
//
//     npm run build
//     uv run yfin api            # or docker compose up api
//     npx playwright install chromium   # once
//     npm run e2e
//
// `E2E_BASE_URL` points it somewhere else (a compose stack, a staging
// host); the default is the API's own dev port.
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
