# Architecture & engine reference

> This page is the deep engineering reference relocated from the original README:
> the phase-by-phase narrative, the full package layout, the safety invariants
> (I1–I7), and the three-mode connectivity/trust model. It documents *how the engine
> is built*. For getting started, see the [documentation index](README.md) and the
> [installation guide](installation.md).

Paperless Assistant is an AI companion for [Paperless-NGX](https://docs.paperless-ngx.com/):
OCR-quality triage, vision re-OCR of garbage scans, and structured metadata
extraction — all through the Paperless REST API, with idempotency,
snapshot-before-write, spend caps, and human-review gates preserved as
non-negotiable invariants.

## Build history (phase by phase)

The three original proof-of-concept scripts (`stage0_triage.py`,
`stage1_reocr.py`, `stage2_metadata.py`) were lifted into an installable
`paperless_assistant` package exposing a single `pa` CLI (Phase 1), the two AI
seams were placed on a clean **provider abstraction** (Phase 2), and Phase 3
shipped the **Docker companion**: a container you drop into your existing
Paperless-NGX compose stack, first-run onboarding (`pa init` / `pa setup` /
`pa doctor`), a **layered configuration system** with safe defaults, a
**scheduled sweep** (`pa run` / `pa serve`), and structured JSON logs + persisted
run reports on a `/data` volume. The original scripts remain in place, untouched
(they are the characterization baseline the test suite pins behavior against).

You can choose **Anthropic**, **OpenAI**, or a local **Ollama** model per task
(re-OCR vs. metadata) via config, without weakening correctness: the engine owns
the `document_metadata` JSON Schema and **re-validates every structured result
against it after every provider call**, so no model — however weak or local — can
ever write invalid metadata to Paperless.

**Phase 4** adds an opt-in **on-ingest webhook nudge**: a Paperless *Workflow →
Webhook* action tells the agent "document N changed," and the agent **pulls** that
doc via REST and runs it through the *same* idempotent pipeline as the sweep — so a
freshly-consumed document is triaged and metadata-proposed within **seconds**. The
nudge carries an **id only** (never content), is **authenticated** with a shared
secret, is **debounced** against duplicates, and its work is **persisted under
`/data`** so a restart resumes without losing or reprocessing anything. The receiver
binds **inside the compose network with no published host port** (Paperless reaches
it by service name), and the **scheduled sweep stays authoritative** — correctness
never depends on the webhook firing.

**Phase 5** adds an opt-in **hosted mode (Mode B)**: the *same* agent, run with
`PA_MODE=hosted`, **dials outbound-only** to a vendor **control plane**, **pulls**
work over a long-poll, runs each job **locally** against your LAN Paperless with
**your own** AI provider (inference stays **BYO** — the control plane does *not* run
or bill inference yet), and **pushes** results back. The control plane **never**
connects into your network: the agent binds **no inbound listener** and publishes
**no host port** — work is pulled, never pushed. On first start the agent exchanges
a **one-time enrollment token** (`PA_ENROLLMENT_TOKEN`, env only) for a long-lived,
**rotatable** agent credential persisted under `/data` (never logged, never in
YAML). The **Paperless token and AI keys never leave your network** — only the
agent credential authenticates to the control plane. The protocol is **resilient**:
jobs are idempotent with a `/data` cursor + stage machine (an at-least-once
redelivered job never double-writes or double-spends), reconnect uses **bounded
backoff + jitter**, in-flight local work continues while the control plane is
unreachable (results queue in `/data` and flush on reconnect), and a **restart
resumes from `/data`** without reprocessing. A **minimal control plane**
(`paperless_control_plane/`, its own `pa-control-plane` console script) exercises
the protocol end-to-end. See [`phase5-acceptance.md`](phase5-acceptance.md)
for the **firewall-all-inbound** live acceptance.

**Phase 6** adds the **"vendor supplies inference"** commercial mode. A subscriber
runs the *same* agent in hosted mode with **NO AI key of their own**
(`PA_HOSTED_INFERENCE=true` and no local key); AI tasks are performed by the vendor's
**inference proxy** (`POST /agent/inference`) using the vendor's model key held
**server-side**, **metered per tenant**, gated by an **active subscription** and a
**server-side per-tenant spend cap** that halts inference when hit. The agent-side
`HostedProvider` satisfies the *same* `AIProvider` interface, so **the engine-side
JSON-schema validation still guards every write** — a malformed proxy response is
retry-then-error, never a bad write. **Privacy (honest):** document contents
**transit** the proxy for the model call **only** and are **not persisted or
logged** server-side; if you want zero egress to *any* cloud, use **BYO-key with
local Ollama**, which stays the default privacy floor. Billing is a **seam with a
stub** (no real payment processor); usage is visible via
`pa-control-plane usage --tenant …`. This phase also **hardens** the Phase-5 control
plane: agent credentials are **hashed at rest** (scrypt + salt, constant-time
compare) and the results map is **bounded/pruned**. See
[`phase6-acceptance.md`](phase6-acceptance.md) for the live acceptance and
the full trust model.

**Phase 7** (the final phase) adds the **optional direct-connection escape hatch
(Mode C)** and read-only **operational dashboards**, plus a 1.0-hardening pass — all
without weakening the agent. **Mode C is advanced and opt-in, never the default.** It
is for users who *already* publish Paperless (reverse proxy / Cloudflare Tunnel /
Tailscale) and would rather the vendor reach it directly than run the companion
agent: the control plane runs the *same* engine against the user's published
Paperless using a **user-provided, service-user-scoped token stored server-side**,
with the AI step **metered/capped through the Phase-6 inference path** (the engine and
billing are **reused, not forked**). Mode C is the **only** mode where the vendor
holds the Paperless token and sees contents — **honestly weaker than Mode B**, and the
**local agent is recommended everywhere**. It is **hardened**: **egress
allow-listing** (the runner only connects to the tenant's approved host; any other host
is refused — an SSRF/misconfig guard), **one-click revocation** (removes the stored URL
+ token immediately), the token is **never logged**, and **encryption-at-rest is
required in production** (flagged in the 1.0-readiness note). The **read-only
dashboards** surface **fleet status** (Phase-5 heartbeats + registered direct targets),
**cost/usage** with spend-vs-cap (the Phase-6 ledger), and **review-queue counts**
(`superseded` / `ai-new-taxonomy`) as **JSON endpoints plus a single self-contained
HTML page** (inline CSS/JS, no framework, no external assets) — **read-only, never
mutating a document**. See [`phase7-acceptance.md`](phase7-acceptance.md)
for the three-mode trust comparison, the live acceptance, and the 1.0-readiness list.

## The three connectivity modes (agent-first)

The local **agent is the default and recommended everywhere**. Mode C is the labelled
exception. For an operator-facing walkthrough of the hosted and direct modes, see
[Advanced modes](advanced-modes.md); this section is the design-level trust summary.

| Mode | Who connects | Vendor sees | Inbound exposure |
|------|--------------|-------------|------------------|
| **A — Agent, BYO-key** *(default)* | Agent → Paperless (LAN) + Agent → your AI provider | **Nothing** (no vendor in the path) | **None** |
| **B — Agent, hosted** | Agent → Paperless (LAN) + Agent → control plane (outbound-only) | Job metadata + contents **transit** the inference proxy for the AI call (not persisted) | **None** |
| **C — Direct connection** *(advanced / opt-in)* | **Control plane → your published Paperless** | **Paperless URL + token + contents** | You already expose Paperless; no *new* exposure asked |

**BYO-key with local Ollama (Mode A) is the zero-egress floor** — document data never
leaves your box. Mode C is genuinely weaker; choose it only if you already publish
Paperless and accept the vendor holding your token. The agent's **six §7 no-inbound
guarantees are unchanged** for Modes A/B (see the acceptance docs).

**Safe by default (I7):** out of the box the first processing run is a bounded
**dry-run** with a report, auto re-OCR is **OFF**, the webhook is **OFF**, hosted
mode is **OFF** (BYO-key local is the default), hosted **inference** is **OFF**
(BYO/local zero-egress is the floor), **Mode C direct-connection is OFF** (opt-in;
runs only when a target is explicitly registered), taxonomy is **reuse-first**, and
per-run **and** per-period spend caps are low and non-zero — a fresh install cannot
write to your documents or run up a bill without you opting in.

## Package layout (`plan/product-architecture.md` §4.1)

```
src/paperless_assistant/
  client.py     PaperlessClient   - the single Paperless REST surface (_request retry/backoff, pagination, download, post, task poll)
  fields.py     CustomFieldResolver - field name->id, select option label<->id, value coercion
  taxonomy.py   TaxonomyResolver  - case-insensitive tag/correspondent/type reuse-first + lazy create (I5)
  safety.py     SafetyLayer       - snapshot-before-write (I2), merge-not-clobber, supersede, restore (I4)
  spend.py      SpendGovernor     - thread-safe USD accumulator + hard-abort cap (I3)
  stages.py     StageOrchestrator - ai_stage state machine, eligibility, queue construction (I1)
  ocr.py        OcrPipeline       - garbage_score, vision re-OCR, invisible-text overlay PDF
  metadata.py   MetadataExtractor - strict structured-output metadata + reuse-first apply
  config.py     Config + Settings  - env/constants (Phase 1) + the layered config resolver (Phase 3, §7)
  report.py     RunReport          - structured run summaries + persisted JSON run reports (Phase 3)
  provision.py  Provisioner        - `pa setup`: idempotent field/tag provisioning (Phase 3, §8.2)
  doctor.py     run_doctor         - `pa doctor`: connectivity/token/field/tag/provider/config checks (Phase 3)
  sweep.py      Sweep / serve      - `pa run` / `pa serve`: the scheduled sweep + single-doc nudge path (Phase 3/4, §6.2)
  webhook.py    WebhookServer      - `pa serve --webhook`: stdlib on-ingest NUDGE receiver + persisted queue (Phase 4, §6.2)
  hosted.py     HostedAgent        - `pa serve` (PA_MODE=hosted): OUTBOUND-ONLY pull-loop trigger (Phase 5)
  transport.py  Http/InProcess     - agent's outbound transport to the control plane (no inbound listener) (Phase 5)
  hosted_state.py credential/cursor/result-queue - durable /data state + idempotency stage machine (Phase 5)
  obs.py        JsonLogger/Ledger  - structured JSON logs, per-period spend ledger, cursor, status (Phase 3, §8.4)
  initcmd.py    `pa init`          - emit the docker-compose service block (Phase 3, §8.1)
  cli.py        the `pa` entry point

  hosted_provider.py HostedProvider - agent-side adapter that calls the control-plane
                               #    inference proxy (Phase 6); same AIProvider interface

src/paperless_control_plane/   # VENDOR-SIDE control plane (Phase 5/6) — a SEPARATE
                               # package so the trust boundary is visible in the repo.
  store.py      ControlPlaneStore - agent registry + per-agent job queue + at-least-once dispatch;
                               #    Phase 6 hardening: credentials HASHED at rest, results map bounded
  app.py        ControlPlane      - transport-agnostic protocol: enroll / work long-poll / results /
                               #    heartbeat / inference (Phase 6 proxy endpoint, strict check order)
  billing.py    BillingStore      - Phase 6 seam: per-tenant usage ledger + stub subscription/entitlement
                               #    store + server-side spend cap (NO real payment processor)
  inference.py  InferenceProxy    - Phase 6: vendor model call (stubbable ModelBackend) + metering
  direct.py     DirectTargetStore/DirectRunner - Phase 7 Mode C: direct-target registry (token server-side,
                               #    egress allow-listed, one-click revoke) + engine run vs. remote Paperless (opt-in)
  dashboard.py  DashboardData     - Phase 7: read-only fleet/cost/review JSON + a single self-contained HTML page
  server.py     ControlPlaneServer- stdlib HTTP adapter (agents dial OUT to it; it never dials in)
  cli.py        `pa-control-plane` - serve / mint-token / enqueue / revoke / subscribe / usage /
                               #    direct-add / direct-list / direct-revoke / direct-run / dashboard (Phase 7)
  providers/    AIProvider abstraction (Phase 2):
    base.py       AIProvider protocol, Transcription/StructuredResult, engine-side validation
    anthropic.py  forced tool-use + vision (reproduces the Phase 1 Anthropic calls)
    openai.py     response_format strict JSON schema + vision (import-guarded SDK)
    ollama.py     JSON Schema via `format`, local/zero-cost (import-guarded httpx)
    pricing.py    per-provider/per-model price tables (moved out of spend.py)
    registry.py   build_provider(task, cfg) -> the configured adapter (incl. "hosted", Phase 6)
```

The four helpers every original script re-implemented (`_request` with backoff,
custom-field-map resolution, `stage_option_id`, `snapshot`) now each have a
single home (`client`, `fields`, `fields`, `safety` respectively).

## Safety invariants (`plan/product-architecture.md` §3)

These are the non-negotiable contracts the engine preserves across every trigger
(sweep, webhook nudge, hosted job, direct run):

- **I1** Idempotency / resumability — re-runs skip done docs unless `--force`.
- **I2** Snapshot-before-write — original state captured once before any mutation.
- **I3** Spend ceiling as a hard abort — thread-safe USD cap gates new work.
- **I4** Nothing destructive without a human gate — `superseded` / `ai-new-taxonomy`.
- **I5** Taxonomy-reuse-first — prefer existing entries; flag anything new.
- **I6** Surface the server's real error — validation failures show what Paperless rejected.
- **I7** Safe by default — dry-run first-class; conservative concurrency; retry/backoff.

## Testing model

The suite runs fully offline — no live Paperless, no real API keys, and no Ollama
server. The Paperless HTTP surface is mocked (`responses` + a hand-rolled fake
session) and every provider client/HTTP is stubbed, so tests are deterministic
and free. `tests/test_providers.py` proves the metadata task yields schema-valid
output through Anthropic, OpenAI, and Ollama on the same input; that a malformed
provider response is caught, retried, and never PATCHed; and that a vision-less
provider selected for re-OCR refuses before any download/consume.

Characterization tests pin the invariant behaviors (I1–I7), `garbage_score`, and
`build_overlay_pdf` against the **original** scripts first
(`tests/test_characterization_originals.py`), then re-assert the identical
contract against the refactored package
(`tests/test_characterization_package.py`) to prove the extraction changed no
observable behavior.

## Deep-dive references

- [Advanced modes](advanced-modes.md) — the operator-facing guide to hosted (Mode B),
  hosted inference, direct connection (Mode C), and the control-plane component.
- `docs/phase3-acceptance.md` … `docs/phase7-acceptance.md` — the per-phase
  live-stack acceptance runbooks, in the repository.
