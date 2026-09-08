/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the API process under /ui; the build lands inside the Python
// package so a wheel carries it (pyproject: package-data "yfin.ui").
const API_ORIGIN = process.env.VITE_API_ORIGIN ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "../src/yfin/ui/static/dist",
    emptyOutDir: true,
  },
  server: {
    // Same origin, so the dev server forwards everything the page calls
    // to the API. The port is not always 8000: `docker-compose` publishes
    // `API_PORT`, and a machine with something else on 8000 sets it to
    // something else -- so `VITE_API_ORIGIN` says where the API actually
    // is, and 8000 stays the default it has always been.
    proxy: {
      "/ui/api": API_ORIGIN,
      "/ui/ws": { target: API_ORIGIN.replace(/^http/, "ws"), ws: true },
      "/v1": API_ORIGIN,
    },
  },
  test: {
    environment: "jsdom",
    // Playwright owns `e2e/`. Vitest's default glob would pick those
    // `.spec.ts` files up and run them in jsdom, where `@playwright/test`
    // has no runner and every one fails on import.
    exclude: ["node_modules/**", "e2e/**"],
    setupFiles: ["src/test/setup.ts"],
    globals: false,
  },
});
