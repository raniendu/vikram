import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 1420 is Tauri's dev-server convention and is in the API's CORS allowlist.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: { port: 1420, strictPort: true },
  build: { target: "safari15", outDir: "dist" },
});
