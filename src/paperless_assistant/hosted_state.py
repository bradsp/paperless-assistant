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

"""Durable agent-side state for hosted mode (Phase 5), all under /data.

Three small JSON-backed stores, each atomic-write + restart-safe:

  * `AgentCredentialStore`  — the long-lived agent credential obtained at
        enrollment. Persisted under /data (NEVER in YAML, NEVER logged). Rotatable:
        a re-enroll overwrites it. This is the ONLY control-plane secret the agent
        holds (§4.1). The Paperless token / AI keys are NOT stored here and never
        travel to the control plane.

  * `JobCursor` — the idempotency ledger (§3.2). For each job_id it records a
        STAGE in a small state machine:

            pulled -> executing -> executed -> acked

        The apply path checks this before doing work, so an AT-LEAST-ONCE
        REDELIVERED job is de-duped: a job already 'executed'/'acked' is NOT
        re-run (no double-write, no double-spend); a job that crashed while
        'executing' is safe to re-run because the ENGINE itself is idempotent
        (snapshot + validate-before-write, I1/I2). The cursor is the fast-path
        dedupe; the engine is the correctness backstop.

  * `ResultQueue` — results that could not be pushed because the control plane was
        unreachable are queued here and FLUSHED on reconnect (§3.2). Restart-safe:
        a crash after executing-but-before-acking leaves the result queued, so it
        is delivered after restart without re-running the job.

Everything is keyed so a container restart resumes cleanly from /data with no
reprocessing of done work.
"""
from __future__ import annotations

import json
import pathlib
import threading


# Ordered stages of a single job's local lifecycle. A later stage subsumes an
# earlier one (a job at 'acked' is past 'executed', etc.).
JOB_STAGES = ("pulled", "executing", "executed", "acked")
_STAGE_RANK = {s: i for i, s in enumerate(JOB_STAGES)}


def _atomic_write(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: pathlib.Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


class AgentCredentialStore:
    """Persisted agent identity + credential under /data. Secret; never logged."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict | None:
        data = _read_json(self.path, None)
        if not data or not data.get("agent_credential"):
            return None
        return data

    def save(self, *, tenant: str, agent_id: str, agent_credential: str) -> None:
        with self._lock:
            _atomic_write(self.path, {
                "tenant": tenant,
                "agent_id": agent_id,
                "agent_credential": agent_credential,
            })

    def clear(self) -> None:
        """Drop the stored credential (e.g. after server-side revocation) so the
        agent re-enrolls on next start."""
        with self._lock:
            if self.path.exists():
                self.path.unlink()

    @property
    def enrolled(self) -> bool:
        return self.load() is not None


class JobCursor:
    """Per-job stage machine persisted under /data — the idempotency ledger."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()

    def _all(self) -> dict:
        return _read_json(self.path, {})

    def stage(self, job_id: str) -> str | None:
        return self._all().get(job_id, {}).get("stage")

    def rank(self, job_id: str) -> int:
        st = self.stage(job_id)
        return _STAGE_RANK.get(st, -1)

    def is_at_least(self, job_id: str, stage: str) -> bool:
        return self.rank(job_id) >= _STAGE_RANK[stage]

    def advance(self, job_id: str, stage: str, **meta) -> None:
        """Record that `job_id` has reached `stage`. Never moves BACKWARDS: a
        redelivered job cannot regress a completed job's stage (idempotency)."""
        if stage not in _STAGE_RANK:
            raise ValueError(f"unknown job stage {stage!r}")
        with self._lock:
            data = self._all()
            cur = data.get(job_id, {})
            if _STAGE_RANK.get(cur.get("stage"), -1) < _STAGE_RANK[stage]:
                cur["stage"] = stage
            cur.update(meta)
            data[job_id] = cur
            _atomic_write(self.path, data)

    def done_count(self) -> int:
        return sum(1 for v in self._all().values()
                   if _STAGE_RANK.get(v.get("stage"), -1) >= _STAGE_RANK["executed"])


class ResultQueue:
    """Durable FIFO of results pending push to the control plane (flush on
    reconnect). Each entry is {job_id, result}. Restart-safe."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()

    def _all(self) -> list:
        return _read_json(self.path, [])

    def enqueue(self, job_id: str, result: dict) -> None:
        with self._lock:
            items = self._all()
            # Dedupe by job_id: re-queuing the same job's result (e.g. after a
            # failed push then a re-execute short-circuit) must not pile up.
            items = [it for it in items if it.get("job_id") != job_id]
            items.append({"job_id": job_id, "result": result})
            _atomic_write(self.path, items)

    def peek_all(self) -> list:
        return list(self._all())

    def remove(self, job_id: str) -> None:
        with self._lock:
            items = [it for it in self._all() if it.get("job_id") != job_id]
            _atomic_write(self.path, items)

    def depth(self) -> int:
        return len(self._all())
