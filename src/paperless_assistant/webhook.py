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

"""On-ingest webhook NUDGE receiver (Phase 4, plan §6.2, §8.1).

A small standard-library HTTP listener that runs alongside the scheduled sweep in
`pa serve`. Paperless' **Workflow -> Webhook** action (configured on "Document
Consumption Finished" / "Document Updated", NOT "Document Added") POSTs a THIN
payload to the agent's IN-NETWORK address. The reliable placeholder is
``{doc_url}``; from it the receiver extracts the integer document id, then PULLS
the document via the Paperless REST API and runs it through the SAME idempotent
single-doc pipeline as the sweep (`Sweep.process_nudge`).

Design guarantees this module upholds:
  * **Nudge, not a data channel.** The webhook carries only an id; content is
    never trusted from the request — the agent always pulls via REST.
  * **Trust boundary (plan §8.1).** The listener binds inside the compose network
    so Paperless reaches it by service name (e.g. http://paperless-assistant:8765).
    It publishes NO host port (no compose `ports:` mapping). It is NOT an external
    exposure of the agent or Paperless.
  * **Authenticated.** Every nudge must carry the shared secret (env only). An
    unauthenticated / malformed / non-integer nudge is rejected with a 4xx and a
    logged reason, and does nothing (I6 spirit).
  * **Debounced + restart-safe.** A persisted queue under /data dedups rapid
    duplicate nudges and remembers processed doc ids across restarts, so a crash
    mid-queue resumes WITHOUT reprocessing already-done docs (idempotency, I1,
    makes this safe).
  * **Sweep stays authoritative.** The webhook is a latency optimisation; if it is
    disabled or a nudge is lost, the scheduled sweep still catches the doc.

No heavy web framework is used — only the Python standard library.
"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# doc id from a Paperless {doc_url} like ".../documents/123/" or ".../documents/123".
_DOC_ID_RE = re.compile(r"/documents/(\d+)(?:/|$|\?)")


def parse_doc_id(payload: dict) -> int | None:
    """Extract the integer document id from a thin Paperless webhook payload.

    Accepts the reliable ``doc_url`` placeholder (a full URL ending in
    ``/documents/<id>/``) and, defensively, a bare ``doc_id``/``id`` field or a
    plain-integer ``doc_url``. Returns an int, or None if nothing parses to a
    positive integer. The nudge is UNTRUSTED input — anything that is not a clean
    positive integer id yields None (caller rejects with a 4xx)."""
    if not isinstance(payload, dict):
        return None
    # Prefer doc_url (the documented, reliable placeholder).
    url = payload.get("doc_url")
    if isinstance(url, str):
        m = _DOC_ID_RE.search(url)
        if m:
            return int(m.group(1))
        if url.strip().isdigit():
            return int(url.strip())
    # Defensive fallbacks (some workflow configs pass the id directly).
    for key in ("doc_id", "document_id", "id"):
        v = payload.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


class NudgeQueue:
    """A restart-safe, deduplicating nudge work queue persisted under /data.

    State (one JSON file):
      * ``pending``:  ordered list of doc ids awaiting processing.
      * ``done``:     doc ids already fully processed (so a replayed/duplicate
                      nudge after a restart is a cheap no-op, not a re-spend).
      * ``last_seen``: {doc_id: monotonic-ish wall ts} for in-window debounce.

    The file is the source of truth ACROSS process restarts; an in-process lock
    serialises concurrent handler threads. `enqueue` returns False when the nudge
    is debounced/duplicate (so the caller can answer 200 without doing work).
    """

    def __init__(self, path, *, debounce_seconds: float = 30.0, now=time.time):
        self.path = pathlib.Path(path)
        self.debounce_seconds = float(debounce_seconds)
        self._now = now
        self._lock = threading.Lock()

    # -- persistence -------------------------------------------------------
    def _read(self) -> dict:
        if not self.path.exists():
            return {"pending": [], "done": [], "last_seen": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"pending": [], "done": [], "last_seen": {}}
        data.setdefault("pending", [])
        data.setdefault("done", [])
        data.setdefault("last_seen", {})
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic-ish replace so a crash mid-write can't corrupt the queue.
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # -- API ---------------------------------------------------------------
    def enqueue(self, doc_id: int) -> bool:
        """Record a nudge for `doc_id`. Returns True if it was newly queued, False
        if it was debounced (a recent duplicate) — duplicates are safe no-ops."""
        doc_id = int(doc_id)
        with self._lock:
            data = self._read()
            key = str(doc_id)
            now = self._now()
            last = data["last_seen"].get(key)
            data["last_seen"][key] = now
            # Debounce rapid duplicates within the window.
            if last is not None and (now - float(last)) < self.debounce_seconds:
                self._write(data)
                return False
            if doc_id not in data["pending"]:
                data["pending"].append(doc_id)
            self._write(data)
            return True

    def next_pending(self) -> int | None:
        """Return the next pending doc id WITHOUT removing it (so a crash before
        completion leaves it pending to resume). None if the queue is empty."""
        with self._lock:
            data = self._read()
            return data["pending"][0] if data["pending"] else None

    def mark_done(self, doc_id: int) -> None:
        """Move `doc_id` from pending to done (processing succeeded / was a no-op)."""
        doc_id = int(doc_id)
        with self._lock:
            data = self._read()
            data["pending"] = [d for d in data["pending"] if d != doc_id]
            if doc_id not in data["done"]:
                data["done"].append(doc_id)
            self._write(data)

    def is_done(self, doc_id: int) -> bool:
        with self._lock:
            return int(doc_id) in self._read().get("done", [])

    def pending_ids(self) -> list[int]:
        with self._lock:
            return list(self._read().get("pending", []))

    def depth(self) -> int:
        return len(self.pending_ids())


class NudgeProcessor:
    """Drains the persisted queue by processing each pending doc through the
    single-doc pipeline, then marking it done. Restart-safe: on startup it resumes
    any docs still `pending` (a crash mid-queue loses nothing, reprocesses nothing
    that was marked done)."""

    def __init__(self, queue: NudgeQueue, process_fn, *, logger=None):
        self.queue = queue
        self.process_fn = process_fn  # callable(doc_id) -> report (Sweep.process_nudge)
        self.logger = logger

    def drain(self) -> int:
        """Process all currently-pending docs. Returns the count processed."""
        n = 0
        while True:
            doc_id = self.queue.next_pending()
            if doc_id is None:
                break
            try:
                self.process_fn(doc_id)
            except Exception as e:  # surface the real error (I6); keep draining.
                if self.logger:
                    self.logger.event("nudge_error", level="error",
                                      doc_id=doc_id, error=str(e))
            # Mark done regardless: processing is idempotent, and a persistent
            # failure must not wedge the queue forever (the sweep is the backstop).
            self.queue.mark_done(doc_id)
            n += 1
        return n


def make_handler(settings, queue: NudgeProcessor, *, logger=None, secret: str,
                 path: str):
    """Build a BaseHTTPRequestHandler class bound to this receiver's state."""

    class _Handler(BaseHTTPRequestHandler):
        # Silence the default stderr access log; we emit structured logs instead.
        def log_message(self, fmt, *args):  # noqa: N802 (stdlib signature)
            return

        def _reply(self, code, body):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _authenticated(self) -> bool:
            """Nudge auth: the shared secret may arrive as a bearer/token header,
            a custom X-PA-Webhook-Secret header, or a ?token= query param. Env-only
            secret; constant-time-ish compare."""
            provided = None
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
            elif auth.lower().startswith("token "):
                provided = auth[6:].strip()
            if provided is None:
                provided = self.headers.get("X-PA-Webhook-Secret")
            if provided is None:
                qs = parse_qs(urlparse(self.path).query)
                vals = qs.get("token") or qs.get("secret")
                provided = vals[0] if vals else None
            if not provided or not secret:
                return False
            # length-independent equality without leaking length via ==.
            return _consteq(provided, secret)

        def do_POST(self):  # noqa: N802 (stdlib signature)
            url_path = urlparse(self.path).path
            if url_path.rstrip("/") != path.rstrip("/"):
                self._reply(404, {"error": "not found"})
                return
            if not self._authenticated():
                if logger:
                    logger.event("nudge_rejected", level="warning",
                                 reason="unauthenticated", path=url_path)
                self._reply(401, {"error": "unauthenticated"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError):
                if logger:
                    logger.event("nudge_rejected", level="warning",
                                 reason="malformed JSON body")
                self._reply(400, {"error": "malformed payload"})
                return
            doc_id = parse_doc_id(payload)
            if doc_id is None:
                if logger:
                    logger.event("nudge_rejected", level="warning",
                                 reason="no valid integer doc id in payload")
                self._reply(400, {"error": "no valid document id in nudge"})
                return
            newly = queue.queue.enqueue(doc_id)
            if logger:
                logger.event("nudge_received", doc_id=doc_id,
                             queued=newly, debounced=(not newly))
            if newly:
                # Drain synchronously so processing is observable + testable; the
                # queue is persisted first so a crash mid-drain resumes on restart.
                queue.drain()
            self._reply(202, {"accepted": True, "doc_id": doc_id,
                              "debounced": (not newly)})

        # A GET on the endpoint is a lightweight liveness probe (no auth, no work).
        def do_GET(self):  # noqa: N802
            url_path = urlparse(self.path).path
            if url_path.rstrip("/") == path.rstrip("/"):
                self._reply(200, {"ok": True, "queue_depth": queue.queue.depth()})
            else:
                self._reply(404, {"error": "not found"})

    return _Handler


def _consteq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(str(a), str(b))


class WebhookServer:
    """Owns the stdlib HTTP server + the persisted queue + the processor. Starts
    in a background thread so `pa serve` can run the scheduler on the main thread.

    On start it FIRST resumes any pending nudges left by a previous run (restart
    safety), then begins listening. Binds inside the compose network (default
    0.0.0.0:8765) with NO published host port (plan §8.1)."""

    def __init__(self, settings, sweep, *, logger=None):
        self.settings = settings
        self.wh = settings.webhook
        self.logger = logger
        self.queue = NudgeQueue(
            str(settings.data_path("webhook-queue.json")),
            debounce_seconds=self.wh.debounce_seconds,
        )
        self.processor = NudgeProcessor(
            self.queue, sweep.process_nudge, logger=logger
        )
        self._httpd = None
        self._thread = None

    def resume_pending(self) -> int:
        """Restart safety: process anything left pending by a previous run before
        we start accepting new nudges. Returns the count resumed."""
        pending = self.queue.pending_ids()
        if pending and self.logger:
            self.logger.event("nudge_resume", pending=pending)
        return self.processor.drain()

    def _make_httpd(self):
        if not self.wh.secret:
            raise RuntimeError(
                "webhook is enabled but PA_WEBHOOK_SECRET is not set. Refusing to "
                "start an unauthenticated nudge receiver. Set PA_WEBHOOK_SECRET in "
                "the environment (never the YAML config)."
            )
        handler = make_handler(
            self.settings, self.processor, logger=self.logger,
            secret=self.wh.secret, path=self.wh.path,
        )
        return ThreadingHTTPServer((self.wh.host, self.wh.port), handler)

    def start(self):
        """Resume pending work, then start listening in a daemon thread."""
        self.resume_pending()
        self._httpd = self._make_httpd()
        if self.logger:
            self.logger.event(
                "webhook_start", host=self.wh.host, port=self.wh.port,
                path=self.wh.path,
            )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="pa-webhook", daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            if self.logger:
                self.logger.event("webhook_stop")

    @property
    def address(self):
        """The actually-bound (host, port) — useful when port 0 picks an ephemeral
        port in tests."""
        if self._httpd is not None:
            return self._httpd.server_address
        return (self.wh.host, self.wh.port)
