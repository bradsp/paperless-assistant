# Advanced modes (optional — not needed for self-hosting)

Everything you need to self-host is the **local agent, bring-your-own-key** setup
(Mode A) covered in [Installation](installation.md). This page documents the optional
hosted and direct-connection modes honestly, so you can tell what they trade away.

> **The local agent (Mode A) is recommended everywhere.** BYO-key with local
> [Ollama](ai-providers.md#ollama-local--no-key-zero-cloud-egress) is the zero-egress
> floor: document data never leaves your machine. The modes below exist for building a
> hosted/subscription service and are **not required** to use the assistant with your
> own key.

---

## The three connectivity modes (trust model)

| Mode | Who connects | Vendor sees | Inbound exposure |
|------|--------------|-------------|------------------|
| **A — Agent, BYO-key** *(default)* | Agent → Paperless (LAN) + Agent → your AI provider | **Nothing** (no vendor in the path) | **None** |
| **B — Agent, hosted** | Agent → Paperless (LAN) + Agent → control plane (outbound-only) | Job metadata; contents **transit** the inference proxy for the AI call only (not persisted) | **None** |
| **C — Direct connection** *(advanced / opt-in)* | **Control plane → your published Paperless** | **Paperless URL + token + contents** | You already expose Paperless; no *new* exposure asked |

Mode C is genuinely weaker on privacy — it is the only mode where the vendor holds your
Paperless token and sees contents. Choose it only if you already publish Paperless and
accept that tradeoff.

---

## Mode B — hosted mode (outbound-only)

The **same** agent, run with `PA_MODE=hosted`, dials **outbound-only** to a vendor
**control plane**, pulls work over a long-poll, runs each job **locally** against your
LAN Paperless, and pushes results back. The control plane **never** connects into your
network — the agent binds no inbound listener and publishes no host port.

- On first start the agent exchanges a **one-time enrollment token**
  (`PA_ENROLLMENT_TOKEN`, env only) for a long-lived, rotatable agent credential
  persisted under `/data`.
- **Your Paperless token and AI keys never leave your network** — only the agent
  credential authenticates to the control plane.
- Inference stays **BYO** by default (you supply the AI key). The protocol is
  idempotent and restart-safe (a redelivered job never double-writes or double-spends).

### Hosted inference (subscriber, no local AI key)

An optional layer where the **vendor supplies inference**: a subscriber runs the same
agent in hosted mode with **no AI key of their own** (`PA_HOSTED_INFERENCE=true` and no
local key). AI tasks route through the vendor's inference proxy, using the vendor's
model key held **server-side**, metered per tenant and capped server-side.

The agent-side `HostedProvider` satisfies the same provider interface, so the
engine-side **JSON-schema validation still guards every write** — a malformed proxy
response is retry-then-error, never a bad write.

**Privacy (honest):** document contents **transit** the proxy for the model call
**only** and are **not persisted or logged** server-side. For zero egress to *any*
cloud, use BYO-key with local Ollama — which stays the default privacy floor. Hosted
inference is **off by default**.

---

## Mode C — direct connection (advanced, opt-in)

For users who *already* publish Paperless (reverse proxy / Cloudflare Tunnel /
Tailscale) and would rather the vendor reach it directly than run the companion agent.
The control plane runs the same engine against your published Paperless using a
**user-provided, service-user-scoped token stored server-side**, with the AI step
metered/capped through the hosted-inference path.

Mode C is the only mode where the vendor holds the Paperless token and sees contents —
**honestly weaker than Mode B**. It is hardened (egress allow-listing to the approved
host only, one-click revocation, the token is never logged, encryption-at-rest required
in production), but the local agent is still recommended everywhere. It runs only when
a target is explicitly registered (off by default).

---

## The control plane component

The vendor-side control plane lives in a **separate package** — `paperless_control_plane`,
with its own `pa-control-plane` console script — so the trust boundary is visible in the
repo. The agent (`pa`) never runs it. It handles enrollment, the work long-poll,
results, heartbeats, the Phase-6 inference proxy + billing seam, and the Mode-C direct
runner, plus read-only fleet/cost/review dashboards.

---

## Deeper references

- [Architecture](architecture.md) — the engine design, package layout, the invariants
  I1–I7, and the full three-mode trust table.
- The `docs/phase{5,6,7}-acceptance.md` runbooks — per-phase live-stack acceptance
  procedures (Mode B, hosted inference, and Mode C respectively), including the
  firewall-all-inbound acceptance and the three-mode trust comparison.

---

**Next step:** [Architecture](architecture.md) for the internals, or back to
[Installation](installation.md) for the recommended Mode A setup.
