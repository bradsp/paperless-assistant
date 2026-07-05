# Phase 4 — On-ingest webhook nudge acceptance runbook

Phase 4 adds near-real-time processing of newly-consumed documents via a Paperless
**Workflow → Webhook** *nudge*, plus a source-level correctness fix to the stage
state machine. This runbook covers **both** the automated offline proof and the
**manual** live-stack webhook path, clearly separated. It builds on
[`phase3-acceptance.md`](./phase3-acceptance.md) (Phase 3 remains the MVP baseline).

The webhook is a **nudge, not a data channel**: Paperless tells the agent "document
N changed, come look," and the agent **pulls** the document via the REST API and runs
it through the *same* idempotent pipeline as the scheduled sweep. The **scheduled
sweep stays authoritative** — correctness never depends on a webhook firing.

---

## Automated (offline) vs. manual (live-stack)

| Check | How it's verified | Where |
|-------|-------------------|-------|
| `{doc_url}` payload → integer doc id parsing; non-integer/malformed rejected | automated, offline | `tests/test_webhook.py::test_parse_doc_id_*` |
| Persisted nudge queue: enqueue, debounce, **restart survival** | automated, offline | `tests/test_webhook.py::test_queue_*` |
| Valid nudge processes **exactly that** doc through the pipeline | automated, offline (fake Paperless + stub provider, ephemeral localhost port) | `tests/test_webhook.py::test_valid_nudge_processes_exactly_that_doc` |
| Unauthenticated / malformed / non-integer nudge → 4xx + **does nothing** | automated, offline | `tests/test_webhook.py::test_unauthenticated_*`, `test_malformed_*`, `test_non_integer_*` |
| Duplicate nudges do **not** double-process / double-spend | automated, offline | `tests/test_webhook.py::test_duplicate_*` |
| Not-yet-OCR'd doc handled gracefully (no garbage write) | automated, offline | `tests/test_webhook.py::test_not_yet_ocrd_doc_handled_gracefully` |
| Restart mid-queue **resumes without reprocessing** done docs (I1) | automated, offline | `tests/test_webhook.py::test_restart_resumes_pending_without_reprocessing_done` |
| Per-period **spend cap** applies to nudge-triggered work (I3) | automated, offline | `tests/test_webhook.py::test_nudge_respects_period_spend_cap` |
| Receiver **refuses to start** unauthenticated (no secret) | automated, offline | `tests/test_webhook.py::test_server_refuses_to_start_without_secret` |
| Webhook config layering; **secret from env only**, refused from YAML | automated, offline | `tests/test_config_layering.py::test_webhook_*` |
| `pa serve --webhook` help + refusal without secret; `pa doctor` reports webhook | automated, offline | `tests/test_cli.py::test_cli_serve_*`, `test_cli_doctor_reports_webhook` |
| Source `already_triaged` fix: a `metadata_done` doc is **not re-triaged** | automated, offline | `tests/test_characterization_package.py::test_already_triaged` |
| **Live stack: Workflow+Webhook, near-real-time processing, sweep backstop** | **MANUAL — this runbook** | below |

Run the offline layer any time with:

```
.\.venv\Scripts\python.exe -m pytest -q
```

---

## The stage state-machine correctness fix (automated)

`StageOrchestrator.already_triaged()` previously matched **only** the exact
`triaged` label, so a document already advanced to `metadata_done` (or `reocr_done`)
looked re-triageable. Re-triaging it reset `ai_stage` back to `triaged`, which made
the metadata stage re-process and **re-bill** it on a later pass — an I1
(idempotency) + I3 (spend) violation. Phase 3 worked around this with an interim
sweep-level guard (`sweep.py::_already_in_pipeline`).

Phase 4 fixes it **at the source**: `already_triaged()` now means "at `triaged` **or
any later stage**" (using the ordered `config.STAGE_ORDER`), and the interim
`_already_in_pipeline` guard has been **removed** so there is a **single
authoritative eligibility rule**. This is a deliberate, intended behavior change,
locked by `tests/test_characterization_package.py::test_already_triaged` (which now
asserts `metadata_done` → handled = True). The frozen original scripts and their
characterization test are untouched.

---

## Manual live-stack webhook acceptance

> Prerequisite: complete the Phase 3 live runbook first (stack up, service user +
> scoped token, `pa setup`, `pa doctor` green).

### 1. Set the webhook shared secret (env only)

In your `.env` (never the YAML config):

```
PA_WEBHOOK_SECRET=<a long random string>
PA_WEBHOOK_ENABLED=true
```

### 2. Start the agent with the nudge receiver

Either set `command: ["serve", "--webhook"]` in the compose block, or rely on
`PA_WEBHOOK_ENABLED=true` with `command: ["serve"]` — both start the in-network
receiver alongside the scheduled sweep. Bring it up:

```
docker compose up -d paperless-assistant
docker compose exec paperless-assistant pa doctor      # webhook check should be [OK]
```

`pa doctor` prints the exact in-network URL and Workflow guidance. The receiver
binds `0.0.0.0:8765` **inside the compose network only** — confirm there is **no
`ports:` mapping** for the service (the agent publishes nothing to the host):

```
docker compose port paperless-assistant 8765     # expect: no host binding
docker inspect --format '{{.Config.ExposedPorts}}' <image>   # expect: map[] / no 8765
```

### 3. Create the Paperless Workflow → Webhook action

In the Paperless UI: **Settings → Workflows → Add Workflow**.

- **Trigger type:** **"Document Consumption Finished"** (or "Document Updated").
  **Do NOT use "Document Added"** — it fires *before* OCR/text extraction, so the
  content the engine needs does not yet exist (verified Paperless limitation,
  issue #12117). A nudge for a not-yet-OCR'd doc is skipped by the agent and left
  for the sweep, so no garbage is written — but Consumption Finished is correct.
- **Action type:** **Webhook**.
- **URL:** `http://paperless-assistant:8765/hooks/paperless?token=<PA_WEBHOOK_SECRET>`
  (in-stack service name; the token authenticates the nudge). Alternatively send the
  secret as an `Authorization: Bearer <secret>` or `X-PA-Webhook-Secret` header.
- **Body / payload:** a JSON object carrying the doc URL placeholder:
  `{"doc_url": "{doc_url}"}`. The agent extracts the integer id from `{doc_url}`
  and pulls the document via REST — it never trusts pushed content.

Save the workflow.

### 4. Observe near-real-time processing

Consume a new document (drop it in the consume dir or upload via the UI). Within
**seconds** of consumption finishing, the nudge fires and the agent processes that
one document. Confirm:

```
docker compose exec paperless-assistant sh -c 'tail -n 20 /data/logs/pa.jsonl'
```

Expect `nudge_received` → `nudge_start` → per-doc `stage_transition` / `doc_outcome`
→ `nudge_end` events for exactly that doc id, and the doc's `ai_stage` advancing
(triaged, then metadata_done for a clean doc). A **duplicate** nudge for the same doc
logs `debounced: true` (or a no-op skip) and does **not** re-spend.

### 5. Confirm the scheduled sweep is still the authoritative backstop

Prove correctness does not depend on the webhook:

1. **Stop** the agent: `docker compose stop paperless-assistant`.
2. Consume another document **while the agent is down** (no nudge is delivered).
3. **Start** the agent: `docker compose start paperless-assistant`. On startup it
   resumes any queued-but-unprocessed nudges from `/data/webhook-queue.json` without
   reprocessing already-done docs, and the **next scheduled sweep** picks up the doc
   consumed while it was down and processes it normally.

---

## Acceptance criteria (all must hold on the live stack)

- [ ] `pa doctor` reports the webhook check `[OK]` with the in-network URL; the
      secret is set via env, not YAML.
- [ ] **No host port** is published for the agent (no `ports:` mapping; image
      exposes no host port); Paperless reaches the receiver by service name.
- [ ] A newly-consumed doc is triaged + metadata-proposed within seconds of a
      "Consumption Finished" nudge, with no manual action.
- [ ] An unauthenticated or malformed nudge is rejected (4xx) and does nothing
      (check the logs for `nudge_rejected`).
- [ ] A duplicate nudge for the same doc does not double-process or double-spend.
- [ ] A doc consumed **while the agent was down** is still processed — by the
      scheduled sweep (and any queued nudge resumes) — proving the sweep is
      authoritative and the webhook is only a latency optimisation.
- [ ] Per-period spend caps still bound nudge-triggered work.
