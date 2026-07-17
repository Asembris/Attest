# Contributing

Everything runs through [`just`](https://github.com/casey/just). Start with
[README.md](README.md) for what Attest is, and [docs/architecture.md](docs/architecture.md) for why the
boundaries sit where they do. [CLAUDE.md](CLAUDE.md) is the full engineering log — read it before
changing anything load-bearing, because most of what is in it was expensive to learn and is not
re-derivable from the code.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install -e ".[dev]"
copy .env.example .env      # then fill in OPENAI_API_KEY
```

Or `just setup`. The key is needed only by the semantic layer — the checkers and the whole offline tier
run without one.

DataHub Core must be running locally for the integration and live tiers (quickstart; GMS on `:8080`,
UI on `:9002`; metadata auth is disabled locally, so no token). See
[docs/datahub-setup.md](docs/datahub-setup.md).

## Commands

```
just setup           # install the package + dev deps
just seed            # generate seed metadata, ingest it, and capture the offline fixtures
just capture         # regenerate tests/fixtures/snapshots/ from the live catalog (run by `just seed`)
just health          # is the pinned DataHub version actually running?

just serve           # the API on :8003. Docs at /docs. (8080/9002 belong to DataHub)
just demo            # build the UI and serve it WITH the API from one process on :8003
just ui              # build the frontend only
just port            # free :8003 from a --reload orphan

just lint
just check           # lint + the truly-offline tier. Hermetic. What CI runs.
just test            # offline + integration tiers, across cores (-n auto)
just test-offline    # the truly-offline tier alone. No DataHub, no key, never skips.
just live            # the live tier by marker: a real model + the anti-drift pin. Costs money.
just e2e             # the BROWSER E2E: real Edge -> real API -> real DataHub -> back out.
just e2e-sabotage    # THE VACUITY CHECK for the E2E. Non-zero if two real bugs go uncaught.
just preflight       # lint + test + live. Before any push touching the semantic layer.
just matrix          # the 12-cell coverage assertion alone
just resume          # durable resume + per-run token billing

just bench           # the benchmark vs the deterministic core, pass@5. Free.
just bench-full      # ...vs the whole pipeline, real model. Costs money.
just bench-sabotage  # THE VACUITY CHECK. Non-zero exit if breaking a checker moves nothing.
just bench-calibrate # cross-family labels (Nemotron). Needs NVIDIA_API_KEY.
just bench-cases     # regenerate benchmark/cases.json

just probe           # prove DataHub's read/write path
just spike-claims    # prove ONE dataset holds TWO queryable claim artifacts
just spike-mcp       # the MCP reader evaluation. EXITS NON-ZERO BY DESIGN — see below.
```

`just test -n0 …` forces serial execution for debugging: the last `-n` wins, so you get real
tracebacks, working pdb, and un-interleaved output.

## The test tiers

| Tier | Marker | Needs | Side effects |
| --- | --- | --- | --- |
| **Offline** | *(default)* | Nothing — captured fixtures | None. Never skips. Gates CI. |
| **Integration** | `integration` | DataHub Core running | Reads only. Skips **loudly**. |
| **Live** | `live` | DataHub + `OPENAI_API_KEY` | Spends tokens; writes to your local catalog. Skips **loudly**. |
| **Browser E2E** | `live` | ...plus `.[e2e]` and a built UI | As above, through a real browser. `just e2e` builds the UI first. |

The browser E2E is part of the **live** tier by marker, so `just live` and `just preflight`
pick it up. It needs two things the rest of that tier does not — `pip install -e ".[e2e]"`
and a built `frontend/dist` — and **skips with a named reason when either is missing**, so a
`just live` on a machine that has never run `just ui` is not silently one test lighter. Use
`just e2e`, which builds the UI first and cannot skip for that reason.

It drives **installed Microsoft Edge**; no browser is downloaded, so the Playwright CDN and
the corporate-CA trap never enter the picture. `playwright` is pinned in its own `e2e` extra
and deliberately **not** in `dev`: CI installs `dev` and must never try to install or run
this.

The offline tier runs on a bare runner and reads no network. If an "offline" test ever reaches for the
network it **fails** in CI rather than skipping — that is the point of the gate. A suspiciously fast
run must say why, so `conftest.pytest_terminal_summary` prints a red separator naming how many
integration tests did not run and what coverage was lost.

**The live suite is refused under parallel workers**, in `pytest_configure`, before any worker spawns.
Two live workers — one approving while another audits the same dataset — read a catalog the other is
halfway through mutating, and the flake lands on the one path that writes to someone's catalog. `just
live` is serial and never trips it.

## The verification cadence — a rule, not a habit

**Run `just preflight` before any push that touches the semantic layer.** That means `llm.py`,
`decompose.py`, `explain.py`, `faithfulness.py`, `polarity.py`, `crosscheck.py`, `sanitize.py`,
`revise.py`, **or any prompt string or JSON schema in them**. `just check` is not enough for those
files, and this is not a nicety:

- **`just check` (offline, free) proves the guard still catches hallucinations.** It runs against a
  scripted fake that lies on demand.
- **`just live` (a real model, a fraction of a cent) proves the guard still lets the truth through.**

**Each half is blind to the other's failure mode.** A guard that rejects *everything* passes `just
check` with flying colours — every hallucination is caught — while the semantic layer silently degrades
to templates and nobody notices until a demo.

> **When `just live` fails, widen the EVIDENCE, not the guard.**

Every live failure so far has been a bug in Attest's prompts or cross-check, never a lie by the model.
If an explanation needs a word, a checker must put that word in the evidence. Loosening the guard to
make a test pass is the one change that would quietly destroy the product's reason to exist.

Generalized: **if the model omits a field, it is usually because you did not tell it what the field is
for.** A JSON-schema field with no `description` is a prompt bug, and it surfaces far downstream as a
failed correction rather than as anything that looks like a prompt problem. Every field in
`decompose.SCHEMA` and `revise.SCHEMA` carries its description for this reason; do not "tidy" them away.

**And note the limit of a fake.** A fake cannot fail in a way the real thing fails through machinery
the fake does not have — transport, TLS, connection reuse, auth refresh, rate limits, timeouts. When a
change touches how the client is **built, cached, or reused**, the offline suite is not evidence. Only
`just live` is.

## Some assertions are meant to fail

Two commands exit non-zero **by design**, and both are tripwires rather than bugs:

- **`just bench-sabotage`** breaks a checker on purpose and fails if the metrics *don't* move. A
  benchmark that cannot fail measures nothing. It also runs inside `just check`, because a guarantee
  that only fires when someone remembers to type it will rot.
- **`just spike-mcp`** measures the DataHub MCP server as a catalog reader and fails because it
  [cannot carry a verdict](docs/mcp-evaluation.md). If it ever goes green, the finding has expired and
  the decision is worth reopening.
- **`just e2e-sabotage`** re-introduces the two UI/API drift bugs that really shipped in
  6903d6c — the `accept: boolean` 422 and the `proposals()` re-park — and fails if the
  browser E2E does *not* go red for each. Both lived a full session in `main` while the whole
  suite was green, and both were caught by a human clicking the app. If the E2E cannot catch
  them it is not wired to what it claims to test.

Same discipline as `test_fixture_drift.py`: an assertion that only ever passes is a green light wired
to nothing.

## Layout

```
src/attest/
  claims.py            Claim / Verdict / Evidence schema (pydantic, frozen, extra=forbid).
  config.py            Per-step model config. Never hardcode a model.
  checkers/            The deterministic core. One checker per claim type. No LLM.
    policy.py          Declared governance semantics — the model boundary, as data.
  datahub/
    client.py          GraphQL client over httpx. Raises EntityNotFoundError.
    snapshot.py        Normalized read model. Preserves "absent" vs "empty".
    cache.py           ONE RUN's view of the catalog. A consistency boundary, not a cache.
  api/
    app.py             FastAPI. The checkpoint does not soften here. /claims reads DATAHUB.
    service.py         Run, persist, approve, write back, retrieve. Audits run CONCURRENTLY.
    schemas.py         Wire types in and out.
  retrieval.py         Claim artifacts, back OUT of the catalog. The three-state read and the
                       declared push-down report.
  record.py            AuditRecord: the persisted projection of a report. Loses nothing.
  replay.py            The record, read backwards: a parked run's typed ledger, rebuilt.
  store.py             The audit history. SQLite, plain SQL, Postgres-shaped. Append-only.
  writeback.py         Approved verdict -> ONE DataHub claim artifact per claim. Idempotent.
  llm.py               The only module that calls a model.
  decompose.py         Agent prose -> typed claims. A URN must be quoted, never minted.
  explain.py           Verdict + evidence -> prose. Three gates; falls back to the template.
  faithfulness.py      Lexical provenance. Every factual token must appear in the evidence.
  polarity.py          The prose may assert only the direction the verdict reached.
  crosscheck.py        Model/checker disagreement -> a Conflict, never a changed verdict.
  sanitize.py          Untrusted agent text in, instruction-like spans stripped out.
  graph.py             The LangGraph pipeline. Routing, the loop, the human checkpoint.
  revise.py            Self-correction. A revision may not change the subject.
  trajectory.py        Seven invariants asserted against the run's own trace.
  observe.py           Step trace: kind, latency, tokens. What trajectory.py reads.
  cost.py              Prices a run. An unpriced model costs None, never 0.
  report.py            AuditReport: verdicts, proposed corrections, receipts.
seed/                  Seed catalog generator + ingestion recipe (ground_truth.json).
benchmark/             The golden benchmark. A standalone, citable artifact.
spikes/                Throwaway proofs, kept because they are receipts.
tests/                 Two-tier suite. See "The test tiers" above.
```

The `acryl-datahub` SDK is used **only** for generating and ingesting seed data. Attest's runtime never
imports it and talks to DataHub via direct GraphQL over `httpx`.

## Commit convention

Conventional Commits with a scope, then tight bullets. **Use the accurate type** — don't force
everything into `feat:`.

- `feat(scope):` new capability
- `fix(scope):` bug fix
- `test(scope):` tests only
- `docs(scope):` documentation
- `refactor(scope):` no behavior change
- `chore(scope):` tooling, deps, config

```
feat(decompose): extract structured claims from agent output

- OpenAI function calling with strict JSON schema, temperature=0
- Retry-on-malformed-output, max 2 attempts
- Model is a per-step config value, not hardcoded
```

Bullets state **what changed, not why it's good**. No prose paragraphs, no emoji.

All session work commits directly to `main`; this repository's history is deliberately linear.
