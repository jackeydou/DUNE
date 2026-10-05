import { defineConfig } from "@playwright/test"

// The specs drive a running stack: SWARM_URL is edge, serving the built console. `mise run
// console:e2e` starts one (tests/worker/test_console_e2e.py) and runs them against it.
// PLAYWRIGHT_CHROMIUM names a Chromium binary to use instead of Playwright's own download.
export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  expect: { timeout: 30_000 },
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.SWARM_URL,
    trace: "retain-on-failure",
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined },
  },
})
