# Bring up the pinned DataHub Core stack (v1.5.0.6) and BLOCK until GMS is actually healthy.
#
# Why this script instead of a bare `datahub docker quickstart`:
#
#  1. NO GITHUB FETCH. `--quickstart-compose-file` points the CLI at the vendored compose
#     (deploy/datahub/docker-compose.yml), so bring-up does not depend on fetching a compose
#     file from raw.githubusercontent at run time — which fails outright on a TLS-inspecting
#     network (docs/datahub-setup.md) and is a single point of failure everywhere else.
#
#  2. WE GATE ON HEALTH, NOT ON THE CLI'S EXIT. The quickstart CLI boots the stack and then
#     crashes printing its success checkmark on a cp1252 console (UnicodeEncodeError), so its
#     exit code is worthless — non-zero on success. Worse, a bring-up that LOOKS hung (a
#     silent 60-90s while the JVM starts and migrations run) reads as a failure to a judge.
#     So the CLI runs in the background and THIS script polls GMS /config with a progress
#     line every few seconds, and it is the health check — not the CLI — that decides done.
#
#  3. IT CANNOT HANG. The poll has a hard deadline; past it the script fails loudly rather
#     than waiting forever on a stack that will never come up.
#
# Measured cold (fresh volumes, images already pulled) and warm numbers live in
# docs/deployment.md. The deadline below is set well above the measured cold time.

param(
    # Generous, because the FIRST bring-up on a fresh machine pulls ~12.6 GB of images before
    # the ~4.5 min cold boot (measured 2026-07-18: 265s to GMS healthy, images already present).
    # The deadline is a runaway backstop, not the expected wait — the progress line is.
    [int]$TimeoutSeconds = 1800,
    [string]$GmsUrl = "http://localhost:8080"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path "$PSScriptRoot/../..").Path
$compose = Join-Path $repoRoot "deploy/datahub/docker-compose.yml"

if (-not (Test-Path $compose)) { Write-Error "vendored compose not found: $compose"; exit 1 }

# Put the repo's venv on PATH if it exists, so `datahub` resolves whether or not the caller
# activated it first — the same tool `just seed` needs. Falls back to PATH as-is otherwise.
$venvScripts = Join-Path $repoRoot ".venv/Scripts"
if (Test-Path $venvScripts) { $env:PATH = "$venvScripts;$env:PATH" }

# FAIL FAST if the DataHub CLI is missing. Below, `datahub docker quickstart` runs in a
# BACKGROUND job with its output discarded (*> $null), so a missing CLI would make that job
# die instantly and SILENTLY -- and the poll would then watch "starting..." until the 1800s
# deadline before failing with a message pointing at docker, which is the wrong thing to
# check. `just setup` installs the CLI (via requirements.txt). This turns a 30-minute hang
# into a one-second, accurate error. Checked AFTER the venv is on PATH so a venv-only install
# is found.
if (-not (Get-Command datahub -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: the 'datahub' CLI is not on PATH." -ForegroundColor Red
    Write-Host "Run 'just setup' first -- it installs acryl-datahub (the CLI + the SDK the seed imports) via requirements.txt -- then 'just up'."
    exit 1
}

Write-Host "Bringing up DataHub Core v1.5.0.6 (vendored compose; no GitHub fetch)."
Write-Host "First bring-up on a fresh machine pulls ~12.6 GB of images and runs one-time"
Write-Host "migrations, so allow several minutes. Watching GMS on $GmsUrl ..."

# The CLI owns the stack's env (image tags, token secrets, HOME binds, service ordering) and
# gets it right; a hand-rolled `docker compose up` with a custom env file recreates containers
# with mismatched secrets and fails system-update (measured). So the CLI does the work, in the
# background, and HOME is exported for its bind mounts on Windows (where $HOME is unset).
# NOTE: no --no-pull-images. On a fresh machine the images are not present yet, so the pull
# must happen or there is nothing to run; when they ARE present it is a fast digest check.
# That is what makes this a true one-command bring-up rather than one that assumes a
# pre-pulled machine.
$job = Start-Job {
    $env:HOME = $env:USERPROFILE
    datahub docker quickstart --quickstart-compose-file $using:compose *> $null
}

try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $version = $null
        try { $version = (Invoke-RestMethod "$GmsUrl/config" -TimeoutSec 3).versions.'acryldata/datahub'.version } catch { }
        $elapsed = [int]$sw.Elapsed.TotalSeconds
        if ($version) {
            Write-Host "DataHub healthy at +${elapsed}s (version $version)."
            exit 0
        }
        if ($elapsed -gt $TimeoutSeconds) {
            Write-Host "DataHub did not become healthy within ${TimeoutSeconds}s. Check 'docker ps' and 'docker logs datahub-datahub-gms-quickstart-1'."
            exit 1
        }
        Write-Host "  +${elapsed}s starting (GMS not answering yet)..."
        Start-Sleep -Seconds 5
    }
}
finally {
    Stop-Job $job -ErrorAction SilentlyContinue | Out-Null
    Remove-Job $job -Force -ErrorAction SilentlyContinue | Out-Null
}
