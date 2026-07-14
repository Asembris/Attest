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
seed:
    python seed/generate_seed.py
    datahub ingest -c ./seed/recipe.yml

# Prove DataHub's read/write path end to end (the Session 0 spike).
probe:
    python spikes/datahub_probe.py

# Is the catalog actually up? Checks the pinned version, not just the port.
health:
    @(Invoke-RestMethod http://localhost:8080/config).versions.'acryldata/datahub'.version

# --- the service -------------------------------------------------------------

# Run the API. Docs at http://localhost:8003/docs.
#
# 8003 is pinned, not incidental: DataHub owns 8080 (GMS) and 9002 (UI) on this machine,
# and a port clash surfaces as an audit that cannot reach the catalog — which reads like a
# DataHub outage rather than a collision. Overridable with ATTEST_API_PORT.
serve:
    python -m uvicorn attest.api.app:app --reload --port {{env("ATTEST_API_PORT", "8003")}}

# --- verification ------------------------------------------------------------

# Run the suite against the live seeded catalog.
test *ARGS:
    python -m pytest {{ARGS}}

# The semantic layer against a REAL model. Costs money; needs OPENAI_API_KEY.
# The rest of the suite runs offline against a scripted fake and is free.
live:
    python -m pytest tests/test_live.py -m live -v

# Just the coverage matrix: can every claim type still reach every verdict?
matrix:
    python -m pytest tests/test_coverage.py -v

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

# Everything CI would run. Free, offline, no API key needed.
check: lint test

# What to run before pushing a change to the semantic layer. `check` proves the guard
# still catches hallucinations; `live` proves it still lets the truth through. Neither
# half is sufficient on its own -- see the cadence rule in CLAUDE.md.
preflight: lint test live
