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
still carries the identical sentence and is due the same correction in the next doc pass.** On a
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
