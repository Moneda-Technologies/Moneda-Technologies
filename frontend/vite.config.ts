import { defineConfig, loadEnv } from "vite";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "..", "");
  const frontendPort = Number(env.VITE_PORT || 3000);
  const securityHeaders = {
    "Content-Security-Policy": "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; frame-src 'self' blob: https://workdrive.zohoexternal.in; form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ws: wss:",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
  };
  return {
    envDir: "..",
    plugins: [tailwindcss()],
    server: {
      port: frontendPort,
      strictPort: true,
      headers: securityHeaders,
      proxy: {
        "/api": {
          target: env.VITE_API_PROXY_TARGET || "http://127.0.0.1:5005",
          changeOrigin: true,
        },
      },
    },
    preview: { port: frontendPort, strictPort: true, headers: securityHeaders },
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
