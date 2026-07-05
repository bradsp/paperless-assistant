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

"""Control-plane state: agent registry + per-agent job queue + dispatch tracking.

This is the VENDOR side. It models exactly enough to exercise the protocol:

  * agents:  agent_id -> {tenant, credential, enrolled_at, last_heartbeat, ...}
  * jobs:    a per-(tenant, agent) FIFO queue of job dicts, each with a lifecycle:
                queued -> dispatched -> (acked | requeued)
  * results: the latest result the agent pushed for each job (for assertions/tests)

AT-LEAST-ONCE DISPATCH (§3.2): a job dispatched to an agent but NOT yet acked
(because the network dropped before the agent's result POST landed) becomes
eligible for REDELIVERY after a visibility timeout — exactly like an SQS-style
queue. The AGENT is responsible for idempotent apply (job-id + stage machine), so
a redelivered job is safe. The control plane therefore need not be exactly-once;
it just must not lose jobs.

Persistence: in-memory by default (fine for the prototype, per r5). An optional
JSON file path makes the queue survive a control-plane restart too — cheap, so we
include it. Thread-safe: a single lock guards all mutation (the long-poll handler
threads and the enqueue path touch this concurrently).

Credentials are opaque random tokens; the enrollment token is one-time. A
credential can be REVOKED (§4.2): a revoked agent's calls are rejected, forcing it
to re-enroll or stop.

HARDENING (Phase 6, r5):
  1. The agent credential is stored HASHED AT REST, never in plaintext. Enrollment
     mints a random plaintext credential, returns it to the agent ONCE, and keeps
     only a salted hash (scrypt). `authenticate` re-derives the hash from the
     presented credential + the stored salt and compares constant-time. A dump of
     the state file therefore never reveals a working credential.
  2. The `_results` map is PRUNED so it cannot grow unbounded: an acked result is
     retained only long enough to be queried (bounded by size + TTL), and old
     entries are evicted. The queue's job records are already removed on ack; this
     bounds the separate result cache.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import pathlib
import secrets
import threading
import time
import uuid


def _now() -> float:
    return time.time()


# --- credential hashing (stdlib scrypt; no external deps) -------------------
# scrypt parameters: cheap enough for a per-request auth check, strong enough that
# a leaked hash is not trivially reversible. These are modest by design — the
# credential is a 256-bit random token (not a human password), so the hash exists
# to avoid storing a REPLAYABLE secret at rest, not to resist offline cracking of a
# weak password.
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_CRED_PREFIX = "agc_"


def _hash_credential(credential: str, salt_hex: str) -> str:
    """Derive the hex scrypt hash of `credential` under `salt_hex`."""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.scrypt(
        credential.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN,
    )
    return dk.hex()


def _new_credential() -> tuple[str, str, str]:
    """Mint a fresh (plaintext_credential, salt_hex, hash_hex) triple. Only the
    plaintext is handed to the agent; only salt+hash are persisted."""
    credential = _CRED_PREFIX + secrets.token_urlsafe(32)
    salt_hex = secrets.token_bytes(16).hex()
    return credential, salt_hex, _hash_credential(credential, salt_hex)


class ControlPlaneStore:
    def __init__(self, path: str | pathlib.Path | None = None,
                 *, visibility_timeout: float = 30.0, now=_now,
                 results_max: int = 1024, results_ttl: float = 86400.0):
        self._lock = threading.RLock()
        self._path = pathlib.Path(path) if path else None
        self._now = now
        self.visibility_timeout = float(visibility_timeout)
        # r5: bound the results cache. `results_max` caps the number of retained
        # acked results (oldest evicted first); `results_ttl` (seconds) evicts
        # entries older than the TTL. Together they keep _results from growing
        # unbounded no matter how many jobs flow through.
        self.results_max = int(results_max)
        self.results_ttl = float(results_ttl)
        # agent_id -> agent record
        self._agents: dict[str, dict] = {}
        # one-time enrollment tokens -> {tenant, agent_hint}
        self._enrollment_tokens: dict[str, dict] = {}
        # (tenant, agent_id) -> list[job]
        self._queues: dict[tuple, list] = {}
        # job_id -> {"result": <pushed result>, "acked_at": <ts>} (bounded)
        self._results: dict[str, dict] = {}
        self._load()

    # -- persistence (optional) -------------------------------------------
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        self._agents = data.get("agents", {})
        self._enrollment_tokens = data.get("enrollment_tokens", {})
        self._queues = {tuple(k.split("\x00", 1)): v
                        for k, v in data.get("queues", {}).items()}
        # r5: results are persisted as {job_id: {"result", "acked_at"}}. Tolerate a
        # legacy flat {job_id: result} shape from a pre-hardening state file.
        loaded_results = data.get("results", {})
        migrated = {}
        for jid, entry in loaded_results.items():
            if isinstance(entry, dict) and "result" in entry and "acked_at" in entry:
                migrated[jid] = entry
            else:
                migrated[jid] = {"result": entry, "acked_at": self._now()}
        self._results = migrated
        self._prune_results()

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "agents": self._agents,
            "enrollment_tokens": self._enrollment_tokens,
            "queues": {"\x00".join(k): v for k, v in self._queues.items()},
            "results": self._results,
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # -- enrollment --------------------------------------------------------
    def mint_enrollment_token(self, *, tenant: str = "t-default",
                              agent_hint: str | None = None) -> str:
        """Issue a one-time enrollment token (admin action). The agent exchanges
        it once for a long-lived credential; it is discarded on use."""
        token = "enr_" + secrets.token_urlsafe(24)
        with self._lock:
            self._enrollment_tokens[token] = {
                "tenant": tenant, "agent_hint": agent_hint, "minted_at": self._now(),
            }
            self._save()
        return token

    def enroll(self, enrollment_token: str) -> dict | None:
        """Exchange a one-time enrollment token for a long-lived agent credential.

        Returns {tenant, agent_id, agent_credential} on success, or None if the
        token is unknown/already-used (one-time). NEVER logs the credential."""
        with self._lock:
            info = self._enrollment_tokens.pop(enrollment_token, None)
            if info is None:
                return None
            agent_id = "agt_" + uuid.uuid4().hex[:12]
            # r5: mint plaintext ONCE; persist only the salted hash.
            credential, salt_hex, hash_hex = _new_credential()
            self._agents[agent_id] = {
                "agent_id": agent_id,
                "tenant": info["tenant"],
                "credential_salt": salt_hex,
                "credential_hash": hash_hex,
                "revoked": False,
                "enrolled_at": self._now(),
                "last_heartbeat": None,
                "last_status": None,
            }
            self._queues.setdefault((info["tenant"], agent_id), [])
            self._save()
            return {
                "tenant": info["tenant"],
                "agent_id": agent_id,
                "agent_credential": credential,  # plaintext, returned ONCE
            }

    def authenticate(self, agent_id: str, credential: str) -> dict | None:
        """Return the agent record iff the credential matches and is not revoked.

        r5: the credential is stored HASHED. We re-derive the hash from the
        presented credential + the record's stored salt and compare constant-time.
        The plaintext is never persisted, so a leaked state file yields no working
        credential."""
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None or rec.get("revoked"):
                return None
            salt = rec.get("credential_salt")
            stored = rec.get("credential_hash")
            if not salt or not stored:
                return None
            candidate = _hash_credential(str(credential), salt)
            if not hmac.compare_digest(candidate, str(stored)):
                return None
            return rec

    def revoke(self, agent_id: str) -> bool:
        """Server-side revocation (§4.2). A revoked agent's calls are rejected;
        it must re-enroll or stop. Returns True if an agent was revoked."""
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return False
            rec["revoked"] = True
            self._save()
            return True

    def rotate_credential(self, agent_id: str) -> str | None:
        """Rotate an agent's credential server-side (§4.2 rotatable). Returns the
        new plaintext credential (the agent must be told it out-of-band / re-enroll);
        the server keeps only its hash. Old credential stops working immediately."""
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is None:
                return None
            credential, salt_hex, hash_hex = _new_credential()
            rec["credential_salt"] = salt_hex
            rec["credential_hash"] = hash_hex
            self._save()
            return credential

    # -- job queue ---------------------------------------------------------
    def enqueue(self, *, tenant: str, agent_id: str, job_type: str,
                payload: dict | None = None, job_id: str | None = None) -> dict:
        """Enqueue a job for a specific agent. `job_type` is e.g. "process_document"
        or "run_sweep"; `payload` carries the LEAST metadata needed to route
        (opaque ids, §5) — never document contents. Returns the job dict."""
        job = {
            "job_id": job_id or ("job_" + uuid.uuid4().hex[:12]),
            "tenant": tenant,
            "agent_id": agent_id,
            "type": job_type,
            "payload": payload or {},
            "state": "queued",
            "enqueued_at": self._now(),
            "dispatched_at": None,
            "deliveries": 0,
        }
        with self._lock:
            self._queues.setdefault((tenant, agent_id), []).append(job)
            self._save()
        return dict(job)

    def _reclaim_expired(self, queue: list) -> None:
        """Return dispatched-but-unacked jobs to 'queued' after the visibility
        timeout, so they REDELIVER (at-least-once). Called under lock."""
        now = self._now()
        for job in queue:
            if (job["state"] == "dispatched"
                    and job["dispatched_at"] is not None
                    and now - job["dispatched_at"] >= self.visibility_timeout):
                job["state"] = "queued"

    def lease_next(self, *, tenant: str, agent_id: str) -> dict | None:
        """Return the next deliverable job for this agent and mark it dispatched
        (starting its visibility timer), or None if the queue is empty. This is
        what the long-poll handler calls when it has a job to hand out."""
        with self._lock:
            queue = self._queues.setdefault((tenant, agent_id), [])
            self._reclaim_expired(queue)
            for job in queue:
                if job["state"] == "queued":
                    job["state"] = "dispatched"
                    job["dispatched_at"] = self._now()
                    job["deliveries"] += 1
                    self._save()
                    return dict(job)
            return None

    def ack(self, *, tenant: str, agent_id: str, job_id: str,
            result: dict | None = None) -> bool:
        """Acknowledge a job as completed by the agent (its result POST landed).
        Removes it from the queue so it is NOT redelivered. Idempotent: acking an
        already-acked / unknown job is a harmless no-op (the agent may resend an
        ack after its own reconnect)."""
        with self._lock:
            if result is not None:
                self._results[job_id] = {"result": result, "acked_at": self._now()}
                self._prune_results()
            queue = self._queues.get((tenant, agent_id), [])
            before = len(queue)
            self._queues[(tenant, agent_id)] = [j for j in queue if j["job_id"] != job_id]
            self._save()
            return len(self._queues[(tenant, agent_id)]) < before

    def _prune_results(self) -> None:
        """Bound the results cache (r5). Evict entries older than the TTL, then, if
        still over the size cap, drop the oldest by ack time. Called under lock.
        The just-acked entry (newest) is always retained so `result_for` works
        immediately after ack."""
        if not self._results:
            return
        now = self._now()
        ttl = self.results_ttl
        if ttl > 0:
            expired = [jid for jid, e in self._results.items()
                       if now - e.get("acked_at", now) > ttl]
            for jid in expired:
                self._results.pop(jid, None)
        over = len(self._results) - self.results_max
        if over > 0:
            # Oldest-first eviction (FIFO by ack time).
            oldest = sorted(self._results.items(),
                            key=lambda kv: kv[1].get("acked_at", 0.0))
            for jid, _ in oldest[:over]:
                self._results.pop(jid, None)

    def record_heartbeat(self, *, agent_id: str, status: dict) -> None:
        with self._lock:
            rec = self._agents.get(agent_id)
            if rec is not None:
                rec["last_heartbeat"] = self._now()
                rec["last_status"] = status
                self._save()

    # -- introspection (tests / admin) ------------------------------------
    def queue_depth(self, *, tenant: str, agent_id: str) -> int:
        with self._lock:
            return sum(1 for j in self._queues.get((tenant, agent_id), [])
                       if j["state"] in ("queued", "dispatched"))

    def result_for(self, job_id: str) -> dict | None:
        with self._lock:
            entry = self._results.get(job_id)
            return entry["result"] if entry else None

    def results_count(self) -> int:
        """Number of cached results (bounded by results_max). For tests/admin."""
        with self._lock:
            return len(self._results)

    def agent(self, agent_id: str) -> dict | None:
        with self._lock:
            rec = self._agents.get(agent_id)
            return dict(rec) if rec else None
