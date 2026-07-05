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

"""ControlPlane — the transport-agnostic protocol logic (vendor side).

The protocol is defined here as pure request->response handlers over dicts, so it
can be driven by EITHER:
  * the stdlib HTTP server (`server.py`) for real outbound-HTTP operation, OR
  * an in-process transport (`transport.InProcessTransport`) for offline tests
    that simulate disconnects/restarts programmatically.

Both call `ControlPlane.handle(method, path, headers, body)`. Keeping the logic
here (not in the HTTP handler) is what lets the resilience tests exercise the real
protocol without binding any socket.

LONG-POLL (§3.1 step 3): `GET /agent/work` PARKS — it blocks up to `poll_timeout`
seconds waiting for a job, then returns 204 (no job) so the agent re-polls. The
control plane NEVER initiates a connection to the agent; it only ever answers a
request the agent made. That is the outbound-only guarantee, server-side.
"""
from __future__ import annotations

import threading
import time

from .store import ControlPlaneStore

# Wire protocol paths (the documented HTTP API the agent speaks).
PATH_ENROLL = "/agent/enroll"
PATH_WORK = "/agent/work"
PATH_RESULTS = "/agent/results"
PATH_HEARTBEAT = "/agent/heartbeat"
PATH_INFERENCE = "/agent/inference"   # Phase 6: hosted-inference proxy
PATH_ADMIN_ENQUEUE = "/admin/enqueue"
# Phase 7: read-only dashboards (§8.4). GET only; NEVER mutate documents.
PATH_DASHBOARD = "/dashboard"
PATH_DASHBOARD_SUMMARY = "/dashboard/summary"
PATH_DASHBOARD_FLEET = "/dashboard/fleet"
PATH_DASHBOARD_COST = "/dashboard/cost"
PATH_DASHBOARD_REVIEW = "/dashboard/review"


class Response:
    __slots__ = ("status", "body", "text", "content_type")

    def __init__(self, status: int, body: dict | None = None, *,
                 text: str | None = None, content_type: str | None = None):
        self.status = status
        self.body = body if body is not None else {}
        # Phase 7: a non-JSON response (e.g. the self-contained dashboard HTML).
        # When `text` is set the server writes it verbatim with `content_type`
        # instead of JSON-encoding `body`.
        self.text = text
        self.content_type = content_type


class ControlPlane:
    def __init__(self, store: ControlPlaneStore | None = None, *,
                 poll_timeout: float = 25.0, admin_token: str = "",
                 billing=None, inference_proxy=None, logger=None,
                 dashboard=None, now=time.time, sleep=time.sleep):
        self.store = store or ControlPlaneStore()
        self.poll_timeout = float(poll_timeout)
        self.admin_token = admin_token
        # Phase 7: optional read-only DashboardData. When present, the control
        # plane serves the dashboard JSON endpoints + the self-contained HTML view.
        # When absent, those routes 404 (dashboards are an additive read surface;
        # nothing about the agent protocol changes).
        self.dashboard = dashboard
        # Phase 6: the billing seam + inference proxy are OPTIONAL. When absent, the
        # control plane behaves exactly as Phase 5 (no hosted inference); the
        # /agent/inference endpoint then reports hosted inference unavailable. When
        # present, hosted inference is enabled and metered.
        self.billing = billing
        self.inference_proxy = inference_proxy
        self.logger = logger
        self._now = now
        self._sleep = sleep
        # Wakes parked long-polls when a job is enqueued (so dispatch is prompt
        # without busy-waiting). Purely a latency optimisation; correctness does
        # not depend on it (the poll also times out and re-polls).
        self._job_event = threading.Event()

    # -- auth helper -------------------------------------------------------
    @staticmethod
    def _bearer(headers: dict) -> str | None:
        auth = (headers or {}).get("Authorization") or (headers or {}).get("authorization")
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return None

    def _authed_agent(self, headers: dict):
        agent_id = (headers or {}).get("X-Agent-Id") or (headers or {}).get("x-agent-id")
        cred = self._bearer(headers)
        if not agent_id or not cred:
            return None
        return self.store.authenticate(agent_id, cred)

    # -- dispatch ----------------------------------------------------------
    def handle(self, method: str, path: str, headers: dict | None = None,
               body: dict | None = None) -> Response:
        method = method.upper()
        headers = headers or {}
        body = body or {}
        path = path.split("?", 1)[0].rstrip("/") or "/"

        if method == "POST" and path == PATH_ENROLL:
            return self._enroll(body)
        if method == "GET" and path == PATH_WORK:
            return self._work(headers)
        if method == "POST" and path == PATH_RESULTS:
            return self._results(headers, body)
        if method == "POST" and path == PATH_HEARTBEAT:
            return self._heartbeat(headers, body)
        if method == "POST" and path == PATH_INFERENCE:
            return self._inference(headers, body)
        if method == "POST" and path == PATH_ADMIN_ENQUEUE:
            return self._admin_enqueue(headers, body)
        # Phase 7: read-only dashboards. GET only — a mutating method on a
        # dashboard path is a 405, never a write.
        if path == PATH_DASHBOARD or path.startswith(PATH_DASHBOARD + "/"):
            if method != "GET":
                return Response(405, {"error": "dashboards are read-only (GET only)"})
            return self._dashboard(path)
        return Response(404, {"error": "not found", "path": path})

    # -- endpoints ---------------------------------------------------------
    def _enroll(self, body: dict) -> Response:
        token = body.get("enrollment_token")
        if not token:
            return Response(400, {"error": "missing enrollment_token"})
        creds = self.store.enroll(token)
        if creds is None:
            # One-time token already used / unknown. Do not leak which.
            return Response(401, {"error": "invalid or already-used enrollment token"})
        return Response(200, creds)

    def _work(self, headers: dict) -> Response:
        agent = self._authed_agent(headers)
        if agent is None:
            return Response(401, {"error": "unauthenticated"})
        tenant, agent_id = agent["tenant"], agent["agent_id"]
        # Park: wait up to poll_timeout for a job, then 204 so the agent re-polls.
        deadline = self._now() + self.poll_timeout
        while True:
            job = self.store.lease_next(tenant=tenant, agent_id=agent_id)
            if job is not None:
                return Response(200, {"job": job})
            remaining = deadline - self._now()
            if remaining <= 0:
                return Response(204, {})
            # Sleep in small slices so an enqueue can wake us promptly and so a
            # test's fake clock/sleep stays responsive.
            self._job_event.wait(timeout=min(remaining, 0.25))
            self._job_event.clear()

    def _results(self, headers: dict, body: dict) -> Response:
        agent = self._authed_agent(headers)
        if agent is None:
            return Response(401, {"error": "unauthenticated"})
        job_id = body.get("job_id")
        if not job_id:
            return Response(400, {"error": "missing job_id"})
        self.store.ack(
            tenant=agent["tenant"], agent_id=agent["agent_id"],
            job_id=job_id, result=body.get("result"),
        )
        return Response(200, {"acked": True, "job_id": job_id})

    def _heartbeat(self, headers: dict, body: dict) -> Response:
        agent = self._authed_agent(headers)
        if agent is None:
            return Response(401, {"error": "unauthenticated"})
        self.store.record_heartbeat(agent_id=agent["agent_id"],
                                    status=body.get("status") or {})
        return Response(200, {"ok": True})

    def _inference(self, headers: dict, body: dict) -> Response:
        """Hosted-inference proxy (Phase 6). Enforces the MANDATED check order:

            1. authenticate agent
            2. resolve tenant (from the authenticated agent record)
            3. check entitlement (active subscription)
            4. check server-side spend cap
            5. ONLY THEN forward to the vendor model + meter usage

        Contents in `body` transit ONLY for the model call and are never persisted;
        only a content-free usage record is written (§5). Refusals (unentitled /
        over-cap) return a clear structured error the agent surfaces so work halts.
        """
        # 1. authenticate agent
        agent = self._authed_agent(headers)
        if agent is None:
            return Response(401, {"error": "unauthenticated"})

        # Hosted inference must be configured on this control plane.
        if self.inference_proxy is None or self.billing is None:
            return Response(501, {
                "error": "hosted inference is not enabled on this control plane",
                "reason": "not_configured",
            })

        # 2. resolve tenant (never trust a tenant field in the body).
        tenant = agent["tenant"]

        from .billing import EntitlementError, SpendCapError
        from .inference import InferenceError, UnpricedModelError

        # 3. entitlement, then 4. spend cap — BEFORE any model work.
        try:
            self.billing.check_entitled(tenant)
            self.billing.check_spend_cap(tenant)
        except EntitlementError as e:
            self._log_refusal(tenant, "unentitled", str(e))
            return Response(402, {"error": str(e), "reason": e.reason})
        except SpendCapError as e:
            self._log_refusal(tenant, "spend_cap", str(e))
            return Response(429, {"error": str(e), "reason": e.reason})

        # 5. forward to the vendor model + meter. The proxy prices + records usage.
        request = body.get("request") or {}
        try:
            result = self.inference_proxy.run(tenant, request)
        except UnpricedModelError as e:
            # Server-side MISCONFIGURATION: an unpriced model would meter $0 and
            # defeat the spend cap. Fail closed (no un-metered inference served).
            self._log_refusal(tenant, "unpriced_model", str(e))
            return Response(500, {"error": str(e), "reason": "unpriced_model"})
        except InferenceError as e:
            return Response(400, {"error": str(e), "reason": "inference_error"})
        return Response(200, {"result": result})

    def _log_refusal(self, tenant, reason, message):
        if self.logger is not None:
            # Metadata only — never contents.
            self.logger.event("inference_refused", level="warning",
                              tenant=tenant, reason=reason)

    def _admin_enqueue(self, headers: dict, body: dict) -> Response:
        # Admin path — protected by a separate admin token (NOT an agent
        # credential). In the prototype this is how a job is pushed to an agent.
        if self.admin_token:
            provided = self._bearer(headers) or headers.get("X-Admin-Token")
            import hmac
            if not provided or not hmac.compare_digest(str(provided), str(self.admin_token)):
                return Response(401, {"error": "admin unauthenticated"})
        tenant = body.get("tenant") or "t-default"
        agent_id = body.get("agent_id")
        job_type = body.get("type")
        if not agent_id or not job_type:
            return Response(400, {"error": "require agent_id and type"})
        job = self.store.enqueue(
            tenant=tenant, agent_id=agent_id, job_type=job_type,
            payload=body.get("payload") or {}, job_id=body.get("job_id"),
        )
        # Wake any parked long-poll so the job dispatches promptly.
        self._job_event.set()
        return Response(200, {"enqueued": True, "job": job})

    # -- Phase 7: read-only dashboards (§8.4) ------------------------------
    def _dashboard(self, path: str) -> Response:
        """Serve the read-only dashboard surfaces. Never mutates anything. Returns
        404 if no DashboardData is wired on this control plane."""
        if self.dashboard is None:
            return Response(404, {"error": "dashboards not enabled on this control plane"})
        if path == PATH_DASHBOARD:
            from .dashboard import render_html

            return Response(200, text=render_html(), content_type="text/html; charset=utf-8")
        if path == PATH_DASHBOARD_FLEET:
            return Response(200, self.dashboard.fleet())
        if path == PATH_DASHBOARD_COST:
            return Response(200, self.dashboard.cost())
        if path == PATH_DASHBOARD_REVIEW:
            return Response(200, self.dashboard.review())
        if path == PATH_DASHBOARD_SUMMARY:
            return Response(200, self.dashboard.summary())
        return Response(404, {"error": "not found", "path": path})
