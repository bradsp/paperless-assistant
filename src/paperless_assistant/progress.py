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

"""In-memory LIVE progress tracker for the active sweep run (auto-refresh UI).

A single process-wide tracker that the `Sweep` updates as it processes documents
and the web dashboard reads (`GET /api/progress`) to show a live view of the
current / most-recent run: which stage is running, how many documents it has
processed vs. the stage total, the running spend, and a bounded rolling list of
what it did with each recent document.

Design constraints (mirrors the activity log's "observational" contract):
  * PURELY OBSERVATIONAL — updates are best-effort and NEVER affect processing
    (the sweep swallows any tracker error), and the tracker records only
    non-secret data already surfaced elsewhere (doc id/title, an outcome
    summary, the PUBLIC Paperless URL). No secret ever lands here.
  * IN-MEMORY ONLY — nothing is persisted; a restart clears it. The last run's
    snapshot stays visible (active=false) until the next run begins.
  * SNAPSHOT-CONSISTENT — reads take the same lock as writes, so the UI never
    sees a half-updated stage.

One run at a time is assumed: manual runs are single-flight and scheduled ticks
are sequential. `begin_run()` resets the tracker, so if a scheduled tick and a
manual run ever overlap, the tracker reflects whichever began most recently.
"""
from __future__ import annotations

import threading
import time
from collections import deque

# How many recent per-document outcomes to keep for the live view. Bounded so a
# large run (thousands of docs) can't grow memory without limit; the per-stage
# `processed` counters stay EXACT regardless.
_RECENT_MAX = 200


class ProgressTracker:
    def __init__(self, *, recent_max: int = _RECENT_MAX):
        self._lock = threading.Lock()
        self._recent_max = recent_max
        self._reset()

    def _reset(self):
        self._active = False
        self._run_id = None
        self._dry_run = None
        self._source = None
        self._started_at = None
        self._updated_at = None
        self._finished_at = None
        self._stage = None
        # stage -> {"total", "processed", "spend", "counts"}; order preserved.
        self._stages: dict[str, dict] = {}
        self._stage_order: list[str] = []
        self._recent: deque = deque(maxlen=self._recent_max)
        self._spend_total = 0.0
        self._counts: dict[str, int] = {}
        # Set when the run is stopped by a FATAL provider error (out of credits /
        # bad key). Stays visible after end_run() so the dashboard can explain why
        # the run stopped; cleared only by the next begin_run().
        self._error: dict | None = None

    # -- writers (called from the sweep) -----------------------------------
    def begin_run(self, run_id, *, dry_run=None, source=None, now=None):
        """Start tracking a new run. Resets any previous run's state."""
        with self._lock:
            self._reset()
            self._active = True
            self._run_id = run_id
            self._dry_run = dry_run
            self._source = source
            self._started_at = _epoch(now)
            self._updated_at = self._started_at

    def begin_stage(self, stage, total, *, now=None):
        """Register a stage about to process `total` documents."""
        with self._lock:
            if stage not in self._stages:
                self._stage_order.append(stage)
            self._stages[stage] = {
                "total": int(total), "processed": 0, "spend": 0.0, "counts": {},
            }
            self._stage = stage
            self._updated_at = _epoch(now)

    def record_doc(self, stage, status, doc_id, *, title=None, summary=None,
                   cost=0.0, url=None, now=None):
        """Record one document's outcome (increments the stage counter + appends
        it to the rolling recent list)."""
        cost = float(cost or 0.0)
        with self._lock:
            st = self._stages.get(stage)
            if st is None:  # defensive: a stage that never called begin_stage
                self._stage_order.append(stage)
                st = self._stages[stage] = {
                    "total": 0, "processed": 0, "spend": 0.0, "counts": {},
                }
                self._stage = stage
            st["processed"] += 1
            st["spend"] += cost
            st["counts"][status] = st["counts"].get(status, 0) + 1
            self._counts[status] = self._counts.get(status, 0) + 1
            self._spend_total += cost
            ts = _epoch(now)
            self._updated_at = ts
            self._recent.append({
                "ts": ts, "stage": stage, "status": status,
                "doc_id": doc_id, "doc_title": title,
                "summary": summary, "cost": cost, "paperless_url": url,
            })

    def set_error(self, kind, message, *, stage=None, help=None, now=None):
        """Record a fatal provider error that stopped the run (out of credits / bad
        key), for the dashboard to surface. Survives end_run()."""
        with self._lock:
            self._error = {
                "kind": kind, "message": message, "stage": stage,
                "help": help, "ts": _epoch(now),
            }
            self._updated_at = _epoch(now)

    def end_run(self, *, counts=None, spend_total=None, now=None):
        """Mark the run finished. The snapshot stays visible (active=false) until
        the next begin_run(). Authoritative final counts/spend (from the run
        report) override the accumulated values when provided."""
        with self._lock:
            self._active = False
            self._finished_at = _epoch(now)
            self._updated_at = self._finished_at
            if counts is not None:
                self._counts = dict(counts)
            if spend_total is not None:
                self._spend_total = float(spend_total)

    # -- reader (web) ------------------------------------------------------
    def snapshot(self) -> dict:
        """A JSON-safe copy of the current state (newest recent events first)."""
        with self._lock:
            recent = list(self._recent)
            recent.reverse()  # newest first for the UI
            return {
                "active": self._active,
                "run_id": self._run_id,
                "dry_run": self._dry_run,
                "source": self._source,
                "started_at": self._started_at,
                "updated_at": self._updated_at,
                "finished_at": self._finished_at,
                "stage": self._stage,
                "spend_total": round(self._spend_total, 6),
                "counts": dict(self._counts),
                "stages": [
                    dict(stage=s, **self._stages[s]) for s in self._stage_order
                ],
                "recent": recent,
                "recent_max": self._recent_max,
                "error": dict(self._error) if self._error else None,
            }


def _epoch(now):
    return time.time() if now is None else now


# Process-wide singleton: the scheduled `pa serve` loop and the dashboard's
# manual runs both update THIS instance, and the web layer reads it — so a single
# in-process tracker is the shared channel. Tests may construct their own
# ProgressTracker and inject it into a Sweep.
_TRACKER = ProgressTracker()


def tracker() -> ProgressTracker:
    return _TRACKER
