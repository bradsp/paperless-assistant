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


class PaperlessClient:
    def __init__(self, base_url: str, token: str, session: requests.Session | None = None,
                 *, http=None):
        self.base = base_url.rstrip("/")
        self.token = token
        # Prompt 011: HTTP timeouts / pagination / retry-backoff come from config
        # (`http`). None -> byte-identical defaults (HttpSettings() == the former
        # hardcoded values), so existing callers/tests are unchanged.
        self.http = http or config.HttpSettings()
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Authorization": f"Token {token}", "Accept": "application/json"}
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
            return r
        raise requests.HTTPError(f"exhausted retries on {method} {url}")

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
        return r.json()  # task UUID (string)

    def find_new_doc_by_task(self, task_uuid, timeout=None):
        """Poll the tasks endpoint until the consume task finishes; return the
        new document id. Prompt 011: the poll timeout + interval come from config
        (defaults byte-identical: 180s timeout, 3s interval)."""
        if timeout is None:
            timeout = self.http.task_poll_timeout
        interval = self.http.task_poll_interval
        t0 = time.time()
        while time.time() - t0 < timeout:
            r = self.request("GET", f"{self.base}/api/tasks/?task_id={task_uuid}")
            results = r.json()
            if results:
                task = results[0] if isinstance(results, list) else results
                status = task.get("status")
                if status == "SUCCESS":
                    doc_id = task.get("related_document") or task.get("result")
                    return doc_id
                if status in ("FAILURE", "REVOKED"):
                    raise RuntimeError(f"consume task failed: {task}")
            time.sleep(interval)
        raise TimeoutError(f"consume task {task_uuid} did not finish in {timeout}s")
