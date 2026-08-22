import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard reads the backend across an origin boundary in development.
// Proxying keeps the browser on one origin, so no CORS negotiation is needed
// for the dev server; the backend also sets permissive CORS for the case where
// the built bundle is served from somewhere else.
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
  },
});
