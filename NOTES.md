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
