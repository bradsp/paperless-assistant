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

"""CustomFieldResolver - custom-field name->id + select option label<->id.

Extracted from: `get_field_map`, `stage_value`, `stage_option_id`,
`_coerce_score` (duplicated across all three scripts). This is one of the four
helpers every script re-implemented; here it has a single home.

Prompt 011: the custom-field NAMES (`ocr_quality`/`ai_stage`/`ai_notes`) and the
`ai_stage` select-option LABELS (`triaged`/`reocr_done`/`metadata_done`) are now
CONFIGURABLE. The resolver holds the configured names (defaulting to the module
constants, so an unconfigured install is byte-identical) and exposes ROLE-based
accessors (`score_field_id()`, `stage_option_for_role("triaged")`, …) so every
consumer resolves the configured name instead of reading `config.FIELD_SCORE`.
The label-based methods (`stage_option_id`/`stage_label_from_value`) are preserved
verbatim for the frozen characterization tests.
"""
from __future__ import annotations

from . import config


class CustomFieldResolver:
    def __init__(self, client, required=None, *, field_names=None, stage_names=None):
        self.client = client
        # Configured names (prompt 011); default to the constants -> byte-identical.
        self.field_names = field_names or config.FieldNames()
        self.stage_names = stage_names or config.StageNames()
        if required is None:
            required = (
                self.field_names.score,
                self.field_names.stage,
                self.field_names.notes,
            )
        self.fmap = self._build_field_map(required)

    def _build_field_map(self, required):
        """Resolve custom-field names -> ids. For select fields, also capture the
        option label->id mapping, since select values must be written as option
        ids."""
        fields = {}
        for f in self.client.get_all("custom_fields"):
            entry = {"id": f["id"], "data_type": f["data_type"]}
            if f["data_type"] == "select":
                opts = (f.get("extra_data") or {}).get("select_options") or []
                entry["options"] = {o["label"]: o["id"] for o in opts}
            fields[f["name"]] = entry
        missing = [n for n in required if n not in fields]
        if missing:
            raise SystemExit(
                f"Missing custom fields in Paperless: {missing}. "
                f"Create them under Settings > Custom Fields first."
            )
        return fields

    def __getitem__(self, name):
        return self.fmap[name]

    def field_id(self, name):
        return self.fmap[name]["id"]

    def data_type(self, name):
        return self.fmap[name]["data_type"]

    # -- role-based accessors (prompt 011) ---------------------------------
    # Consumers resolve by ROLE so the configured name is always used, never the
    # module constant. Defaults keep behaviour byte-identical.
    def score_field_id(self):
        return self.field_id(self.field_names.score)

    def stage_field_id(self):
        return self.field_id(self.field_names.stage)

    def notes_field_id(self):
        return self.field_id(self.field_names.notes)

    def score_data_type(self):
        return self.data_type(self.field_names.score)

    def stage_option_for_role(self, role):
        """Option id for a state-machine ROLE (triaged/reocr_done/metadata_done),
        resolved via the configured label for that role."""
        return self.stage_option_id(self._label_for_role(role))

    def role_for_value(self, value):
        """Map a stored ai_stage value back to its state-machine ROLE
        (triaged/reocr_done/metadata_done), or None. The inverse of
        stage_option_for_role."""
        label = self.stage_label_from_value(value)
        return self._role_for_label(label)

    def stage_order_labels(self):
        """The configured labels for the ordered state machine (STAGE_ORDER)."""
        return self.stage_names.order()

    def _label_for_role(self, role):
        return {
            config.STAGE_TRIAGED: self.stage_names.triaged,
            config.STAGE_REOCR_DONE: self.stage_names.reocr_done,
            config.STAGE_METADATA_DONE: self.stage_names.metadata_done,
        }[role]

    def _role_for_label(self, label):
        return {
            self.stage_names.triaged: config.STAGE_TRIAGED,
            self.stage_names.reocr_done: config.STAGE_REOCR_DONE,
            self.stage_names.metadata_done: config.STAGE_METADATA_DONE,
        }.get(label)

    # -- select option resolution (label-based; preserved verbatim) --------
    def stage_option_id(self, label):
        """ai_stage select-option id for `label`; passthrough for text fields."""
        f = self.fmap[self.field_names.stage]
        if f["data_type"] == "select":
            if label not in f["options"]:
                raise SystemExit(
                    f"{self.field_names.stage} has no option '{label}'. "
                    f"Options: {list(f['options'])}"
                )
            return f["options"][label]
        return label

    def stage_label_from_value(self, value):
        """Map a stored ai_stage value back to its label (select) or itself."""
        f = self.fmap[self.field_names.stage]
        if f["data_type"] == "select":
            id_to_label = {v: k for k, v in f["options"].items()}
            return id_to_label.get(value)
        return value

    # -- value coercion -----------------------------------------------------
    @staticmethod
    def coerce_score(score, data_type):
        """Format the float score to whatever the ocr_quality field expects."""
        if data_type in ("float", "monetary"):
            return float(score)
        if data_type == "integer":
            # No float field available -> store as 0-100 integer percentage.
            return int(round(score * 100))
        if data_type in ("string", "text", "url"):
            return str(score)
        return float(score)
