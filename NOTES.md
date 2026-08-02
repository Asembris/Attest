# NOTES — available levers, deliberately not built

Small, reversible design levers that are **available if a specific problem shows up** but are
intentionally not built now, because building them speculatively costs more than it saves (a
second code path that can drift from the real one is its own reproducibility risk). Each entry
says the trigger, the lever, and why it stays on the shelf.

## `just setup` installs the seed/ingestion deps, so CI installs them too

**State.** `just setup` runs `pip install -r requirements.txt` (which pins `acryl-datahub`,
the `datahub` CLI plus the SDK `seed/generate_seed.py` imports) *before* the editable dev
install — the exact two-step [CONTRIBUTING](CONTRIBUTING.md) documents. Without it the README's
four-command path dies: `just up` runs `datahub docker quickstart` with nothing on PATH and
hangs for the full deadline. CI runs `just setup`, so CI now installs `acryl-datahub` too, even
though the offline tier never imports it — a heavier CI install than before.

**Trigger.** CI wall-clock (install time) becomes a real problem.

**Lever (NOT built).** A lean `setup-ci` recipe that installs only `.[dev]` (what the offline
gate actually needs), pointed at by `.github/workflows/ci.yml` instead of `just setup`.

**Why it stays on the shelf.** Correctness beats CI speed, and a second setup path can drift
from the real one — CI would then be green about a program a developer never installs, which is
the exact "green on my machine" trap this repo keeps closing. The heavier install is a
pure-Python cost CI never *runs*, and it keeps "CI runs exactly what a developer runs." Build
`setup-ci` only when CI time is measurably a problem, and keep it a strict subset of `just
setup` so it cannot diverge in what it proves. Related: the general "CI never installs the
declared dependency FLOOR" gap is logged in [CLAUDE.md](CLAUDE.md)'s deferred-items table.

## E2E runs accumulate unprunable claim artifacts, so a capped-listing scan has an expiry date

**State.** Every `just e2e` run publishes ~2 permanent claim artifacts to the seeded catalog,
and **nothing prunes them**: a claim artifact is content-addressed, so the tests that vary
their claim's content per run (the repair test's freshness window, deliberately, so the
read-state is not masked by an earlier verdict) mint a NEW artifact every time, and
`DELETE /openapi/v3/entity/assertion/{urn}` returns **HTTP 200 and removes nothing** — the
timeseries run events survive the entity delete, measured. Only `datahub delete --hard`, or
`just reset`, actually clears them.

That is fine on its own. It became a defect when combined with a **capped listing**:
`GET /claims` returns at most 50, and `dataset.assertions` returns a dataset's artifacts
**oldest first**. So a test that locates its claim by SCANNING that listing is looking for the
newest artifact in a page of the oldest ones, and passes only while the dataset holds fewer
than 50.

**The trigger already fired.** `test_e2e_browser.py`'s repair test scanned
`service.claims(target_urn=…)` for its claim. `support_tickets` — hammered by three tests in
that one file — reached **62 artifacts**; the claim sat at **position 60**, absent at
`limit=50`, present at `limit=None`, found instantly by `ClaimReader.get(claim_urn)`. The poll
returned `None` and the assertion crashed on `None.value`. It had passed on a fresh catalog and
was **permanently red** thereafter — a green light with an expiry date, and the expiry had
passed. It was deferred twice because the symptom moved as the count grew.

**Fixed, in two parts.** The poll now reads **by identity** (`service.claim(claim_urn)`) —
O(1), order-independent, cap-independent, and it cannot decay. And the repair test moved to
`RECENTLY_MODIFIED` (`attest_db.public.users`), which nothing else in the E2E writes to, with
a **precondition guard** that fails immediately and by name — naming `just reset` — the day
that dataset does reach the cap. A second guard covers the same test's other drift: the seeded
`fresh` timestamp ages, and once the dataset is older than the claim's window floor the verdict
flips to Contradicted and drags in the correction loop. Remedy there is `just seed`.

**And fixing it surfaced an ACCIDENTAL BARRIER, which is the second half of the lesson.** The
old capped scan read `dataset.assertions` — a RELATIONSHIP read off an eventually-consistent
index. The identity read is a direct entity fetch and answers as soon as the entity exists. So
the old scan had been implicitly *waiting for the relationship index to catch up* before the
browser step ran, and the correct fix removed that wait. The E2E passed once and failed on the
very next run, at the badge — the exact stale symptom this defect was reported under. It is
made explicit now (`_await_dataset_visibility`, `limit=None` so it cannot decay in turn) and it
has to be crossed BEFORE the browser is pointed at the dataset, because `ClaimsExplorer`
fetches once per filter change and re-polls only for `pending-lag` — which `incomplete`
deliberately is not. A `wait_for` on a page that will never re-request can only time out.
**When you replace a slow read with a fast one, ask what was being synchronised by the slowness.**

**The generalisable lesson, which is why this is written down.** *Locating a specific claim by
scanning a capped listing against a shared, accumulating, unprunable catalog is a test that
expires.* Ask for the artifact BY IDENTITY. Where a scan is genuinely the thing under test —
the store-less dataset-scoped read at the end of the repair test IS the thesis query and is
kept — the failure must name truncation (`retrieval.total` vs `considered`) rather than read as
"the claim is absent", because silent absence is the one collapse this project exists to refuse.

**Lever (NOT built).** Pagination in the claims explorer, or a dataset-scoped read that keeps
the NEWEST artifacts when it truncates.

**Why it stays on the shelf.** Newest-first would rest on **undocumented ordering** —
`dataset.assertions` makes no ordering guarantee; it happens to return insertion order on this
version, in this deployment. Building load-bearing retrieval on an unverified property of
someone else's system is what this repo refuses everywhere else, and it would be a real change
to the route that IS the thesis. Truncation is already disclosed honestly (`total` is
round-tripped and the UI renders `TRUNCATED · catalog holds N`, Session 21 gap 1), so the gap
costs findability past the cap and never correctness. Pagination remains the honest deferred
item it is logged as in [CLAUDE.md](CLAUDE.md)'s table.

## No test reads the root README, so its prose is unpinned

**State.** Two tests pin displayed numbers to committed receipts, and neither one looks at
`README.md`. [`test_benchmark_display_traces.py`](tests/test_benchmark_display_traces.py) pins
`frontend/src/data/benchmarkData.ts`; [`test_calibration_consistency.py`](tests/test_calibration_consistency.py)
pins `benchmark/README.md` (`BENCH_README`) and the same UI file. Every figure in the root
README *does* trace to a committed JSON — verified by hand each time it is edited — but nothing
fails when one drifts.

**Trigger.** A number in the root README is found disagreeing with its receipt, or a prose
claim is found asserting a guarantee no test makes.

**The second half of that trigger has already fired once, which is why this is written down.**
Before the Session 25 docs pass the README said *"`checkers/` imports no model client, and a
test asserts that."* The **fact** is true — inspected, and no checker imports a model client —
but no test asserted the static-import property; what the tests actually assert is the stronger
runtime one (the checker step spent **zero tokens**, a run's model calls are decomposition and
explanation and nothing else, `NO_LLM_IN_THE_VERDICT_PATH` FLAGs a violator un-approvable). The
README was corrected to say what is asserted. **[docs/architecture.md](docs/architecture.md)
carried the identical sentence; it took the same correction in `b848d83`, so this half of the
trigger is SATISFIED — the claim survives nowhere.** On a
project whose whole thesis is that an unverified claim is not a verified one, a doc overstating
what a test proves is the one self-inflicted wound worth a tripwire.

**Lever (NOT built).** Extend `test_calibration_consistency.py`'s file list to the root README —
its table regex already generalizes — and add a receipts-table trace of the same shape as
`test_benchmark_display_traces.py`.

**Why it stays on the shelf.** It was deferred deliberately during the rework, not forgotten:
test-pinning a file being substantially rewritten invites churn in the pin rather than in the
prose, and the property that actually matters — every number traces to a committed JSON — holds
today. Build it once the README settles. Note the limit of what it would buy: a regex pins
*numbers*, and the miss that really happened was a **sentence**.
