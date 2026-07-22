# Throw-away test stack — Ollama vision re-OCR

A disposable Docker stack to verify the fix for **re-OCR via local Ollama vision
failing with HTTP 400/405**. It runs Paperless-NGX, Ollama, and Paperless
Assistant **built from this repo** (so it carries the fix, including `pypdfium2`
in the `[ollama]` extra), then drives a real document through the Ollama vision
re-OCR path.

> Not for production. Trivial admin creds, published ports, throw-away library.

## Prerequisites

- Docker Desktop (Compose v2) running.
- ~6–8 GB free disk (Paperless + Ollama images + the `moondream` model).
- Internet access for the first run (image + model pulls).

## Quick start (PowerShell)

```powershell
cd test-stack
./bootstrap.ps1
```

That single command:

1. Builds the assistant image and starts Paperless, Redis, and Ollama.
2. Pulls the vision model (`moondream` by default).
3. Mints a Paperless API token from the auto-created admin user.
4. Generates an **image-only PDF** invoice and lets Paperless ingest it.
5. Runs `pa setup` → `pa doctor --probe-ollama` → `pa triage` → a forced
   `pa reocr --threshold 0` through Ollama.

**Success looks like** the `pa reocr` step printing `[dry] doc N: OCR ok
(… chars …)` — i.e. the Ollama `/api/generate` vision call succeeded, no 400/405.

Flags:

- `-SkipModelPull` — skip step 2 if the model is already pulled.
- `-FullReocr` — actually re-consume the corrected PDF as a new Paperless doc
  (default is a dry-run that builds but doesn't consume).

## What exercises the fix

- The sample is an **image-only PDF**, so `pa reocr` downloads a PDF original and
  hits the fix's **PDF → PNG rasterization** path — the exact branch that used to
  send raw PDF bytes to Ollama's `images` field and 400.
- `PA_OLLAMA_ENDPOINT=http://ollama:11434` is the clean root; the fixed code
  normalizes/validates it.
- `pa doctor --probe-ollama` runs the new endpoint-shape + model-reachability
  checks.

## Verifying the improved error messages (negative tests)

The old code surfaced only "error 400/405". The fix surfaces the server body plus
a remediation hint. To see each distinct error:

| Failure mode | How to trigger | Expected |
| --- | --- | --- |
| **405 / wrong endpoint** | set `PA_OLLAMA_ENDPOINT: http://ollama:11434/v1` in `docker-compose.yml`, re-run `pa reocr` | error says point the endpoint at the root, not `/v1` (auto-strip may also just fix it) |
| **Model not pulled** | set `OLLAMA_MODEL` to a bogus name in `.env`, run `pa reocr` | error says run `ollama pull <model>` |
| **Non-vision model** | `docker compose exec ollama ollama pull llama3.2`; set `OLLAMA_MODEL=llama3.2` | `CapabilityError`: not vision-capable, refused before any HTTP call |

Run a one-off assistant command against the live stack like this (token from the
Paperless UI → top-right → **My Profile** → API token, or re-run `bootstrap.ps1`):

```powershell
docker compose run --rm -e PAPERLESS_TOKEN=<token> paperless-assistant reocr --dry-run --threshold 0
```

## Try a better model

`moondream` is tiny/fast but low-accuracy. For sharper transcription:

```powershell
# in .env: OLLAMA_MODEL=llava:7b   (also update model: in config.yml)
docker compose exec ollama ollama pull llava:7b
./bootstrap.ps1 -SkipModelPull:$false
```

## Explore

- Paperless UI: <http://localhost:8000> (login `admin` / `admin`).
- With `-FullReocr`, the re-OCR'd copy appears as a new document; the old one is
  tagged `superseded`.

## Tear down (deletes everything)

```powershell
./teardown.ps1
```

## Bash equivalents

No PowerShell? The same flow in `bash` (the tool's Bash shell works too):

```bash
docker compose up -d --build broker webserver ollama
docker compose exec -T ollama ollama pull moondream
TOKEN=$(curl -s -X POST http://localhost:8000/api/token/ -d 'username=admin&password=admin' | python -c 'import sys,json;print(json.load(sys.stdin)["token"])')
docker compose cp gen-sample.py webserver:/tmp/gen-sample.py
docker compose exec -T webserver python3 /tmp/gen-sample.py
# wait ~15s for ingestion, then:
docker compose run --rm -e PAPERLESS_TOKEN=$TOKEN paperless-assistant setup
docker compose run --rm -e PAPERLESS_TOKEN=$TOKEN paperless-assistant doctor --probe-ollama
docker compose run --rm -e PAPERLESS_TOKEN=$TOKEN paperless-assistant triage
docker compose run --rm -e PAPERLESS_TOKEN=$TOKEN paperless-assistant reocr --dry-run --threshold 0
docker compose down -v
```
