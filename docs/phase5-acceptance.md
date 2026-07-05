# Phase 5 — Hosted control plane + outbound-only agent acceptance runbook

Phase 5 introduces **hosted mode (Mode B)**: the *same* agent, running inside the
user's network, dials **outbound-only** to a vendor **control plane**, **pulls**
work (long-poll), executes it **locally** against the user's Paperless with the
user's **own** AI provider (inference stays **BYO** this phase), and **pushes**
results back. The control plane **never** connects into the user's network. This
runbook separates the **automated offline proofs** from the **manual
firewall-all-inbound live acceptance** (product-architecture §9 Phase 5).

> **The single riskiest assumption in the plan** (product-architecture §11 risk #1,
> connectivity §7): *outbound-only, no inbound exposure, survives network loss.*
> Everything here exists to de-risk it with a working, tested protocol.

---

## What is and isn't built in Phase 5

**Built:** the outbound pull-loop trigger adapter (`paperless_assistant/hosted.py`),
a minimal, separate control plane (`paperless_control_plane/`), enrollment →
rotatable agent credential, durable cursor + result queue under `/data`,
reconnect with bounded backoff + jitter, at-least-once dispatch with idempotent
apply.

**NOT built (later phases, deliberately):** the `HostedProvider` **inference
proxy** and any **billing/metering/entitlement** (Phase 6 — *inference stays BYO /
agent-side this phase*); dashboards/UI, multi-tenant depth; the Mode C
**direct-connection** escape hatch (Phase 7). The control plane is a **thin
protocol gateway** only.

---

## Automated (offline) vs. manual (firewall live acceptance)

Every functional guarantee below is proven by an **offline** test driven against an
**in-process** control plane (no real cloud, no real Paperless, no real keys),
simulating disconnects/restarts programmatically. Only the **network-level**
firewall proof is inherently manual.

| Check | How it's verified | Where |
|-------|-------------------|-------|
| Enrollment exchanges a one-time token for a **persisted, rotatable** agent credential under `/data` (never YAML, never logged) | automated, offline | `tests/test_hosted.py::test_enrollment_persists_agent_credential_under_data`, `::test_enrollment_is_one_time`, `::test_credential_is_rotatable_and_revocable`, `::test_credential_never_logged` |
| Agent **pulls** a dispatched job, **executes** it via the existing engine, **pushes** a result, sends a **heartbeat** | automated, offline | `tests/test_hosted.py::test_pull_execute_push_and_heartbeat` |
| The pull-loop runs the **SAME engine** (`Sweep`), not a forked pipeline | automated, offline | `tests/test_hosted.py::test_default_runner_uses_the_existing_engine` |
| **NO inbound listener / no host port** — structural proof of outbound-only (§7 pt 2) | automated, offline | `tests/test_hosted.py::test_hosted_pull_loop_opens_no_inbound_listener`, `::test_hosted_serve_binds_no_port_via_cli` |
| Forced **disconnect mid-flow** resumes cleanly — result durably queued, flushed on reconnect, **no duplicate/partial write** | automated, offline | `tests/test_hosted.py::test_disconnect_after_execute_queues_result_and_flushes_on_reconnect`, `::test_in_flight_work_continues_while_control_plane_unreachable` |
| **At-least-once redelivery** de-duped — a redelivered job does **not** double-write or **double-spend** | automated, offline | `tests/test_hosted.py::test_redelivered_job_is_deduped_no_double_execute`, `::test_redelivery_after_full_completion_is_noop`, `tests/test_control_plane.py::test_at_least_once_redelivery_after_visibility_timeout` |
| **Restart** resumes from `/data` without reprocessing done work | automated, offline | `tests/test_hosted.py::test_restart_resumes_from_data_without_reprocessing`, `::test_restart_reuses_stored_credential_no_reenroll` |
| **Reconnect** with bounded exponential backoff + jitter, then resume | automated, offline | `tests/test_hosted.py::test_reconnect_loop_backs_off_then_resumes` |
| **No control-plane payload contains the Paperless token or an AI key** (§4) | automated, offline | `tests/test_hosted.py::test_no_secret_egress_in_any_control_plane_request`, `::test_build_result_rejects_forbidden_secret_keys` |
| **Credential revocation** server-side forces re-enroll/stop (§4.2) | automated, offline | `tests/test_hosted.py::test_credential_is_rotatable_and_revocable` |
| Control-plane protocol: parking long-poll → 204/job, ack idempotent, unauth rejected, admin enqueue | automated, offline | `tests/test_control_plane.py::*` |
| Real **localhost HTTP** round-trip (agent dials OUT over a socket) | automated, offline | `tests/test_control_plane.py::test_http_server_full_protocol_over_localhost` |
| Hosted config layering; **enrollment token from env only**, refused from YAML | automated, offline | `tests/test_config_layering.py::test_hosted_*` |
| **Firewall ALL inbound** on the agent host; a dispatched job still completes; pull the network mid-job, restore, clean resume, no duplicate writes | **MANUAL** (live stack) | this document, below |

Run the automated layer:

```bash
pytest -q                       # full suite (Phases 1–5), fully offline
pytest -q tests/test_hosted.py tests/test_control_plane.py   # Phase 5 only
```

---

## The six "no inbound exposure" points (connectivity §7) — how each is met

1. **Agent publishes no host ports.** The compose service has no `ports:`
   (`docker-compose.example.yml`; `tests/test_docker.py` asserts the Dockerfile
   exposes nothing). Hosted mode adds no port.
2. **Hosted work is pulled, not pushed.** The agent dials out and long-polls
   `GET /agent/work`; the control plane parks the request and answers it — it never
   initiates a connection to the agent. Proven structurally by
   `test_hosted_pull_loop_opens_no_inbound_listener` (the loop opens **no**
   listening socket) and `test_hosted_serve_binds_no_port_via_cli`.
3. **Paperless token never leaves the LAN in agent modes.** It is used only to talk
   to LAN Paperless and is **never** placed in a control-plane payload; the single
   result-building choke point (`hosted.build_result`) emits opaque ids + coarse
   usage only, guarded by `_assert_no_secrets`. Proven by
   `test_no_secret_egress_in_any_control_plane_request`.
4. **The webhook nudge is intra-LAN.** Unchanged from Phase 4 — Paperless → agent on
   the same network, no internet inbound path. Hosted mode does not use it.
5. **Phase 5 verification firewalls all inbound.** The manual acceptance below.
6. **The only mode that touches user-exposed surface is Mode C** — not built here
   (Phase 7).

---

## Manual firewall-all-inbound acceptance (live stack)

> This is the network-level proof that cannot be simulated offline: with **all
> inbound blocked** on the agent host, a job dispatched from the control plane still
> completes, and a mid-job network cut resumes cleanly with no duplicate writes.

### 0. Prerequisites

- A test Paperless-NGX stack reachable on the LAN, with a scoped service-user token
  (run `pa setup` + `pa doctor` first — see `phase3-acceptance.md`).
- The control plane running on a **separate** host/VM the agent can reach outbound
  (e.g. a cloud VM or another machine). Start it:
  ```bash
  pa-control-plane --state ./cp-state.json serve --host 0.0.0.0 --port 8080
  ```
- The agent host has an AI provider configured (BYO — e.g. `ANTHROPIC_API_KEY`), or
  a local Ollama. **Inference is agent-side; the control plane does not do it.**

### 1. Block ALL inbound on the agent host

The agent must be reachable from **nothing**. Drop all inbound; allow outbound
(so the agent can still reach LAN Paperless and dial out to the control plane).

- **Linux (nftables/iptables):**
  ```bash
  sudo iptables -P INPUT DROP
  sudo iptables -P FORWARD DROP
  sudo iptables -P OUTPUT ACCEPT
  sudo iptables -A INPUT -m state --state ESTABLISHED,RELATED -j ACCEPT   # replies to our OUTbound
  sudo iptables -A INPUT -i lo -j ACCEPT
  ```
- **UFW:** `sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw enable`
- **Docker note:** if the agent runs in compose, confirm the service still has **no
  `ports:`** mapping (it should not) — nothing is published to the host regardless.

Confirm from another machine that the agent host answers on **no** port
(`nmap -Pn <agent-host>` shows all closed/filtered).

### 2. Enroll + start the agent in hosted mode

On the control-plane host, mint a one-time enrollment token:

```bash
pa-control-plane --state ./cp-state.json mint-token --tenant demo
# -> prints an enr_… token
```

On the agent host, set the env and start `pa serve` (hosted):

```bash
export PA_MODE=hosted
export PA_CONTROL_PLANE_URL=http://<control-plane-host>:8080
export PA_ENROLLMENT_TOKEN=enr_…        # env only; one-time
export PAPERLESS_URL=http://webserver:8000
export PAPERLESS_TOKEN=…                 # scoped service-user token
export ANTHROPIC_API_KEY=…              # BYO inference (agent-side)
pa serve
```

**Expect:** the agent enrolls once (a `agent-credential.json` appears under
`/data`, containing the credential — **never** logged), then begins long-polling.
Note the printed `agent_id` (or read it from `/data/agent-credential.json`).

### 3. Dispatch a job — it completes despite all-inbound-blocked

On the control-plane host:

```bash
pa-control-plane --state ./cp-state.json enqueue \
    --tenant demo --agent-id agt_… --type run_sweep
# or: --type process_document --document-id <N>
```

**Expect:** within one long-poll cycle the agent **pulls** the job, runs the sweep
locally against LAN Paperless (snapshots I2, spend checks I3, review gates I4 all
happen agent-side), and **pushes** a result. Verify:
- The control plane recorded a result for that job (queue depth for the agent
  returns to 0).
- Paperless shows the expected changes (triage fields / metadata proposals).
- The agent host still answers on **no** inbound port (re-run `nmap` — unchanged).

This is the core proof: **a job flowed in and results flowed out with zero inbound
exposure** — every connection originated inside the network and pointed out.

### 4. Pull the network mid-job; restore; confirm clean resume, no duplicate writes

1. Enqueue a job (ideally a multi-doc `run_sweep`).
2. While it is executing, **cut the agent's outbound network** (e.g. unplug/`ifdown`,
   or `sudo iptables -A OUTPUT -p tcp --dport 8080 -j DROP` to block just the
   control plane). The agent's **in-flight local work continues**; when it tries to
   push the result the transport fails and the result is **queued under `/data`**.
   Watch the logs for `hosted_reconnect` with a bounded backoff.
3. **Restore** the network. The agent's reconnect loop resumes, **flushes** the
   queued result, and the control plane acks it.
4. **Restart the agent container mid-job** (`docker compose restart
   paperless-assistant`) as an extra check. On restart it **reuses the stored
   credential** (no re-enroll), **resumes from `/data`**, and does **not**
   reprocess already-completed work.

**Expect / verify:**
- No duplicate or partial-corrupt writes in Paperless (snapshot + validate-before-
  write, I1/I2). A re-pulled or redelivered job is de-duped by job-id + the stage
  machine — it does **not** double-write or **double-spend**.
- The result lands exactly once on the control plane after recovery.
- The agent never opened an inbound port at any point.

### 5. (Optional) Revoke the credential

On the control plane: `pa-control-plane --state ./cp-state.json revoke --agent-id
agt_…`. The agent's next call is rejected; it clears its local credential and stops
(or re-enrolls if a fresh `PA_ENROLLMENT_TOKEN` is provided). This demonstrates the
credential is **rotatable/revocable** server-side (§4.2).

### 6. Restore your firewall

Undo the DROP policies from step 1 when finished (`sudo iptables -P INPUT ACCEPT`,
flush the rules you added, or `sudo ufw disable`).

---

## Pass criteria

- [ ] With **all inbound blocked**, a dispatched job **completes** and results flow
      back — zero inbound exposure.
- [ ] A mid-job network cut + restore **resumes cleanly**, result delivered exactly
      once, **no duplicate writes**.
- [ ] A container **restart** mid-job resumes from `/data` with no reprocessing and
      no re-enrollment.
- [ ] The agent answers on **no** inbound port throughout (verified externally).
- [ ] The Paperless token and AI key never appear in any control-plane payload
      (automated test proves this; spot-check control-plane logs if desired).
