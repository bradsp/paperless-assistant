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

"""HostedProvider — the AGENT-side adapter for hosted inference (Phase 6, r1).

Mode B (plan §5.4, connectivity §1/§5): a subscriber runs the agent with NO local
AI key. Instead of calling a model directly, `HostedProvider` performs inference by
DIALING OUT to the control-plane inference proxy (POST /agent/inference),
authenticated with the AGENT CREDENTIAL (the only agent→control-plane secret; the
vendor's model key stays server-side, §4). It satisfies the SAME `AIProvider`
protocol as the BYO adapters — `transcribe` / `extract_structured` — and returns
the SAME shapes (`Transcription` / `StructuredResult`, token usage + cost), so the
engine is untouched and BILLING-AGNOSTIC (§8.5).

THE STRUCTURED-OUTPUT GUARANTEE IS UNCHANGED. This adapter only translates the
call onto the proxy; it does NOT validate. The engine still calls
`extract_structured_validated`, which re-validates the returned dict against the
engine-owned JSON Schema and retries/errors on a malformed response — so a bad
proxy response can never produce a bad write (plan §5.3). Validation authority
never leaves the engine.

Refusals from the proxy (unentitled / over server-side cap) surface as a clear
`HostedInferenceRefused` so work HALTS rather than silently failing.

Capabilities: the hosted vendor path offers both vision and structured output, so
the adapter advertises {vision, structured_output} (config may pin otherwise).
Contents (doc bytes / prompt) transit the proxy ONLY for the model call.
"""
from __future__ import annotations

import base64

from .base import (
    CAP_STRUCTURED,
    CAP_VISION,
    ProviderError,
    StructuredResult,
    Transcription,
)

# The agent's view of the proxy route + inference tasks. Kept as local constants so
# the agent does NOT import the vendor package at runtime (trust boundary stays
# clean); they must match the control plane's routes.
PATH_INFERENCE = "/agent/inference"
TASK_TRANSCRIBE = "transcribe"
TASK_EXTRACT = "extract_structured"


class HostedInferenceError(ProviderError):
    """The inference proxy failed for a non-billing reason (transport, bad model
    response envelope, server error). The engine's retry/validation handles the
    structured case; transcription surfaces it."""


class HostedInferenceRefused(ProviderError):
    """The proxy REFUSED the call: the tenant is unentitled (no active
    subscription) or over its server-side spend cap. Work halts and the agent
    surfaces this clearly (it is not a transient error to retry blindly)."""

    def __init__(self, message: str, *, reason: str, status: int):
        super().__init__(message)
        self.reason = reason
        self.status = status


class HostedProvider:
    """AIProvider whose "endpoint" is the control-plane inference proxy.

    Constructed with a `transport` (the agent's outbound Transport) and an
    `auth_headers` callable returning the agent-credential bearer headers — i.e.
    exactly what the HostedAgent already uses to reach the control plane. This
    keeps the agent credential the ONLY secret sent, and reuses the outbound-only
    transport (no new connection surface).
    """

    name = "hosted"

    def __init__(self, *, transport, auth_headers, ocr_model: str = "",
                 metadata_model: str = "", vision: bool | None = None,
                 timeout: float | None = None):
        self.transport = transport
        # `auth_headers` is a zero-arg callable -> {"X-Agent-Id", "Authorization"}.
        self._auth_headers = auth_headers
        self.ocr_model = ocr_model
        self.metadata_model = metadata_model
        self.timeout = timeout
        # The hosted vendor path supports both tasks. `vision` can pin it off if a
        # deployment's vendor model lacks vision (capability negotiation still runs
        # engine-side, plan §5.3).
        caps = {CAP_STRUCTURED}
        if vision is None or vision:
            caps.add(CAP_VISION)
        self.capabilities = caps

    # -- the single outbound call to the proxy -----------------------------
    def _call(self, request: dict) -> dict:
        """POST the inference request to the proxy and return the `result` body.
        Raises HostedInferenceRefused on an entitlement/cap refusal, or
        HostedInferenceError on any other non-200."""
        from ..transport import TransportError

        try:
            status, body = self.transport.request(
                "POST", PATH_INFERENCE,
                headers=self._auth_headers(),
                body={"request": request},
                timeout=self.timeout,
            )
        except TransportError as e:
            # The proxy is unreachable — a genuine transport failure. Surface it as
            # a hosted error; the engine/caller decides whether to retry.
            raise HostedInferenceError(
                f"inference proxy unreachable: {e}") from e

        if status == 200:
            result = (body or {}).get("result")
            if not isinstance(result, dict):
                raise HostedInferenceError(
                    "inference proxy returned no result body")
            return result

        reason = (body or {}).get("reason") or "error"
        message = (body or {}).get("error") or f"inference proxy status {status}"
        # 402 unentitled / 429 over-cap -> a clear refusal that halts work.
        if status in (402, 429) or reason in ("unentitled", "spend_cap"):
            raise HostedInferenceRefused(message, reason=reason, status=status)
        raise HostedInferenceError(f"inference proxy error ({status}): {message}")

    # -- AIProvider: vision transcription ----------------------------------
    def transcribe(self, doc: bytes, *, opts=None) -> Transcription:
        b64 = base64.standard_b64encode(doc).decode("utf-8")
        req = {"task": TASK_TRANSCRIBE, "doc_b64": b64}
        if self.ocr_model:
            req["model"] = self.ocr_model
        if opts:
            req["opts"] = opts
        result = self._call(req)
        usage = result.get("usage") or {}
        return Transcription(
            text=result.get("text", ""),
            in_tokens=int(usage.get("in_tokens", 0)),
            out_tokens=int(usage.get("out_tokens", 0)),
            cost=float(usage.get("cost", 0.0)),
        )

    # -- AIProvider: structured extraction (engine re-validates the dict) --
    def extract_structured(self, prompt: str, schema: dict, *, opts=None) -> StructuredResult:
        req = {"task": TASK_EXTRACT, "prompt": prompt, "schema": schema}
        if self.metadata_model:
            req["model"] = self.metadata_model
        if opts:
            req["opts"] = opts
        result = self._call(req)
        usage = result.get("usage") or {}
        # `data` is the model's raw dict; the engine validates it against the
        # engine-owned schema (plan §5.3). We do NOT validate here.
        return StructuredResult(
            data=result.get("data"),
            in_tokens=int(usage.get("in_tokens", 0)),
            out_tokens=int(usage.get("out_tokens", 0)),
            cost=float(usage.get("cost", 0.0)),
        )
