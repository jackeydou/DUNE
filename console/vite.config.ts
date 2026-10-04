import { resolve } from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

// The build goes where edge embeds it from. In development the API is proxied to a local edge,
// which must be started with `--public-url http://127.0.0.1:5173`: it accepts cookie requests
// from that origin only.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": resolve(import.meta.dirname, "./src"),
    },
  },
  build: {
    outDir: "../go/internal/edge/webui/static",
    // The directory holds a tracked .gitkeep; the build script clears the rest.
    emptyOutDir: false,
  },
  server: {
    host: "127.0.0.1",
    proxy: {
      "/swarmeval.api.v1.": process.env.SWARM_EDGE ?? "http://127.0.0.1:7443",
    },
  },
  test: {
    include: ["src/**/*.test.ts"],
  },
})
