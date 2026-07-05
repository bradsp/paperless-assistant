# Phase 7 — Direct-connection (Mode C) + dashboards + 1.0-hardening acceptance

Phase 7 is the **final** phase. It ships three things without weakening anything
that came before:

1. **Mode C direct-connection** — an **opt-in, clearly-labelled, never-default**
   escape hatch for users who *already* publish Paperless. The vendor runs the SAME
   engine directly against the user's published Paperless with a **user-provided,
   service-user-scoped token stored server-side**, with the AI step **metered/capped
   through the Phase-6 inference path**. This is the ONE mode where the vendor holds
   the Paperless token and sees contents (connectivity §1).
2. **Read-only dashboards** — fleet status, per-tenant cost/usage (spend-vs-cap), and
   review-queue counts, as **JSON endpoints** plus a **single self-contained HTML
   page** (inline CSS/JS, no framework, no external assets). Dashboards **never
   mutate documents**.
3. **1.0-hardening + honest trust-model docs** — the three-mode comparison, a
   confirmation that the six §7 no-inbound points still hold for Modes A/B, and a
   1.0-readiness list (including **encryption-at-rest for the Mode C token**).

This runbook separates the **automated offline proofs** from the **manual live
acceptance** (product-architecture §9 Phase 7, connectivity §6/§1/§7).

---

## What is and isn't built in Phase 7

**Built:**
- `paperless_control_plane/direct.py`:
  - `DirectTargetStore` — per-tenant registry of direct targets
    `{paperless_url, token (server-side), allowed_hosts, enabled}` with
    **add / list / one-click revoke**. The token is a **reversible secret** (the
    vendor must present it to Paperless) so it is **NOT hashed**; it is stored
    access-controlled (owner-only file), **never logged**, and stripped from every
    public view. **Encryption-at-rest is required in production** — flagged below.
  - `AllowListSession` — an **egress allow-listing** wrapper over `requests.Session`
    that refuses any request to a host outside the target's approved list *before a
    socket opens* (SSRF / misconfig / swapped-URL guard).
  - `DirectRunner` — runs the **existing `Sweep` engine** against the remote
    Paperless via the stored token, with the AI step routed through an **in-process
    `InferenceProxy` transport** so it is **metered/capped per tenant** through the
    Phase-6 billing/inference path. **The engine and Phase-6 inference are reused,
    not forked.**
- `paperless_control_plane/dashboard.py`:
  - `DashboardData` — assembles **read-only** fleet / cost / review payloads from the
    existing Phase-5 heartbeats, the Phase-6 billing ledger, and the direct-target
    registry.
  - `render_html()` — the **single self-contained** dashboard page.
- `paperless_control_plane/app.py` + `server.py` — GET-only `/dashboard`,
  `/dashboard/summary|fleet|cost|review` routes (a mutating method on a dashboard
  path is **405**, never a write). `serve` wires the dashboard + direct store.
- `paperless_control_plane/cli.py` — `direct-add` / `direct-list` / `direct-revoke`
  / `direct-run` and `dashboard`. The direct token comes from `PA_DIRECT_TOKEN`
  (**env only** — never a CLI flag, never YAML, never printed).

**NOT built (deliberately, per scope fences):**
- **No change makes Mode C the default** and **nothing weakens the agent** — the
  agent's outbound-only protocol and the six §7 guarantees for Modes A/B are
  untouched. Mode C runs ONLY when a tenant explicitly registers a target.
- **No heavy web framework / SPA** — the dashboard is stdlib HTTP + one inline HTML
  page with no external assets.
- **No real payment processor** — billing stays the Phase-6 stub seam.
- **No change** to the `AIProvider` interface, the engine, the Paperless client, or
  the Phase 5–6 protocol beyond the direct runner + dashboards + hardening.

---

## The three connectivity modes — honest trust comparison

The local **agent is recommended everywhere**. Mode C is the labelled exception.

| Mode | Who connects | Inference | **Vendor sees** | Inbound exposure |
|------|--------------|-----------|-----------------|------------------|
| **A — Agent, BYO-key** *(default)* | Agent → Paperless (LAN) + Agent → your AI provider (outbound) | Your provider / local Ollama | **Nothing** (no vendor cloud in the path) | **None** |
| **B — Agent, hosted** | Agent → Paperless (LAN) + Agent → control plane (outbound-only) | Vendor inference proxy | Control metadata + contents **transit** the proxy for the AI call (not persisted) | **None** |
| **C — Direct connection** *(advanced / opt-in)* | **Control plane → your published Paperless** | Vendor inference proxy | **Paperless URL + token + document contents** | You already expose Paperless (RP / Tunnel / Tailscale); asks **no new** exposure of anyone |

**Say it plainly:** Mode C is **genuinely weaker** than Mode B. It is the **only**
mode where the vendor **holds your Paperless token** and **can see your document
contents and metadata** directly. **BYO-key with local Ollama (Mode A) is the
zero-egress floor** — document data never leaves your box — and it stays the marketed
privacy default. Choose Mode C only if you already accept publishing your Paperless
and prefer the vendor reach it over running the companion agent.

### The six "no inbound exposure" points still hold for Modes A/B (connectivity §7)

Phase 7 adds **no** inbound path for the agent modes. Re-audited:

1. **Agent publishes no host ports** — unchanged (compose service has no `ports:`).
2. **Hosted work is pulled, not pushed** — the agent still dials out and long-polls;
   the control plane still never routes into the user's network. Dashboards are read
   endpoints on the **control plane** (which the agent dials out to), not a path into
   the agent.
3. **Paperless token never leaves the LAN in agent modes** — unchanged; the token is
   held server-side **only** in Mode C, which is opt-in.
4. **The webhook nudge is intra-LAN** — unchanged.
5. **Phase-5 firewall-all-inbound verification** — unchanged and still valid.
6. **The only mode that touches user-exposed surface is Mode C**, opt-in, relying
   solely on exposure the user already chose — this is exactly the §7 point-6
   exception, and Phase 7 implements it as such (labelled, non-default, revocable,
   egress-allow-listed).

If any future proposal violates points 1–5, it is out of bounds by the fixed
connectivity decision.

---

## Part 1 — Automated offline proofs (`tests/test_phase7_direct_dashboards.py`)

All run **fully offline**: no real cloud, no real Paperless, no real model API. A
**fake remote Paperless** stands in for the user's published instance; the vendor
model call is **stubbed**; stores are in-memory / tmp files.

```bash
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pytest -q tests/test_phase7_direct_dashboards.py
```

Proven (each an automated test):

- **Register → run:** a tenant registers a direct target (published URL + scoped
  token) and the `DirectRunner` completes an engine run against the **fake remote
  Paperless via the stored token** — a metadata PATCH lands on the remote doc and
  the AI step is **metered to the tenant** through the Phase-6 path.
- **Egress allow-listing (SSRF guard):** `AllowListSession` **refuses** any host
  outside the approved list (`169.254.169.254`, `evil.example`) *before* any request
  runs.
- **One-click revocation:** `revoke` immediately removes the URL + token; the next
  run **refuses** (`DirectTargetError`); the token is **scrubbed from the persisted
  file**; revoking again is a harmless no-op.
- **Token never logged:** a full run emits only routing metadata (`direct_run_start`
  / `direct_run_end`); the token and the remote document content **never appear** in
  any log line.
- **Spend cap / entitlement halt Mode C too:** an over-cap or unentitled tenant has
  its AI step **refused** — no metadata write reaches the remote Paperless.
- **Dashboard shapes:** fleet (agents + direct targets, tokens stripped), cost
  (spend-vs-cap, `over_cap` flag), review (per-tenant superseded / ai-new-taxonomy).
- **Self-contained HTML:** `render_html()` has **no `src=`/`href=` assets, no
  `http(s)://` references**, and its script issues **only GETs** (no POST/PUT/PATCH/
  DELETE).
- **Read-only over the wire:** a mutating method on any `/dashboard` path returns
  **405**; no token ever appears in a dashboard response.
- **Default is still the agent:** `Settings` default `mode` is `conservative`
  (BYO/local); `hosted_mode()` and `hosted_inference_active()` are `False`; there is
  **no `direct_mode` attribute or default** anywhere agent-side; with an empty
  direct-target store Mode C is inert.
- **CLI:** `direct-add` reads the token from `PA_DIRECT_TOKEN` (env only) and never
  prints it; `direct-list` strips it; `direct-revoke` removes it; `dashboard`
  prints read-only JSON.

---

## Part 2 — Manual live acceptance (publish Paperless, register, run, revoke)

> **This part is MANUAL** — it requires a real published Paperless instance and is
> **not** part of the offline suite. Do it once before a 1.0 cut.

**Prerequisites:** a real Paperless-NGX you already publish via **Cloudflare Tunnel**
or **Tailscale** (preferred — identity-bound, no open ports) or, less preferred, a
raw public reverse proxy. A **service-user** API token (NOT admin), scoped to
documents / custom_fields / tags / correspondents / document_types / tasks.

1. **Run the control plane with a vendor key + a subscription** (so the AI step is
   metered), exactly as Phase 6:
   ```bash
   export PA_VENDOR_ANTHROPIC_KEY=sk-ant-…        # SERVER-SIDE only
   pa-control-plane --state ./cp-state.json --billing ./cp-billing.json \
       --direct ./cp-direct.json serve --host 0.0.0.0 --port 8080
   pa-control-plane --billing ./cp-billing.json subscribe --tenant my-tenant --spend-cap 2.00
   ```
2. **Register the direct target** (token from env, never a flag):
   ```bash
   export PA_DIRECT_TOKEN=<service-user Paperless token>
   pa-control-plane --direct ./cp-direct.json direct-add \
       --tenant my-tenant --paperless-url https://paperless.mydomain.example
   # prints the PUBLIC record (token stripped) + a dt_… target id
   ```
   Confirm the printed record contains **no token**, `token_configured: true`, and
   `allowed_hosts` = your Paperless host only.
3. **Run a job against the remote Paperless:**
   ```bash
   pa-control-plane --direct ./cp-direct.json --billing ./cp-billing.json \
       direct-run --target-id dt_000001 --dry-run     # inspect proposals first
   pa-control-plane --direct ./cp-direct.json --billing ./cp-billing.json \
       direct-run --target-id dt_000001                # write metadata
   ```
   Verify in Paperless that the metadata proposals appear, and that
   `pa-control-plane --billing ./cp-billing.json usage --tenant my-tenant` shows the
   metered cost.
4. **Egress guard (optional):** temporarily register a target whose `--allowed-host`
   is a host you do NOT control and confirm `direct-run` **refuses** (it never dials
   a non-approved host).
5. **One-click revoke:** `pa-control-plane --direct ./cp-direct.json direct-revoke
   --target-id dt_000001`. Confirm `direct-list` is now empty and a further
   `direct-run` **refuses** — the URL + token are gone.
6. **Dashboards:** open `http://<control-plane-host>:8080/dashboard` in a browser.
   Confirm the page renders with the network panel showing **no external requests**
   (only same-origin GETs to `/dashboard/summary`), that it shows the fleet / cost /
   review tables, and that **no token** is visible anywhere. Confirm a `curl -X POST
   http://…/dashboard/summary` returns **405**.

**Pass criteria:** a user who already publishes Paperless opts into Mode C, runs the
engine against their instance directly (AI metered), and can revoke instantly; the
dashboards render read-only with no external assets and no token; and **every default
and recommendation still points to the local agent**.

---

## 1.0-readiness note — outstanding production-hardening items

Collected so nothing ships silently. None of these change the shipped behavior;
they are the known gaps to close before a real 1.0 / commercial launch.

- **Encryption-at-rest for the Mode C token (REQUIRED).** The direct token is a
  **reversible** secret the vendor must present to Paperless, so — unlike the agent
  credential — it **cannot be hashed**. This prototype stores it in an **owner-only
  (mode 0600) JSON file** and never logs it, but a production deployment **must**
  encrypt the direct-target store at rest (KMS-backed envelope encryption / a secrets
  manager / an encrypted volume) and put it behind strict access control. On Windows
  the `chmod 0600` is a best-effort no-op — use NTFS ACLs or an encrypted volume
  there. **This is the single most important 1.0 item.**
- **Agent credential + enrollment token at rest.** Already hashed at rest (scrypt,
  Phase 6); the JSON stores would still benefit from the same encrypted-volume /
  secrets-manager treatment in production.
- **Real payment processor.** Billing is a stub seam (no Stripe et al.); wire a real
  provider behind the existing `BillingStore` interface for launch.
- **Dashboard authn/authz.** The read-only dashboards are unauthenticated in the
  prototype (they expose no secrets — tokens are stripped, contents are never
  surfaced). Production must put them behind operator authentication and per-tenant
  authorization so one tenant cannot read another's cost/fleet data.
- **Transport security.** Production runs the control plane behind TLS (https base
  URL); optional mTLS for agent connections as noted in connectivity §3.
- **Direct-runner network hardening.** The egress allow-list refuses non-approved
  hosts; a production deployment should also pin DNS resolution / block link-local
  and private ranges at the network layer as defence-in-depth against DNS-rebinding.
- **Rate limiting + audit logging** on the control-plane endpoints (agent + admin +
  dashboard) for abuse resistance and traceability.
- **Supported Paperless version range** — pin empirically (Phase-4 note) before 1.0.
