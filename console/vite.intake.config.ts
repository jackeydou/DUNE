import { resolve } from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The intake site's pages: intake/index.html is the entry, so the build's page is the
// index.html that the Worker's static assets fall back to for every page path. The build goes
// where the intake Worker serves it from; intake/public/_headers rides along. In development the
// API and the sign-in paths are proxied to a local Worker (`wrangler dev`, port 8787 by default).
export default defineConfig({
  root: resolve(import.meta.dirname, "intake"),
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": resolve(import.meta.dirname, "./src"),
    },
  },
  build: {
    outDir: resolve(import.meta.dirname, "../intake/public"),
    // The directory holds a tracked .gitkeep; the build script clears the rest.
    emptyOutDir: false,
  },
  server: {
    host: "127.0.0.1",
    port: 5174,
    proxy: {
      "/swarmeval.intake.v1.": process.env.INTAKE_WORKER ?? "http://127.0.0.1:8787",
      "/auth/": process.env.INTAKE_WORKER ?? "http://127.0.0.1:8787",
    },
  },
})
