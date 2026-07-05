# Paperless Assistant — Documentation

Welcome. This is the documentation index for **Paperless Assistant**, an AI companion
for [Paperless-NGX](https://docs.paperless-ngx.com/). New here? Start with
[Installation](installation.md), then [Quick start](quick-start.md).

## Getting started

| Page | What it covers |
|------|----------------|
| [Installation](installation.md) | The primary install guide (Docker companion, Mode A) + install-from-source for developers. |
| [Quick start](quick-start.md) | The shortest happy path — zero to a first safe dry-run. |
| [Usage](usage.md) | Day-to-day operation: the `pa` CLI, dry-run vs. writes, the scheduled sweep, reviewing results in Paperless. |

## Configuration

| Page | What it covers |
|------|----------------|
| [Configuration](configuration.md) | Full config reference: env vars, mounted YAML, precedence, field names, spend caps, advanced knobs. |
| [AI providers](ai-providers.md) | Where to get and how to configure Anthropic, OpenAI, and local Ollama access. |

## Optional features

| Page | What it covers |
|------|----------------|
| [Dashboard](dashboard.md) | The optional token-protected web dashboard. |
| [Webhook](webhook.md) | Near-real-time on-ingest processing. |
| [Re-OCR](re-ocr.md) | Enabling AI re-OCR of genuinely garbage scans. |

## Advanced

| Page | What it covers |
|------|----------------|
| [Advanced modes](advanced-modes.md) | Hosted (Mode B), inference proxy, direct (Mode C), and the control plane — all optional. |
| [Architecture](architecture.md) | Engine design, package layout, safety invariants (I1–I7). |

## Help

| Page | What it covers |
|------|----------------|
| [Troubleshooting](troubleshooting.md) | `pa doctor` first, then a symptom → fix table and rollback. |

---

The `phase{3..7}-acceptance.md` files in this directory are internal live-stack
acceptance runbooks kept for reference. The former single-page **integration guide** has
been split into the focused pages above; [`integration-guide.md`](integration-guide.md)
now redirects here.
