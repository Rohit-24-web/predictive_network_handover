import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The dashboard talks to FastAPI directly over ws://; no proxy needed.
    // Override the backend with VITE_WS_URL (see README).
  },
  test: { environment: "node", include: ["src/test/**/*.test.ts"] },
});
