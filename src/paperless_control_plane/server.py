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

"""Stdlib HTTP server exposing the control-plane protocol (vendor side).

A thin `http.server` adapter that turns real inbound HTTP requests FROM AGENTS
into `ControlPlane.handle(...)` calls. NO heavy web framework (r5). This is the
server the AGENT dials OUT to — the control plane still never initiates any
connection into the agent's network; it only answers requests.

Long-poll safe: `ThreadingHTTPServer` handles each parked `GET /agent/work` on its
own thread, so many agents can hold long-polls concurrently.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app import ControlPlane


def make_handler(cp: ControlPlane):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: N802 — silence default access log
            return

        def _headers_dict(self) -> dict:
            return {k: v for k, v in self.headers.items()}

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        def _dispatch(self, method: str):
            resp = cp.handle(method, self.path, self._headers_dict(),
                             self._read_body() if method != "GET" else {})
            # Phase 7: a Response may carry raw text (e.g. the self-contained
            # dashboard HTML) instead of a JSON body.
            if getattr(resp, "text", None) is not None:
                payload = resp.text.encode("utf-8")
                content_type = resp.content_type or "text/plain; charset=utf-8"
            else:
                payload = json.dumps(resp.body).encode("utf-8")
                content_type = "application/json"
            self.send_response(resp.status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

    return _Handler


class ControlPlaneServer:
    """Owns the ThreadingHTTPServer + the ControlPlane. Starts in a background
    thread so callers/tests can drive it over real localhost HTTP."""

    def __init__(self, cp: ControlPlane, *, host: str = "127.0.0.1", port: int = 8080):
        self.cp = cp
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None

    def start(self):
        self._httpd = ThreadingHTTPServer((self.host, self.port), make_handler(self.cp))
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="pa-control-plane", daemon=True)
        self._thread.start()
        return self

    @property
    def address(self):
        return self._httpd.server_address if self._httpd else (self.host, self.port)

    @property
    def base_url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def serve_forever(self):
        """Blocking run (used by the console script)."""
        self._httpd = ThreadingHTTPServer((self.host, self.port), make_handler(self.cp))
        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()
