import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Config for the live end-to-end check only. Separate from vite.config.ts so
// the standing suite stays runnable with nothing else started: these tests
// require a running backend and a served dashboard, and are meaningless
// without them.
//
//   npm run test:live
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/setupTests.ts"],
    css: false,
    include: ["src/__live__/**/*.test.tsx"],
    // A real HTTP round trip plus 500 rendered rows is slower than the offline
    // suite; the default 5s timeout is not enough headroom.
    testTimeout: 60_000,
    hookTimeout: 60_000,
  },
});
