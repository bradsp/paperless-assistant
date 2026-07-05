---
name: Bug report
about: Report a problem with Paperless Assistant
title: "[Bug] "
labels: bug
assignees: ''
---

<!--
Thanks for reporting! Please DO NOT paste any real secrets — redact your
Paperless token, AI API keys, agent credential, and any real hostnames/URLs.
For security vulnerabilities, use SECURITY.md instead of a public issue.
-->

## Description

A clear description of what went wrong and what you expected to happen.

## Steps to reproduce

1. …
2. …
3. …

## `pa doctor` output

<!-- Paste the output of `pa doctor` (redact secrets). This checks connectivity,
     token scope, fields/tags, provider credentials, and the resolved config. -->

```
paste `pa doctor` output here (secrets redacted)
```

## Environment

- **Deployment mode:** A (agent, BYO-key) / B (agent, hosted) / C (direct) —
- **AI provider(s):** anthropic / openai / ollama (which task — re-OCR? metadata?) —
- **Image tag / version:** e.g. `ghcr.io/bradsp/paperless-assistant:0.1.0` or from-source —
- **Paperless-NGX version:**
- **Host OS / Docker version:**

## Logs

<!-- Relevant lines from /data/logs/pa.jsonl or the container logs (redact secrets). -->

```
paste relevant logs here (secrets redacted)
```

## Additional context

Anything else that might help.
