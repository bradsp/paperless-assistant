# Configuration

The full configuration reference. Everything is optional — omit a value to keep its
safe default. Secrets are **environment-only** by design.

---

## Layered precedence

Settings resolve from four layers, lowest to highest precedence:

```
built-in safe defaults  <  /data/config.yml  <  environment variables  <  CLI flags
```

- **Built-in defaults** are conservative (safe by default, I7).
- **YAML** (`/data/config.yml`, mounted from your `./pa-data` volume) sets non-secret
  tunables. A copy of [`config.example.yml`](../config.example.yml) documents the whole
  surface.
- **Environment variables** override the YAML. This is also where **all secrets** come
  from.
- **CLI flags** (`--limit`, `--write`, `--max-spend`, …) are per-run overrides at the
  top of the stack.

A field set by an environment variable **beats** the same field in YAML — the
[dashboard](dashboard.md) shows such fields as **env-locked** so a save can't silently
no-op.

---

## Secrets are environment-only

Secrets **never** go in the YAML config. The assistant **refuses to load** a config
file containing a secret-looking key (`paperless_token`, `token`, `api_key`,
`anthropic_api_key`, `openai_api_key`, `agent_token`, `secret`, `enrollment_token`,
and similar). Put secrets in a `.env` beside your `docker-compose.yml`:

| Secret env var | Purpose |
|----------------|---------|
| `PAPERLESS_TOKEN` | Scoped Paperless API token (required). |
| `ANTHROPIC_API_KEY` | Anthropic key (when a task uses Anthropic). |
| `OPENAI_API_KEY` | OpenAI key (when a task uses OpenAI). |
| `PA_WEBHOOK_SECRET` | Shared secret for the [webhook](webhook.md) receiver. |
| `PA_UI_TOKEN` | Token for the [web dashboard](dashboard.md). |
| `PA_ENROLLMENT_TOKEN` | One-time enrollment token for [hosted mode](advanced-modes.md). |

Ollama needs **no key** — see [AI providers](ai-providers.md).

---

## Environment variables

Add these to the service `environment:` block (or reference `.env` values there).

### Connection & providers

| Variable | Default | What it does |
|----------|---------|--------------|
| `PAPERLESS_URL` | `http://localhost:8000` | Paperless base URL (in-stack: `http://webserver:8000`). |
| `PAPERLESS_PUBLIC_URL` | *(unset)* | External browser URL used only to build dashboard document links; falls back to `PAPERLESS_URL`. |
| `PAPERLESS_TOKEN` | *(required)* | Scoped Paperless API token (from `.env`, never YAML). |
| `PA_MODE` | `conservative` | Global posture. `hosted` switches to [Mode B](advanced-modes.md). |
| `PA_METADATA_PROVIDER` / `PA_METADATA_MODEL` | `anthropic` / `claude-sonnet-4-6` | Provider/model for metadata. |
| `PA_OCR_PROVIDER` / `PA_OCR_MODEL` | `anthropic` / `claude-opus-4-8` | Provider/model for re-OCR. |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Provider keys (see [AI providers](ai-providers.md)). |
| `OPENAI_BASE_URL` | — | Optional OpenAI-compatible/proxy endpoint. |
| `PA_OLLAMA_ENDPOINT` | `http://localhost:11434` | Ollama endpoint (in Docker, e.g. `http://ollama:11434`). |

### Stages, thresholds, concurrency

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_TRIAGE_ENABLED` | `true` | Toggle the OCR-quality triage stage. |
| `PA_METADATA_ENABLED` | `true` | Toggle the metadata stage. |
| `PA_REOCR_ENABLED` | `false` | Turn on AI [re-OCR](re-ocr.md) (spends money, rewrites docs). |
| `PA_TRIAGE_THRESHOLD` | `0.55` | Score ≥ this flags a doc for re-OCR. |
| `PA_WORKERS` | `3` | Concurrent workers (keep modest; ≤ DB pool). |

### Spend caps (the safety net)

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_SPEND_PER_RUN` | `1.00` | USD hard-abort cap **per run**. |
| `PA_SPEND_PER_PERIOD` | `5.00` | USD cap across the period (unattended safety). |

Both caps are **hard aborts, not accounting** (invariant I3): new work stops once a
cap is reached. The period bucket (`daily` / `weekly` / `monthly`) is set in YAML
(`spend.period`, default `monthly`).

### Schedule & run behavior

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_SCHEDULE_INTERVAL` | `3600` | `pa serve` sweep interval, seconds. |
| `PA_LIMIT` | `0` | Per-stage document cap per run (`0` = all eligible). |
| `PA_DRY_RUN` | *(unset)* | Force dry-run every run. **Leave unset** for the safe first-run default. |
| `PA_DATA_DIR` | `/data` | The mounted state directory (relocate for local dev only). |

### Webhook & dashboard

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_WEBHOOK_ENABLED` / `PA_WEBHOOK_SECRET` | `false` / — | On-ingest [webhook](webhook.md) (secret is env-only). |
| `PA_WEBHOOK_HOST` / `PA_WEBHOOK_PORT` / `PA_WEBHOOK_PATH` | `0.0.0.0` / `8765` / `/hooks/paperless` | Webhook bind + endpoint (in-network only, no host port). |
| `PA_WEBHOOK_DEBOUNCE` | `30` | Collapse rapid duplicate nudges for one doc (seconds). |
| `PA_UI_ENABLED` / `PA_UI_TOKEN` | `false` / — | Web [dashboard](dashboard.md); token is **env-only**, required, fail-closed. |
| `PA_UI_HOST` / `PA_UI_PORT` | `0.0.0.0` / `8770` | Dashboard bind host/port (publish the port to reach it). |

### Activity / audit log

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_ACTIVITY_ENABLED` | `true` | Per-document [activity/audit log](dashboard.md#activity-log) — set `false` to turn off. |
| `PA_ACTIVITY_RETENTION_DAYS` | `90` | Auto-purge activity older than this after each sweep; `0` = keep forever. |

### Custom-field & stage names

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_FIELD_SCORE` / `PA_FIELD_STAGE` / `PA_FIELD_NOTES` | `ocr_quality` / `ai_stage` / `ai_notes` | Custom-field names (match your Paperless). |
| `PA_STAGE_TRIAGED` / `PA_STAGE_REOCR_DONE` / `PA_STAGE_METADATA_DONE` | `triaged` / `reocr_done` / `metadata_done` | `ai_stage` select-option labels. |

### HTTP timeouts / pagination

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_HTTP_REQUEST_TIMEOUT` | `90` | Per-request timeout (s) — raise for slow servers. |
| `PA_HTTP_DOWNLOAD_TIMEOUT` | `120` | Download original PDF (s). |
| `PA_HTTP_POST_TIMEOUT` | `180` | Upload corrected PDF / consume (s). |
| `PA_HTTP_TASK_POLL_TIMEOUT` / `PA_HTTP_TASK_POLL_INTERVAL` | `180` / `3` | Consume-task poll timeout / cadence (s). |
| `PA_HTTP_PAGE_SIZE` | `100` | Document-list pagination size. |

### Metadata content window

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_METADATA_CONTENT_HEAD` | `6000` | Leading chars of a doc sent to the model. |
| `PA_METADATA_CONTENT_TAIL` | `1500` | Trailing chars sent (long docs are head + … + tail). |
| `PA_METADATA_MAX_TOKENS` | `1024` | Metadata structured-output token cap. |

### Prompt customization (config, not secret)

| Variable | What it does |
|----------|--------------|
| `PA_METADATA_EXTRA_INSTRUCTIONS` / `PA_OCR_EXTRA_INSTRUCTIONS` | Text *appended* to the built-in instruction for that task. |
| `PA_METADATA_PROMPT_OVERRIDE` / `PA_OCR_PROMPT_OVERRIDE` | *Replaces* the built-in instruction (advanced; empty = default). |

The structured-output **JSON schema is fixed and never customizable** — every write is
revalidated against it, so a custom prompt can change *quality* but can never corrupt
Paperless. See [AI providers → schema revalidation](ai-providers.md#the-schema-revalidation-guarantee).

### Advanced (env)

| Variable | Default | What it does |
|----------|---------|--------------|
| `PA_HTTP_RETRIES` / `PA_HTTP_BACKOFF_INITIAL` / `PA_HTTP_BACKOFF_CAP` | `6` / `1.0` / `30.0` | Retry/backoff internals — too many retries can mask a real outage. |

---

## Matching your custom-field / stage names

If your Paperless already uses different names for the fields or the `ai_stage`
options, set `field_names` / `stage_names` (YAML) or the `PA_FIELD_*` / `PA_STAGE_*`
env vars. The assistant threads the configured names through the whole chain: **`pa
setup` provisions them, `pa doctor` checks them, and every sweep resolves + processes
with them.**

After changing a name, **re-run `pa setup` then `pa doctor`** so the new name exists
and is verified.

`metadata_eligible_roles` (default `["", "triaged"]`) controls which stages get
metadata — add `reocr_done` to also re-run metadata on re-OCR'd docs.

---

## The mounted YAML config file

Copy [`config.example.yml`](../config.example.yml), edit it, and mount it at
`/data/config.yml` (i.e. drop it in your `./pa-data/` volume as `config.yml`). It
documents the full tunable surface with inline comments. Remember: **secrets are
refused from YAML** — set those via the environment.

Multi-line values (like prompt customization) use a YAML block scalar:

```yaml
metadata:
  provider: anthropic
  model: claude-sonnet-4-6
  extra_instructions: |
    Always format dates as YYYY-MM-DD.
    Prefer my existing tags over inventing new ones.
```

---

## Advanced tuning (defaults reproduce stock behavior exactly)

Two knob groups are quarantined behind an **Advanced** section (collapsed in the
dashboard, with per-field *Reset to default* and a warning), because misconfiguring
them can degrade behavior:

- **`garbage_heuristic`** — the OCR-quality (garbage) score coefficients + gates that
  decide which scans are flagged for re-OCR (`min_length`, `word_ratio_weight`,
  `plausible_weight`, `fragment_weight`, `fragment_threshold`, `plausible_min_len`).
  The defaults reproduce today's scores **byte-for-byte**; changing them changes what
  gets flagged (spend + rewrites).
- **`http.retries` / `backoff_initial` / `backoff_cap`** — the Paperless-client
  retry/backoff. Too many retries can mask a real Paperless outage.

Leave them untouched for identical behavior; the dashboard's *Reset to default*
restores the exact stock values.

> **`delete_originals` is refused.** Deletion of originals is never automated (I4).
> Setting `delete_originals: true` in YAML — or via any override — makes `pa` error.
> Use the `superseded` review set and delete manually in the Paperless UI.

---

**Next step:** [AI providers](ai-providers.md) to wire in a model, or the
[dashboard](dashboard.md) to edit these tunables from a browser.
