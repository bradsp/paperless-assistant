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

"""TaxonomyResolver - case-insensitive name<->id for tags / correspondents /
document_types with thread-safe lazy create-if-missing (I5).

Extracted from: the `Taxonomy` class, `get_or_create_tag`,
`get_or_create_superseded_tag` (stage2 + stage1). Reuse-first is the default;
`*_id` returns (id, was_newly_created) so callers can flag new taxonomy (I4/I5).
"""
from __future__ import annotations

import threading

from . import config


class TaxonomyResolver:
    def __init__(self, client):
        self.client = client
        self.tags = {t["name"]: t["id"] for t in client.get_all("tags", "id,name")}
        self.correspondents = {
            c["name"]: c["id"] for c in client.get_all("correspondents", "id,name")
        }
        self.doc_types = {
            d["name"]: d["id"] for d in client.get_all("document_types", "id,name")
        }
        # case-insensitive lookup helpers
        self._tags_ci = {k.lower(): v for k, v in self.tags.items()}
        self._corr_ci = {k.lower(): v for k, v in self.correspondents.items()}
        self._type_ci = {k.lower(): v for k, v in self.doc_types.items()}
        # Lock for lazy taxonomy creation so two threads don't create the same.
        self._lock = threading.Lock()

    def existing_lists(self):
        return (sorted(self.tags), sorted(self.correspondents), sorted(self.doc_types))

    def _resolve_or_create(self, name, ci_map, name_map, endpoint):
        if not name:
            return None, False
        key = name.strip().lower()
        if key in ci_map:
            return ci_map[key], False
        with self._lock:
            if key in ci_map:  # re-check inside lock
                return ci_map[key], False
            payload = {"name": name.strip()}
            if endpoint == "tags":
                payload["color"] = "#3b82f6"
            r = self.client.request("POST", f"{self.client.base}/api/{endpoint}/", json=payload)
            new_id = r.json()["id"]
            name_map[name.strip()] = new_id
            ci_map[key] = new_id
            return new_id, True

    def tag_id(self, name):
        return self._resolve_or_create(name, self._tags_ci, self.tags, "tags")

    def correspondent_id(self, name):
        return self._resolve_or_create(
            name, self._corr_ci, self.correspondents, "correspondents"
        )

    def doc_type_id(self, name):
        return self._resolve_or_create(name, self._type_ci, self.doc_types, "document_types")

    # -- standalone review-gate tags (superseded / ai-new-taxonomy) ---------
    def get_or_create_tag(self, name, color="#a0a0a0"):
        data = self.client.request(
            "GET", f"{self.client.base}/api/tags/?name__iexact={name}"
        ).json()
        if data["results"]:
            return data["results"][0]["id"]
        return self.client.request(
            "POST", f"{self.client.base}/api/tags/", json={"name": name, "color": color}
        ).json()["id"]

    def get_or_create_superseded_tag(self):
        return self.get_or_create_tag(config.SUPERSEDED_TAG, color="#a0a0a0")
