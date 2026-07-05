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

"""Ollama / local adapter.

Structured output via the `format` parameter carrying a JSON Schema (plan §5.2);
zero marginal cost; no data leaves the box. Vision is only advertised when the
configured model is known to be a vision model (e.g. a llava-class model) - a
text-only local model selected for re-OCR must refuse, not silently degrade.

HTTP is via `httpx` (an anthropic dependency, already present) and is import-
guarded so the Anthropic-only path is unaffected if it were ever absent. Talks
to a local Ollama server; in tests the transport is stubbed - no server runs.
"""
from __future__ import annotations

import base64

from .base import (
    CAP_STRUCTURED,
    CAP_VISION,
    CapabilityError,
    StructuredResult,
    Transcription,
)


def _load_httpx():
    try:
        import httpx  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise CapabilityError(
            "The Ollama provider requires 'httpx'. Install it with "
            "`pip install paperless-assistant[ollama]` (or `pip install httpx`)."
        ) from e
    return httpx


# Substrings that indicate a local vision model.
_VISION_HINTS = ("llava", "vision", "bakllava", "llama3.2-vision", "minicpm-v", "moondream")


def _looks_vision(model: str) -> bool:
    m = model.lower()
    return any(h in m for h in _VISION_HINTS)


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, ocr_model: str, metadata_model: str,
                 endpoint: str = "http://localhost:11434", vision: bool | None = None,
                 **_ignored):
        self.ocr_model = ocr_model
        self.metadata_model = metadata_model
        self.endpoint = endpoint.rstrip("/")
        caps = {CAP_STRUCTURED}
        has_vision = vision if vision is not None else _looks_vision(ocr_model)
        if has_vision:
            caps.add(CAP_VISION)
        self.capabilities = caps

    def transcribe(self, doc: bytes, *, opts=None) -> Transcription:
        if CAP_VISION not in self.capabilities:
            raise CapabilityError(
                f"Ollama model '{self.ocr_model}' is not vision-capable; re-OCR "
                f"requires a vision model (e.g. a llava-class model)."
            )
        from .anthropic import OCR_PROMPT

        # Honor a resolved custom OCR instruction (prompt 010) when supplied.
        prompt = (opts or {}).get("prompt") or OCR_PROMPT
        httpx = _load_httpx()
        b64 = base64.standard_b64encode(doc).decode("utf-8")
        r = httpx.post(
            f"{self.endpoint}/api/generate",
            json={
                "model": self.ocr_model,
                "prompt": prompt,
                "images": [b64],
                "stream": False,
            },
            timeout=600,
        )
        r.raise_for_status()
        data = r.json()
        text = data.get("response", "")
        it = data.get("prompt_eval_count", 0)
        ot = data.get("eval_count", 0)
        # Local inference: zero marginal cost.
        return Transcription(text=text, in_tokens=it, out_tokens=ot, cost=0.0)

    def extract_structured(self, prompt: str, schema: dict, *, opts=None) -> StructuredResult:
        import json

        httpx = _load_httpx()
        r = httpx.post(
            f"{self.endpoint}/api/generate",
            json={
                "model": self.metadata_model,
                "prompt": prompt,
                "format": schema,  # Ollama accepts a JSON Schema here
                "stream": False,
            },
            timeout=600,
        )
        r.raise_for_status()
        payload = r.json()
        raw = payload.get("response", "{}")
        data = json.loads(raw) if isinstance(raw, str) else raw
        it = payload.get("prompt_eval_count", 0)
        ot = payload.get("eval_count", 0)
        return StructuredResult(data=data, in_tokens=it, out_tokens=ot, cost=0.0)
