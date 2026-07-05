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

"""RunReport - structured run summaries (counts, flags, spend, per-doc outcomes)
with human + JSON output.

Extracted from: the `--- summary ---` blocks and `print` lines in all three
scripts. Phase 3 extends this (plan §8.4) to PERSIST per-run JSON to /data, to
carry the new-taxonomy + superseded lists, and to record cumulative per-period
spend from the SpendGovernor - without changing the Phase 1 record/counts/to_dict
surface the existing callers/tests rely on.
"""
from __future__ import annotations

import datetime as _dt
import json
import pathlib
import uuid
from collections import Counter


class RunReport:
    def __init__(self, stage):
        self.stage = stage
        self.statuses = []
        self.per_doc = []  # list of (status, doc_id, detail, cost)
        self.spend_total = 0.0
        # Phase 3 additions (all optional; default empty so Phase 1 to_dict shape
        # is unchanged unless a caller populates them).
        self.run_id = uuid.uuid4().hex[:12]
        self.started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.finished_at = None
        self.new_taxonomy: list[str] = []
        self.superseded: list[int] = []
        self.dry_run: bool | None = None
        self.period_spend: float | None = None  # cumulative per-period spend
        self.period_cap: float | None = None

    def record(self, status, doc_id=None, detail=None, cost=0.0):
        self.statuses.append(status)
        self.per_doc.append((status, doc_id, detail, cost))

    def counts(self):
        return dict(Counter(self.statuses))

    def note_new_taxonomy(self, names):
        for n in names or []:
            if n and n not in self.new_taxonomy:
                self.new_taxonomy.append(n)

    def note_superseded(self, doc_id):
        if doc_id is not None and doc_id not in self.superseded:
            self.superseded.append(doc_id)

    def finish(self):
        self.finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    def to_dict(self):
        d = {
            "stage": self.stage,
            "counts": self.counts(),
            "spend_total": round(self.spend_total, 4),
            "per_doc": [
                {"status": s, "doc_id": d, "detail": det, "cost": c}
                for (s, d, det, c) in self.per_doc
            ],
        }
        # Phase 3 metadata is additive; keep the Phase 1 keys stable above.
        d.update(
            {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "dry_run": self.dry_run,
                "new_taxonomy": self.new_taxonomy,
                "superseded": self.superseded,
            }
        )
        if self.period_spend is not None:
            d["period_spend"] = round(self.period_spend, 4)
            d["period_cap"] = self.period_cap
        return d

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, reports_dir) -> pathlib.Path:
        """Write this run report as JSON under `reports_dir` (plan §8.4). Returns
        the path written. Filename is time-sortable + unique so runs never clash."""
        d = pathlib.Path(reports_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = (self.started_at or "").replace(":", "").replace("-", "")[:15]
        path = d / f"run-{stamp}-{self.stage}-{self.run_id}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path


class MultiStageReport:
    """A `pa run` sweep runs several single-stage RunReports; this aggregates them
    into one persisted /data report so the user sees the whole tick at a glance."""

    def __init__(self):
        self.run_id = uuid.uuid4().hex[:12]
        self.started_at = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.finished_at = None
        self.dry_run: bool | None = None
        self.stage_reports: list[RunReport] = []
        self.period_spend: float | None = None
        self.period_cap: float | None = None

    def add(self, report: RunReport):
        self.stage_reports.append(report)

    def finish(self):
        self.finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat()

    def merged_counts(self):
        c = Counter()
        for r in self.stage_reports:
            c.update(r.counts())
        return dict(c)

    def total_spend(self):
        return round(sum(r.spend_total for r in self.stage_reports), 4)

    def all_new_taxonomy(self):
        out = []
        for r in self.stage_reports:
            for n in r.new_taxonomy:
                if n not in out:
                    out.append(n)
        return out

    def all_superseded(self):
        out = []
        for r in self.stage_reports:
            for d in r.superseded:
                if d not in out:
                    out.append(d)
        return out

    def to_dict(self):
        return {
            "kind": "sweep",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "dry_run": self.dry_run,
            "counts": self.merged_counts(),
            "spend_total": self.total_spend(),
            "period_spend": (None if self.period_spend is None
                             else round(self.period_spend, 4)),
            "period_cap": self.period_cap,
            "new_taxonomy": self.all_new_taxonomy(),
            "superseded": self.all_superseded(),
            "stages": [r.to_dict() for r in self.stage_reports],
        }

    def to_json(self, indent=2):
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def persist(self, reports_dir) -> pathlib.Path:
        d = pathlib.Path(reports_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = (self.started_at or "").replace(":", "").replace("-", "")[:15]
        path = d / f"sweep-{stamp}-{self.run_id}.json"
        path.write_text(self.to_json(), encoding="utf-8")
        return path
