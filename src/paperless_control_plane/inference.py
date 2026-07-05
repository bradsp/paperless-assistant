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

"""Vendor inference proxy (Phase 6, r2/r3) — the control-plane side of hosted
inference.

    ┌───────────────────────────────────────────────────────────────────────┐
    │  The agent's HostedProvider DIALS OUT to POST /agent/inference. The     │
    │  control plane, using the VENDOR's model key held SERVER-SIDE, performs │
    │  the actual model call and returns the result + usage. The agent holds  │
    │  NO AI key (§4). Document contents transit here ONLY for the model call │
    │  and are NEVER persisted; logs record routing/usage/metadata only (§5). │
    └───────────────────────────────────────────────────────────────────────┘

STRICT CHECK ORDER (r2) before any model work, enforced in `ControlPlane`:
    authenticate agent  →  resolve tenant  →  check entitlement (active sub)
    →  check server-side spend cap  →  ONLY THEN call the vendor model
    →  meter usage against the tenant  →  return result.

The engine still owns and re-validates the JSON Schema on the AGENT side after the
proxy returns (Phase 2 guarantee). The proxy does NOT validate structured output —
it forwards the model's dict verbatim, so a malformed proxy response is caught by
the agent's engine-side validation, never written. Keeping validation on the agent
side is deliberate: the guarantee lives with the engine, not the vendor.

The VENDOR MODEL CALL is behind `ModelBackend`, a seam that is STUBBED in tests so
the offline suite makes no real API call and incurs no spend.
"""
from __future__ import annotations


# Inference task tokens (mirror the agent-side provider tasks).
TASK_TRANSCRIBE = "transcribe"
TASK_EXTRACT = "extract_structured"


class InferenceError(RuntimeError):
    """A model-call failure server-side (bad request / backend error). Surfaced to
    the agent as a structured error; the agent's retry/validation handles it."""


class UnpricedModelError(InferenceError):
    """The resolved vendor model has no pricing entry, so its cost would meter to
    $0 and SILENTLY DEFEAT the server-side spend cap. We FAIL CLOSED: refuse the
    call (before spending the vendor's money) rather than serve un-metered
    inference. Fix by configuring a priced model name or adding a pricing row."""

    def __init__(self, provider: str, model: str):
        self.provider = provider
        self.model = model
        super().__init__(
            f"hosted inference model {provider}/{model!r} has no pricing entry; "
            f"refusing to serve because the server-side spend cap cannot be "
            f"enforced without a price. Configure a priced vendor model "
            f"(e.g. via PA_HOSTED_OCR_MODEL / PA_HOSTED_METADATA_MODEL) or add a "
            f"pricing row."
        )


class ModelBackend:
    """Seam for the VENDOR's real model provider. Holds the vendor key SERVER-SIDE.

    In production this would wrap the Anthropic/OpenAI SDK exactly like the agent's
    BYO adapters, but keyed with the VENDOR's credentials. In tests a stub subclass
    (or a stub instance) replaces it so no real call is made and no spend occurs.

    The two methods mirror the AIProvider tasks and return content-free usage
    metadata alongside the model output.
    """

    def __init__(self, *, api_key: str = "", **_ignored):
        # The vendor key lives here (server-side config / env), NEVER on the agent.
        self._api_key = api_key

    def transcribe(self, *, doc_b64: str, model: str, opts: dict | None = None) -> dict:
        """Return {"text", "in_tokens", "out_tokens"}. Real impl calls the vendor
        vision model with the vendor key; must be overridden/stubbed."""
        raise NotImplementedError(
            "ModelBackend.transcribe must be implemented server-side (or stubbed "
            "in tests). The vendor key is used here, never sent to the agent."
        )

    def extract_structured(self, *, prompt: str, schema: dict, model: str,
                           opts: dict | None = None) -> dict:
        """Return {"data", "in_tokens", "out_tokens"}. `data` is the model's dict —
        the AGENT re-validates it against the engine-owned schema (Phase 2)."""
        raise NotImplementedError(
            "ModelBackend.extract_structured must be implemented server-side (or "
            "stubbed in tests)."
        )


class AnthropicModelBackend(ModelBackend):
    """Production vendor backend: the vendor's Anthropic account, keyed SERVER-SIDE.

    Reuses the SAME provider adapter the BYO agent uses, but constructed with the
    VENDOR's key (from server-side config/env), so the vendor's model relationship
    lives entirely in the control plane. The agent never sees this key.

    Import of the agent package is fine here: the control plane already depends on
    the same repo; the trust boundary that matters is the KEY, and it stays here.
    """

    def __init__(self, *, api_key: str, ocr_model: str, metadata_model: str,
                 max_ocr_tokens: int = 8000):
        super().__init__(api_key=api_key)
        from paperless_assistant.providers.anthropic import AnthropicProvider

        self._provider = AnthropicProvider(
            api_key=api_key, ocr_model=ocr_model,
            metadata_model=metadata_model, max_ocr_tokens=max_ocr_tokens,
        )

    def transcribe(self, *, doc_b64, model, opts=None) -> dict:
        import base64

        doc = base64.standard_b64decode(doc_b64)
        t = self._provider.transcribe(doc)
        return {"text": t.text, "in_tokens": t.in_tokens, "out_tokens": t.out_tokens}

    def extract_structured(self, *, prompt, schema, model, opts=None) -> dict:
        r = self._provider.extract_structured(prompt, schema)
        return {"data": r.data, "in_tokens": r.in_tokens, "out_tokens": r.out_tokens}


class InferenceProxy:
    """Executes an authenticated, entitled, under-cap inference request.

    Given the tenant (already resolved from the authenticated agent), it:
      1. dispatches to the vendor ModelBackend for the requested task,
      2. prices the call from the pricing table,
      3. meters the usage against the tenant in the BillingStore,
      4. returns a content-free-metered result body.

    It does NOT authenticate, check entitlement, or check the cap — those are done
    by the caller (ControlPlane) in the mandated order BEFORE this runs, so a
    refusal never reaches the model. It also NEVER persists the request contents.
    """

    def __init__(self, backend: ModelBackend, billing, *, logger=None,
                 default_model_transcribe: str = "claude-opus-4-8",
                 default_model_extract: str = "claude-sonnet-4-6",
                 pricing_provider: str = "anthropic"):
        self.backend = backend
        self.billing = billing
        self.logger = logger
        self.default_model_transcribe = default_model_transcribe
        self.default_model_extract = default_model_extract
        # Which pricing table row to meter against (the vendor's own model).
        self.pricing_provider = pricing_provider

    def _require_priced(self, model: str) -> None:
        """Fail closed if `model` has no pricing entry (would meter $0 and defeat
        the spend cap). Checked BEFORE the paid backend call so we never spend the
        vendor's money on a call we cannot meter."""
        from paperless_assistant.providers import pricing

        if not pricing.is_priced(self.pricing_provider, model):
            raise UnpricedModelError(self.pricing_provider, model)

    def _price(self, model: str, in_tokens: int, out_tokens: int) -> float:
        # Reuse the shared pricing table so metering matches the agent's accounting
        # shape. Unknown models price at zero (still metered as a usage record).
        try:
            from paperless_assistant.providers import pricing
        except Exception:  # pragma: no cover - pricing is always importable
            return 0.0
        return pricing.cost_of(self.pricing_provider, model, in_tokens, out_tokens)

    def run(self, tenant: str, request: dict) -> dict:
        """Perform the (already-authorized) inference. `request` carries the task,
        the transient content (prompt/schema or doc bytes), and an optional model
        hint. Returns a result body with model output + metered usage.

        CONTENTS ARE TRANSIENT: `request` is used for the model call and then goes
        out of scope. Nothing here writes prompt/doc/schema to the billing store,
        the queue, or disk. Only a content-free usage record is persisted (§5)."""
        task = request.get("task")
        opts = request.get("opts") or {}

        if task == TASK_TRANSCRIBE:
            doc_b64 = request.get("doc_b64")
            if not doc_b64:
                raise InferenceError("transcribe request missing 'doc_b64'")
            model = request.get("model") or self.default_model_transcribe
            self._require_priced(model)   # fail closed BEFORE the paid call
            out = self.backend.transcribe(doc_b64=doc_b64, model=model, opts=opts)
            in_tok = int(out.get("in_tokens", 0))
            out_tok = int(out.get("out_tokens", 0))
            cost = self._price(model, in_tok, out_tok)
            self.billing.record_usage(
                tenant, task=task, model=model,
                in_tokens=in_tok, out_tokens=out_tok, cost=cost,
            )
            self._log_usage(tenant, task, model, in_tok, out_tok, cost)
            return {
                "task": task,
                "text": out.get("text", ""),
                "usage": {"in_tokens": in_tok, "out_tokens": out_tok, "cost": cost},
                "model": model,
            }

        if task == TASK_EXTRACT:
            prompt = request.get("prompt")
            schema = request.get("schema")
            if prompt is None or schema is None:
                raise InferenceError(
                    "extract_structured request missing 'prompt' or 'schema'")
            model = request.get("model") or self.default_model_extract
            self._require_priced(model)   # fail closed BEFORE the paid call
            out = self.backend.extract_structured(
                prompt=prompt, schema=schema, model=model, opts=opts)
            in_tok = int(out.get("in_tokens", 0))
            out_tok = int(out.get("out_tokens", 0))
            cost = self._price(model, in_tok, out_tok)
            self.billing.record_usage(
                tenant, task=task, model=model,
                in_tokens=in_tok, out_tokens=out_tok, cost=cost,
            )
            self._log_usage(tenant, task, model, in_tok, out_tok, cost)
            # NB: 'data' is the model's raw dict; the AGENT re-validates it against
            # the engine-owned schema. The proxy forwards it verbatim.
            return {
                "task": task,
                "data": out.get("data"),
                "usage": {"in_tokens": in_tok, "out_tokens": out_tok, "cost": cost},
                "model": model,
            }

        raise InferenceError(f"unknown inference task {task!r}")

    def _log_usage(self, tenant, task, model, in_tok, out_tok, cost):
        """Log ONLY routing/usage metadata — NEVER contents or prompts (§5)."""
        if self.logger is None:
            return
        self.logger.event(
            "inference_metered", tenant=tenant, task=task, model=model,
            in_tokens=in_tok, out_tokens=out_tok, cost=round(cost, 6),
        )
