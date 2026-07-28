# Deployment

How to stand up a drivable Attest, and the honest cost of each part. Every number here was
measured on 2026-07-18 against the pinned DataHub Core v1.5.0.6 on an 8 GB Windows machine;
where a number is network-dependent (an image pull), it says so instead of pretending.

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
just setup          # install Attest (+ dev deps) into a venv
just up             # bring up DataHub Core v1.5.0.6 from the vendored compose
just seed           # generate + ingest the seed catalog, capture the offline fixtures
just demo           # build the UI and serve it WITH the API on :8003
```

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
