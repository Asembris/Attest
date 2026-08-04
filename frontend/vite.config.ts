import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// THE REPLAY SEAM. `vite build --mode replay` builds the SAME app against committed JSON
// captured off one real run, for `docs/replay/` — so a judge who will not run an 8 GB Docker
// stack can still drive the product instead of watching a video of it.
//
// IT IS A BUILD MODE, NOT A RUNTIME TOGGLE, and it is expressed as an ALIAS rather than an
// `import.meta.env` branch. That is the load-bearing choice: a conditional inside a component
// puts the replay module in the production module graph and leaves the shipped bundle
// depending on dead-code elimination to stay honest. An alias means the default build's graph
// is unchanged — provably, by hashing dist/ before and after — and no component contains a
// line of replay-awareness.
//
// Two modules are swapped. `api/client` -> `api/replayClient` is the whole API surface. And
// `data/mockData` -> `replay/mockData` makes the Hero textarea show the prose the recorded
// audit was actually of; showing one text and replaying another is the one lie this build is
// arranged not to be able to tell.
//
// The regexes match both `./api/client` (App.tsx) and `../api/client` (ClaimsExplorer.tsx) —
// the specifier's depth varies with the importer's.
const replayAliases = [
  {
    find: /^(?:\.\.?\/)+api\/client$/,
    replacement: fileURLToPath(new URL('./src/api/replayClient.ts', import.meta.url)),
  },
  {
    find: /^(?:\.\.?\/)+data\/mockData$/,
    replacement: fileURLToPath(new URL('./src/replay/mockData.ts', import.meta.url)),
  },
];

// The shipped artifact is `vite build` -> dist/, static-mounted by the FastAPI app on :8003
// (one origin, one process). The dev proxy below is for iteration only: `vite dev` serves the
// UI on :5173 and forwards the API calls to the running backend, so the same relative paths
// (`/health`, `/audit`, `/claims`) work in both dev and the mounted build without an
// env-dependent base.
//
// EVERY API PATH PREFIX MUST BE LISTED HERE. The mounted build needs no proxy — same origin —
// so a missing entry breaks dev ONLY, which is the worst way round: it looks like a broken
// endpoint rather than a missing line of config, and the demo it is missing from works fine.
export default defineConfig(({ mode }) => {
  const replay = mode === 'replay';
  return {
    plugins: [react()],
    optimizeDeps: {
      exclude: ['lucide-react'],
    },
    // RELATIVE, so one bundle serves from every subdirectory it has to. The replay lives at
    // `/Attest/replay/` on github.io and at `/replay/` when the built docs/ tree is checked
    // with a local static server — a hardcoded absolute base works in exactly one of those,
    // and is silently wrong in the other. There is no router (App.tsx's `View` is component
    // state), so nothing needs an absolute path to resolve.
    ...(replay ? { base: './' } : {}),
    ...(replay ? { resolve: { alias: replayAliases } } : {}),
    // Only present in replay mode — the default config object must stay exactly the shape it
    // was, so "the production build is unchanged" is a fact about the config and not a hope
    // about how Vite merges an empty override.
    ...(replay
      ? {
          build: {
            outDir: 'dist-replay',
            rollupOptions: {
              input: fileURLToPath(new URL('./index.replay.html', import.meta.url)),
            },
          },
        }
      : {}),
    server: {
      proxy: {
        '/health': 'http://localhost:8003',
        '/audit': 'http://localhost:8003',
        '/claims': 'http://localhost:8003',
        '/catalog': 'http://localhost:8003',
      },
    },
  };
});
