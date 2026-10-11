import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/runs": "http://127.0.0.1:8000",
      "/traces": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/evaluations": "http://127.0.0.1:8000",
      "/experiments": "http://127.0.0.1:8000",
      "/experiment-runs": "http://127.0.0.1:8000",
      "/agent-analytics": "http://127.0.0.1:8000",
      "/policies": "http://127.0.0.1:8000",
      "/policy-evaluations": "http://127.0.0.1:8000",
      "/violations": "http://127.0.0.1:8000",
      "/docs": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000",
    },
  },
  test: { environment: "jsdom", setupFiles: "./tests/setup.ts" },
});
