# Phase 3 — Manual live-stack acceptance runbook

This is the **manual** end-to-end acceptance for the MVP (plan §9, Phase 3
verify). It proves, against a **real Paperless-NGX instance**, that `pa doctor`
goes green and a sweep triages + proposes metadata with **zero manual field
setup**. It is a human-run acceptance step for the product owner — it is **not**
part of the automated offline suite and must not be reported as automated-passing.

---

## Automated (offline) vs. manual (live-stack)

| Check | How it's verified | Where |
|-------|-------------------|-------|
| Layered config precedence + YAML-secret refusal | automated, offline | `tests/test_config_layering.py` |
| `pa setup` idempotency + incompatible-field reporting | automated, offline (fake Paperless) | `tests/test_provision.py` |
| `pa doctor` green + every failure mode | automated, offline (fake Paperless) | `tests/test_doctor.py` |
| One `pa run` sweep tick (triage + metadata, **no** re-OCR by default), persisted report, idempotent re-run, JSON log shape | automated, offline (fake Paperless + stub provider) | `tests/test_sweep.py` |
| CLI subcommands wired (`init`/`setup`/`doctor`/`run`/`serve`) | automated, offline | `tests/test_cli.py` |
| Dockerfile pure-python/non-root/no-ports; image builds; `pa --help` runs | automated **if Docker is available**, else skipped (clearly) | `tests/test_docker.py` |
| **Full live stack: real Paperless-NGX, real doctor-green, real sweep** | **MANUAL — this runbook** | below |

Run the offline layer any time with:

```
.\.venv\Scripts\python.exe -m pytest -q
```

---

## Manual live-stack acceptance

### 0. Prerequisites
- Docker + docker compose on the host.
- An AI provider key (e.g. `ANTHROPIC_API_KEY`) for the metadata proposal step.
- The `paperless-assistant` image built/available (`docker build -t paperless-assistant:local .`).

### 1. Stand up a stock Paperless-NGX stack
Use the official compose file (https://docs.paperless-ngx.com/setup/). Then create
the admin user and log in:

```
docker compose up -d
docker compose run --rm webserver createsuperuser   # if not already prompted
```

### 2. Create a **dedicated service user + scoped token** (least privilege, connectivity §4)
In the Paperless UI: create a **non-admin** user, grant it document / custom field /
tag / correspondent / document type / task permissions, then create an **API token**
for that user (Settings → your profile → API Token). Do **not** use the admin token —
`pa doctor` will warn if you do.

### 3. Add the assistant service with `pa init`
Print the compose block and paste it into your Paperless `docker-compose.yml`
(alongside `webserver`):

```
docker run --rm paperless-assistant:local init
```

Set secrets in your `.env` (never in the YAML config):

```
PAPERLESS_ASSISTANT_TOKEN=<the scoped service-user token from step 2>
ANTHROPIC_API_KEY=<your key>
```

Bring it up:

```
docker compose up -d paperless-assistant
```

### 4. Provision prerequisites with `pa setup` (idempotent)
```
docker compose exec paperless-assistant pa setup
```
Expect it to **create** the custom fields `ocr_quality` (Number/float), `ai_stage`
(Select: triaged / reocr_done / metadata_done), `ai_notes` (Text), and the review
tags `superseded` + `ai-new-taxonomy`. Run it a **second** time — expect
`Nothing to do … (idempotent no-op)`.

### 5. `pa doctor` — expect all green
```
docker compose exec paperless-assistant pa doctor
```
Expect `[OK ]` for connectivity, token-scope (non-admin), each field, each tag, the
metadata provider, and a config summary line showing the spend caps. Exit code `0`.
If anything is red, the message tells you exactly how to fix it.

### 6. Consume a garbage-OCR document
Feed a deliberately bad scan (image-only / gibberish OCR) into Paperless (drop it in
the consume dir or upload via the UI) and wait for consumption to finish.

### 7. Run a sweep and confirm the report + proposed metadata
The first run defaults to a **bounded dry-run** (I7):

```
docker compose exec paperless-assistant pa run
```

Expect:
- console summary: the garbage doc **triaged** and **flagged** for re-OCR
  (`ocr_quality >= 0.55`), and the clean docs get **proposed metadata**
  (title / correspondent / type / tags) — all as *proposals*, nothing written yet;
- a persisted JSON run report under `./pa-data/run-reports/sweep-*.json`;
- structured JSON logs under `./pa-data/logs/pa.jsonl`.

Inspect the report:

```
docker compose exec paperless-assistant sh -c 'cat /data/run-reports/sweep-*.json'
```

To actually apply the proposals, re-run (subsequent runs write) or `pa run --write`.
Re-OCR stays **OFF** unless you set `PA_REOCR_ENABLED=true` / `--reocr`.

### 8. (Optional) confirm unattended operation
```
docker compose exec paperless-assistant pa status     # local status, no heartbeat
```
`pa serve` (the default container command) sweeps on the configured interval; because
every run is idempotent, restarting the container is safe and never reprocesses
already-done docs.

---

## Acceptance criteria (all must hold on the live stack)
- [ ] `pa setup` provisioned all fields + tags with zero manual UI field creation; a
      second run was a no-op.
- [ ] `pa doctor` exited `0` with all green (a WARN for admin token is acceptable
      only if you intentionally used admin; prefer the scoped service user).
- [ ] A garbage-OCR doc was triaged + flagged; clean docs got proposed metadata.
- [ ] The first `pa run` was a dry-run and wrote a JSON run report to `/data`.
- [ ] Re-OCR did **not** run unless explicitly enabled.
- [ ] No host ports were published by the assistant container.

---

## Live acceptance run log — 2026-07-03

Run against a throwaway stock Paperless-NGX stack (postgres + redis + paperless-ngx
`latest`) via the `paperless-assistant:local` container joined to the stack network
(no published ports). Admin token used (token-scope WARN expected/accepted).

**Two real bugs were found live that the offline fake could not catch — both fixed,
offline suite hardened to guard them, 138 tests still green:**
1. `pa setup` created `ai_notes` with `data_type="text"` → real Paperless returns
   `400 "text" is not a valid choice`. Paperless-NGX's text type is `"string"`.
   Fixed in `provision.py`; the test fake now validates `data_type` against the real
   allowed set and the healthy fixture uses `"string"`.
2. `pa doctor` connectivity probed the API root `/api/`, which 302-redirects and
   (followed) yields `406 Not Acceptable` → doctor always FAILED connectivity on a
   real instance. Fixed to probe `/api/ui_settings/`; the fake now returns 406 for the
   bare root and the doctor tests inject failure at the real probe endpoint.

**Results after fixes:**
| Criterion | Result |
|-----------|--------|
| `pa setup` provisions fields+tags, zero manual; 2nd run no-op | ✅ PASS (also recovered cleanly from a partial-failure first run) |
| `pa doctor` all Paperless checks green | ✅ connectivity / 3 fields / 2 tags / config all `[OK]`; token-scope `[WARN]` (admin, expected) |
| `pa doctor` exits 0 fully green | ✅ with `ANTHROPIC_API_KEY` set: `provider:metadata [OK]`, exit 0 (admin-token WARN is the only warning) |
| Garbage-OCR doc triaged + flagged | ✅ doc 2 `score=0.681 FLAG-reocr`; clean doc `score=0.126` not flagged |
| Clean doc got proposed + written metadata | ✅ doc 1 → title `ACME Utilities Invoice INV-2026-04817 June 2026`, correspondent `ACME Utilities Inc.`, type `Invoice`, tags `[billing, electricity, utilities]` + `ai-new-taxonomy` review flag (I4/I5), `ai_stage=metadata_done`; cost $0.0062 (< $1 cap). Garbage doc correctly EXCLUDED from metadata. |
| First `pa run` was a dry-run + wrote JSON report to `/data` | ✅ dry-run; `/data/run-reports/sweep-*.json` + `/data/logs/pa.jsonl` + `cursor.json`; docs' `custom_fields` remained `[]` |
| Idempotency (I1) | ✅ a repeat `pa run --write` skipped both docs (`already triaged`), spent $0.00; the `metadata_done` doc was NOT re-triaged (sweep-level guard held); period spend ledger persisted |
| Re-OCR did not run unless enabled | ✅ `reocr=OFF (default)`, not executed |
| No host ports published; non-root | ✅ image `ExposedPorts={}`, `User=pa` |

**Outcome: FULL PASS.** All acceptance criteria verified live on a throwaway
Paperless-NGX stack (with `ANTHROPIC_API_KEY` supplied for the metadata/doctor steps).
The two bugs above were fixed and guarded before the pass. The only residual advisory
is the admin-token WARN — a scoped service-user token is recommended for production
(connectivity §4), not a blocker.
