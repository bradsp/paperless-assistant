<#
.SYNOPSIS
  Stand up the throw-away stack and drive Paperless Assistant's Ollama vision
  re-OCR end-to-end, to verify the 400/405 fix on a real document.

.DESCRIPTION
  1. Brings up Paperless-NGX, Redis, and Ollama.
  2. Pulls the vision model into Ollama.
  3. Mints a Paperless API token from the auto-created admin user.
  4. Generates an image-only PDF invoice and lets Paperless ingest it.
  5. Runs `pa setup`, `pa doctor --probe-ollama`, `pa triage`, then a forced
     `pa reocr` through the local Ollama vision model.

  Re-runnable. Heavy steps (model pull, image build) are idempotent.

.EXAMPLE
  ./bootstrap.ps1
  ./bootstrap.ps1 -SkipModelPull        # model already pulled
#>
[CmdletBinding()]
param(
  [switch]$SkipModelPull,
  [switch]$FullReocr,                    # actually round-trip (consume) instead of dry-run
  [int]$IngestTimeoutSec = 180
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" -ForegroundColor DarkGray }

# --- .env -------------------------------------------------------------------
if (-not (Test-Path ./.env)) {
  Copy-Item ./.env.example ./.env
  Info "created .env from .env.example"
}
# Load a few values we need on the host side.
$envMap = @{}
foreach ($line in Get-Content ./.env) {
  if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { $envMap[$Matches[1]] = $Matches[2].Trim() }
}
$port      = if ($envMap.PAPERLESS_PORT) { $envMap.PAPERLESS_PORT } else { "8000" }
$adminUser = if ($envMap.PAPERLESS_ADMIN_USER) { $envMap.PAPERLESS_ADMIN_USER } else { "admin" }
$adminPass = if ($envMap.PAPERLESS_ADMIN_PASSWORD) { $envMap.PAPERLESS_ADMIN_PASSWORD } else { "admin" }
$model     = if ($envMap.OLLAMA_MODEL) { $envMap.OLLAMA_MODEL } else { "moondream" }
$base      = "http://localhost:$port"

# --- 1. bring up core services (also builds the assistant image) -----------
Step "Building assistant image + starting Paperless, Redis, Ollama"
docker compose up -d --build broker webserver ollama
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed" }

# --- 2. wait for Paperless to answer ---------------------------------------
Step "Waiting for Paperless at $base"
$deadline = (Get-Date).AddSeconds(240)
do {
  Start-Sleep -Seconds 5
  try {
    # Any HTTP status (even 401/403) means it's up; only connection errors throw.
    Invoke-WebRequest -Uri "$base/api/" -SkipHttpErrorCheck -TimeoutSec 5 | Out-Null
    $up = $true
  } catch { $up = $false; Write-Host "." -NoNewline }
} while (-not $up -and (Get-Date) -lt $deadline)
if (-not $up) { throw "Paperless did not become reachable at $base" }
Info "Paperless is up."

# --- 3. pull the vision model ----------------------------------------------
if (-not $SkipModelPull) {
  Step "Pulling Ollama model '$model' (first pull downloads a few GB)"
  docker compose exec -T ollama ollama pull $model
  if ($LASTEXITCODE -ne 0) { throw "ollama pull '$model' failed" }
}

# --- 4. mint a Paperless API token -----------------------------------------
Step "Minting Paperless API token for '$adminUser'"
$token = $null
$deadline = (Get-Date).AddSeconds(120)
do {
  try {
    $resp = Invoke-RestMethod -Uri "$base/api/token/" -Method Post `
      -Body @{ username = $adminUser; password = $adminPass } -TimeoutSec 5
    $token = $resp.token
  } catch { Start-Sleep -Seconds 5; Write-Host "." -NoNewline }
} while (-not $token -and (Get-Date) -lt $deadline)
if (-not $token) { throw "could not obtain an API token (is the admin user created yet?)" }
Info "token acquired."

# --- 5. generate the sample doc + let Paperless ingest it ------------------
Step "Generating image-only sample PDF and dropping it into the consume folder"
docker compose cp ./gen-sample.py webserver:/tmp/gen-sample.py
docker compose exec -T webserver python3 /tmp/gen-sample.py
if ($LASTEXITCODE -ne 0) { throw "sample generation failed" }

Info "waiting for ingestion..."
$headers = @{ Authorization = "Token $token" }
$deadline = (Get-Date).AddSeconds($IngestTimeoutSec)
do {
  Start-Sleep -Seconds 5
  try {
    $docs = Invoke-RestMethod -Uri "$base/api/documents/?page_size=1" -Headers $headers -TimeoutSec 5
    $count = $docs.count
  } catch { $count = 0 }
  Write-Host "." -NoNewline
} while ($count -lt 1 -and (Get-Date) -lt $deadline)
Write-Host ""
if ($count -lt 1) { throw "no document was ingested within $IngestTimeoutSec s" }
Info "$count document(s) in Paperless."

# --- 6. drive the assistant -------------------------------------------------
function Pa([string[]]$paArgs) {
  docker compose run --rm -e "PAPERLESS_TOKEN=$token" paperless-assistant @paArgs
  if ($LASTEXITCODE -ne 0) { throw "pa $($paArgs -join ' ') exited $LASTEXITCODE" }
}

Step "pa setup (provision custom fields + review tags)"
Pa @("setup")

Step "pa doctor --probe-ollama (validates endpoint shape + model reachability)"
Pa @("doctor", "--probe-ollama")

Step "pa triage (local, free — marks the doc ai_stage=triaged)"
Pa @("triage")

if ($FullReocr) {
  Step "pa reocr --threshold 0 (FULL: Ollama vision re-OCR + re-consume)"
  Pa @("reocr", "--threshold", "0", "--limit", "5")
} else {
  Step "pa reocr --dry-run --threshold 0 (Ollama vision re-OCR, no consume)"
  Info "this is the fix under test: it rasterizes the PDF and calls Ollama /api/generate."
  Pa @("reocr", "--dry-run", "--threshold", "0", "--limit", "5")
}

Step "Done"
Write-Host @"
Success criteria:
  * The 'pa reocr' step above printed [dry] / [done] lines with 'OCR ok (N chars ...)'
    — that means the Ollama vision call SUCCEEDED (no 400/405).
  * 'pa doctor --probe-ollama' reported the endpoint + model checks.

Explore:
  * Paperless UI:  $base   (login: $adminUser / $adminPass)
  * Re-run with -FullReocr to actually re-consume the corrected PDF as a new doc.
  * Negative tests (should now give CLEAR errors, not a bare 400/405):
      - wrong endpoint (405):  edit compose PA_OLLAMA_ENDPOINT to http://ollama:11434/v1 and re-run reocr
      - model not pulled:      set OLLAMA_MODEL to a not-pulled name and run 'pa reocr'
      - non-vision model:      docker compose exec ollama ollama pull llama3.2 ; set OLLAMA_MODEL=llama3.2

Tear down (removes ALL data + volumes):
  ./teardown.ps1
"@ -ForegroundColor Green
