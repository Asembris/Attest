# Attest — task runner. `just` with no argument lists everything.
#
# The full path from an empty machine to a green suite is `just setup && just up
# && just seed && just test`. Every step is idempotent; none of them is a
# remembered incantation.

set windows-shell := ["powershell.exe", "-NoLogo", "-NoProfile", "-Command"]

# The Node twin of the Python truststore fix (CLAUDE.md: every runtime needs its own
# system-CA opt-in on a TLS-inspecting network). Exported ONCE here rather than repeated per
# recipe, because the recipes that need it are shell-specific -- `$env:X="1"` is PowerShell
# and `X=1 cmd` is sh, and `check-ui` has to run under BOTH (a developer on Windows, CI on
# Linux). It is harmless when the CA is already trusted, which is exactly why CLAUDE.md says
# to set these unconditionally rather than only on failure. `ui` and `replay-build` still set
# it inline; that is now redundant, and left alone rather than churned.
export NODE_USE_SYSTEM_CA := "1"

default:
    @just --list

# Install the package and its dev dependencies -- AND the seed/ingestion deps.
#
# requirements.txt FIRST, then the editable dev install: this is the exact two-step
# CONTRIBUTING documents, and `just setup` was doing only the second half. requirements.txt is
# the one place acryl-datahub (the `datahub` CLI and the SDK `seed/generate_seed.py` imports)
# is pinned; without it the very next README command, `just up`, runs `datahub docker
# quickstart` with NOTHING on PATH -- the background job dies instantly and silently and the
# poll watches "starting..." for the full deadline. `just seed` fails the same way on
# `import datahub.emitter`. The exact pins go in first so the editable install's floors are
# already satisfied and nothing is re-resolved.
setup:
    python -m pip install -r requirements.txt
    python -m pip install -e ".[dev]"

# --- the catalog -------------------------------------------------------------

# Bring up the pinned DataHub Core stack (v1.5.0.6) and BLOCK until GMS is healthy.
#
# Uses the VENDORED compose (deploy/datahub/docker-compose.yml) via the CLI's
# --quickstart-compose-file, so bring-up does not fetch a compose file from GitHub at run
# time -- which fails outright on a TLS-inspecting network and is a single point of failure
# elsewhere. It gates on GMS /config, NOT on the CLI's exit code: quickstart crashes printing
# its success checkmark on a cp1252 console (non-zero on success), and a cold boot's silent
# 60-90s reads as a hang. So the CLI runs in the background and up.ps1 polls with a progress
# line and a hard deadline. See deploy/datahub/up.ps1 and docs/deployment.md.
up:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/datahub/up.ps1

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

# DESTROY the catalog and rebuild it from the seed -- the definitive reset for the demo.
#
# A published verdict is a content-addressed artifact whose history is an append-only
# TIMESERIES aspect. Measured: DELETE /openapi/v3/entity/assertion returns 200 and LEAVES the
# verdict; only `datahub delete --hard` removes it. A targeted delete-script would have to
# enumerate every artifact to be complete, and a reset that misses one is a partial lie -- so
# this nukes the VOLUMES, where completeness is definitional. Touches only the catalog, never
# Attest's own store; regenerates the offline fixtures (relative seed dates -> fresh
# timestamps). Per-judge isolation is the real reset; this is the operator's rebuild. Several
# minutes. See deploy/datahub/reset.ps1.
reset:
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File deploy/datahub/reset.ps1

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

# CATALOG DISCOVERY over the REAL MCP server: the picker's search, and the handoff. Launches
# the actual mcp-server-datahub over stdio, searches the seeded catalog, and then fetches every
# URN it returned over GRAPHQL into a real DatasetSnapshot -- which is the whole architecture in
# one command: MCP discovers, a human resolves, GraphQL verifies, code decides.
#
# The counterpart to `just spike-mcp`, which stays non-zero: that proves MCP cannot carry a
# VERDICT, this proves it can carry a NAME. Needs DataHub, `pip install -e '.[mcp]'` and uvx;
# no model, so no money. Skips LOUDLY by name if either requirement is missing.
discover:
    python -m pytest tests/test_discovery_live.py -m live -v -s

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

# --- the replay (docs/replay/, served by GitHub Pages) -----------------------

# Capture the replay fixtures from ONE REAL AUDIT through the shipped API.
#
# Needs DataHub up, seeded, and OPENAI_API_KEY. Runs its own uvicorn on an ephemeral port
# with its own store, so a `just serve` on :8003 is neither used nor disturbed. It PUBLISHES,
# which appends a permanent verdict event to three real claim artifacts -- content-addressed,
# and `DELETE` on an assertion returns 200 and removes nothing, so only `just reset` clears
# them. Re-run it deliberately, not idly. `--resume` re-captures only the GET /claims matrix
# against the run already recorded, and costs no new verdict.
replay-capture *ARGS:
    python spikes/capture_replay.py {{ARGS}}

# Build the REPLAY bundle and stage it at docs/replay/ for GitHub Pages.
#
# The same React app, every real component, with `api/client` aliased to `api/replayClient`
# so every call is answered from the committed fixtures. ATTEST_REPLAY=1 is what moves
# Tailwind's content scan onto src/replay/ -- WITHOUT it the banner's classes compile into
# the PRODUCTION css instead, which is a real change to the shipped bundle made by a file
# nothing imports (caught by hashing dist/; see tailwind.config.js).
#
# Same fresh-or-abort discipline as `just ui`: the old output goes first and a failed build
# THROWS, so docs/replay/ can never quietly serve yesterday's bundle. `base: './'` means the
# staged tree works at /Attest/replay/ on github.io and at /replay/ from a local static
# server -- `just replay-verify` proves both.
replay-build:
    $env:NODE_USE_SYSTEM_CA="1"; $env:ATTEST_REPLAY="1"; cd frontend; if (Test-Path dist-replay) { Remove-Item -Recurse -Force dist-replay }; npm install; if ($LASTEXITCODE -ne 0) { throw "npm install failed -- refusing to leave a stale replay bundle" }; npx vite build --mode replay; if ($LASTEXITCODE -ne 0) { throw "replay build failed -- refusing to leave a stale replay bundle" }
    if (Test-Path docs/replay) { Remove-Item -Recurse -Force docs/replay }
    New-Item -ItemType Directory -Path docs/replay | Out-Null
    Copy-Item -Recurse frontend/dist-replay/* docs/replay/
    Move-Item docs/replay/index.replay.html docs/replay/index.html
    @Write-Host "replay staged -> docs/replay/  (entry: docs/replay/index.html)"

# PROVE the staged replay works: served from a subdirectory, with no network beyond itself.
#
# Serves docs/ two ways -- at /replay/ and at the Pages-shaped /Attest/replay/ -- drives the
# real Edge through the whole recorded flow, and records EVERY request the page makes. A
# single request outside the replay's own directory fails it: the point of a static replay is
# that it needs nothing, and "no backend" is a claim to be checked rather than assumed.
replay-verify:
    python spikes/replay_verify.py

# --- the external trial ------------------------------------------------------

# Load the EXTERNAL trial catalog: DataHub's own showcase-ecommerce datapack (67 datasets,
# 7 platforms), checksum-pinned and filtered against THIS server's aspect registry.
#
# Not `datahub docker ingest-sample-data`, for three stated reasons: the CLI's registry
# fetch dies on a TLS-inspecting network and ships no offline fallback; its --pack path
# time-shifts every timestamp to NOW, which would make every freshness verdict an artifact
# of the ingest clock; and the pack carries DataHub Cloud aspects Core has no place for.
# `--plan` reports what Core refuses and ingests nothing. See docs/external-trial/ingest.md.
external-ingest *ARGS:
    python spikes/external_ingest.py {{ARGS}}

# ATTEST AGAINST A CATALOG WE DID NOT AUTHOR. 15 claims through the real pipeline, four
# families, all three verdicts, five expected Insufficient-Coverage.
#
# NOT a benchmark and nothing here is scored: the golden benchmark is a conformance gate
# where 100% is EXPECTED, and re-using that frame on unlabelled foreign claims would import
# a scoring apparatus this run has not earned. The question is whether the verdicts are
# DEFENSIBLE against metadata we did not design, and where Attest hits its own documented
# limits. Publishes 3 verdicts through the real approval path (--no-publish to skip); uses a
# TEMPORARY store, so attest.db is untouched. --dry-run resolves every target and audits
# nothing (free). Needs `just external-ingest` first. Write-up: docs/external-trial.md.
external-trial *ARGS:
    python spikes/external_trial.py {{ARGS}}

# THE CATALOG CENSUS: how many of the pack's datasets can Attest's GraphQL read resolve AT
# ALL, measured WITH and WITHOUT the `... on CorpGroup` union arm against one loaded catalog
# state. Free -- no model, no claims, no writes.
#
# A DIFFERENT EXPERIMENT from the trial, and a separate script on purpose: the trial asks
# whether 15 hand-written claims get defensible verdicts, this asks a catalog-wide question
# about the read itself. Keeping them apart keeps the trial runner on its original
# methodology. The pre-fix arm is re-derived LIVE rather than quoted, because the baseline
# receipt's "52 readable / 15 refused" were hardcoded literals that run never measured.
# Needs `just external-ingest` first.
external-census *ARGS:
    python spikes/external_census.py {{ARGS}}

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

# THE BROWSER E2E: a real Edge drives the real UI against the real API and real DataHub, a
# human publishes a verdict, and it is read back OUT of the catalog. The hop no other test
# makes -- `test_live` looks end-to-end but drives TestClient, an in-process ASGI transport
# that never binds a port and never runs a `fetch`, so nothing else in the suite executes
# frontend/src/api/client.ts at all. That is the gap the 6903d6c drift shipped through.
#
# Needs: DataHub, OPENAI_API_KEY, `pip install -e ".[e2e]"`, and a BUILT UI (`just ui`) --
# it serves frontend/dist, so a stale bundle would test yesterday's code. Drives INSTALLED
# Edge; no browser is downloaded. Live tier: costs money, writes to your catalog.
e2e: ui
    python -m pytest tests/test_e2e_browser.py -m live -v

# THE VACUITY CHECK FOR THE E2E. Re-introduce four real browser-boundary defects: the 422,
# re-park, correction strand, and same-dataset receipt alias. The last one restores target-URN
# receipt matching and must make the successful second card display the first claim's failure.
# Restores the source and rebuilds the honest bundle even on a crash.
e2e-sabotage:
    python spikes/e2e_sabotage.py

# CRASH-RECOVERABLE SETTLEMENT: a REAL uvicorn subprocess is SIGKILL'd mid-write (at four
# points, and once DURING recovery), and a fresh process recovers the settlement from the
# durable write-ahead intent alone. The hop test_e2e_browser called "not simulatable without
# faking it" -- it is. Read back through a store=None reader and a fresh store, never Attest's
# in-memory state. Live tier: real DataHub, writes claim artifacts to your catalog. Uses the
# scripted fake model, so NO OPENAI_API_KEY is needed. Falsifiable by `just settle-sabotage`.
settle-recover:
    python -m pytest tests/test_settlement_recovery.py -m live -v

# THE VACUITY CHECK FOR SETTLEMENT RECOVERY. No-op the durable intent write (test-side) and
# demand that the same post-upsert kill restarts into an unrecoverable UNKNOWN artifact.
# Non-zero by design if recovery completes without the intent -- which would mean the intent
# is not load-bearing and the recovery test proves nothing. Run this RED before trusting green.
settle-sabotage:
    python spikes/settle_sabotage.py

# THE DEPLOYMENT SMOKE TEST: bring DataHub up, build the UI fresh, launch the shipped uvicorn
# command on a real socket, then prove the UI asset and demo API answer through that socket.
#
# `up` is idempotent (~1s if healthy); `ui` deletes the old bundle before building, so this
# cannot pass against yesterday's frontend. The runner owns one uvicorn subprocess and gives
# the test only its HTTP URL -- no TestClient fallback. Live tier: needs a real model and
# writes nothing to the catalog (POST /audit only parks). Falsifiable by `just smoke-sabotage`.
smoke: up ui
    python spikes/smoke_runner.py

# THE VACUITY CHECK FOR THE SMOKE TEST. Break each deployed boundary: dead GMS, dead uvicorn,
# missing built JS asset, and a demo audit that resolves no claims. Every fault must go red at
# its own marker, not merely fail somewhere. `up ui` makes this command self-contained.
smoke-sabotage: up ui
    python spikes/smoke_sabotage.py

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

# THE FRONTEND AND THE REPLAY, gated the way the Python tier is. What CI's `frontend` job runs.
#
# Nothing in `just check` executes a line of TypeScript, and until this recipe existed nothing
# ran `npm run lint` at all -- it had been RED on two deliberate lines for as long as they
# existed. The judge-facing surface is the React app and `docs/replay/`, and both could break
# while every Python gate stayed green.
#
# `npm --prefix frontend` rather than `cd frontend; ...` deliberately: `cd` does not persist
# between just's lines, and a `cd X && Y` chain is a PARSER ERROR in PowerShell 5.1 (no `&&`),
# so this is the one form that runs identically on a developer's Windows shell and on CI's sh.
# `npm ci` (not `install`) so the lockfile is what is proven, exactly as CI resolves it.
#
# The browser half is `just replay-verify`, kept separate because it needs a browser and the
# `e2e` extra; CI runs it as its own step, straight after this one.
check-ui:
    npm --prefix frontend ci
    npm --prefix frontend run typecheck
    npm --prefix frontend run lint -- --max-warnings=0
    npm --prefix frontend run build
    npm --prefix frontend run build:replay
    python spikes/bundle_boundary.py
    python -m pytest tests/test_replay_fixtures.py tests/test_pages_assets.py -q

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
