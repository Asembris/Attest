# Attest UI

The web front end for [Attest](../README.md), the groundedness auditor. A React + Vite +
Tailwind single-page app that submits an agent's output to the API, renders the per-claim
verdicts (Supported / Contradicted / Insufficient-Coverage) with their evidence, walks a
reviewer through proposed corrections at the human checkpoint, and presents the golden
benchmark's committed numbers.

## Running it

The UI is not served on its own port in the demo. `just demo` (from the repo root) builds it
into `frontend/dist` and serves the built SPA and the API from a **single** uvicorn process on
`:8003` — the build is static-mounted under FastAPI, so a reviewer opens
[localhost:8003](http://localhost:8003) for the whole thing.

```
just demo      # build the UI, then serve UI + API from one process on :8003
just ui        # build only, into frontend/dist (a fresh build or a loud abort — never stale)
just serve     # API only, with reload, for iterating on the backend
```

For UI-only iteration with hot reload, run Vite directly from this directory (`npm run dev`),
pointing it at a `just serve` backend.

## Notes

- **`NODE_USE_SYSTEM_CA=1`** is required behind a TLS-inspecting network: the corporate CA is
  not in Node's bundled root store, so `npm install` / `vite build` otherwise fail on a
  certificate error. `just ui` / `just demo` set it; set it yourself before any manual
  `npm`/`node` command. It is the Node twin of the Python `truststore` fix (see the root
  `CLAUDE.md`).
- **The benchmark numbers shown in the app are not hand-typed.** They live in
  [`src/data/benchmarkData.ts`](src/data/benchmarkData.ts), traced to the committed receipts in
  `benchmark/results/`; `tests/test_calibration_consistency.py` and
  `tests/test_benchmark_display_traces.py` fail if a displayed figure stops matching its
  receipt.

## Stack

React 18, TypeScript, Vite, Tailwind CSS, Framer Motion, lucide-react.
