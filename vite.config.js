import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  root: "frontend",
  build: {
    outDir: "../static",
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      input: "frontend/src/main.jsx",
      output: {
        entryFileNames: "app.js",
        assetFileNames: (assetInfo) =>
          assetInfo.name?.endsWith(".css") ? "app.css" : "assets/[name]-[hash][extname]",
      },
    },
  },
});
