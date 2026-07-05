# Security Policy

Paperless Assistant holds a **Paperless API token** and one or more **AI provider API
keys**, and it can **write to your document library**. We take its security seriously
and appreciate responsible disclosure.

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues,
discussions, or pull requests.**

Instead, report privately through **GitHub's private vulnerability reporting**:

- Open <https://github.com/bradsp/paperless-assistant/security/advisories/new>
  ("Report a vulnerability").

<!-- MAINTAINER: confirm the security contact. If you prefer email-based reports,
     add a monitored address here and in the line below. -->
If you cannot use GitHub advisories, contact the maintainer privately at the email
listed on the repository owner's GitHub profile.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (a minimal proof-of-concept if possible).
- The version / image tag (e.g. `ghcr.io/bradsp/paperless-assistant:0.1.0`) and
  deployment mode (A / B / C).
- Any relevant configuration (with **all secrets redacted**).

We will acknowledge your report, work with you to understand and validate the issue,
and coordinate a fix and disclosure timeline with you.

## Scope

In scope:

- The `paperless_assistant` engine and `pa` CLI.
- The `paperless_control_plane` component and `pa-control-plane` CLI.
- The published Docker image `ghcr.io/bradsp/paperless-assistant`.

Examples of in-scope issues: leakage of a Paperless token, AI key, or agent
credential; a write path that bypasses the snapshot / spend-cap / review-gate
invariants; SSRF or egress-allow-list bypass in the Mode C direct runner;
authentication bypass on the webhook nudge or the control-plane protocol.

Out of scope: vulnerabilities in Paperless-NGX itself (report those upstream),
third-party AI provider services, and issues that require an already-compromised host.

## Supported versions

This project is pre-1.0. Security fixes are applied to the **latest released
version** (and the `latest` Docker tag). Please upgrade before reporting.

| Version | Supported |
|---------|-----------|
| `0.1.x` (latest) | ✅ |
| older | ❌ |

## Secrets are environment-only by design

By design, all secrets — the Paperless token, AI provider keys, and the hosted-mode
agent credential — are read **only from the environment** (or secret files), **never**
from YAML config, and are **never logged**. `pa` refuses to load a config file that
contains a secret-looking key. If you find a path that violates this, treat it as a
security issue and report it privately.
