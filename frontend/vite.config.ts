import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The shipped artifact is `vite build` -> dist/, static-mounted by the FastAPI app on :8003
// (one origin, one process). The dev proxy below is for iteration only: `vite dev` serves the
// UI on :5173 and forwards the API calls to the running backend, so the same relative paths
// (`/health`, `/audit`) work in both dev and the mounted build without an env-dependent base.
export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    exclude: ['lucide-react'],
  },
  server: {
    proxy: {
      '/health': 'http://localhost:8003',
      '/audit': 'http://localhost:8003',
    },
  },
});
