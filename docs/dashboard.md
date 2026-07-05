# Web dashboard

An optional, **token-protected** web dashboard to observe and operate the (Mode A /
BYO-key) assistant from a browser: view status, spend-vs-cap, run history, and your
library's review queues; start a manual sweep and watch it live; browse the
per-document activity log; and edit config tunables in a form.

It is served by the **same container** — either as its own `pa web` process, or (more
commonly) as a background thread inside `pa serve` when `PA_UI_ENABLED` is set (one
container = scheduler + dashboard). It's a single self-contained HTML page over a
Python-stdlib HTTP server: **no framework, no external assets**.

> Unlike the outbound-only agent and the in-network [webhook](webhook.md), the
> dashboard is the **one port you deliberately publish** — so it is protected by a
> **built-in token**.

---

## Enabling it

The token comes from the environment **only** (never YAML), and the dashboard
**fails closed**: enabled with no token, it refuses to start.

1. Add the token and enable flag to your `.env`:

   ```dotenv
   PA_UI_TOKEN=a-long-random-string
   PA_UI_ENABLED=true
   ```

2. **Publish the port** so your browser can reach it — add a `ports:` mapping to the
   `paperless-assistant` service (this is the only published port):

   ```yaml
   ports:
     - "8770:8770"     # host:container — the token-protected dashboard
   ```

3. Restart the service and open `http://your-docker-host:8770/`. Enter the token; it is
   stored in your browser and sent as an `Authorization: Bearer` header on every
   request — it is **never** embedded in the page.

Run it standalone (no scheduler) with `docker compose exec paperless-assistant pa web`.
Override the bind with `PA_UI_HOST` / `PA_UI_PORT` if needed.

---

## The tabbed layout

The dashboard is organized into tabs, deep-linkable via the URL hash (`#runs`,
`#activity`, `#setup`, …) and keyboard-navigable:

- **Overview** — health, spend-vs-cap, and the library / review-queue breakdown
  (`ai_stage` distribution, flagged count, `superseded` / `ai-new-taxonomy` counts).
  A prominent **PAUSED** banner appears here whenever automatic processing is paused,
  and a **Live run** panel shows an in-progress sweep.
- **Runs** — the **Run now** control (single-flight: dry-run / write / re-OCR, with a
  doc `limit` or a `max-spend`), run history, and recent errors, with live progress.
- **Activity** — the per-document [audit log](#activity-log): a filterable, searchable,
  server-side-paginated table of field-level before → after changes, plus the retention
  setting and a **Purge now** control.
- **Setup & Health** — do first-run and ops tasks **without** `docker compose exec`:
  - **Setup** runs the idempotent provisioner (same as `pa setup`).
  - **Doctor** runs the same `pa doctor` checks, each rendered OK / WARN / FAIL with its
    fix.
  - **Pause / Resume** is a **persisted** switch (a `/data/paused.json` flag) that halts
    scheduled sweeps **and** webhook nudges **without stopping the container**; it
    survives a restart. A manual **Run now** always works, even while paused.
  - **Onboarding helper** shows the compose service block plus a guided first-run
    checklist whose steps reflect **live** state.
- **Settings** — the config tunables, grouped into **General · Models · Prompts ·
  Advanced** (see [Configuration](configuration.md)).

---

## Activity log

The **Activity** tab is a per-document **audit log** — a searchable record of exactly
what the assistant changed on each document (`title`, `correspondent`, `document_type`,
`tags`, `ocr_quality`, `ai_stage`, `ai_notes`, and re-OCR supersessions) as
**field-level before → after** changes. In a **dry-run** it shows the *proposed* change
("would set title *X* → *Y*") before you enable writes, so you can preview exactly what
a run would do.

- **Records only real changes / proposals / errors** — an already-processed document
  produces **no** row, keeping the log meaningful and bounded.
- **Observational & non-intrusive** — recording never changes what is processed and
  **never fails a document or run** if the audit write itself fails (best-effort).
- **Filter, search, paginate** — by document id, date range, applied vs. dry-run, stage,
  and status; free-text search title / changed values; server-side pagination. Each row
  links to the document in Paperless (using `PAPERLESS_PUBLIC_URL` if set).
- **Storage** — a single SQLite database at `/data/activity.db` (stdlib, WAL mode,
  indexed on time + doc id). **No secret is ever stored in or returned from it** —
  document metadata and the non-secret Paperless URL only.

**Retention & purge.** Set `PA_ACTIVITY_RETENTION_DAYS` (default **90**; `0` = keep
forever). Rows older than the window are purged automatically after each sweep, and you
can purge immediately with **Purge now**. Turn the whole log off with
`PA_ACTIVITY_ENABLED=false`.

---

## Security notes

- **Every** endpoint (reads *and* the run/config writes) requires the token, compared
  in constant time. Enabled with **no** token, the dashboard **refuses to start** (fail
  closed).
- **No secret is ever exposed.** The config view shows secrets only as *set: yes/no*;
  saving config **refuses** any secret-looking key and `delete_originals`.
- **Env-locked fields.** Fields overridden by an environment variable are shown
  **locked** (env beats YAML), so a save can't silently no-op.
- **Reverse proxy (recommended for remote access).** Rather than exposing `8770`
  directly, front it with your existing reverse proxy (Caddy / Traefik / nginx) to add
  TLS — e.g. proxy `dashboard.example.com → paperless-assistant:8770`. The token still
  applies; the proxy just adds HTTPS. Prefer keeping it on your LAN.
- The container still runs **non-root**: the app self-drops to the `pa` user and the
  dashboard binds as that user inside the container; only the published host port maps
  in.

---

**Next step:** [Webhook](webhook.md) for near-real-time processing, or
[Configuration](configuration.md) for the tunables the Settings tab edits.
