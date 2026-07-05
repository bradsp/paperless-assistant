# Paperless Assistant - an AI companion for Paperless-NGX.
# Copyright (C) 2026 BP Technology Advisors LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Hosted-mode agent: the OUTBOUND-ONLY pull-loop trigger adapter (Phase 5).

    ┌──────────────────────────────────────────────────────────────────────┐
    │  Mode B (plan/connectivity-design.md §1, §2.3).  The SAME agent, same  │
    │  engine — a NEW trigger adapter that DIALS OUT to a control plane and  │
    │  PULLS work, instead of a local cron/webhook.  The control plane never │
    │  connects to the agent; this loop binds NO inbound listener and needs  │
    │  NO host port.  Work is pulled, never pushed (§7 point 2).             │
    └──────────────────────────────────────────────────────────────────────┘

Lifecycle (§3.1), all outbound:
  1. ENROLL (one-time): exchange PA_ENROLLMENT_TOKEN for a long-lived agent
     credential, persist it under /data (never logged, never YAML). Skipped if a
     credential is already stored (restart-safe).
  2. CONNECT & PULL: long-poll GET /agent/work with the agent credential.
  3. EXECUTE LOCALLY: run the job through the EXISTING engine (Sweep) against LAN
     Paperless — all snapshots (I2), spend checks (I3), review gates (I4) happen
     agent-side, unchanged. Inference is BYO/agent-side (the control plane does
     NOT run or bill inference this phase).
  4. PUSH RESULTS: POST /agent/results. If the control plane is unreachable, the
     result is QUEUED under /data and flushed on reconnect.
  5. HEARTBEAT: periodic POST /agent/heartbeat (status, queue depth, spend-vs-cap).

Resilience (§3.2), the point of the phase:
  * Idempotent apply + job-id/stage-machine dedupe: a redelivered (at-least-once)
    job is NOT re-executed and NOT re-billed (JobCursor).
  * Reconnect with bounded exponential backoff + jitter on TransportError.
  * In-flight LOCAL work continues regardless of control-plane reachability;
    results queue in /data and flush on reconnect.
  * Restart resumes cleanly from /data (credential, cursor, result queue) without
    reprocessing done work.

SECURITY (§4): the ONLY secret this loop sends to the control plane is the agent
credential (as a bearer token in headers). Job PAYLOADS and result BODIES carry
opaque ids + coarse outcome/usage only — the Paperless token and any AI provider
key are NEVER included. `build_result` is the single place result bodies are
constructed, so the "no token/key egress" property is enforced in one auditable
spot and asserted by tests.
"""
from __future__ import annotations

import random
import time

from .transport import Transport, HttpTransport, TransportError
from .hosted_state import AgentCredentialStore, JobCursor, ResultQueue
from .obs import JsonLogger

# Wire paths — the agent's view of the documented control-plane HTTP API. Kept as
# local constants so the agent does NOT import the vendor package at runtime (the
# trust boundary stays clean); they simply must match the control plane's routes.
PATH_ENROLL = "/agent/enroll"
PATH_WORK = "/agent/work"
PATH_RESULTS = "/agent/results"
PATH_HEARTBEAT = "/agent/heartbeat"

# Secret-looking substrings that must NEVER appear in a control-plane payload
# (defensive scrub used by build_result; the real guarantee is that we simply
# never put them in — this catches accidental future regressions).
_FORBIDDEN_RESULT_KEYS = frozenset({
    "paperless_token", "token", "anthropic_api_key", "openai_api_key",
    "api_key", "agent_credential", "secret",
})


class EnrollmentError(RuntimeError):
    """Enrollment failed (bad/used token, or control plane unreachable at first
    start with no stored credential)."""


class RevokedError(RuntimeError):
    """The control plane rejected the agent credential as unauthenticated even
    after a successful enroll earlier — i.e. it was revoked server-side (§4.2).
    The agent clears its credential and must re-enroll or stop."""


def build_result(job: dict, report) -> dict:
    """Construct the result BODY pushed to the control plane for a finished job.

    THIS IS THE TRUST-BOUNDARY CHOKE POINT. It emits ONLY opaque ids + coarse
    outcome/usage metadata (§5 minimize payloads). It NEVER includes the Paperless
    token, an AI key, document contents, or the agent credential. A final assert
    guards against a forbidden key sneaking in via `report`."""
    counts = {}
    spend = 0.0
    if report is not None:
        try:
            counts = report.merged_counts()
        except AttributeError:
            counts = getattr(report, "counts", lambda: {})()
        try:
            spend = float(report.total_spend())
        except AttributeError:
            spend = float(getattr(report, "spend_total", 0.0) or 0.0)

    body = {
        "job_id": job["job_id"],
        "type": job.get("type"),
        "outcome": "completed",
        "counts": counts,          # e.g. {"wrote": 2, "skip": 1} — no doc content
        "usage": {"spend_usd": round(spend, 6)},
    }
    _assert_no_secrets(body)
    return body


def _assert_no_secrets(body: dict) -> None:
    """Fail loudly if a forbidden secret key appears anywhere in a payload we are
    about to send to the control plane. Belt-and-suspenders for §4."""
    def walk(node, trail=""):
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in _FORBIDDEN_RESULT_KEYS:
                    raise AssertionError(
                        f"refusing to send '{trail}{k}' to the control plane — "
                        f"secrets never leave the agent (§4)."
                    )
                walk(v, f"{trail}{k}.")
        elif isinstance(node, list):
            for it in node:
                walk(it, trail)
    walk(body)


class HostedAgent:
    """The outbound pull-loop. Constructed with resolved `settings`, a `transport`
    (defaults to real HTTP against settings.control_plane_url), and a `job_runner`
    callable(job) -> report that executes a job through the engine (defaults to a
    Sweep-backed runner). `enrollment_token` defaults to settings.enrollment_token.
    """

    def __init__(self, settings, *, transport: Transport | None = None,
                 job_runner=None, logger: JsonLogger | None = None,
                 sleep=time.sleep, now=time.time, rng=None):
        self.settings = settings
        self.data = settings.data_path
        self.logger = logger or JsonLogger(path=str(self.data("logs", "pa.jsonl")))
        self._sleep = sleep
        self._now = now
        self._rng = rng or random.Random()

        self.creds = AgentCredentialStore(str(self.data("agent-credential.json")))
        self.cursor = JobCursor(str(self.data("hosted-cursor.json")))
        self.results = ResultQueue(str(self.data("hosted-results.json")))

        self.hosted = settings.hosted
        self.transport = transport or HttpTransport(self.hosted.control_plane_url)
        self.enrollment_token = self.hosted.enrollment_token
        self._job_runner = job_runner  # lazy default built on first use

        self._last_heartbeat = 0.0
        self._identity = self.creds.load()  # {tenant, agent_id, agent_credential} or None

    # -- job runner (executes a job through the EXISTING engine) -----------
    def _runner(self):
        if self._job_runner is not None:
            return self._job_runner
        # Default: back onto the same Sweep engine every other trigger uses, so
        # the pipeline is NOT forked. "run_sweep" -> a full sweep tick;
        # "process_document" -> the single-doc nudge path (idempotent).
        from .sweep import Sweep

        sweep = Sweep(self.settings, logger=self.logger, cfg=self._engine_cfg())

        def run(job):
            jtype = job.get("type")
            payload = job.get("payload") or {}
            if jtype == "process_document":
                doc_id = payload.get("document_id") or payload.get("doc_id")
                return sweep.process_nudge(int(doc_id))
            if jtype == "run_sweep":
                return sweep.run_once(limit=payload.get("limit"))
            raise ValueError(f"unknown hosted job type {jtype!r}")

        self._job_runner = run
        return self._job_runner

    # -- engine config, with hosted-inference wiring when active (Phase 6) -
    def _engine_cfg(self):
        """Build the Config the engine consumes. When hosted inference is active
        (hosted mode + toggle ON + NO local AI key), inject a HostedInferenceContext
        so the registry resolves AI tasks to the HostedProvider — which calls the
        control-plane inference proxy over THIS agent's outbound transport + agent
        credential. BYO/local produces the same Config as before (context = None),
        so the zero-egress floor is unchanged.

        The agent must be enrolled first so `_auth_headers` has a live credential;
        the hosted job runner always enrolls before executing, but we guard here
        too."""
        cfg = self.settings.to_config()
        if self.settings.hosted_inference_active():
            from .config import HostedInferenceContext

            self.ensure_enrolled()
            cfg.hosted_inference = HostedInferenceContext(
                transport=self.transport,
                auth_headers=self._auth_headers,
                ocr_model=self.hosted.inference_ocr_model,
                metadata_model=self.hosted.inference_metadata_model,
            )
        return cfg

    # -- headers (agent credential is the ONLY control-plane secret) -------
    def _auth_headers(self) -> dict:
        ident = self._identity
        return {
            "X-Agent-Id": ident["agent_id"],
            "Authorization": f"Bearer {ident['agent_credential']}",
        }

    # -- 1. enrollment -----------------------------------------------------
    def ensure_enrolled(self) -> dict:
        """Return the agent identity, enrolling once if needed. Restart-safe: if a
        credential is already stored we DO NOT re-enroll (the one-time token may be
        gone). The credential is persisted under /data and never logged."""
        if self._identity is not None:
            return self._identity
        if not self.enrollment_token:
            raise EnrollmentError(
                "hosted mode: no stored agent credential and PA_ENROLLMENT_TOKEN "
                "is not set. Set PA_ENROLLMENT_TOKEN (env only) for first enrollment."
            )
        status, body = self.transport.request(
            "POST", PATH_ENROLL, body={"enrollment_token": self.enrollment_token})
        if status != 200 or not body.get("agent_credential"):
            raise EnrollmentError(
                f"enrollment rejected (status {status}). The enrollment token is "
                f"one-time; it may be used or invalid."
            )
        self.creds.save(
            tenant=body["tenant"], agent_id=body["agent_id"],
            agent_credential=body["agent_credential"],
        )
        self._identity = self.creds.load()
        # Log identity WITHOUT the credential (never log the secret, §4.1).
        self.logger.event("agent_enrolled", tenant=body["tenant"],
                          agent_id=body["agent_id"])
        return self._identity

    # -- 2+3+4. pull one job, execute it, push the result ------------------
    def poll_once(self) -> dict | None:
        """Long-poll for ONE job; if one arrives, execute it idempotently and push
        the result. Returns the job dict processed, or None if the poll returned no
        work. Raises TransportError if the control plane is unreachable (caller's
        reconnect loop handles it). Raises RevokedError if the credential is
        rejected."""
        self.ensure_enrolled()
        # Flush any results queued from a previous unreachable window first.
        self.flush_results()

        status, body = self.transport.request(
            "GET", PATH_WORK, headers=self._auth_headers())
        if status == 401:
            self._on_revoked()
        if status == 204 or not body.get("job"):
            return None
        if status != 200:
            raise TransportError(f"unexpected /agent/work status {status}")
        job = body["job"]
        self._execute_and_push(job)
        return job

    def _execute_and_push(self, job: dict) -> None:
        job_id = job["job_id"]

        # --- AT-LEAST-ONCE DEDUPE (§3.2). A redelivered job already executed is
        # NOT re-run and NOT re-billed; we just (re)push/ack its result.
        if self.cursor.is_at_least(job_id, "executed"):
            self.logger.event("hosted_job_dedup", job_id=job_id,
                              stage=self.cursor.stage(job_id))
            # Ensure the ack eventually lands even if the earlier push was lost.
            queued = {it["job_id"] for it in self.results.peek_all()}
            if job_id not in queued and not self.cursor.is_at_least(job_id, "acked"):
                self.results.enqueue(job_id, self._dedup_result(job))
            self.flush_results()
            return

        self.cursor.advance(job_id, "pulled", type=job.get("type"))
        self.logger.event("hosted_job_pulled", job_id=job_id, type=job.get("type"),
                          deliveries=job.get("deliveries"))

        # --- EXECUTE LOCALLY through the existing engine. This runs regardless of
        # control-plane reachability once we hold the job (in-flight local work
        # continues, §3.2). The engine's own idempotency (I1/I2) makes a re-run
        # after a mid-execute crash safe.
        self.cursor.advance(job_id, "executing")
        report = self._runner()(job)
        result = build_result(job, report)          # trust-boundary choke point
        self.cursor.advance(job_id, "executed")
        self.logger.event("hosted_job_executed", job_id=job_id,
                          counts=result.get("counts"))

        # --- PUSH RESULT. On unreachable control plane, queue for flush-on-reconnect.
        self.results.enqueue(job_id, result)
        self.flush_results()

    def _dedup_result(self, job: dict) -> dict:
        body = {
            "job_id": job["job_id"], "type": job.get("type"),
            "outcome": "already_done", "counts": {}, "usage": {"spend_usd": 0.0},
        }
        _assert_no_secrets(body)
        return body

    # -- 4b. flush queued results (called on every poll + on reconnect) ----
    def flush_results(self) -> int:
        """Push any results queued under /data. Returns count flushed. On transport
        failure it stops (leaving the rest queued) and re-raises nothing — the next
        poll retries. Ack is idempotent server-side, so a redelivered ack is safe."""
        flushed = 0
        for item in self.results.peek_all():
            try:
                status, _ = self.transport.request(
                    "POST", PATH_RESULTS,
                    headers=self._auth_headers(),
                    body={"job_id": item["job_id"], "result": item["result"]},
                )
            except TransportError:
                break  # still unreachable; leave queued, retry next reconnect
            if status == 401:
                self._on_revoked()
            if status == 200:
                self.results.remove(item["job_id"])
                self.cursor.advance(item["job_id"], "acked")
                flushed += 1
            else:
                break
        return flushed

    # -- 5. heartbeat ------------------------------------------------------
    def maybe_heartbeat(self, *, force: bool = False) -> bool:
        """Send a heartbeat if the interval elapsed (or forced). Best-effort: a
        TransportError is swallowed (heartbeat is liveness, not correctness)."""
        interval = self.hosted.heartbeat_interval_seconds
        if not force and (self._now() - self._last_heartbeat) < interval:
            return False
        try:
            self.ensure_enrolled()
            status = self._build_status()
            code, _ = self.transport.request(
                "POST", PATH_HEARTBEAT,
                headers=self._auth_headers(), body={"status": status})
        except TransportError:
            return False
        if code == 401:
            self._on_revoked()
        self._last_heartbeat = self._now()
        self.logger.event("hosted_heartbeat", queue_depth=self.results.depth())
        return code == 200

    def _build_status(self) -> dict:
        """Coarse liveness metadata ONLY (no secrets, no contents)."""
        return {
            "result_queue_depth": self.results.depth(),
            "jobs_done": self.cursor.done_count(),
            "mode": "hosted",
        }

    # -- revocation handling ----------------------------------------------
    def _on_revoked(self):
        self.logger.event("agent_credential_revoked", level="warning",
                          agent_id=(self._identity or {}).get("agent_id"))
        self.creds.clear()
        self._identity = None
        raise RevokedError(
            "control plane rejected the agent credential (revoked). Cleared local "
            "credential; set PA_ENROLLMENT_TOKEN to re-enroll, or stop the agent."
        )

    # -- the pull-loop with reconnect + backoff + jitter -------------------
    def run(self, *, iterations=None):
        """Run the outbound pull-loop. `iterations` bounds it for tests (None =
        forever). Reconnects with bounded exponential backoff + jitter on
        TransportError; the loop never binds an inbound listener."""
        self.logger.event("hosted_serve_start",
                          control_plane=self.hosted.control_plane_url)
        n = 0
        backoff = self.hosted.reconnect_backoff_min
        while iterations is None or n < iterations:
            try:
                self.maybe_heartbeat()
                self.poll_once()
                backoff = self.hosted.reconnect_backoff_min  # healthy -> reset
            except TransportError as e:
                delay = self._backoff_delay(backoff)
                self.logger.event("hosted_reconnect", level="warning",
                                  error=str(e), backoff_seconds=round(delay, 3))
                self._sleep(delay)
                backoff = min(backoff * 2, self.hosted.reconnect_backoff_max)
            except RevokedError:
                # Credential revoked: stop the loop (agent must re-enroll/stop).
                break
            n += 1
            if iterations is not None and n >= iterations:
                break
        self.logger.event("hosted_serve_stop", iterations=n)

    def _backoff_delay(self, base: float) -> float:
        """Full-jitter bounded exponential backoff: sleep in [0, base]."""
        return self._rng.uniform(0.0, min(base, self.hosted.reconnect_backoff_max))
