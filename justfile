# Attest — task runner. `just` with no argument lists everything.
#
# The full path from an empty machine to a green suite is `just setup && just up
# && just seed && just test`. Every step is idempotent; none of them is a
# remembered incantation.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

default:
    @just --list

# Install the package and its dev dependencies.
setup:
    python -m pip install -e ".[dev]"

# --- the catalog -------------------------------------------------------------

# Generate the seed metadata and ingest it into DataHub.
#
# The last step captures the offline test fixtures from the freshly seeded catalog, so the
# fixtures and the catalog they describe are regenerated TOGETHER and cannot drift apart.
seed:
    python seed/generate_seed.py
    datahub ingest -c ./seed/recipe.yml
    just capture

# Capture the offline snapshot fixtures from the live catalog (tests/fixtures/snapshots/).
# Run automatically by `just seed`; run it by hand after any manual catalog change. The
# fixtures are held honest by `test_fixture_drift.py` in the live tier -- capture writes,
# the pin verifies.
capture:
    python seed/capture_snapshots.py

# Prove DataHub's read/write path end to end (the Session 0 spike).
probe:
    python spikes/datahub_probe.py

# Prove ONE dataset can hold TWO independent, queryable claim artifacts (the Session 15
# spike). Writes to the seeded catalog and cleans up after itself; exits non-zero if any
# invariant fails. This is what docs/design/claim-artifact.md is designed against.
spike-claims:
    python spikes/claim_artifact_probe.py

# Can the official DataHub MCP server serve a faithful DatasetSnapshot (the Session 17
# spike)? Diffs every seeded dataset MCP-vs-GraphQL, then shows the same gap as verdicts.
# Exits NON-ZERO, and is MEANT to: it is the receipt for why the catalog read is not on
# MCP, so a green run here would mean the finding had expired and the decision is worth
# revisiting. Needs `pip install mcp` and uvx; downloads the MCP server on first run.
spike-mcp:
    python spikes/mcp_reader_probe.py

# Is the catalog actually up? Checks the pinned version, not just the port.
health:
    @(Invoke-RestMethod http://localhost:8080/config).versions.'acryldata/datahub'.version

# --- the service -------------------------------------------------------------

# Run the API. Docs at http://localhost:8003/docs.
#
# 8003 is pinned, not incidental: DataHub owns 8080 (GMS) and 9002 (UI) on this machine,
# and a port clash surfaces as an audit that cannot reach the catalog — which reads like a
# DataHub outage rather than a collision. Overridable with ATTEST_API_PORT.
#
# `--reload` RUNS TWO PROCESSES, and on Windows Ctrl-C does not reliably kill both. uvicorn's
# reloader is a supervisor that spawns a CHILD worker, and the child is what holds the socket
# — so Ctrl-C can leave it orphaned, still LISTENING, with its parent gone. The next `just
# serve` then dies on
#
#     [WinError 10048] only one usage of each socket address is normally permitted
#
# which reads like the port is misconfigured rather than like your own last run never left.
# `just port` says who has it and frees it. `just demo` does NOT use --reload (one process,
# nothing to orphan), which is part of why it is the demo artifact.
serve:
    python -m uvicorn attest.api.app:app --reload --port {{env("ATTEST_API_PORT", "8003")}}

# Who is holding the API port, and kill it. For the --reload orphan described above.
#
# Kills the WHOLE tree, child first: stop the supervisor alone and its watcher can respawn
# the worker straight back onto the port, which looks like the kill silently failed.
port:
    @$p = {{env("ATTEST_API_PORT", "8003")}}; \
     $owners = (Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue).OwningProcess; \
     if (-not $owners) { Write-Host "port $p is free" } \
     else { \
       foreach ($id in ($owners | Select-Object -Unique)) { \
         $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id"; \
         Write-Host "killing $id  $($proc.CommandLine)"; \
         Get-CimInstance Win32_Process -Filter "ParentProcessId=$id" | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }; \
         Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; \
       } \
       Write-Host "port $p freed"; \
     }

# Build the frontend into frontend/dist, which `serve` static-mounts at the root.
#
# ALWAYS a fresh build, or a loud abort — never a stale dist. The old dist is deleted FIRST,
# then a failed `npm install` or `vite build` THROWS (non-zero exit), so `just demo` can never
# fall through to serving yesterday's bundle: it either serves this source or refuses to start.
# The guard is `$LASTEXITCODE`, not `$?` — `$?` is unreliable for native commands and was the
# hole a skipped build slipped through. vite empties outDir on every build, so a completed
# build fully replaces dist; the pre-delete covers the case where the build never completes.
#
# NODE_USE_SYSTEM_CA=1 is the Node twin of the Python truststore fix (see CLAUDE.md): this
# is a TLS-inspecting network and npm/Vite otherwise fail on an untrusted corporate CA.
ui:
    $env:NODE_USE_SYSTEM_CA="1"; cd frontend; if (Test-Path dist) { Remove-Item -Recurse -Force dist }; npm install; if ($LASTEXITCODE -ne 0) { throw "npm install failed -- refusing to leave a stale bundle" }; npm run build; if ($LASTEXITCODE -ne 0) { throw "vite build failed -- refusing to leave a stale bundle" }
    @Write-Host "frontend built FRESH -> frontend/dist. Run 'just serve' and open http://localhost:8003"

# The whole demo, one command, ONE process. Build the UI into frontend/dist, then serve the
# SPA and the API from a single uvicorn on :8003 (the build is static-mounted under FastAPI,
# app.py). This is the demo ARTIFACT — no Vite dev server on :5173, no second process, no
# --reload supervisor: a judge runs `just demo` and opens http://localhost:8003 for the whole
# thing. (Use `just serve` for API-only dev with reload; `just ui` then `just serve` to iterate.)
demo: ui
    @Write-Host "Attest demo -> open http://localhost:8003  (UI + API, one process)"
    python -m uvicorn attest.api.app:app --port {{env("ATTEST_API_PORT", "8003")}}

# --- verification ------------------------------------------------------------

# Run the suite against the live seeded catalog, ACROSS CORES.
#
# `-n auto` parallelizes the offline suite (pytest-xdist). It is safe because every real
# write a test makes is scoped to a per-test tmp_path and every catalog read is idempotent
# -- so N workers are only more load on DataHub, never a different answer. The LIVE suite is
# the one exception (it writes attest.* to shared datasets) and conftest.py REFUSES to run
# it in parallel, by name, before any worker spawns. `just live` stays serial.
#
# Debugging one test? `just test -n0 path::test` -- the last -n wins, so -n0 forces serial
# (real tracebacks, working pdb, un-interleaved output).
#
# This runs the OFFLINE tier plus the INTEGRATION tier (test_client, live GMS wire format).
# When DataHub is down the integration tier SKIPS -- loudly, announced in the summary, never
# buried -- and the offline tier runs in full. For a run that needs nothing but Python, use
# `just test-offline` (what CI runs).
test *ARGS:
    python -m pytest -n auto {{ARGS}}

# The TRULY-OFFLINE tier: checkers, benchmark, coverage, semantic guard, graph, store. Reads
# captured fixtures, never the network -- no DataHub, no API key, and it never skips. This is
# the CI gate, and the honest one: a green here is a green about the whole tier, not half of
# it. `not integration` drops the live-GMS wire-format tests; `not live` drops the real model.
test-offline *ARGS:
    python -m pytest -n auto -m 'not live and not integration' {{ARGS}}

# The LIVE tier, by marker: the semantic layer against a REAL model (test_live) AND the
# anti-drift pin that re-fetches every seeded URN and holds the offline fixtures honest
# (test_fixture_drift). Costs money; needs OPENAI_API_KEY and a live catalog. Serial: the
# live suite writes to shared datasets, and conftest refuses it under -n.
live:
    python -m pytest -m live -v

# Just the coverage matrix: can every claim type still reach every verdict?
matrix:
    python -m pytest tests/test_coverage.py -v

# --- the golden benchmark ----------------------------------------------------

# 40 hand-labeled claims against the DETERMINISTIC CORE. Free, exact, no model involved.
# Precision/recall/F1 per verdict, a confusion matrix, and pass@5 consistency.
bench:
    python -m benchmark.run_eval -k 5

# The same 40 claims through the WHOLE pipeline, on a real model. Costs about 1.5 cents.
bench-full:
    python -m benchmark.run_eval --full -k 3

# THE VACUITY CHECK. Break a checker and prove the benchmark notices. A benchmark that
# cannot fail is a green light wired to nothing. Exits non-zero if the numbers do NOT move.
bench-sabotage:
    python -m benchmark.run_eval --sabotage classification

# Cross-family calibration: re-label the benchmark with Nemotron (Llama family), so that
# GPT is not grading GPT's homework. Needs NVIDIA_API_KEY. Surfaces every disputed label.
bench-calibrate:
    python -m benchmark.labeler

# Rebuild benchmark/cases.json from benchmark/cases.py (the generator is the source).
bench-cases:
    python -m benchmark.cases

# Durable resume and per-run token billing: the two Session 5 properties, on their own.
# A parked run survives the death of its process, and two concurrent audits do not bill
# each other. Offline and free.
resume:
    python -m pytest tests/test_resume.py tests/test_concurrency.py -v

# Lint. `ruff format` is deliberately not a gate — see the note below.
lint:
    python -m ruff check .

# `ruff format` is NOT wired into `lint` or `check`. The repo predates it, so turning
# it on would bury every real diff under a whole-tree reformat. Run it deliberately or
# not at all.

# Assert the OFFLINE tier COLLECTS with zero import errors -- fast, no execution, no server.
#
# Collection errors are a blind spot: they are IMPORT failures, not test failures, so they
# slip past a dev whose live artifacts (seed/ground_truth.json, an installed sklearn) happen
# to be present, and only surface on a bare runner. `--collect-only` exits non-zero on any
# import error, so this makes "the offline tier imports cleanly with DataHub stopped and no
# seed" a NAMED gate that fails in seconds, distinctly from a test failure. Run before the
# full offline run in CI so a collection break is unmistakable and immediate.
collect-check:
    python -m pytest --collect-only -q -m 'not live and not integration'

# Everything CI runs. Genuinely free, offline, no API key and no DataHub needed -- and since
# Session 8 that claim is TRUE: the offline tier reads captured fixtures, so nothing here
# skips for want of a catalog. This is what .github/workflows/ci.yml runs on every push.
# `collect-check` first: a collection (import) error fails fast and by name, before the run.
check: lint collect-check test-offline

# What to run before pushing a change to the semantic layer. Three tiers, each blind to the
# others' failure mode: `check` proves the guard still catches hallucinations (offline, on
# fixtures); `test` adds the integration tier (live GMS wire format); `live` proves the guard
# still lets the truth through AND re-pins the fixtures against the real catalog. Neither the
# offline half nor the live half is sufficient alone -- see the cadence rule in CLAUDE.md.
preflight: lint test live
