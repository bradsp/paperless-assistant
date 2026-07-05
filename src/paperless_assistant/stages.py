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

"""StageOrchestrator - the ai_stage state machine, eligibility predicates and
queue construction with idempotency/skip logic (I1).

Extracted from: `already_triaged`, `fetch_queue`, `eligible`, `ELIGIBLE_STAGES`
(across all three scripts). Client-side queue filtering is deliberate and stays
the default (plan §4.2): the server-side custom_field_query grammar is
version-sensitive.

Prompt 011: field/stage NAMES, metadata eligibility, and the garbage threshold
are resolved from the CONFIGURED values (via the resolver's role-based accessors
and the injected `settings`) rather than the module constants — defaults keep
behaviour byte-identical.
"""
from __future__ import annotations

from . import config


class StageOrchestrator:
    def __init__(self, client, resolver, settings=None):
        self.client = client
        self.resolver = resolver
        # `settings` supplies configured eligibility + garbage threshold + page
        # size. None -> byte-identical historical defaults (ELIGIBLE_STAGES /
        # GARBAGE_THRESH / page_size=100).
        self.settings = settings

    @property
    def _page_size(self):
        return self.settings.http.page_size if self.settings else config.DEFAULT_PAGE_SIZE

    def _iter(self, fields):
        return self.client.iter_documents(fields, page_size=self._page_size)

    # ---- Stage 0 (triage) -------------------------------------------------
    def already_triaged(self, doc):
        """I1: skip a doc that is already at ``triaged`` OR ANY later stage
        (``reocr_done`` / ``metadata_done``) — i.e. "already handled" — unless
        --force.

        SOURCE CORRECTNESS FIX (Phase 4): this predicate previously matched ONLY
        the exact ``triaged`` label, so a doc already advanced to ``metadata_done``
        (or ``reocr_done``) looked re-triageable. Re-triaging it reset ai_stage
        back to ``triaged``, which made the metadata stage re-process and re-bill
        it on the next pass — an I1 (idempotency) + I3 (spend) violation. The
        correct meaning of "handled" is "at ``triaged`` or any later stage"; the
        ordered ``config.STAGE_ORDER`` is the single source of truth (resolved by
        ROLE so configured stage labels work)."""
        stage_id = self.resolver.stage_field_id()
        for cf in doc.get("custom_fields") or []:
            if cf["field"] == stage_id:
                role = self.resolver.role_for_value(cf.get("value"))
                return role in config.STAGE_ORDER
        return False

    def fetch_triage_queue(self, limit=0):
        """All documents (with fields triage needs), truncated to `limit`."""
        docs = []
        fields = "id,title,content,custom_fields"
        for d in self._iter(fields):
            docs.append(d)
            if limit and len(docs) >= limit:
                break
        return docs

    # ---- Stage 1 (re-OCR) -------------------------------------------------
    def reocr_matches(self, doc, threshold):
        """ai_stage == triaged AND ocr_quality >= threshold."""
        triaged_id = self.resolver.stage_option_for_role(config.STAGE_TRIAGED)
        score_fid = self.resolver.score_field_id()
        stage_fid = self.resolver.stage_field_id()
        cf = {c["field"]: c.get("value") for c in (doc.get("custom_fields") or [])}
        if cf.get(stage_fid) != triaged_id:
            return False
        score = cf.get(score_fid)
        try:
            return score is not None and float(score) >= threshold
        except (TypeError, ValueError):
            return False

    def fetch_reocr_queue(self, threshold, limit=0):
        """Documents eligible for re-OCR (client-side filtered, plan §4.2)."""
        docs = []
        fields = (
            "id,title,correspondent,document_type,tags,created,"
            "archive_serial_number,custom_fields"
        )
        for d in self._iter(fields):
            if self.reocr_matches(d, threshold):
                docs.append(d)
                if limit and len(docs) >= limit:
                    return docs
        return docs

    # ---- Stage 2 (metadata) ----------------------------------------------
    def _eligible_labels(self):
        if self.settings is not None:
            return self.settings.eligible_stage_labels()
        return config.ELIGIBLE_STAGES

    def _garbage_threshold(self):
        if self.settings is not None:
            return self.settings.garbage_threshold
        return config.GARBAGE_THRESH

    def metadata_eligible(self, doc):
        """Eligible = ai_stage label in the configured eligible set AND
        ocr_quality < garbage threshold."""
        r = self.resolver
        cf = {c["field"]: c.get("value") for c in (doc.get("custom_fields") or [])}
        stage_val = cf.get(r.stage_field_id())
        stage_label = r.stage_label_from_value(stage_val)
        if stage_label not in self._eligible_labels():
            return False
        score = cf.get(r.score_field_id())
        thresh = self._garbage_threshold()
        try:
            if score is not None and float(score) >= thresh:
                return False  # one of the garbage docs - skip
        except (TypeError, ValueError):
            pass
        return True

    def fetch_metadata_queue(self, limit=0):
        docs = []
        fields = (
            "id,title,content,correspondent,document_type,tags,custom_fields"
        )
        for d in self._iter(fields):
            if self.metadata_eligible(d):
                docs.append(d)
                if limit and len(docs) >= limit:
                    return docs
        return docs
