import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The shipped artifact is `vite build` -> dist/, static-mounted by the FastAPI app on :8003
// (one origin, one process). The dev proxy below is for iteration only: `vite dev` serves the
// UI on :5173 and forwards the API calls to the running backend, so the same relative paths
// (`/health`, `/audit`, `/claims`) work in both dev and the mounted build without an
// env-dependent base.
//
// EVERY API PATH PREFIX MUST BE LISTED HERE. The mounted build needs no proxy — same origin —
// so a missing entry breaks dev ONLY, which is the worst way round: it looks like a broken
// endpoint rather than a missing line of config, and the demo it is missing from works fine.
export default defineConfig({
  plugins: [react()],
  optimizeDeps: {
    exclude: ['lucide-react'],
  },
  server: {
    proxy: {
      '/health': 'http://localhost:8003',
      '/audit': 'http://localhost:8003',
      '/claims': 'http://localhost:8003',
    },
  },
});
