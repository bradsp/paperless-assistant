# Webhook — near-real-time processing

By default the scheduled [sweep](usage.md#the-scheduled-sweep-pa-serve) processes new
documents on its interval (hourly). If you want newly-consumed documents processed
within **seconds**, enable the optional on-ingest **webhook nudge**. It stays **inside
your compose network** — no published host port.

The nudge only tells the assistant *"document N changed."* The assistant then **pulls**
the document over the Paperless REST API and runs it through the *same* idempotent
pipeline as the sweep. The **scheduled sweep stays authoritative** — correctness never
depends on a nudge firing.

---

## Enable the receiver

The shared secret comes from the environment **only** (never YAML), and the receiver
**fails closed**: enabled with no secret, `pa serve` refuses to start.

1. Add a secret to your `.env` and turn the receiver on:

   ```dotenv
   PA_WEBHOOK_SECRET=a-long-random-string
   PA_WEBHOOK_ENABLED=true
   ```

   (Reference these in the service `environment:` block, or set
   `PA_WEBHOOK_ENABLED: "true"` directly.)

The receiver binds inside the compose network on **port 8765**, path
`/hooks/paperless`, with **no `ports:` mapping** — Paperless reaches it by service name,
nothing outside your private network can.

---

## Add the Paperless workflow

In Paperless, create a **Workflow** (**Settings → Workflows**) with a **Webhook**
action:

- **Trigger:** *Document Consumption Finished* or *Document Updated* — **not**
  *Document Added*. Added fires before OCR runs, so the text isn't ready yet.
- **URL:** `http://paperless-assistant:8765/hooks/paperless?token=YOUR_PA_WEBHOOK_SECRET`
  (use your compose service name if it isn't `paperless-assistant`).
- **Body:** `{"doc_url": "{doc_url}"}`

The shared secret may be supplied as the `?token=` query parameter (shown above), an
`Authorization` bearer/token header, or an `X-PA-Webhook-Secret` header. It is compared
in constant time; a nudge without the correct secret is rejected.

---

## Guarantees

- **Id-only.** The nudge carries a document id (via the `{doc_url}` placeholder) —
  never document content.
- **Authenticated.** Every nudge must carry the shared secret (env only).
- **Debounced.** Rapid duplicate nudges for the same doc are collapsed within a window
  (`PA_WEBHOOK_DEBOUNCE`, default 30s). Duplicates are harmless anyway — the pipeline is
  idempotent.
- **Restart-safe.** The nudge queue is persisted under `/data`, so a restart resumes
  without losing or reprocessing work.
- **Sweep stays authoritative.** If a nudge is lost or the assistant was down, the next
  scheduled sweep still picks the document up. Nothing is missed.

You can also force the receiver on for a single foreground run with
`pa serve --webhook`.

---

**Next step:** [Dashboard](dashboard.md) to watch processing live, or
[Troubleshooting → webhook not firing](troubleshooting.md) if a nudge doesn't arrive.
