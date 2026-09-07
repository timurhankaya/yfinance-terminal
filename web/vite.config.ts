/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Served by the API process under /ui; the build lands inside the Python
// package so a wheel carries it (pyproject: package-data "yfin.ui").
export default defineConfig({
  plugins: [react()],
  base: "/ui/",
  build: {
    outDir: "../src/yfin/ui/static/dist",
    emptyOutDir: true,
  },
  server: {
    // Same origin for the cookie: the dev server forwards everything the
    // page calls to the API on :8000.
    proxy: {
      "/ui/api": "http://localhost:8000",
      "/ui/ws": { target: "ws://localhost:8000", ws: true },
      "/v1": "http://localhost:8000",
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
