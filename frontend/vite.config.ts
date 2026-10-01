import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// The dashboard reads the backend across an origin boundary in development.
// Proxying keeps the browser on one origin, so no CORS negotiation is needed
// for the dev server. For the built bundle served by `vite preview`, the
// backend allows the dev and preview origins explicitly (not "*"); any other
// host needs REVORA_CORS_ORIGINS set on the backend.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    css: false,
    // Live checks need a running backend and a served dashboard, so they are
    // not part of the standing suite. Run them with:
    //   npx vitest run --dir src/__live__ --exclude ''
    exclude: ["**/node_modules/**", "**/dist/**", "**/__live__/**"],
  },
});
