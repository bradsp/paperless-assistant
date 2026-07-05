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

"""Provisioner - `pa setup` idempotently ensures the Paperless prerequisites.

Today the three custom fields (`ocr_quality`, `ai_stage`, `ai_notes`) and the two
review tags (`superseded`, `ai-new-taxonomy`) are MANUAL prerequisites the scripts
assume already exist (see CustomFieldResolver's SystemExit). Phase 3 makes the
product create them (plan §8.2 step 2).

Guarantees (plan §8.2, I1/I7):
  * create-if-missing, NEVER duplicate;
  * a second run is a verified no-op (nothing created, no errors);
  * an existing field with an INCOMPATIBLE data_type is REPORTED, never clobbered
    (we don't mutate/delete a field a human may already be using).

The module named `setup.py` is intentionally AVOIDED (setuptools/pip collision on
some toolchains); this file is `provision.py` and the CLI subcommand is `setup`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config


# The exact required custom fields (plan §8.2). `ai_stage` is a select with the
# state-machine options the orchestrator relies on. Prompt 011: `required_fields`
# builds this spec from the CONFIGURED names/labels so `pa setup` / `pa doctor`
# provision + check the user's chosen names. `REQUIRED_FIELDS` remains the default
# spec (byte-identical) for callers that pass nothing.
def required_fields(field_names=None, stage_names=None) -> dict:
    fn = field_names or config.FieldNames()
    sn = stage_names or config.StageNames()
    return {
        fn.score: {
            "data_type": "float",  # Number/float so the UI can filter with >=
            "role": "score",
        },
        fn.stage: {
            "data_type": "select",
            "options": [sn.triaged, sn.reocr_done, sn.metadata_done],
            "role": "stage",
        },
        fn.notes: {
            "data_type": "string",  # Paperless-NGX's text type is "string" (not "text")
            "role": "notes",
        },
    }


REQUIRED_FIELDS = required_fields()


@dataclass
class ProvisionReport:
    """Outcome of a `pa setup` run. `ok` is False if anything is incompatible."""

    created_fields: list[str] = field(default_factory=list)
    existing_fields: list[str] = field(default_factory=list)
    created_tags: list[str] = field(default_factory=list)
    existing_tags: list[str] = field(default_factory=list)
    incompatible: list[str] = field(default_factory=list)  # human-readable messages

    @property
    def ok(self) -> bool:
        return not self.incompatible

    @property
    def is_noop(self) -> bool:
        return not self.created_fields and not self.created_tags and self.ok

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "noop": self.is_noop,
            "created_fields": self.created_fields,
            "existing_fields": self.existing_fields,
            "created_tags": self.created_tags,
            "existing_tags": self.existing_tags,
            "incompatible": self.incompatible,
        }


class Provisioner:
    """Idempotent Paperless prerequisite provisioner (plan §8.2 step 2)."""

    def __init__(self, client, *, superseded_tag=None, new_taxonomy_tag=None,
                 field_names=None, stage_names=None,
                 superseded_tag_color="#a0a0a0", new_taxonomy_tag_color="#f59e0b"):
        self.client = client
        self.superseded_tag = superseded_tag or config.SUPERSEDED_TAG
        self.new_taxonomy_tag = new_taxonomy_tag or config.NEW_TAXONOMY_TAG
        # Prompt 011: configurable field/stage NAMES + tag colors (defaults
        # byte-identical). `pa setup` provisions the CONFIGURED names.
        self.field_names = field_names or config.FieldNames()
        self.stage_names = stage_names or config.StageNames()
        self.required = required_fields(self.field_names, self.stage_names)
        self.superseded_tag_color = superseded_tag_color
        self.new_taxonomy_tag_color = new_taxonomy_tag_color

    # -- custom fields -----------------------------------------------------
    def _existing_fields(self):
        return {f["name"]: f for f in self.client.get_all("custom_fields")}

    def _create_field(self, name, spec):
        payload = {"name": name, "data_type": spec["data_type"]}
        if spec["data_type"] == "select":
            payload["extra_data"] = {
                "select_options": [{"label": o} for o in spec["options"]]
            }
        self.client.request(
            "POST", f"{self.client.base}/api/custom_fields/", json=payload
        )

    def _check_select_options(self, existing_field, wanted_options):
        """Return a list of missing option labels for an existing select field."""
        opts = (existing_field.get("extra_data") or {}).get("select_options") or []
        have = {o.get("label") for o in opts}
        return [o for o in wanted_options if o not in have]

    def ensure_fields(self, report: ProvisionReport):
        existing = self._existing_fields()
        for name, spec in self.required.items():
            if name not in existing:
                self._create_field(name, spec)
                report.created_fields.append(name)
                continue
            # Exists: verify compatibility, never clobber (I4/I7).
            have_type = existing[name].get("data_type")
            want_type = spec["data_type"]
            compatible = have_type == want_type
            if spec.get("role") == "score" and have_type in ("float", "integer", "monetary"):
                # ocr_quality tolerates any numeric type (coerce_score handles it).
                compatible = True
            if not compatible:
                report.incompatible.append(
                    f"custom field '{name}' exists with data_type='{have_type}' but "
                    f"'{want_type}' is required. Not modified. Rename or delete the "
                    f"existing field in the Paperless UI, then re-run `pa setup`."
                )
                continue
            if want_type == "select":
                missing = self._check_select_options(existing[name], spec["options"])
                if missing:
                    report.incompatible.append(
                        f"select field '{name}' is missing option(s) {missing}. "
                        f"Add them under Settings > Custom Fields, then re-run "
                        f"`pa setup` (existing options were left untouched)."
                    )
                    continue
            report.existing_fields.append(name)

    # -- review-gate tags --------------------------------------------------
    def _tag_exists(self, name):
        data = self.client.request(
            "GET", f"{self.client.base}/api/tags/?name__iexact={name}"
        ).json()
        return bool(data.get("results"))

    def _create_tag(self, name, color):
        self.client.request(
            "POST", f"{self.client.base}/api/tags/", json={"name": name, "color": color}
        )

    def ensure_tags(self, report: ProvisionReport):
        for name, color in ((self.superseded_tag, self.superseded_tag_color),
                            (self.new_taxonomy_tag, self.new_taxonomy_tag_color)):
            if self._tag_exists(name):
                report.existing_tags.append(name)
            else:
                self._create_tag(name, color)
                report.created_tags.append(name)

    def run(self) -> ProvisionReport:
        report = ProvisionReport()
        self.ensure_fields(report)
        self.ensure_tags(report)
        return report
