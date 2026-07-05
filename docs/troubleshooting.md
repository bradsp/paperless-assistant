# Troubleshooting

**Run `pa doctor` first** — it names the problem and the fix. It checks connectivity,
token scope, the required fields/tags, your AI provider credentials, and the resolved
config, and prints each result as **OK / WARN / FAIL** with a suggested fix.

```bash
docker compose exec paperless-assistant pa doctor
docker compose exec paperless-assistant pa doctor --json   # machine-readable
```

---

## Symptom → fix

| Symptom | Likely cause / fix |
|---------|--------------------|
| `pa doctor` **connectivity FAIL** | `PAPERLESS_URL` wrong or Paperless not reachable on the compose network. In-stack use `http://webserver:8000` (match your service name). |
| Auth error / **token rejected** | `PAPERLESS_TOKEN` invalid. Recreate the API token; ensure it's in `.env` and referenced in the compose `environment:`. |
| **`token-scope` WARN** (admin token) | You're using an admin token. Create a scoped **service user** and use its token (see [Installation → Step 1](installation.md#step-1--create-a-scoped-paperless-api-token-recommended)). Not fatal. |
| A required field is **missing** / wrong type | Run `pa setup`. If it reports an **incompatible** existing field, rename/delete that field in the Paperless UI, then re-run `pa setup`. Nothing is ever clobbered. |
| **`provider:metadata` FAIL — key not set** | Set the selected provider's key in `.env` (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`), or switch `PA_METADATA_PROVIDER` to local `ollama`. See [AI providers](ai-providers.md). |
| Run **stopped** — "AI account out of credits or over its quota" | Your provider account is out of credits / over its billing quota. The run stops on the first such error (it does **not** burn through the batch). Add credits / raise your billing limit with the provider, then re-run. (Providers don't expose a queryable balance, so the dashboard surfaces the provider's own error text.) |
| **Rate-limit (429)** errors | Transient provider throttling. The engine rides out transient TPM 429s with retry/backoff; if it persists, lower `PA_WORKERS` or your request rate, or check your provider tier. |
| Run reports **"spend cap reached"** | Expected safety stop (I3). Raise `PA_SPEND_PER_RUN` / `PA_SPEND_PER_PERIOD` deliberately if you want more headroom. |
| **Nothing is being written** | The first run is a **dry-run** by design (I7). Run `pa run --write`, or let the next scheduled sweep write. |
| **`PermissionError: /data/...`** on startup | The current image fixes `/data` ownership automatically — `docker compose pull paperless-assistant` to get the latest. If you pin an old version, `chown -R 10001:10001 ./pa-data` on the host and restart. |
| **Webhook not firing** | Trigger must be *Consumption Finished* / *Updated* (not *Added*); URL must use the in-stack name `http://paperless-assistant:8765/hooks/paperless`; `PA_WEBHOOK_SECRET` must match the `?token=` in the Workflow URL; `PA_WEBHOOK_ENABLED=true`. See [Webhook](webhook.md). |
| **Dashboard won't start** | If `PA_UI_ENABLED=true` but `PA_UI_TOKEN` is unset, it refuses to start (fail closed). Set `PA_UI_TOKEN` in the env. Can't reach it? Make sure you published the port (`8770:8770`). See [Dashboard](dashboard.md). |
| **Re-OCR refused** — "not vision-capable" | You selected a text-only model for re-OCR. Pick a vision model (`claude-opus-4-8`, `gpt-4o`, a llava-class Ollama model). See [Re-OCR → choosing a model](re-ocr.md#choosing-a-vision-model). |
| A config change **didn't take effect** | An environment variable overrides YAML for that field (env > YAML). The dashboard shows such fields as **env-locked**. Change it via the env, or unset the env var to let YAML win. |

---

## Where to look

```bash
docker compose exec paperless-assistant pa status   # connectivity, queue, last run, spend-vs-cap
docker compose logs -f paperless-assistant          # structured JSON logs
cat ./pa-data/run-reports/*.json                    # per-run reports (counts, spend, new taxonomy)
cat ./pa-data/spend-ledger.json                     # cumulative spend this period
cat ./pa-data/cursor.json                           # first-run marker + last-run status
```

The `pa-data/` volume is the source of truth for state and rollback:

```
pa-data/
  config.yml            # optional mounted YAML config (no secrets)
  snapshots/<stage>/    # snapshot-before-write records (rollback)
  run-reports/          # per-run JSON reports
  logs/pa.jsonl         # structured JSON logs
  spend-ledger.json     # cumulative per-period spend
  activity.db           # per-document audit log (if enabled)
  cursor.json           # first-run marker + last-run status
```

---

## Uninstalling / rolling back

The assistant only **adds** metadata; it never deletes originals. To remove it:

1. Stop and remove the service:
   ```bash
   docker compose stop paperless-assistant
   docker compose rm -f paperless-assistant
   ```
   (Delete the `paperless-assistant:` block from your `docker-compose.yml`.)
2. Your documents are unchanged. The fields it wrote (`ai_stage`, `ocr_quality`,
   `ai_notes`) and tags remain filterable in Paperless for manual cleanup; the original
   state before each write is captured under `pa-data/snapshots/`.
3. If you enabled [re-OCR](re-ocr.md), review the `superseded` set before deleting
   anything — those are the old originals the assistant replaced.
4. To wipe assistant state entirely, remove the `./pa-data` directory (after you're
   sure you don't need the snapshots/reports).

---

**Next step:** back to the [documentation index](README.md), or
[Configuration](configuration.md) to adjust a tunable.
