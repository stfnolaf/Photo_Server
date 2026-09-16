import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: process.env.PHOTO_DEV_API ?? "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
      "/docs": process.env.PHOTO_DEV_API ?? "http://localhost:8000",
      "/openapi.json": process.env.PHOTO_DEV_API ?? "http://localhost:8000",
    },
  },
});
