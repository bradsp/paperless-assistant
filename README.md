# Paperless Assistant

**An AI companion for [Paperless-NGX](https://docs.paperless-ngx.com/).** It scores
the OCR quality of every document, optionally re-OCRs genuinely garbage scans with a
vision model, and proposes clean, structured metadata (title, correspondent,
document type, tags) — all through the Paperless REST API, and all **safe by
default**: the first run is a bounded dry-run that writes nothing.

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](LICENSE)
[![Container image](https://img.shields.io/badge/ghcr.io-bradsp%2Fpaperless--assistant-blue?logo=github)](https://github.com/bradsp/paperless-assistant/pkgs/container/paperless-assistant)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue?logo=python&logoColor=white)

It runs as a small container you drop into your existing Paperless-NGX compose
stack. It uses **your own AI key** (bring-your-own-key), needs **no inbound ports**,
and talks only to Paperless on your LAN and outbound to your chosen AI provider.

## Why it's safe

This tool holds a Paperless token and can write to your document library, so it is
built to be conservative:

- **Dry-run first.** The very first processing run only *reports* what it would do —
  it writes nothing until you opt in.
- **Auto re-OCR is off by default.** Re-OCR costs money and rewrites scans, so it
  never runs unless you explicitly enable it.
- **Spend caps.** Low, non-zero per-run and per-period USD caps hard-abort new work
  before you can run up a bill.
- **Snapshot before every write.** Original state is captured first, so any change
  can be rolled back.
- **Never deletes your originals.** Superseded documents are tagged for human review,
  never destroyed.
- **Zero cloud egress option.** BYO-key with a local **Ollama** model keeps document
  data entirely on your machine — nothing leaves your box.

These safety properties are enforced as engine invariants (I1–I7); see
[docs/architecture.md](docs/architecture.md).

## Features

- **OCR-quality triage** — a free, local heuristic scores every document's existing
  OCR and flags bad scans, written back to Paperless custom fields you can filter on.
- **AI metadata** — proposes a clean title, correspondent, document type, and tags,
  **reusing your existing taxonomy first** and flagging anything new for review.
- **AI re-OCR (optional, off by default)** — re-transcribes genuinely garbage scans
  with a vision model and re-files them, snapshotting the original first.
- **Schema-validated writes** — the engine owns the metadata JSON Schema and
  re-validates every model result, so no provider (however weak or local) can write
  invalid metadata to Paperless.
- **Choose your provider per task** — Anthropic, OpenAI, or local Ollama, selected
  independently for re-OCR vs. metadata.
- **Scheduled sweeps + optional on-ingest webhook** — keep the library tidy on an
  interval, or process freshly-consumed documents within seconds.
- **Optional token-protected web dashboard** — run status, spend-vs-cap, and review
  queues; start a manual sweep and edit config from the browser.

## Quick start

The fast path is the Docker companion. From a shell on the host running your
Paperless-NGX stack:

```bash
# 1. Print the compose service block for the assistant:
docker run --rm ghcr.io/bradsp/paperless-assistant:latest init

# 2. Paste the printed block into your Paperless-NGX docker-compose.yml
#    (alongside your `webserver` service), then bring it up:
docker compose up -d

# 3. First-run onboarding (secrets come from the environment only):
docker compose exec paperless-assistant pa setup    # create custom fields + review tags
docker compose exec paperless-assistant pa doctor    # verify connectivity, token, config

# 4. The first sweep is a SAFE DRY-RUN — it reports, writes nothing:
docker compose exec paperless-assistant pa run
```

That is the five-minute path. For the complete, annotated walkthrough (scoped
Paperless token, environment variables, applying writes, enabling the scheduler),
see **[docs/installation.md](docs/installation.md)** and the full
**[documentation set](docs/README.md)**.

## Choose your AI provider

Selection is per task (re-OCR vs. metadata) and resolves as
**CLI flag > environment variable > default (Anthropic)**. Keys and endpoints come
from the **environment only** — never from config files.

| Provider | Credentials / endpoint | Notes |
|----------|------------------------|-------|
| `anthropic` *(default)* | `ANTHROPIC_API_KEY` | Vision + forced tool use. |
| `openai` | `OPENAI_API_KEY` (opt. `OPENAI_BASE_URL`) | Strict JSON-schema structured output. |
| `ollama` | `PA_OLLAMA_ENDPOINT` (default `http://localhost:11434`) | Local, zero cloud egress, no key. |

See **[docs/ai-providers.md](docs/ai-providers.md)** for getting and configuring keys.

## Documentation

The full documentation set lives in [`docs/`](docs/README.md):

| Guide | What it covers |
|-------|----------------|
| [Installation](docs/installation.md) | Install the Docker companion (+ from-source for devs). |
| [Quick start](docs/quick-start.md) | The 5-minute path. |
| [Usage](docs/usage.md) | Running sweeps, the `pa` CLI, reviewing results in Paperless. |
| [Configuration](docs/configuration.md) | Full config reference (env vars, YAML, precedence). |
| [AI providers](docs/ai-providers.md) | Anthropic / OpenAI keys and local Ollama. |
| [Dashboard](docs/dashboard.md) | The optional token-protected web dashboard. |
| [Webhook](docs/webhook.md) | Near-real-time on-ingest processing. |
| [Re-OCR](docs/re-ocr.md) | Enabling AI re-OCR of garbage scans. |
| [Advanced modes](docs/advanced-modes.md) | Hosted (Mode B), inference proxy, direct (Mode C). |
| [Troubleshooting](docs/troubleshooting.md) | `pa doctor` + common problems. |
| [Architecture](docs/architecture.md) | Engine design, package layout, invariants (I1–I7). |

## Contributing

Contributions are welcome. The test suite runs **fully offline** — no live
Paperless, no real API keys, no Ollama. See **[CONTRIBUTING.md](CONTRIBUTING.md)**
for a dev environment, running the tests, and the safety invariants your change
must preserve. To report a vulnerability, see **[SECURITY.md](SECURITY.md)**.

## License

Paperless Assistant is free software licensed under the
**[GNU General Public License v3.0 or later](LICENSE)** (GPL-3.0-or-later). It comes
with no warranty; see the [LICENSE](LICENSE) file for the full text.

## Related

- [Paperless-NGX](https://docs.paperless-ngx.com/) — the upstream document
  management system this tool is a companion to.
