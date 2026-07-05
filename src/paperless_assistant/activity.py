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

"""ActivityStore - a per-document activity/audit log (prompt 013).

A single SQLite database under /data recording, ONE ROW PER REAL CHANGE, exactly
what the assistant changed on each document: field-level before -> after diffs for
triage / metadata / re-OCR (and the PROPOSED old -> would-be diffs in dry-run),
plus errors. It is ADDITIVE + OBSERVATIONAL — it sits beside the existing JSONL
logs + run reports, never replaces them, and NEVER changes what gets processed.

Design center (fixed decisions):
  * Storage: SQLite (stdlib `sqlite3`, NO new dependency), WAL mode, thread-safe
    writes (the sweep records from `_drain` worker threads while the UI reads),
    indexed on `ts` (purge + date filter) and `doc_id` (per-doc lookup),
    server-side filtered/paginated queries, efficient `DELETE WHERE ts < cutoff`.
  * Detail: field-level before -> after diffs per changed doc, stored as JSON.

SIZE + SIGNAL: record ONLY real changes / proposals / errors — never skips or
no-ops. An already-processed doc produces NO row. That keeps the audit meaningful
AND bounded.

NO SECRET is ever stored here: only document metadata (title/tags/stage/...) and
the non-secret Paperless document URL.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time


# The columns the store persists / returns. `changes` is a JSON blob shaped like
#   {"fields": {field: {"before": ..., "after": ...}},
#    "tags": {"added": [...], "removed": [...]},
#    "supersede": {"old_doc_id": N, "new_doc_id": M},
#    "flags": ["ai-new-taxonomy", ...],
#    "error": "..."}
# Only the sub-keys that apply to a given row are present.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    run_id       TEXT,
    doc_id       INTEGER,
    doc_title    TEXT,
    stage        TEXT,
    dry_run      INTEGER NOT NULL DEFAULT 0,
    status       TEXT,
    changes      TEXT,
    summary      TEXT,
    paperless_url TEXT
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity(ts);
CREATE INDEX IF NOT EXISTS idx_activity_doc ON activity(doc_id);
"""


def _now() -> float:
    return time.time()


class ActivityStore:
    """Thread-safe SQLite activity log. One connection, guarded by a lock; the
    connection is opened with `check_same_thread=False` so the `_drain` worker
    threads and the UI read thread can all use it. WAL mode lets a UI read run
    concurrently with a sweep write without blocking.

    Every public method is best-effort at the call site (the sweep wraps `record`
    in a try/except so a store failure can never fail a document/run), but the
    store itself raises normally so tests can force + observe failures.
    """

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL: concurrent reader (UI) + writer (sweep) without blocking.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- write -------------------------------------------------------------
    def record(self, entry: dict) -> int:
        """Insert one activity row. `entry` keys: run_id, doc_id, doc_title,
        stage, dry_run, status, changes (dict), summary, paperless_url, ts
        (optional; defaults to now). Returns the new row id.

        Raises on a real DB error — the CALLER (the sweep) wraps this in a
        best-effort try/except so a recording failure never fails the document."""
        changes = entry.get("changes")
        changes_json = json.dumps(changes, ensure_ascii=False) if changes is not None else None
        row = (
            float(entry.get("ts") or _now()),
            entry.get("run_id"),
            entry.get("doc_id"),
            entry.get("doc_title"),
            entry.get("stage"),
            1 if entry.get("dry_run") else 0,
            entry.get("status"),
            changes_json,
            entry.get("summary"),
            entry.get("paperless_url"),
        )
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO activity "
                "(ts, run_id, doc_id, doc_title, stage, dry_run, status, changes, "
                " summary, paperless_url) VALUES (?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self._conn.commit()
            return int(cur.lastrowid)

    # -- read --------------------------------------------------------------
    def query(self, *, doc_id=None, since=None, until=None, dry_run=None,
              stage=None, status=None, search=None, limit=50, offset=0) -> dict:
        """Filtered, server-side-paginated query. Returns
        {"rows": [row_dict, ...], "total": N, "limit": L, "offset": O}.

        Filters (all optional, ANDed): `doc_id`, `since`/`until` (epoch seconds on
        `ts`), `dry_run` (bool), `stage`, `status`, and a free-text `search` over
        the doc title + the changes JSON + the summary. Rows come back newest-first
        (by ts, then id). `total` is the full filtered count (for pagination)."""
        where, params = self._build_where(
            doc_id=doc_id, since=since, until=until, dry_run=dry_run,
            stage=stage, status=status, search=search,
        )
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))
        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) AS n FROM activity{where}", params
            ).fetchone()["n"]
            cur = self._conn.execute(
                f"SELECT * FROM activity{where} ORDER BY ts DESC, id DESC "
                f"LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            rows = [self._row_to_dict(r) for r in cur.fetchall()]
        return {"rows": rows, "total": int(total), "limit": limit, "offset": offset}

    def get(self, row_id: int) -> dict | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM activity WHERE id = ?", (int(row_id),)
            ).fetchone()
        return self._row_to_dict(r) if r is not None else None

    def stats(self) -> dict:
        """Lightweight store stats: row count, oldest/newest ts, on-disk size."""
        with self._lock:
            r = self._conn.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS oldest, MAX(ts) AS newest "
                "FROM activity"
            ).fetchone()
        size = 0
        try:
            size = self.path.stat().st_size
        except OSError:
            pass
        return {
            "count": int(r["n"] or 0),
            "oldest_ts": r["oldest"],
            "newest_ts": r["newest"],
            "size_bytes": size,
        }

    # -- purge -------------------------------------------------------------
    def purge(self, older_than_ts: float) -> int:
        """Delete rows with ts < `older_than_ts` (efficient, index-backed).
        Returns the number of rows deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM activity WHERE ts < ?", (float(older_than_ts),)
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self):
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _build_where(*, doc_id, since, until, dry_run, stage, status, search):
        clauses = []
        params: list = []
        if doc_id is not None:
            clauses.append("doc_id = ?")
            params.append(int(doc_id))
        if since is not None:
            clauses.append("ts >= ?")
            params.append(float(since))
        if until is not None:
            clauses.append("ts <= ?")
            params.append(float(until))
        if dry_run is not None:
            clauses.append("dry_run = ?")
            params.append(1 if dry_run else 0)
        if stage:
            clauses.append("stage = ?")
            params.append(str(stage))
        if status:
            clauses.append("status = ?")
            params.append(str(status))
        if search:
            like = f"%{search}%"
            clauses.append(
                "(IFNULL(doc_title,'') LIKE ? OR IFNULL(changes,'') LIKE ? "
                "OR IFNULL(summary,'') LIKE ?)"
            )
            params.extend([like, like, like])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    @staticmethod
    def _row_to_dict(r: sqlite3.Row) -> dict:
        changes = None
        if r["changes"]:
            try:
                changes = json.loads(r["changes"])
            except (ValueError, TypeError):
                changes = None
        return {
            "id": r["id"],
            "ts": r["ts"],
            "run_id": r["run_id"],
            "doc_id": r["doc_id"],
            "doc_title": r["doc_title"],
            "stage": r["stage"],
            "dry_run": bool(r["dry_run"]),
            "status": r["status"],
            "changes": changes,
            "summary": r["summary"],
            "paperless_url": r["paperless_url"],
        }


# ===========================================================================
# Diff helpers — compute a field-level before -> after `changes` dict from the
# pre-write snapshot / in-memory doc ("before") and the applied/proposed values
# ("after"). Pure functions, no I/O; the sweep calls these inside each stage's
# process(doc) closure where the before-doc + computed change are in hand.
# ===========================================================================
def _norm(v):
    """Normalise a value for equality comparison (treat None and '' the same)."""
    if v is None:
        return ""
    return v


def diff_fields(before: dict, after: dict) -> dict:
    """Return {field: {"before": b, "after": a}} for every field whose value
    actually CHANGED. Fields present in `after` are compared to `before`;
    unchanged fields are omitted (so an unchanged field never produces noise)."""
    out = {}
    for field, aval in after.items():
        bval = before.get(field)
        if _norm(bval) != _norm(aval):
            out[field] = {"before": bval, "after": aval}
    return out


def tag_delta(before_names, after_names) -> dict:
    """Return {"added": [...], "removed": [...]} of tag NAMES. Order-stable,
    de-duplicated. Empty lists are omitted by the caller if both are empty."""
    b = list(before_names or [])
    a = list(after_names or [])
    bset = set(b)
    aset = set(a)
    added = [t for t in a if t not in bset]
    removed = [t for t in b if t not in aset]
    out = {}
    if added:
        out["added"] = added
    if removed:
        out["removed"] = removed
    return out


def paperless_doc_url(base_url: str, doc_id) -> str:
    """The non-secret Paperless UI URL for a document (for the table link).
    Best-effort; returns '' if either input is missing."""
    if not base_url or doc_id is None:
        return ""
    return f"{str(base_url).rstrip('/')}/documents/{doc_id}/details"


def changes_summary(changes: dict) -> str:
    """A concise one-line human summary of a `changes` dict for the table row."""
    if not changes:
        return ""
    bits = []
    for field, ba in (changes.get("fields") or {}).items():
        after = ba.get("after")
        bits.append(f"{field}={after!r}")
    tags = changes.get("tags") or {}
    if tags.get("added"):
        bits.append("+" + ",".join(tags["added"]))
    if tags.get("removed"):
        bits.append("-" + ",".join(tags["removed"]))
    sup = changes.get("supersede")
    if sup:
        bits.append(f"superseded->{sup.get('new_doc_id')}")
    for fl in changes.get("flags") or []:
        bits.append(f"[{fl}]")
    if changes.get("error"):
        bits.append("ERROR: " + str(changes["error"])[:80])
    return "; ".join(bits)
