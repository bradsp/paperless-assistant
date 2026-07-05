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

"""OpenAI adapter.

Structured output via `response_format` JSON-schema strict mode (plan §5.2);
vision via image content blocks. The `openai` SDK is import-guarded so a user
with only the Anthropic path installed can still run.

The engine still owns the schema and re-validates the returned dict - this
adapter merely asks OpenAI to honor the schema natively.
"""
from __future__ import annotations

import base64
import json

from .base import (
    CAP_STRUCTURED,
    CAP_VISION,
    CapabilityError,
    StructuredResult,
    Transcription,
)
from . import pricing


def _load_openai():
    """Import-guarded SDK load. Raises an actionable error if not installed."""
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:  # pragma: no cover - exercised via stub in tests
        raise CapabilityError(
            "The OpenAI provider requires the 'openai' package. Install it with "
            "`pip install paperless-assistant[openai]` (or `pip install openai`)."
        ) from e
    return OpenAI


# A conservative default of vision-capable OpenAI models. Anything not listed is
# treated as text-only so re-OCR refuses rather than silently degrading. The
# model catalog reuses this set, so keep it in sync when adding a catalog entry.
_VISION_MODELS = {
    "gpt-4o", "gpt-4o-mini", "gpt-4-turbo",
    "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
    "gpt-5.4", "o4-mini",
}


def _openai_strict_schema(node):
    """Return a COPY of a JSON Schema adapted to OpenAI's structured-output STRICT
    dialect: every object must declare `additionalProperties: false` AND list every
    property in `required`. OpenAI rejects a schema that omits these with a 400
    ("'additionalProperties' is required to be supplied and to be false"). Recurses
    into nested objects / array items. Does NOT mutate the input — the engine keeps
    its own canonical schema and re-validates every result against THAT (plan §5.3),
    so this translation only shapes the request to satisfy OpenAI's API."""
    if isinstance(node, dict):
        out = {k: _openai_strict_schema(v) for k, v in node.items()}
        if out.get("type") == "object" and isinstance(out.get("properties"), dict):
            out["additionalProperties"] = False
            out["required"] = list(out["properties"].keys())
        return out
    if isinstance(node, list):
        return [_openai_strict_schema(x) for x in node]
    return node


class OpenAIProvider:
    name = "openai"

    def __init__(self, *, api_key: str, ocr_model: str, metadata_model: str,
                 max_ocr_tokens: int, base_url: str | None = None,
                 vision: bool | None = None, metadata_max_tokens: int = 1024,
                 max_retries: int = 6):
        self.api_key = api_key
        self.ocr_model = ocr_model
        self.metadata_model = metadata_model
        self.max_ocr_tokens = max_ocr_tokens
        # Prompt 011: metadata output token cap (byte-identical default 1024).
        self.metadata_max_tokens = metadata_max_tokens
        self.base_url = base_url
        # Retry budget for the SDK (it honors the `retry-after` header + backs off
        # exponentially). OpenAI TPM rate limits are transient ("try again in
        # ~400ms"); a generous retry budget rides them out instead of failing the
        # document. Defaults to the configured HTTP retries (6).
        self.max_retries = max_retries
        caps = {CAP_STRUCTURED}
        has_vision = vision if vision is not None else (ocr_model in _VISION_MODELS)
        if has_vision:
            caps.add(CAP_VISION)
        self.capabilities = caps

    def _client(self):
        OpenAI = _load_openai()
        kw = {"api_key": self.api_key, "max_retries": self.max_retries}
        if self.base_url:
            kw["base_url"] = self.base_url
        return OpenAI(**kw)

    def transcribe(self, doc: bytes, *, opts=None) -> Transcription:
        if CAP_VISION not in self.capabilities:
            raise CapabilityError(
                f"OpenAI model '{self.ocr_model}' is not configured as vision-"
                f"capable; re-OCR requires a vision model."
            )
        from .anthropic import OCR_PROMPT  # reuse the identical OCR prompt

        # Honor a resolved custom OCR instruction (prompt 010) when supplied.
        prompt = (opts or {}).get("prompt") or OCR_PROMPT
        client = self._client()
        b64 = base64.standard_b64encode(doc).decode("utf-8")
        # OpenAI takes images (incl. rendered PDFs) as data URLs.
        resp = client.chat.completions.create(
            model=self.ocr_model,
            max_tokens=self.max_ocr_tokens,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:application/pdf;base64,{b64}"},
                        },
                    ],
                }
            ],
        )
        text = resp.choices[0].message.content or ""
        it = resp.usage.prompt_tokens
        ot = resp.usage.completion_tokens
        cost = pricing.cost_of(self.name, self.ocr_model, it, ot)
        return Transcription(text=text, in_tokens=it, out_tokens=ot, cost=cost)

    def extract_structured(self, prompt: str, schema: dict, *, opts=None) -> StructuredResult:
        client = self._client()
        # Translate the engine schema into OpenAI's strict dialect (adds
        # additionalProperties:false + full `required` on every object). Without
        # this OpenAI 400s every request; the engine still re-validates the result
        # against its own canonical schema.
        resp = client.chat.completions.create(
            model=self.metadata_model,
            max_tokens=self.metadata_max_tokens,
            messages=[{"role": "user", "content": prompt}],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "document_metadata",
                    "schema": _openai_strict_schema(schema),
                    "strict": True,
                },
            },
        )
        raw = resp.choices[0].message.content or "{}"
        data = json.loads(raw)
        it = resp.usage.prompt_tokens
        ot = resp.usage.completion_tokens
        cost = pricing.cost_of(self.name, self.metadata_model, it, ot)
        return StructuredResult(data=data, in_tokens=it, out_tokens=ot, cost=cost)
