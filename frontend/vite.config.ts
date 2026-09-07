import { defineConfig, loadEnv } from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "..", "");
  const frontendPort = Number(env.VITE_PORT || 3000);
  return {
    envDir: "..",
    plugins: [tailwindcss()],
    server: {
      port: frontendPort,
      strictPort: true,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://127.0.0.1:5005",
          changeOrigin: true,
        },
      },
    },
    preview: { port: frontendPort, strictPort: true },
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
  };
});
