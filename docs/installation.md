# Installation

The primary way to run Paperless Assistant is as a **Docker companion** in your
existing Paperless-NGX compose stack (**Mode A** — local agent, bring-your-own-key).
It runs next to Paperless, uses **your own AI API key**, and needs **no inbound
ports**: it only talks to Paperless on your LAN and outbound to your chosen AI
provider.

> Just want the fast path? See the [quick start](quick-start.md). This page is the
> complete, annotated walkthrough.

## Prerequisites

- A running **Paperless-NGX** stack managed by **docker compose** — i.e. you have a
  `docker-compose.yml` with a `webserver` service.
- Shell access to the host running the stack.
- An **AI API key** for your chosen provider (Anthropic by default). Local **Ollama**
  needs no key and sends nothing to any cloud. See [AI providers](ai-providers.md) for
  where to get a key and how to wire it in.

You do **not** need to pre-create any custom fields or tags — `pa setup` does that.

---

## Step 1 — Create a scoped Paperless API token (recommended)

The assistant authenticates to Paperless with an API token. Best practice is a
**dedicated, non-admin service user**, so it has only the access it needs
(`pa doctor` warns if you use an admin token).

1. In the Paperless UI, go to **Settings → Users & Groups** and create a user, e.g.
   `assistant`, with permissions to view/edit **Documents, Custom Fields, Tags,
   Correspondents, Document Types, and Tasks**.
2. Log in as that user (or, as admin, edit the user) and open **Settings → (your
   profile) → API Token** to create a token. Copy it.

> In a hurry / testing? You can use your admin token — `pa doctor` will still pass
> but warn you to switch to a scoped service user. Prefer the service user for a
> real install.

---

## Step 2 — Add the service to your compose stack

Print the ready-made service block:

```bash
docker run --rm ghcr.io/bradsp/paperless-assistant:latest init
```

Paste the `paperless-assistant:` block it prints into your existing
`docker-compose.yml`, alongside the `webserver` service. It looks like this:

```yaml
  paperless-assistant:
    image: ghcr.io/bradsp/paperless-assistant:latest
    depends_on: [webserver]              # the paperless-ngx service
    environment:
      PAPERLESS_URL: http://webserver:8000       # in-stack service name, LAN-internal
      PAPERLESS_TOKEN: ${PAPERLESS_ASSISTANT_TOKEN}   # scoped service-user token (NOT admin)
      PA_MODE: byo-key
      # AI provider + model default to Anthropic. Pin them with
      # PA_METADATA_PROVIDER / PA_OCR_PROVIDER only if you want to.
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}     # byo-key only; NEVER put secrets in the YAML config
    volumes:
      - ./pa-data:/data                           # snapshots, config, run reports, cursor
    restart: unless-stopped
    command: ["serve"]                            # scheduled sweeps (first run = dry-run report)
    # NOTE: no `ports:` - the agent exposes nothing to the host by default.
```

Notes:

- **`PAPERLESS_URL`** uses the in-stack service name (`webserver`) — adjust if your
  Paperless service is named differently. No port needs to be published for the
  assistant to reach Paperless.
- The **`./pa-data`** volume holds snapshots, run reports, logs, and state. Keep it —
  it is how state survives restarts and how you roll back. You do **not** need to set
  its ownership: the container fixes `/data` permissions on start and then runs as a
  non-root user, so a fresh (root-owned) bind mount works out of the box.

Put your **secrets in a `.env` file** next to your `docker-compose.yml` (compose reads
it automatically). **Never put secrets in a YAML config file** — the assistant refuses
to load one that contains a secret-looking key.

```dotenv
# .env  (same directory as docker-compose.yml)
PAPERLESS_ASSISTANT_TOKEN=your-scoped-paperless-api-token
ANTHROPIC_API_KEY=sk-ant-...
```

A full annotated compose example lives in
[`docker-compose.example.yml`](../docker-compose.example.yml) at the repo root.

---

## Step 3 — Start it, provision, and verify

```bash
docker compose up -d paperless-assistant

# Create the required custom fields + review tags (idempotent — safe to re-run):
docker compose exec paperless-assistant pa setup

# Health check — expect all [OK] (a WARN about an admin token is fine to fix later):
docker compose exec paperless-assistant pa doctor
```

`pa setup` creates, if missing:

- **Custom fields:** `ocr_quality` (Number), `ai_stage` (Select: `triaged` /
  `reocr_done` / `metadata_done`), `ai_notes` (Text).
- **Review tags:** `superseded`, `ai-new-taxonomy`.

Re-running `pa setup` is a no-op; if a field already exists with an incompatible type
it is **reported, never overwritten**.

`pa doctor` checks connectivity, token scope, the fields/tags, your AI provider
credentials, and prints the resolved config (including spend caps). If anything is
red, it tells you exactly how to fix it. See [troubleshooting](troubleshooting.md) if a
check fails.

---

## Step 4 — The first run is a safe dry-run

Because `command: ["serve"]` is set, the container runs **scheduled sweeps**. The
**very first sweep is a bounded dry-run**: it scores documents and proposes metadata
but **writes nothing**, and saves a report you can inspect.

```bash
docker compose logs -f paperless-assistant          # structured JSON logs
ls ./pa-data/run-reports/                            # persisted per-run JSON reports
cat ./pa-data/run-reports/sweep-*.json               # what it WOULD change
```

You can also trigger a one-off dry-run immediately instead of waiting for the schedule:

```bash
docker compose exec paperless-assistant pa run          # first run = dry-run
```

Review the proposals, then turn on writes with `pa run --write`. The full day-to-day
workflow — applying writes, the scheduled sweep, and reviewing what the AI did inside
Paperless — is in [usage](usage.md).

---

## Upgrading

```bash
docker compose pull paperless-assistant
docker compose up -d paperless-assistant
docker compose exec paperless-assistant pa setup    # idempotent; applies any new prerequisites
docker compose exec paperless-assistant pa doctor
```

Your config and state live in `./pa-data`, so upgrades are just an image bump. Pin a
specific version (e.g. `ghcr.io/bradsp/paperless-assistant:0.1.0`) instead of `:latest`
if you prefer controlled upgrades.

To uninstall or roll back, see [troubleshooting → uninstalling](troubleshooting.md#uninstalling--rolling-back).

---

## Install from source (developers)

You don't need this to run the Docker companion — it's for contributing or running the
`pa` CLI directly. The project requires **Python 3.10+**.

```bash
git clone https://github.com/bradsp/paperless-assistant.git
cd paperless-assistant

python -m venv .venv
# Windows:      .\.venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -e ".[test]"          # editable install + the test extras
```

Optional provider extras (the Anthropic path needs none of these; the OpenAI and
Ollama adapters import-guard their clients, so the core runs without them):

```bash
pip install -e ".[openai]"        # the OpenAI adapter
pip install -e ".[ollama]"        # the Ollama adapter (httpx)
pip install -e ".[openai,ollama,test]"   # everything
```

Then set connection + provider secrets in your environment and run the CLI:

```bash
export PAPERLESS_URL=http://localhost:8000
export PAPERLESS_TOKEN=your-scoped-paperless-api-token
export ANTHROPIC_API_KEY=sk-ant-...

pa doctor
pa run              # first run = dry-run
```

The test suite runs **fully offline** — no live Paperless, no real API keys, no
Ollama:

```bash
python -m pytest -q
```

See [`CONTRIBUTING.md`](../CONTRIBUTING.md) for the dev workflow and the safety
invariants your change must preserve.

---

**Next step:** [Usage](usage.md) — running sweeps, the `pa` CLI, and reviewing what
the AI did inside Paperless. New to AI keys? Start with [AI providers](ai-providers.md).
