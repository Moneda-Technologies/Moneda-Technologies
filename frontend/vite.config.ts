import { defineConfig } from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [tailwindcss()],
  server: {
    port: 3005,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:5005",
        changeOrigin: true,
      },
    },
  },
  preview: { port: 3005, strictPort: true },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          charts: ["chart.js/auto"],
          icons: ["lucide"],
        },
      },
    },
  },
});
