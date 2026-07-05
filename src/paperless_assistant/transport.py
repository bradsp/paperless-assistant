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

"""Agent-side OUTBOUND transport to the control plane (hosted mode, Phase 5).

    ┌──────────────────────────────────────────────────────────────┐
    │  OUTBOUND-ONLY.  Every method here OPENS a connection FROM the │
    │  agent TO the control plane.  Nothing in this module binds a   │
    │  listening socket or accepts an inbound connection.  This is   │
    │  the structural half of §7 point 2 (work is PULLED, not pushed)│
    └──────────────────────────────────────────────────────────────┘

Two implementations of the same tiny interface:

  * `HttpTransport`      — real operation: long-poll over HTTPS with `requests`
                           (already a dependency). The agent is always the client.
  * `InProcessTransport` — offline tests: routes the SAME request dicts straight
                           into `ControlPlane.handle(...)` with NO socket at all,
                           and can be told to `disconnect()`/`reconnect()` to
                           simulate network loss programmatically.

The interface is deliberately request/response over dicts so the agent's
pull-loop is identical regardless of transport, and so a test can prove the loop
opens no inbound listener (there is simply no server object here to bind one).

A `TransportError` models "the control plane is unreachable" (network down); the
agent's reconnect/backoff loop catches it and retries. This never affects
in-flight LOCAL work — that continues regardless (§3.2).
"""
from __future__ import annotations

import json


class TransportError(RuntimeError):
    """The control plane could not be reached (network loss / connection refused).

    The agent treats this as 'retry with backoff', NOT as a job failure — local
    work continues and results queue in /data until the transport recovers."""


class Transport:
    """Minimal outbound client interface. Returns (status_code, body_dict)."""

    def request(self, method: str, path: str, *, headers=None, body=None,
                timeout: float | None = None) -> tuple[int, dict]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


class HttpTransport(Transport):
    """Real outbound HTTP client (long-poll friendly). Uses `requests`.

    The agent DIALS OUT to `base_url` (the control plane's stable public
    endpoint). TLS in production is just an https:// base_url; optional mTLS can
    be layered by passing a configured `session` with a client cert. This class
    never listens for or accepts inbound connections."""

    def __init__(self, base_url: str, *, session=None, default_timeout: float = 30.0):
        import requests

        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.default_timeout = default_timeout
        self._requests = requests

    def request(self, method, path, *, headers=None, body=None, timeout=None):
        url = self.base + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            r = self.session.request(
                method, url, data=data, headers=hdrs,
                timeout=timeout if timeout is not None else self.default_timeout,
            )
        except self._requests.RequestException as e:
            # Connection refused / timeout / DNS / reset -> the control plane is
            # unreachable. Surface as TransportError so the agent reconnects.
            raise TransportError(str(e)) from e
        try:
            payload = r.json() if r.content else {}
        except ValueError:
            payload = {}
        return r.status_code, payload

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass


class InProcessTransport(Transport):
    """Offline test transport: routes requests into a `ControlPlane` object with
    NO socket. Simulates network loss via `disconnect()` / `reconnect()`.

    While 'disconnected', every request raises `TransportError`, exactly as a real
    dropped connection would — this is how the resilience tests force a disconnect
    mid-job and assert clean resume."""

    def __init__(self, control_plane):
        self.cp = control_plane
        self._connected = True

    def disconnect(self):
        self._connected = False

    def reconnect(self):
        self._connected = True

    def request(self, method, path, *, headers=None, body=None, timeout=None):
        if not self._connected:
            raise TransportError("in-process transport is disconnected (simulated)")
        resp = self.cp.handle(method, path, headers=headers or {}, body=body or {})
        return resp.status, resp.body
