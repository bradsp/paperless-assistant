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

"""Anthropic adapter.

Reproduces today's EXACT calls (plan §5.2): forced tool use for structured
output (`tool_choice={type:"tool", name:"document_metadata"}`) and vision via a
`document` content block, with the same models, prompt, max_tokens, and
retry/backoff on rate/overload as the Phase 1 seams. This keeps the Anthropic
characterization tests byte-identical.
"""
from __future__ import annotations

import base64
import time

from .base import (
    CAP_STRUCTURED,
    CAP_VISION,
    StructuredResult,
    Transcription,
)
from . import pricing


# Byte-identical to the Phase 1 ocr.OCR_PROMPT.
OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text from this document exactly as "
    "it appears, preserving reading order, line breaks, numbers, dates, and "
    "punctuation. Do not summarize, interpret, translate, or add commentary. "
    "If a region is illegible, write [illegible]. Output only the transcribed text."
)


def _is_transient(exc: Exception) -> bool:
    s = str(exc).lower()
    return "rate" in s or "overload" in s or "529" in s


class AnthropicProvider:
    """AIProvider over the Anthropic SDK. The `anthropic` package is a core
    dependency (Phase 1), so no import guard is needed here."""

    name = "anthropic"

    def __init__(self, *, api_key: str, ocr_model: str, metadata_model: str,
                 max_ocr_tokens: int, metadata_max_tokens: int = 1024):
        self.api_key = api_key
        self.ocr_model = ocr_model
        self.metadata_model = metadata_model
        self.max_ocr_tokens = max_ocr_tokens
        # Prompt 011: metadata output token cap (byte-identical default 1024).
        self.metadata_max_tokens = metadata_max_tokens
        # Claude (opus/sonnet class) supports both vision and structured output.
        self.capabilities = {CAP_VISION, CAP_STRUCTURED}

    # -- vision transcription (mirrors ocr._transcribe exactly) ------------
    def transcribe(self, doc: bytes, *, opts=None) -> Transcription:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key)
        b64 = base64.standard_b64encode(doc).decode("utf-8")
        # Honor a resolved custom OCR instruction (prompt 010) when supplied; else
        # the byte-identical default OCR_PROMPT.
        prompt = (opts or {}).get("prompt") or OCR_PROMPT

        delay = 2.0
        for _ in range(6):
            try:
                msg = client.messages.create(
                    model=self.ocr_model,
                    max_tokens=self.max_ocr_tokens,
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "document",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "application/pdf",
                                        "data": b64,
                                    },
                                },
                                {"type": "text", "text": prompt},
                            ],
                        }
                    ],
                )
                text = "".join(b.text for b in msg.content if b.type == "text")
                it, ot = msg.usage.input_tokens, msg.usage.output_tokens
                cost = pricing.cost_of(self.name, self.ocr_model, it, ot)
                return Transcription(text=text, in_tokens=it, out_tokens=ot, cost=cost)
            except Exception as e:  # crude rate-limit / transient handling
                if _is_transient(e):
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise
        raise RuntimeError("Claude OCR failed after retries")

    # -- forced-tool structured extraction (mirrors metadata._extract) -----
    def extract_structured(self, prompt: str, schema: dict, *, opts=None) -> StructuredResult:
        from anthropic import Anthropic

        client = Anthropic(api_key=self.api_key)
        # Translate the engine-owned JSON Schema into Anthropic's native tool.
        tool = {
            "name": "document_metadata",
            "description": "Return structured metadata for a document.",
            "input_schema": schema,
        }

        delay = 2.0
        for _ in range(6):
            try:
                msg = client.messages.create(
                    model=self.metadata_model,
                    max_tokens=self.metadata_max_tokens,
                    tools=[tool],
                    tool_choice={"type": "tool", "name": "document_metadata"},
                    messages=[{"role": "user", "content": prompt}],
                )
                result = next(b.input for b in msg.content if b.type == "tool_use")
                it, ot = msg.usage.input_tokens, msg.usage.output_tokens
                cost = pricing.cost_of(self.name, self.metadata_model, it, ot)
                return StructuredResult(data=result, in_tokens=it, out_tokens=ot, cost=cost)
            except StopIteration:
                raise RuntimeError("model did not return tool_use block")
            except Exception as e:
                if _is_transient(e):
                    time.sleep(delay)
                    delay = min(delay * 2, 60)
                    continue
                raise
        raise RuntimeError("Claude metadata failed after retries")
