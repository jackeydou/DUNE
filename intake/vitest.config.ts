import { defineConfig } from "vitest/config"

// Unit tests of the pure parts, in Node. The Worker against a database is
// vitest.integration.config.ts.
export default defineConfig({
  test: { include: ["src/**/*.test.ts"] },
})
