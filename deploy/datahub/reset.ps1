# DESTROY the DataHub catalog and rebuild it from the seed. The definitive, honest reset.
#
# WHY A FULL WIPE AND NOT A TARGETED DELETE. A published verdict is a content-addressed
# artifact whose verdict history is a TIMESERIES aspect, and removing that cleanly is only
# possible one way (measured 2026-07-18 against Core v1.5.0.6):
#
#   DELETE /openapi/v3/entity/assertion/{urn}  -> HTTP 200, and the timeseries verdict
#                                                 SURVIVES (history=1). A delete that reports
#                                                 success and changes nothing.
#   `datahub delete --urn <urn> --hard`        -> removes the timeseries rows. Clean.
#
# So a per-artifact reset script COULD hard-delete each artifact — but it would have to
# enumerate every Attest artifact across every seeded dataset to be complete, and a reset
# that misses one is a partial lie. `datahub docker nuke` wipes the volumes, so completeness
# is definitional rather than a property of an enumeration we have to get right. That is why
# this is the reset, and why per-judge isolation (each judge runs their own stack) is the
# real answer — this is the operator's rebuild, not a per-request endpoint.
#
# It regenerates tests/fixtures/snapshots/ too (via `just capture`, last seed step), because
# the seed uses relative dates: a fresh catalog has fresh lastModified timestamps, and the
# offline fixtures must match the catalog they describe. That is existing `just seed`
# behavior, surfaced here so it is not a surprise.

param(
    [int]$TimeoutSeconds = 720
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path "$PSScriptRoot/../..").Path
$up = Join-Path $PSScriptRoot "up.ps1"

# Put the repo's venv on PATH if present, so `datahub`/`python` resolve without prior
# activation — the same convention `just seed` follows. Falls back to PATH as-is.
$venvScripts = Join-Path $repoRoot ".venv/Scripts"
if (Test-Path $venvScripts) { $env:PATH = "$venvScripts;$env:PATH" }

Write-Host "============================================================================"
Write-Host " RESET: this DESTROYS the DataHub catalog (all containers AND data volumes)"
Write-Host " and rebuilds it from the seed. Nothing in Attest's own store is touched; only"
Write-Host " the catalog. Allow several minutes for the cold bring-up and reseed."
Write-Host "============================================================================"

$total = [System.Diagnostics.Stopwatch]::StartNew()

# 1. Wipe. No --keep-data, so the data volumes go too.
Write-Host "`n[1/3] Nuking the DataHub stack (containers + volumes)..."
datahub docker nuke

# 2. Cold bring-up from the vendored compose. Subprocess so up.ps1's `exit` cannot abort
#    this script before the reseed runs.
Write-Host "`n[2/3] Cold bring-up..."
$upStart = $total.Elapsed.TotalSeconds
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $up -TimeoutSeconds $TimeoutSeconds
if ($LASTEXITCODE -ne 0) { Write-Error "bring-up failed; catalog is empty. Re-run 'just up' once Docker is healthy."; exit 1 }
$upSecs = [int]($total.Elapsed.TotalSeconds - $upStart)

# 3. Reseed: generate metadata, ingest it, recapture the offline fixtures.
Write-Host "`n[3/3] Reseeding the catalog..."
$seedStart = $total.Elapsed.TotalSeconds
Push-Location $repoRoot
try {
    python seed/generate_seed.py
    if ($LASTEXITCODE -ne 0) { throw "seed generation failed" }
    datahub ingest -c ./seed/recipe.yml
    if ($LASTEXITCODE -ne 0) { throw "seed ingestion failed" }
    python seed/capture_snapshots.py
    if ($LASTEXITCODE -ne 0) { throw "fixture capture failed" }
}
finally {
    Pop-Location
}
$seedSecs = [int]($total.Elapsed.TotalSeconds - $seedStart)

Write-Host "`n============================================================================"
Write-Host (" RESET complete in {0}s  (bring-up {1}s, reseed {2}s)." -f [int]$total.Elapsed.TotalSeconds, $upSecs, $seedSecs)
Write-Host " The catalog is fresh. A judge's demo story starts clean."
Write-Host "============================================================================"
