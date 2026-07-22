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

"""PaperlessClient - the single Paperless REST integration surface.

Extracted from: `_request`, `iter_documents`, `get_all`, `download_original`,
`post_document`, `find_new_doc_by_task` (all three scripts). De-duplicates the
`_request` retry/backoff helper that every script re-implemented.

Design (plan §4.2): the REST API is the SOLE integration surface - no DB access,
no consume-dir filesystem coupling. Surfaces the server's real error (I6).
Retry/backoff on 429/5xx (I7).
"""
from __future__ import annotations

import time

import requests

from . import config

# --- Paperless API versioning (v2 + v3-beta compatibility) -----------------
# Paperless-NGX negotiates its REST response shapes through an `Accept:
# application/json; version=N` parameter. A version-less request gets the
# server's DEFAULT, which changed from 9 (v2.x) to 10 (v3-beta) — and the v10
# `/api/tasks/` shape (paginated + renamed fields) breaks post-and-poll upload
# discovery. Pinning version=9 is accepted by BOTH v2 (`["1".."9"]`) and v3
# (`["9","10"]`) and makes every surface this client uses behave identically on
# either server, with no per-request branching. See
# analyses/paperless-v3-api-migration.md.
PINNED_API_VERSION = "9"
ACCEPT_HEADER = f"application/json; version={PINNED_API_VERSION}"
# The known-good fallback generation when a server does not advertise its API
# level (old instance, missing header, network hiccup): treat it as v2/API v9,
# the currently-shipping tested path. Never fail closed, never assume v3.
DEFAULT_API_VERSION = 9
# First API version served by the v3 line; >= this means a v3-generation server.
V3_MIN_API_VERSION = 10


class PaperlessClient:
    def __init__(self, base_url: str, token: str, session: requests.Session | None = None,
                 *, http=None, logger=None):
        self.base = base_url.rstrip("/")
        self.token = token
        # Prompt 011: HTTP timeouts / pagination / retry-backoff come from config
        # (`http`). None -> byte-identical defaults (HttpSettings() == the former
        # hardcoded values), so existing callers/tests are unchanged.
        self.http = http or config.HttpSettings()
        # Optional JsonLogger for observability (version-detection event). None
        # keeps the client silent (existing callers/tests unchanged).
        self.logger = logger
        # Auto-detected Paperless API version, cached once per session (never
        # recomputed per request). None until the first authenticated response
        # advertising `X-Api-Version` is seen; falls back to DEFAULT_API_VERSION.
        self._api_version: int | None = None
        self._server_version: str | None = None
        self._fallback_logged = False
        self.session = session or requests.Session()
        # Pin the request API version (version=9) so v2 and v3-beta servers both
        # return the v9-shaped responses this client parses.
        self.session.headers.update(
            {"Authorization": f"Token {token}", "Accept": ACCEPT_HEADER}
        )

    # -- HTTP with retry/backoff (handles 429 + transient 5xx). I6 + I7. -----
    def request(self, method, url, *, timeout=None, **kw):
        """Retry/backoff request. Surfaces the server's actual validation
        message on 4xx instead of a bare HTTPError (I6). Prompt 011: the default
        timeout + retry count + backoff bounds come from config (defaults byte-
        identical: timeout=90, retries=6, initial backoff 1.0s, cap 30s)."""
        if timeout is None:
            timeout = self.http.request_timeout
        delay = self.http.backoff_initial
        for _ in range(self.http.retries):
            r = self.session.request(method, url, timeout=timeout, **kw)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay = min(delay * 2, self.http.backoff_cap)
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except Exception:
                    detail = r.text[:500]
                raise requests.HTTPError(
                    f"{r.status_code} on {method} {url}\n  server says: {detail}",
                    response=r,
                )
            self._note_version(r)
            return r
        raise requests.HTTPError(f"exhausted retries on {method} {url}")

    # -- Version detection (auto, cached once per session) ------------------
    @property
    def api_version(self) -> int:
        """The detected Paperless API version, or DEFAULT_API_VERSION (9) if the
        server never advertised one. Reflects the CACHED detection — it does not
        trigger a probe."""
        return self._api_version if self._api_version is not None else DEFAULT_API_VERSION

    @property
    def server_version(self) -> str | None:
        """The server's release string (`X-Version` header), if seen."""
        return self._server_version

    @property
    def is_v3(self) -> bool:
        """True when the connected server is a v3-generation instance (API >= 10)."""
        return self.api_version >= V3_MIN_API_VERSION

    @property
    def server_generation(self) -> str:
        """'v3' or 'v2' — the human-facing Paperless generation label."""
        return "v3" if self.is_v3 else "v2"

    def _note_version(self, response) -> None:
        """Detect + cache the server's API generation from an authenticated
        response's `X-Api-Version` / `X-Version` headers. Runs at most once
        effectively: the version is cached and never recomputed per request. If a
        response carries no usable header (old instance / unauthenticated path /
        network quirk), the cache stays empty so a later response can still
        detect, and the known-good v2 fallback is logged exactly once — we default
        to v2/API v9 rather than fail closed or silently assume v3."""
        if self._api_version is not None:
            return
        raw = response.headers.get("X-Api-Version")
        detected = None
        if raw is not None:
            try:
                detected = int(str(raw).strip())
            except (TypeError, ValueError):
                detected = None
        if detected is not None:
            self._api_version = detected
            self._server_version = response.headers.get("X-Version")
            self._log_version(detected=True)
            return
        if not self._fallback_logged:
            self._fallback_logged = True
            self._log_version(detected=False)

    def _log_version(self, *, detected: bool) -> None:
        """Emit the observability event recording the active API generation and
        whether it was auto-detected from headers or defaulted. Best-effort:
        never let logging break a request."""
        if self.logger is None:
            return
        version = self._api_version if self._api_version is not None else DEFAULT_API_VERSION
        generation = "v3" if version >= V3_MIN_API_VERSION else "v2"
        try:
            self.logger.event(
                "paperless_version_detected",
                api_version=version,
                server_version=self._server_version,
                generation=generation,
                pinned_version=PINNED_API_VERSION,
                detected=bool(detected),
            )
        except Exception:
            pass

    # -- Pagination helpers -------------------------------------------------
    def iter_documents(self, fields, page_size=None):
        """Yield documents (paginated) requesting only the given `fields`. Prompt
        011: `page_size` defaults to the configured value (byte-identical: 100)."""
        if page_size is None:
            page_size = self.http.page_size
        url = f"{self.base}/api/documents/?fields={fields}&page_size={page_size}"
        while url:
            data = self.request("GET", url).json()
            for d in data["results"]:
                yield d
            url = data.get("next")

    def get_document(self, doc_id, fields=None):
        """Fetch a single document by id (Phase 4 webhook nudge PULLS the doc via
        REST — the nudge only carries the id, never content). Returns the document
        dict, or None if Paperless reports it does not exist (404)."""
        url = f"{self.base}/api/documents/{int(doc_id)}/"
        if fields:
            url += f"?fields={fields}"
        try:
            return self.request("GET", url).json()
        except requests.HTTPError as e:
            if getattr(e, "response", None) is not None and e.response.status_code == 404:
                return None
            raise

    def get_all(self, endpoint, fields=None):
        """Fetch every page of a list endpoint into a flat list."""
        out = []
        url = f"{self.base}/api/{endpoint}/?page_size=200" + (
            f"&fields={fields}" if fields else ""
        )
        while url:
            data = self.request("GET", url).json()
            out.extend(data["results"])
            url = data.get("next")
        return out

    # -- File I/O over the API ---------------------------------------------
    def download_original(self, doc_id):
        """Original source file (not the archived render)."""
        r = self.request(
            "GET",
            f"{self.base}/api/documents/{doc_id}/download/?original=true",
            timeout=self.http.download_timeout,
        )
        return r.content

    def post_document(self, pdf_bytes, doc, filename):
        """Upload a corrected PDF carrying core metadata. Returns the consume
        task UUID. Mirrors stage1's post_document exactly (including the
        deliberate ASN omission)."""
        data = {}
        if doc.get("title"):
            data["title"] = doc["title"]
        if doc.get("correspondent"):
            data["correspondent"] = str(doc["correspondent"])
        if doc.get("document_type"):
            data["document_type"] = str(doc["document_type"])
        if doc.get("created"):
            data["created"] = doc["created"]
        if doc.get("archive_serial_number"):
            # ASN must be unique; only carry it if you intend to free it from the
            # old doc first. Safer to leave ASN off and re-apply after deletion.
            pass
        files = {"document": (filename, pdf_bytes, "application/pdf")}
        tag_fields = [("tags", str(t)) for t in (doc.get("tags") or [])]
        r = self.session.post(
            f"{self.base}/api/documents/post_document/",
            files=files,
            data=list(data.items()) + tag_fields,
            timeout=self.http.post_document_timeout,
        )
        if r.status_code >= 400:
            raise requests.HTTPError(f"post_document {r.status_code}: {r.text[:500]}")
        self._note_version(r)
        return r.json()  # task UUID (string)

    @staticmethod
    def _task_document_id(task):
        """Extract the consumed document id from a task record, tolerating BOTH
        the v9 shape (`related_document` int, `result` string) and the v10 shape
        (`related_document_ids` list, `result_data.document_id`). The v9 fields
        are tried first, so on a v9-pinned response the result is bit-identical to
        the pre-v3 `related_document or result` behavior."""
        doc_id = task.get("related_document")
        if doc_id:
            return doc_id
        ids = task.get("related_document_ids")
        if isinstance(ids, (list, tuple)) and ids:
            return ids[0]
        result_data = task.get("result_data")
        if isinstance(result_data, dict) and result_data.get("document_id"):
            return result_data["document_id"]
        return task.get("result")

    def find_new_doc_by_task(self, task_uuid, timeout=None):
        """Poll the tasks endpoint until the consume task finishes; return the
        new document id. Prompt 011: the poll timeout + interval come from config
        (defaults byte-identical: 180s timeout, 3s interval).

        Tolerates both task-list shapes: the v9 bare list (the default our
        `version=9` Accept pin restores on v2 AND v3) and the v10 paginated dict
        (`{results: [...]}`), so it keeps working if v9 is ever dropped from a
        future v3."""
        if timeout is None:
            timeout = self.http.task_poll_timeout
        interval = self.http.task_poll_interval
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.request("GET", f"{self.base}/api/tasks/?task_id={task_uuid}")
            payload = r.json()
            # v9 (pinned/default): bare list. v10: paginated {count,next,results,…}.
            results = payload["results"] if isinstance(payload, dict) and "results" in payload else payload
            if results:
                task = results[0] if isinstance(results, list) else results
                status = task.get("status")
                if status == "SUCCESS":
                    return self._task_document_id(task)
                if status in ("FAILURE", "REVOKED"):
                    raise RuntimeError(f"consume task failed: {task}")
            time.sleep(interval)
        raise TimeoutError(f"consume task {task_uuid} did not finish in {timeout}s")
