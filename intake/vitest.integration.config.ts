import { cloudflareTest } from "@cloudflare/vitest-plugin"
import { defineConfig } from "vitest/config"

// The Worker in workerd, with its Hyperdrive binding on a throwaway Postgres and GitHub and
// Turnstile replaced by a local mock (test/global-setup.ts). Needs docker.
export default defineConfig({
  plugins: [
    cloudflareTest(({ inject }) => ({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        hyperdrives: { HYPERDRIVE: inject<string>("appDatabaseUrl") },
        bindings: {
          PUBLIC_URL: "https://intake.test",
          GITHUB_CLIENT_ID: "test-client",
          GITHUB_CLIENT_SECRET: "test-client-secret",
          TURNSTILE_SITE_KEY: "test-site-key",
          TURNSTILE_SECRET_KEY: "test-turnstile-secret",
          GITHUB_WEB_URL: inject<string>("mockUrl"),
          GITHUB_API_URL: inject<string>("mockUrl"),
          TURNSTILE_VERIFY_URL: `${inject<string>("mockUrl")}/siteverify`,
        },
      },
    })),
  ],
  test: {
    include: ["test/**/*.test.ts"],
    globalSetup: ["./test/global-setup.ts"],
    testTimeout: 30_000,
    hookTimeout: 120_000,
  },
})
