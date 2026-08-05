# Deployment

How to stand up a drivable Attest, and the honest cost of each part. Every figure in
[Measured cost](#measured-cost) was taken on 2026-07-18 against the pinned DataHub Core
v1.5.0.6 on an 8 GB Windows machine; where a number is network-dependent (an image pull), it
says so instead of pretending. Timings elsewhere carry their own "measured" label and were
taken with the harness they describe.

**In this document:** [the local-vs-hosted ruling](#the-ruling-a-local-one-command-stack-not-a-hosted-url) ·
[the one command](#the-one-command) · [measured cost](#measured-cost) ·
[the reset design](#the-reset-design) · [configuration](#configuration) ·
[reproducible installs](#reproducible-installs) · [the smoke test](#the-smoke-test) ·
[honest limitations](#honest-limitations)

## The ruling: a local one-command stack, not a hosted URL

Attest's demo needs DataHub Core, which is a **multi-container stack** (MySQL, OpenSearch,
Kafka, GMS, frontend, actions), not a thing that trivially hosts. The two options were a
public hosted URL and a local one-command bring-up, and the local one wins on merit:

| | Hosted public URL | **Local one-command (chosen)** |
| --- | --- | --- |
| What it costs | the full stack running 24/7 on a cloud host with ~5 GB RAM in use, plus the shared-state reset problem below | Docker + ~8 GB free on the judge's own machine |
| The risk | a judge hits a down or half-reset instance; nothing keeps it honest between visits | first bring-up pulls images and runs migrations — minutes, but then it is theirs |
| Reset | a genuine problem (see below) — one judge's published verdict spoils the next judge's story | **free**: each judge runs their own stack against their own catalog |

**A hosted URL that flakes is worse than none.** And the deciding point is the last row: a
per-judge local stack makes the hardest part of the demo — resetting a catalog a previous
judge mutated — *structurally unreachable* rather than something to manage. That is the whole
reason the local shape was chosen, not a fallback from a hosted one.

## The one command

```bash
just setup                # pinned deps (incl. the datahub CLI) + Attest, into a venv
copy .env.example .env    # then set OPENAI_API_KEY in it   (cp … on macOS/Linux)
just up                   # bring up DataHub Core v1.5.0.6 from the vendored compose
just seed                 # generate + ingest the seed catalog, capture the offline fixtures
just demo                 # build the UI and serve it WITH the API on :8003
```

Two of those five are load-bearing in ways their names do not show:

- **`just setup` installs `requirements.txt` before the editable install**, so the `datahub`
  CLI and the seed's SDK are on PATH. Without them `just up` runs `datahub docker quickstart`
  with nothing on PATH and watches "starting…" for its full deadline, and `just seed` dies on
  `import datahub.emitter`. Neither failure names the missing CLI.
- **`OPENAI_API_KEY` is required for the demo path.** The verdicts are deterministic, but
  claim extraction and explanation are model calls, so `POST /audit` fails without a key. The
  offline tier (`just check`) needs neither Docker nor a key — see
  [CONTRIBUTING.md](../CONTRIBUTING.md).

Then open `http://localhost:8003`, paste agent prose (a sample is pre-filled), publish a
verdict at the checkpoint, and read it back from DataHub with `GET /claims`.

`just up` uses the **vendored, pinned** compose ([deploy/datahub/](../deploy/datahub/)) via the
CLI's `--quickstart-compose-file`, so bring-up never fetches a compose file from GitHub at run
time — which fails outright on a TLS-inspecting network and is a single point of failure
everywhere else. It gates on GMS actually answering `/config`, not on the CLI's exit code (the
quickstart CLI is non-zero on success on a cp1252 console), prints a progress line every few
seconds so a cold boot never reads as a hang, and has a hard deadline so it never hangs.

## Measured cost

| Thing | Measured | Notes |
| --- | --- | --- |
| RAM in use (6 containers) | **~4.7 GB** | 8 GB free is the practical floor; it is tight |
| Image footprint (cold pull) | **~12.6 GB** | pulled once on a fresh machine; network-dependent |
| Cold bring-up to GMS healthy | **~265 s (~4.5 min)** | fresh volumes, images already present |
| Full `just reset` (nuke + up + seed) | **~407 s (~7 min)** | bring-up 265 s, reseed 81 s |
| Warm restart to GMS healthy | ~50 s | stopped containers, existing volumes |

The `just up` health-gate deadline is 1800 s — a runaway backstop well above the cold number
and roomy enough for a first-machine image pull, not the expected wait. The progress line is
the expected feedback.

## The reset design

A judge (or anyone who finds the URL first) who publishes the sample verdict mutates the
catalog — Attest writes a content-addressed claim artifact with an append-only verdict
history. So a naive shared demo is a one-shot story. The design has three honest answers, and
the first is the load-bearing one:

**1. Per-judge isolation makes reset mostly unnecessary.** Each judge runs their own local
stack, so there is no shared catalog to spoil. This is why the vehicle is local, not hosted.

**2. The demo sample self-freshens where it can.** The freshness claim's window varies per
page load ([frontend/src/data/mockData.ts](../frontend/src/data/mockData.ts)), minting a fresh
content-addressed artifact URN each visit so its read-back starts clean — no deletion. **The
limitation, stated plainly:** this works for the *freshness* claim only. A classification or
ownership claim's asserted content *is* what determines its verdict, so it cannot be varied
without changing what it means; those accumulate history on a shared instance. Only local
isolation resets them.

**3. `just reset` is the operator's definitive wipe.** `datahub docker nuke` destroys the data
volumes and reseeds from scratch. It touches only the catalog, never Attest's own store.

### Why a full wipe, and not a targeted delete (measured)

A published verdict's history is a DataHub **timeseries** aspect, and removing it cleanly is
only possible one way. Measured against Core v1.5.0.6:

| Delete path | Result |
| --- | --- |
| `DELETE /openapi/v3/entity/assertion/{urn}` | HTTP **200**, and the timeseries verdict **survives** (`history=1`). A delete that reports success and changes nothing. |
| GraphQL `deleteAssertion` | rejects CUSTOM assertions (HTTP 500) |
| `batchUpdateSoftDeleted` | "Entity does not exist" after the openapi delete — not a clean path |
| **`datahub delete --urn <urn> --hard`** | removes the timeseries rows (`verdict=None history=0`). The one clean per-artifact delete. |
| Badge (`removeStructuredProperties`) | clears a verdict key to "never audited" |

A per-artifact reset script *could* hard-delete each artifact — but it would have to enumerate
every Attest artifact across every seeded dataset to be complete, and a reset that misses one
is a partial lie. `datahub docker nuke` wipes the volumes, where completeness is *definitional*
rather than a property of an enumeration we have to get right. That is why `just reset` is a
volume wipe, and why it is the operator's rebuild — not a per-request API endpoint, which
would inherit the delete-that-does-nothing trap.

## Configuration

The service is configured by environment (see [.env.example](../.env.example)). The three that
matter behind a real URL:

| Var | Default | What it is |
| --- | --- | --- |
| `DATAHUB_GMS_URL` | `http://localhost:8080` | the metadata API Attest reads and writes |
| `DATAHUB_UI_URL` | `http://localhost:9002` | the DataHub **UI** origin the frontend deep-links to |
| `ATTEST_API_PORT` | `8003` | the port Attest binds (DataHub owns 8080/9002) |

`DATAHUB_UI_URL` is the one host the built SPA cannot hardcode: it has no runtime env, so the
backend hands it the UI origin over `/health`. Without this, a dataset deep-link would point at
`localhost:9002` behind any real URL — the kind of drift that breaks a deployed instance while
every local test stays green.

## Catalog discovery (the DataHub MCP Server)

The URN picker searches your catalog through the **DataHub MCP Server**, launched on demand as
a child process over stdio. It is **discovery only** — nothing it returns is evidence, and the
catalog read that decides verdicts stays on GraphQL for the reasons
[docs/mcp-evaluation.md](mcp-evaluation.md) measured. See CLAUDE.md §22 for the boundary.

| Var | Default | What it is |
| --- | --- | --- |
| `ATTEST_DISCOVERY_ENABLED` | `true` | set `false` to switch the picker's search off entirely |
| `ATTEST_MCP_COMMAND` | `uvx --native-tls --from mcp-server-datahub==0.6.0 mcp-server-datahub --transport stdio` | how the server is launched — **version-pinned**, see below |
| `ATTEST_MCP_STARTUP_TIMEOUT` | `30` | seconds to wait for spawn + `initialize` (measured: 3.3 s warm) |
| `ATTEST_MCP_CALL_TIMEOUT` | `10` | seconds to wait for one search (measured: 0.13–0.34 s) |

**It is optional in every direction, and nothing about an audit depends on it.** The client is
an extra CI never installs (`pip install -e '.[mcp]'`); without it `/catalog/search` answers
**501** and the picker shows a visible offline state with manual URN entry. A server that will
not start answers **503 + `Retry-After`**. Neither ever answers with an empty result list or a
list baked into the bundle — "we could not ask" and "your catalog has nothing like that" are
different facts, and only the second is an answer.

**The first search pays the spawn; nothing else does.** The session starts lazily and is reused
(measured: 3.33 s to spawn, 125–344 ms per search), so a deployment where nobody opens the
picker never runs an MCP server at all. `GET /health` reports discovery's last-known state and
**never probes it** — otherwise every browser load and every uptime check would spawn one.

**On the first run `uvx` downloads the server**, so the first search is slower and needs
network. On a TLS-inspecting network that download fails until `--native-tls` is passed, which
the default command already does (the same system-CA opt-in as Python's `truststore` and Node's
`NODE_USE_SYSTEM_CA`).

### The server version is pinned, and why

`--from mcp-server-datahub==0.6.0`, not a bare `--from mcp-server-datahub`. **Every claim
Attest makes about this server is version-bounded**: the parity finding is 130 mismatches
measured on 0.6.0, `just spike-mcp` exits non-zero *by design* as the tripwire that will say
the day that finding expires, and [PR #182](https://github.com/acryldata/mcp-server-datahub/pull/182)
proposes a fix to this same codebase. Unpinned, `uvx` resolves whatever is newest at first
use — so a judge running the demo could get a server none of the receipts describe, and if the
upstream fix lands, `just spike-mcp` and `just discover` start behaving differently than
documented, **silently**. A tripwire pointed at a moving target is a green light wired to
nothing, which is the same argument that pins DataHub Core (see
[datahub-setup.md](datahub-setup.md)) and `langgraph-checkpoint-sqlite`.

It is a **freeze, not a downgrade**: 0.6.0 was the latest release on PyPI when this was
pinned (checked 2026-08-05), so it changes nothing today and holds it that way. Measured, it
costs no cold-start penalty — the pinned and unpinned launches are indistinguishable inside
run-to-run variance, because `uvx` caches the resolved environment either way (spawn +
`initialize` + first search: 3.35–6.12 s over three cold sessions; warm searches 63–89 ms).

**What the pin does NOT bound, stated plainly:** it fixes the top-level package, not its
dependency closure. `uvx` still resolves that fresh — measured today, `mcp-server-datahub
0.6.0` pulls `acryl-datahub 1.7.0`, `fastmcp 3.4.5`, `mcp 1.29.0`. So the server's *own* code
is frozen and the libraries under it are not. Pinning the closure would need a lockfile for a
tool `uvx` fetches at run time, which is more than this build ships; naming the limit is the
honest alternative to implying a guarantee the pin does not give.

**`GET /health` still reports the version the running server announced at `initialize`, not
the pin**, and that is deliberate — there are *three* version spaces here and only one of them
is the pin:

| What | Value today | Where it comes from |
| --- | --- | --- |
| the pin | `mcp-server-datahub==0.6.0` | `ATTEST_MCP_COMMAND` — the PyPI package |
| what `/health` shows | `datahub v3.4.5` | the `initialize` handshake — the **fastmcp framework** version, under the name the server registers itself as |
| the catalog | Core `v1.5.0.6` | DataHub itself |

That middle row is the whole argument. Session 17 recorded `datahub v3.4.4` from the *same*
server package: the number moved because **fastmcp** moved, not because the server did. So
substituting the pin into the health string would put a package version where a framework
version goes, and a reader comparing the two would conclude the pin was not in force. More
decisively still, `ATTEST_MCP_COMMAND` is overridable — a deployment pointing at an
already-installed server would have `/health` reciting a pin that is genuinely not in force,
which is a field that *lies*. The live value is the measurement; the pin is a launch fact, and
it lives in the config where it is actually true.

### The child process does not outlive a hard-killed Attest — MEASURED

An orderly shutdown closes the session in FastAPI's `lifespan`, which closes the child's stdin
and ends it. The question worth answering is the other one: **what if the Attest process is
killed outright** (`taskkill /F`, a container OOM), so no teardown runs at all? A stdio child
holds no port, so an orphan would not announce itself the way `just serve`'s `--reload` worker
does — it would just sit there holding RAM.

Measured on Windows 10, twice, by holding a live session open and hard-killing **only** the
parent (`taskkill /F /PID <attest>`, no `/T`). The tree under it:

```
python (Attest) -> uvx.exe -> uv.exe -> mcp-server-datahub.exe -> python -> python
```

**Every descendant was gone within 2 seconds, both trials.** Killing the parent closes its
pipe handles, the server reads EOF on stdin, and the chain exits. So there is no orphan to
clean up and no `just port` equivalent to reach for — a null result, recorded because "we did
not check" and "we checked and it is fine" are different facts, which is the whole argument
this project makes about everything else.

## Reproducible installs

[requirements.lock](../requirements.lock) pins the **full transitive closure** of the runtime
(`pip install .`), captured from the tested venv — so a clone-and-run instance resolves to the
same versions rather than drifting newer while local tests stay green. It is runtime only:
`.[dev]` (test tooling), the seed's `acryl-datahub`, and the `.[e2e]` Playwright extra install
on top and are deliberately kept out of the default and CI install paths.

## The smoke test

`just smoke` makes "one command runs everything" falsifiable. It depends on `up` **and a
fresh `ui` build**, then [spikes/smoke_runner.py](../spikes/smoke_runner.py) launches the
shipped `python -m uvicorn attest.api.app:app` command on an ephemeral port with temporary
store/checkpoint files. [tests/test_smoke.py](../tests/test_smoke.py) receives only that HTTP
base URL -- it imports neither the ASGI app nor `TestClient` -- and asserts, in order:

1. GMS is reachable, failing at the wire in ~3 seconds rather than as a downstream timeout.
2. `/health` answers through the real uvicorn socket and Attest sees the catalog.
3. `/` serves the newly built index and its referenced JavaScript asset answers with content.
4. `/audit` produces verdicts resolving every seeded URN in the demo sample.

`just smoke-sabotage` is the vacuity check
([spikes/smoke_sabotage.py](../spikes/smoke_sabotage.py)). It independently breaks all four
boundaries and requires each boundary-specific marker: dead GMS (measured 5.1 s), uvicorn
startup (4.3 s), the built JavaScript asset (7.0 s), and a demo audit resolving no claims.
A fault that merely exits nonzero at another layer is rejected; this caught the first runner
polling `/health` before the direct GMS gate and misreporting a dead container as a server
startup timeout.

## Honest limitations

- **It is local, not hosted.** There is no public URL to click; a judge runs the stack on
  their own machine. This is the deliberate choice above, not a gap.
- **It needs Docker and ~8 GB free RAM**, and the first bring-up pulls ~12.6 GB of images.
- **`just up`/`just reset` assume the repo's `.venv`** (they prepend `.venv/Scripts` to PATH so
  `datahub`/`python` resolve), the same convention `just seed` follows.
- **Windows execution policy / mark-of-the-web.** The recipes invoke the `.ps1` scripts under
  `-ExecutionPolicy Bypass`, so a repo downloaded as a ZIP (whose files carry the
  mark-of-the-web) still runs. A `git clone` never carries it.
- **On a TLS-inspecting corporate network**, each runtime needs its OS-truststore opt-in
  (Python `truststore`, Node `NODE_USE_SYSTEM_CA=1`, uv `--native-tls`) and the vendored compose
  avoids the GitHub fetch. A clean judge host has none of this and is simpler, not harder.
