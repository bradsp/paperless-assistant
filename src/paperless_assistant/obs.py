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

"""Observability - structured JSON logs, a persisted period spend ledger, a
processing cursor, and a local status surface (plan §8.4).

Design center: everything durable lands under the single /data mount so state
survives restarts/upgrades (plan §8.1). NO heartbeat to any control plane - that
is Phase 5; a local status file + `pa status` is sufficient this phase.

The JSON log shape is stable and asserted by tests:
    {"ts", "level", "event", ...fields}
Per-doc outcomes, stage transitions, retries, and the REAL Paperless error on
failure (I6) are all emitted through `JsonLogger.event(...)`.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys
import threading


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class JsonLogger:
    """Emit one JSON object per line (JSONL). Writes to a stream (stderr by
    default so it doesn't pollute the human console output on stdout) and, if a
    path is given, appends to a durable log file under /data."""

    def __init__(self, *, stream=None, path=None, run_id=None):
        self.stream = stream if stream is not None else sys.stderr
        self.path = pathlib.Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._lock = threading.Lock()

    def event(self, event, level="info", **fields):
        rec = {"ts": _now_iso(), "level": level, "event": event}
        if self.run_id:
            rec["run_id"] = self.run_id
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            if self.stream is not None:
                self.stream.write(line + "\n")
                self.stream.flush()
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        return rec

    # Convenience wrappers used by the sweep.
    def doc_outcome(self, doc_id, status, detail=None, cost=0.0, stage=None):
        return self.event(
            "doc_outcome", level=("error" if status in ("ERROR", "error") else "info"),
            doc_id=doc_id, status=status, detail=detail, cost=round(cost, 6), stage=stage,
        )

    def stage_transition(self, stage, phase, **fields):
        return self.event("stage_transition", stage=stage, phase=phase, **fields)

    def failure(self, doc_id, error, stage=None):
        # I6: surface the server's real error text.
        return self.event("failure", level="error", doc_id=doc_id,
                          error=str(error), stage=stage)


# ---------------------------------------------------------------------------
# Period spend ledger (plan §8.4 "cumulative spend per period").
# ---------------------------------------------------------------------------
def _period_key(period: str) -> str:
    now = _dt.datetime.now(_dt.timezone.utc)
    if period == "daily":
        return now.strftime("%Y-%m-%d")
    if period == "weekly":
        return f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"
    # default monthly
    return now.strftime("%Y-%m")


class SpendLedger:
    """Persisted cumulative spend, bucketed per period, under /data. Lets the
    per-period cap survive restarts (unattended scheduled runs, plan §7.2/§8.4).
    Thread-safe within a process; the file is the source of truth across runs."""

    def __init__(self, path, period="monthly"):
        self.path = pathlib.Path(path)
        self.period = period
        self._lock = threading.Lock()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def current(self) -> float:
        with self._lock:
            return float(self._read().get(_period_key(self.period), 0.0))

    def add(self, cost: float) -> float:
        if not cost:
            return self.current()
        with self._lock:
            data = self._read()
            key = _period_key(self.period)
            data[key] = round(float(data.get(key, 0.0)) + float(cost), 6)
            self._write(data)
            return data[key]

    def would_exceed(self, cap: float) -> bool:
        if not cap:
            return False
        return self.current() >= cap


# ---------------------------------------------------------------------------
# Processing cursor + status surface (plan §8.1 cursor, §8.4 health/status).
# ---------------------------------------------------------------------------
class Cursor:
    """Tiny durable marker under /data recording that the out-of-box first run
    has happened (drives the first-run dry-run default, I7) and the last run
    timestamp. Restart-safe: it's just a file."""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @property
    def first_run_done(self) -> bool:
        return bool(self._read().get("first_run_done"))

    def mark_first_run_done(self):
        data = self._read()
        data["first_run_done"] = True
        self._write(data)

    def record_run(self, *, run_id, dry_run, counts, spend):
        data = self._read()
        data["last_run"] = {
            "ts": _now_iso(),
            "run_id": run_id,
            "dry_run": dry_run,
            "counts": counts,
            "spend": round(spend, 6),
        }
        self._write(data)


class PauseFlag:
    """A tiny durable switch under /data that halts AUTOMATIC processing (scheduled
    sweeps + webhook nudges) WITHOUT stopping the container (prompt 012).

    It is a single file (`paused.json`): present + {"paused": true} means paused.
    Restart-safe by construction — the scheduler reads it each tick, so a pause set
    from the dashboard survives a container restart. It NEVER blocks an explicit
    manual "Run now"; that is a deliberate user action, not automatic processing."""

    def __init__(self, path):
        self.path = pathlib.Path(path)

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def is_paused(self) -> bool:
        return bool(self._read().get("paused"))

    def set_paused(self, paused: bool):
        """Persist the pause state. Returns the new boolean state."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = self._read()
        data["paused"] = bool(paused)
        data["updated_at"] = _now_iso()
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return bool(paused)

    def state(self) -> dict:
        data = self._read()
        return {"paused": bool(data.get("paused")), "updated_at": data.get("updated_at")}


def pause_flag_for(settings) -> PauseFlag:
    """The single PauseFlag for this deployment (under the /data mount)."""
    return PauseFlag(str(settings.data_path("paused.json")))


def build_status(settings, cursor: Cursor, ledger: SpendLedger, *, queue_depth=None):
    """A lightweight local status dict (plan §8.4). NO control-plane heartbeat."""
    return {
        "base_url": settings.base_url,
        "mode": settings.mode,
        "stages_enabled": settings.enabled_stages(),
        "queue_depth": queue_depth,
        "last_run": cursor._read().get("last_run"),
        "first_run_done": cursor.first_run_done,
        "paused": pause_flag_for(settings).is_paused(),
        "spend": {
            "period": settings.spend.period,
            "period_spend": ledger.current(),
            "per_period_cap": settings.spend.per_period,
            "per_run_cap": settings.spend.per_run,
        },
    }
