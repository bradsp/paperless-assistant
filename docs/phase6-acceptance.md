# Phase 6 — Hosted inference + subscription/billing acceptance runbook

Phase 6 adds the **"vendor supplies inference"** commercial mode. A subscriber runs
the *same* agent in **hosted mode with NO AI key of their own**; AI tasks are
performed by the vendor's **inference proxy** in the control plane, **metered** to
the tenant, gated by **entitlement** (an active subscription) and a **server-side
per-tenant spend cap** — while document **contents transit the proxy only for the
model call and are never persisted or logged**, the **engine-side schema guarantee**
still protects every write, and the **vendor's model key never reaches the agent**.
BYO-key/local remains the default **zero-egress** option.

This runbook separates the **automated offline proofs** from the **manual live
acceptance** (product-architecture §9 Phase 6, connectivity §5).

---

## What is and isn't built in Phase 6

**Built:**
- `HostedProvider` (agent-side, `paperless_assistant/providers/hosted_provider.py`)
  — an `AIProvider` whose "endpoint" is the control-plane inference proxy, wired
  through the existing registry so hosted mode + inference toggle + **no local key**
  resolves to it. **Engine-side JSON-schema validation still runs on its output.**
- Inference proxy (`paperless_control_plane/inference.py`) + the `POST
  /agent/inference` endpoint (`paperless_control_plane/app.py`) with the strict
  check order below. The vendor model call is behind a `ModelBackend` seam
  (stubbed in tests; `AnthropicModelBackend` keyed server-side in production).
- Billing seam (`paperless_control_plane/billing.py`): a per-tenant **usage
  ledger**, a **stub subscription/entitlement store** (active/suspended/canceled),
  and a **server-side per-tenant spend cap**. Usage is queryable via the CLI
  (`pa-control-plane usage --tenant …`).
- The two Phase-5 control-plane **hardening fixes** in `store.py`: the agent
  credential is **hashed at rest** (scrypt + per-credential salt; `authenticate`
  compares constant-time), and the `_results` map is **bounded/pruned** (size + TTL).

**NOT built (deliberately, per scope fences):**
- **No real payment-processor integration** (Stripe et al.) and no external billing
  calls — billing is modeled as a **seam with an in-memory/JSON stub** a real
  provider could replace later.
- **No Mode C** direct-connection escape hatch and **no dashboards/UI** (Phase 7).
  Usage visibility is **CLI/JSON only**.
- No change to the `AIProvider` interface, the engine, the Paperless client, or the
  Phase-5 protocol beyond adding the proxy path and the two hardening fixes.

---

## The strict check order (r2) — enforced in `ControlPlane._inference`

Before **any** model work, in this exact order:

1. **Authenticate the agent** (agent credential; 401 if bad).
2. **Resolve the tenant** from the authenticated agent record (never from the body).
3. **Check entitlement** — active subscription (402 `unentitled` if not).
4. **Check the server-side spend cap** (429 `spend_cap` if reached).
5. **Only then** forward to the vendor `ModelBackend` (vendor key server-side),
   **meter** the usage against the tenant, and return the result.

Entitlement is checked **before** the cap (`test_check_order_entitlement_before_cap`).
A refusal returns a clear structured error the agent surfaces as
`HostedInferenceRefused`, so **work halts** rather than silently failing.

---

## The privacy commitment (connectivity §5) — how it's enforced

- **Contents transit, not store.** The proxy uses the request's transient content
  (prompt/schema or doc bytes) for the model call and then it goes out of scope.
  Nothing writes prompt/doc/schema to the billing store, the queue store, or disk.
  Only a **content-free usage record** (tenant, task, model, tokens, cost, ts) is
  persisted. Proven by `test_no_document_content_persisted_server_side`.
- **Logs are content-free.** The proxy logs `inference_metered` / `inference_refused`
  with routing/usage metadata only — never contents or prompts. Proven by
  `test_control_plane_logs_are_content_free`, `test_refusal_logs_are_content_free`.
- **Minimize payloads.** The Phase-5 job dispatch stays content-free (opaque ids);
  contents cross the boundary **only** in the transient `/agent/inference` call.
- **Zero-egress floor preserved.** BYO-key with local Ollama sends nothing to the
  vendor and stays the default. Proven by `test_byo_local_still_zero_egress`.

---

## The structured-output guarantee survives the proxy

`HostedProvider` does **not** validate. It returns the model's raw dict; the
**engine** (`extract_structured_validated`) re-validates it against the
engine-owned JSON Schema and retries-then-errors on a malformed response — so a
malformed proxy response is **caught and never written**. Proven by
`test_malformed_proxy_response_caught_by_engine_never_written` (asserts **zero**
PATCH to Paperless).

---

## Automated (offline) vs. manual (live) acceptance

Every functional guarantee below is proven by an **offline** test driven against an
**in-process** control plane with a **stubbed vendor model backend** — no real
cloud, no real Paperless, no real model API, **no spend**. Only the end-to-end
**live stack** proof is manual.

| Check | How it's verified | Where |
|-------|-------------------|-------|
| A no-local-key hosted agent completes a metadata run end-to-end via the proxy → schema-valid write to fake Paperless | automated, offline | `tests/test_phase6_hosted_inference.py::test_end_to_end_metadata_run_no_local_key`, `::test_hosted_agent_wires_inference_context_and_sweep_runs_end_to_end` |
| `HostedProvider` satisfies `AIProvider` (same shapes, usage + cost) and meters | automated, offline | `::test_hosted_provider_returns_same_shape_and_meters`, `::test_hosted_transcribe_meters_and_returns_text` |
| **Engine-side schema validation still runs** on HostedProvider output | automated, offline | `::test_engine_validation_still_runs_on_hosted_output` |
| Registry resolves hosted mode + no local key → `HostedProvider`; a local key keeps BYO | automated, offline | `::test_registry_resolves_hosted_provider_when_no_local_key`, `::test_settings_hosted_inference_active_predicate`, `::test_byo_key_agent_does_not_route_to_proxy` |
| **Usage metered to the tenant** (ledger shows it; tagged by tenant) | automated, offline | `::test_usage_metered_to_tenant` |
| **Server-side per-tenant spend cap HALTS** inference (model not called after cap) | automated, offline | `::test_server_side_spend_cap_halts_inference` |
| **Unentitled / suspended tenant refused** with a clear error; model never reached | automated, offline | `::test_unentitled_tenant_refused`, `::test_suspended_tenant_refused` |
| Check ORDER: entitlement before cap | automated, offline | `::test_check_order_entitlement_before_cap` |
| **No document content persisted server-side** after an inference call | automated, offline | `::test_no_document_content_persisted_server_side` |
| **Control-plane logs are content-free** (metered + refusal) | automated, offline | `::test_control_plane_logs_are_content_free`, `::test_refusal_logs_are_content_free` |
| **Malformed proxy response caught by engine-side validation, never written** | automated, offline | `::test_malformed_proxy_response_caught_by_engine_never_written` |
| **Credential stored HASHED at rest** (persisted ≠ plaintext); `authenticate` still works across restart | automated, offline | `::test_credential_stored_hashed_not_plaintext` |
| **Results map bounded** (size cap + TTL eviction) | automated, offline | `::test_results_map_is_bounded`, `::test_results_map_ttl_eviction` |
| **BYO/local zero-egress** preserved (nothing to the vendor) | automated, offline | `::test_byo_local_still_zero_egress` |
| `/agent/inference` 501 when hosted inference not configured; 401 unauthenticated | automated, offline | `::test_inference_endpoint_501_when_not_configured`, `::test_inference_endpoint_requires_auth` |
| Usage **visible via CLI/JSON**; subscription admin (subscribe/suspend) | automated, offline | `::test_cli_subscribe_and_usage`, `::test_cli_subscribe_suspend_then_usage_shows_status` |
| Existing Phase-5 hosted/control-plane behavior unchanged by the hardening | automated, offline | `tests/test_hosted.py::*`, `tests/test_control_plane.py::*` |

Run the automated layer:

```bash
pytest -q                                       # full suite (Phases 1–6), offline
pytest -q tests/test_phase6_hosted_inference.py # Phase 6 only
```

---

## The honest hosted trust model (say it plainly)

| Data | BYO-key / local (Mode A) | Hosted inference (Mode B, this phase) |
|------|--------------------------|----------------------------------------|
| Paperless URL / token | Never leaves your LAN | Never leaves your LAN |
| Document **contents** (OCR text, page images) | Only your chosen provider sees them; **local Ollama = zero egress** | **Transit** the vendor inference proxy for the model call; **NOT persisted** server-side |
| Document **metadata** | Vendor never sees | Only what a job/result needs (opaque ids); contents only transit at inference |
| Usage / cost | Local only | **Metered per tenant** (for billing) |
| Vendor model key | n/a (you hold your key) | **Server-side only** — never sent to the agent |

**Bottom line:** hosted inference means your document contents **pass through** the
vendor's proxy for the AI call. They are **not stored** and **not logged** — but if
your threat model forbids contents reaching *any* cloud, run **BYO-key with local
Ollama**, which is the **zero-egress** floor and remains the default. This is a
deliberate, documented trade-off, not a hidden one.

---

## Manual live acceptance (real hosted stack)

> The end-to-end proof that a real subscriber with **no AI key** processes
> documents through the vendor proxy, metered and capped, with logs showing no
> content persisted. This cannot be fully simulated offline (it needs a real model
> account server-side).

### 0. Prerequisites
- A test Paperless-NGX stack on the LAN with a scoped service-user token
  (`pa setup` + `pa doctor` first — see `phase3-acceptance.md`).
- A control-plane host the agent can reach outbound, with the **vendor's** model key
  set **server-side only**:
  ```bash
  export PA_VENDOR_ANTHROPIC_KEY=sk-ant-…      # SERVER-SIDE ONLY; never on the agent
  pa-control-plane --state ./cp-state.json --billing ./cp-billing.json \
      serve --host 0.0.0.0 --port 8080
  # startup prints: "hosted inference: ENABLED (vendor key set)"
  ```

### 1. Provision the tenant's subscription + spend cap (billing stub)
On the control-plane host:
```bash
pa-control-plane --billing ./cp-billing.json subscribe --tenant demo --spend-cap 2.00
pa-control-plane --state ./cp-state.json mint-token --tenant demo   # -> enr_… token
```

### 2. Start the agent in HOSTED mode with NO AI key
On the agent host — **do not set `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`**:
```bash
export PA_MODE=hosted
export PA_CONTROL_PLANE_URL=http://<control-plane-host>:8080
export PA_ENROLLMENT_TOKEN=enr_…              # env only, one-time
export PA_HOSTED_INFERENCE=true               # route AI via the proxy
export PAPERLESS_URL=http://webserver:8000
export PAPERLESS_TOKEN=…                       # scoped service-user token
pa serve
```
The agent enrolls once (credential persisted under `/data`), then long-polls. It
holds **no** AI key.

### 3. Dispatch a metadata job — it completes via the proxy, metered
```bash
pa-control-plane --state ./cp-state.json enqueue \
    --tenant demo --agent-id agt_… --type run_sweep
```
**Expect:** the agent pulls the job, runs metadata locally, and each AI call goes
**outbound to `POST /agent/inference`**. Paperless shows schema-valid metadata
proposals. Check the metered usage:
```bash
pa-control-plane --billing ./cp-billing.json usage --tenant demo
# -> calls > 0, spend_usd > 0, cap_remaining decreasing
```

### 4. Hit the server-side spend cap — inference halts
Keep dispatching (or lower the cap: `subscribe --tenant demo --spend-cap 0.001`).
Once cumulative spend reaches the cap, the agent's next AI call is **refused**
(HTTP 429 `spend_cap`); the run halts with a clear message and **no further model
calls occur**. Raise the cap to resume.

### 5. Suspend the subscription — inference is refused
```bash
pa-control-plane --billing ./cp-billing.json subscribe --tenant demo --status suspended
```
The agent's next AI call is refused (HTTP 402 `unentitled`); work halts clearly.
Reactivate with `--status active`.

### 6. Confirm no content persisted / logged server-side
- Inspect `./cp-state.json` and `./cp-billing.json`: they contain agents, queue
  metadata, subscriptions, and **usage records only** — grep for any snippet of a
  document's text; it must not appear.
- Inspect the control-plane logs: `inference_metered` / `inference_refused` lines
  carry tenant/task/model/tokens/cost only — **no document text, no prompt**.

### 7. Verify BYO/local is still zero-egress
On a second agent, set a local key (or `PA_OLLAMA_ENDPOINT`) and **do not** set
`PA_HOSTED_INFERENCE`. Run a sweep and confirm the control plane's usage ledger for
that tenant stays empty — nothing reached the vendor.

---

## Pass criteria
- [ ] A hosted agent with **NO local AI key** completes a metadata run via the proxy;
      schema-valid metadata is written to Paperless.
- [ ] Usage is **metered to the tenant** (visible via `pa-control-plane usage`).
- [ ] The **server-side spend cap halts** inference when reached.
- [ ] A **suspended/unentitled** tenant is **refused** with a clear error; work halts.
- [ ] Control-plane state files and logs contain **no document content**.
- [ ] The **vendor model key** is set only server-side and never appears agent-side.
- [ ] **BYO/local** remains zero-egress (no vendor usage recorded).
- [ ] (automated) The full offline suite is green; Phases 1–5 unchanged.
