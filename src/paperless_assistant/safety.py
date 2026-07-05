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

"""SafetyLayer - snapshot-before-write (I2), merge-not-clobber (I4/I5),
review-gate tagging + restore.

Extracted from: `snapshot` (all three), `merge_custom_fields` (stage0),
`mark_old_superseded` (stage1), and the tag-merge logic in `apply_metadata`
(stage2, exercised via metadata.py).

Snapshot semantics preserved exactly: written ONCE per document id; an existing
snapshot is never overwritten (it is the original-state rollback record).
"""
from __future__ import annotations

import json
import pathlib

from . import config


class SafetyLayer:
    def __init__(self, client, resolver, snapshot_dir):
        self.client = client
        self.resolver = resolver
        self.snapshot_dir = pathlib.Path(snapshot_dir)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    # -- I2: snapshot-before-write -----------------------------------------
    def snapshot_path(self, doc_id):
        return self.snapshot_dir / f"{doc_id}.json"

    def snapshot(self, doc):
        """Write the document's original state to disk exactly once. Never
        overwrites an existing snapshot."""
        path = self.snapshot_path(doc["id"])
        if not path.exists():
            path.write_text(json.dumps(doc, ensure_ascii=False, indent=2))

    def load_snapshot(self, doc_id):
        path = self.snapshot_path(doc_id)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def restore(self, doc_id):
        """Replay a snapshot's title/tags/correspondent/type/custom_fields back
        onto the document (I2/I4 reversibility). Returns the PATCH body sent."""
        snap = self.load_snapshot(doc_id)
        if snap is None:
            raise FileNotFoundError(f"no snapshot for doc {doc_id}")
        body = {}
        for key in ("title", "correspondent", "document_type", "tags", "created",
                    "archive_serial_number", "custom_fields"):
            if key in snap:
                body[key] = snap[key]
        self.client.request(
            "PATCH", f"{self.client.base}/api/documents/{doc_id}/", json=body
        )
        return body

    # -- I4/I5: merge-not-clobber triage custom fields (stage0) ------------
    def merge_triage_fields(self, existing, score, note):
        """Build the custom_fields array preserving foreign fields, updating the
        three triage fields (ocr_quality / ai_stage / ai_notes)."""
        r = self.resolver
        target_ids = {
            r.score_field_id(),
            r.stage_field_id(),
            r.notes_field_id(),
        }
        kept = [cf for cf in (existing or []) if cf["field"] not in target_ids]
        kept.extend(
            [
                {
                    "field": r.score_field_id(),
                    "value": r.coerce_score(score, r.score_data_type()),
                },
                {
                    "field": r.stage_field_id(),
                    "value": r.stage_option_for_role(config.STAGE_TRIAGED),
                },
                {"field": r.notes_field_id(), "value": note[:255]},
            ]
        )
        return kept

    # -- I4: supersede the old doc (never delete) --------------------------
    def mark_old_superseded(self, old_doc, superseded_tag_id):
        """Add 'superseded' tag to the old doc and advance its ai_stage so it
        leaves the queue. Does NOT delete - that's left to the user."""
        r = self.resolver
        existing_tags = list(old_doc.get("tags") or [])
        if superseded_tag_id not in existing_tags:
            existing_tags.append(superseded_tag_id)
        keep = [
            cf
            for cf in (old_doc.get("custom_fields") or [])
            if cf["field"] != r.stage_field_id()
        ]
        keep.append(
            {
                "field": r.stage_field_id(),
                "value": r.stage_option_for_role(config.STAGE_REOCR_DONE),
            }
        )
        self.client.request(
            "PATCH",
            f"{self.client.base}/api/documents/{old_doc['id']}/",
            json={"tags": existing_tags, "custom_fields": keep},
        )
