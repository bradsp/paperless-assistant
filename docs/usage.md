# Usage

Day-to-day operation of Paperless Assistant: the `pa` CLI, dry-run vs. writes, the
scheduled sweep, and how to review what the AI did inside Paperless.

All commands below assume the Docker companion; prefix them with
`docker compose exec paperless-assistant`. If you installed from source, run `pa …`
directly.

---

## The `pa` CLI at a glance

| Command | What it does |
|---------|--------------|
| `pa init` | Print the docker-compose service block (`--out FILE` to also write it). |
| `pa setup` | Idempotently provision the required custom fields + review tags. |
| `pa doctor` | Health check: connectivity, token scope, fields/tags, provider, config (`--json` for machine-readable). |
| `pa run` | Run one sweep tick over the enabled stages (first run = dry-run). |
| `pa serve` | Run scheduled sweeps on an interval; optionally the webhook receiver + dashboard. |
| `pa status` | Print a local status snapshot (connectivity, queue, last run, spend-vs-cap). |
| `pa web` | Run only the token-protected [web dashboard](dashboard.md). |
| `pa triage` | Score OCR quality into custom fields (stage 0, the free local heuristic). |
| `pa reocr` | Vision re-OCR of garbage scans (stage 1 — see [Re-OCR](re-ocr.md)). |
| `pa metadata` | AI metadata refresh (stage 2). |

`pa agent` is an alias for `pa serve`.

The three stage commands (`triage` / `reocr` / `metadata`) run a single stage
directly. `pa run` / `pa serve` orchestrate the enabled stages together and honor the
[layered config](configuration.md) (including the first-run dry-run and spend caps).
Most operators use `pa run` / `pa serve`; the per-stage commands are handy for
targeted work and testing.

---

## Dry-run vs. writes

**The first processing run is always a bounded dry-run** — it scores and proposes but
writes nothing, and saves a report. This is invariant I7 (safe by default).

```bash
pa run                # first run = DRY-RUN (proposes, writes nothing)
cat ./pa-data/run-reports/sweep-*.json   # inspect what it WOULD change
pa run --write        # apply changes now
```

After the first dry-run tick, subsequent **scheduled** `serve` sweeps write
automatically. You can force either mode explicitly:

- `pa run --dry-run` — force a dry-run (propose, write nothing), even later.
- `pa run --write` — force a real write, even on the very first run.

Every run is **idempotent**: already-processed documents are skipped (based on their
`ai_stage`), so re-running is always safe. Use `--force` to re-process already-done
docs deliberately.

---

## Flags, and where each applies

| Flag | Applies to | Meaning |
|------|-----------|---------|
| `--dry-run` | `triage`, `reocr`, `metadata`, `run`, `serve` | Propose only; write nothing. |
| `--write` | `run`, `serve` | Force a real write, even on the first run. |
| `--force` | `triage`, `run`, `serve` | Re-process documents already marked done. |
| `--limit N` | all stages + `run` / `serve` | Cap each stage to N docs per run (`0` = all eligible). |
| `--workers N` | `triage`, `reocr`, `metadata`, `run`, `serve` | Concurrent requests (keep modest; ≤ DB pool). |
| `--threshold F` | `triage`, `reocr`, `run` / `serve` | OCR-quality score at/above which a doc is flagged for re-OCR (default `0.55`). |
| `--max-spend F` | `reocr`, `metadata`; on `run`/`serve` as per-run cap | USD ceiling that hard-aborts new work once exceeded. |
| `--provider NAME` | `reocr`, `metadata` | Override the AI provider for that task (`anthropic` / `openai` / `ollama`). |
| `--model ID` | `reocr`, `metadata` | Override the model for that task. |
| `--reocr` | `run`, `serve` | Enable the (default-off) re-OCR stage for this run. |
| `--iterations N` | `serve` | Stop after N sweep ticks (default: run forever). |
| `--webhook` | `serve` | Also run the on-ingest [webhook](webhook.md) nudge receiver. |
| `--config PATH` | `setup`, `doctor`, `run`, `serve`, `status`, `web` | Path to a YAML config file (default `/data/config.yml`). |

Default worker counts differ per stage: triage `4`, re-OCR `3`, metadata `3`. The
sweep default is `3`.

> On `pa run` / `pa serve`, `--max-spend` maps to the **per-run** spend cap.
> `--threshold` maps to the triage threshold. These are per-run overrides — the
> highest layer of the [config precedence](configuration.md).

Examples:

```bash
pa run --write --limit 50               # apply changes to at most 50 docs per stage
pa triage --dry-run --limit 25          # just score 25 docs, write nothing
pa metadata --dry-run --limit 10        # just preview metadata proposals
pa reocr --dry-run --limit 2 --max-spend 5.00   # try re-OCR on 2 docs, cap at $5
```

---

## The scheduled sweep (`pa serve`)

With `command: ["serve"]` (the default in the compose block), the container runs
sweeps on an interval — **hourly by default** (`PA_SCHEDULE_INTERVAL`, seconds).
Each sweep runs the enabled stages (triage + metadata by default; re-OCR only if you
[enable it](re-ocr.md)), skips already-done docs, and persists a run report under
`/data/run-reports/`.

`serve` is restart-safe: a `/data/cursor.json` marker records the first-run state and
last-run status, so a restart resumes without reprocessing. `serve` can also host the
optional [webhook receiver](webhook.md) and [web dashboard](dashboard.md) in the same
container.

```bash
pa status                                   # queue depth, last run, spend-vs-cap
docker compose logs -f paperless-assistant  # structured JSON logs
cat ./pa-data/run-reports/*.json            # per-run reports (counts, spend, new taxonomy)
cat ./pa-data/spend-ledger.json             # cumulative spend this period
```

---

## Reviewing what the AI did (inside Paperless)

The assistant leaves everything auditable in the Paperless UI:

- **`ai_stage`** custom field shows each document's state: `triaged` →
  `metadata_done` (and `reocr_done` if you enable re-OCR). This is how idempotency
  works — a document that has reached a stage is not re-processed back to an earlier
  one.
- **`ocr_quality`** custom field (`0.0` = clean … `1.0` = garbage). Filter
  `ocr_quality >= 0.55` to see the scans that would benefit from re-OCR.
- **`ai_notes`** custom field carries the heuristic's short note about why a document
  scored the way it did.
- **`ai-new-taxonomy`** tag marks any document where the AI **created a new** tag,
  correspondent, or type — filter this tag to review new taxonomy it introduced
  (taxonomy is reuse-first, so this should be rare).
- **`superseded`** tag marks old originals that re-OCR replaced (only if you enable
  re-OCR). They are **never auto-deleted** — you review the `superseded` set and
  bulk-delete when satisfied.

If you enable the [web dashboard](dashboard.md), its **Activity** tab is a
searchable, per-document audit log of exactly what changed (field-level before → after),
including *proposed* changes during a dry-run.

The names above are the defaults; if your Paperless uses different field or stage
names, see [Configuration → matching your names](configuration.md#matching-your-custom-field--stage-names).

---

**Next step:** [Configuration](configuration.md) for the full tunable surface, or
[AI providers](ai-providers.md) to choose and configure your model.
