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
import io

from .base import (
    CAP_STRUCTURED,
    CAP_VISION,
    CapabilityError,
    ProviderError,
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


def _load_pdfium():
    """Import-guarded PDF rasterizer. `pypdfium2` ships prebuilt wheels (bundled
    PDFium) so it stays pure-pip with NO system deps (no poppler/ghostscript),
    preserving the project's no-system-deps constraint (plan §4.2)."""
    try:
        import pypdfium2  # type: ignore
    except ImportError as e:
        raise CapabilityError(
            "Ollama vision re-OCR must rasterize PDF pages to images first, which "
            "requires 'pypdfium2'. Install it with "
            "`pip install paperless-assistant[ollama]` (or `pip install pypdfium2`)."
        ) from e
    return pypdfium2


# Substrings that indicate a local vision model.
_VISION_HINTS = ("llava", "vision", "bakllava", "llama3.2-vision", "minicpm-v", "moondream")


def _looks_vision(model: str) -> bool:
    m = model.lower()
    return any(h in m for h in _VISION_HINTS)


# Render resolution for PDF -> PNG. 200 DPI balances OCR legibility vs. payload
# size; llava-class models downscale internally so higher rarely helps.
_RENDER_DPI = 200

# Magic-byte prefixes for raster formats Ollama consumes directly (no render).
_IMAGE_MAGIC = (
    b"\x89PNG",       # PNG
    b"\xff\xd8\xff",  # JPEG
    b"GIF8",          # GIF
    b"BM",            # BMP
    b"RIFF",          # WEBP (RIFF container)
    b"II*\x00",       # TIFF (little-endian)
    b"MM\x00*",       # TIFF (big-endian)
)


def _is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-" or data[:4] == b"%PDF"


def _looks_image(data: bytes) -> bool:
    return any(data.startswith(sig) for sig in _IMAGE_MAGIC)


def _doc_to_images(doc: bytes) -> list:
    """Return base64 PNG string(s) for Ollama's `images` field.

    ROOT-CAUSE FIX: Ollama's `/api/generate` `images` expects RASTER image bytes
    (PNG/JPEG), NOT a PDF container. Paperless' `download_original` returns the
    stored PDF (or, for an image upload, the raster original). We therefore:
      * render each PDF page to a PNG (pypdfium2, pure-pip, no system deps), one
        base64 entry per page (multi-page handled); or
      * pass an already-raster original straight through.
    Sending raw PDF bytes here is exactly what made Ollama 400 with an image-
    decode error."""
    if _is_pdf(doc):
        pdfium = _load_pdfium()
        pdf = pdfium.PdfDocument(doc)
        try:
            scale = _RENDER_DPI / 72.0
            images = []
            for i in range(len(pdf)):
                bitmap = pdf[i].render(scale=scale)
                pil = bitmap.to_pil()
                buf = io.BytesIO()
                pil.save(buf, format="PNG")
                images.append(base64.standard_b64encode(buf.getvalue()).decode("utf-8"))
        finally:
            pdf.close()
        if not images:
            raise ProviderError(
                "the downloaded PDF had no renderable pages for Ollama re-OCR."
            )
        return images
    # Already a raster image (or an unknown blob we hand over unchanged) — Ollama
    # decodes PNG/JPEG/etc directly.
    return [base64.standard_b64encode(doc).decode("utf-8")]


def _normalize_endpoint(endpoint: str) -> str:
    """Normalize `PA_OLLAMA_ENDPOINT` to the Ollama ROOT.

    The native API lives at `<root>/api/generate`. Users frequently paste the
    OpenAI-compatible base (`.../v1`) or a full API path (`.../api`,
    `.../api/generate`); left alone these build `.../v1/api/generate` or
    `.../api/api/generate`, which the server answers with 404/405. We strip a
    single trailing well-known suffix so the effective URL is always correct."""
    e = (endpoint or "").strip().rstrip("/")
    for suffix in ("/api/generate", "/api/chat", "/api/tags", "/v1", "/api"):
        if e.endswith(suffix):
            e = e[: -len(suffix)].rstrip("/")
            break
    return e


def _error_body(r) -> str:
    """Best-effort extraction of Ollama's error message from a response. Ollama
    reports failures as `{"error": "..."}`; fall back to raw text."""
    try:
        data = r.json()
    except Exception:
        data = None
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"]).strip()
    txt = (getattr(r, "text", "") or "").strip()
    if txt:
        return txt
    if data is not None:
        return str(data)
    return ""


def _raise_for_ollama(r, *, model: str, url: str) -> None:
    """Replace `raise_for_status()` with an actionable error carrying the server's
    body + a remediation hint. A bare status code told the user nothing (the
    reported 400/405 opacity)."""
    status = getattr(r, "status_code", 200)
    if status < 400:
        return
    body = _error_body(r)
    lo = body.lower()

    # Model missing / not pulled (Ollama: 404 + "model '...' not found, try pulling").
    if status == 404 or "not found" in lo or "try pulling" in lo or "no such model" in lo:
        raise CapabilityError(
            f"Ollama returned HTTP {status} for model '{model}': "
            f"{body or 'model not found'}. The model may not be pulled — run "
            f"`ollama pull {model}` on the Ollama host, and confirm it is a vision "
            f"model (re-OCR needs one, e.g. a llava-class model)."
        )

    # Wrong endpoint shape (hitting a path Ollama does not serve for POST).
    if status == 405:
        raise ProviderError(
            f"Ollama returned HTTP 405 Method Not Allowed for {url}. "
            f"Check PA_OLLAMA_ENDPOINT points at the Ollama ROOT "
            f"(e.g. http://host:11434), NOT an OpenAI-compatible '/v1' base or an "
            f"'/api' path. Server said: {body or '(no body)'}"
        )

    hint = ""
    if "image" in lo:
        hint = (
            " This typically means the image payload was not a valid raster image "
            "(Ollama needs PNG/JPEG, not a PDF container)."
        )
    raise ProviderError(
        f"Ollama request to {url} failed with HTTP {status}: "
        f"{body or '(no response body)'}.{hint}"
    )


class OllamaProvider:
    name = "ollama"

    def __init__(self, *, ocr_model: str, metadata_model: str,
                 endpoint: str = "http://localhost:11434", vision: bool | None = None,
                 **_ignored):
        self.ocr_model = ocr_model
        self.metadata_model = metadata_model
        self.endpoint = _normalize_endpoint(endpoint)
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
        # Rasterize PDFs (or pass raster originals through) so `images` carries
        # real image data — the fix for the 400 image-decode failure.
        images = _doc_to_images(doc)
        url = f"{self.endpoint}/api/generate"
        r = httpx.post(
            url,
            json={
                "model": self.ocr_model,
                "prompt": prompt,
                "images": images,
                "stream": False,
            },
            timeout=600,
        )
        _raise_for_ollama(r, model=self.ocr_model, url=url)
        data = r.json()
        text = data.get("response", "")
        it = data.get("prompt_eval_count", 0)
        ot = data.get("eval_count", 0)
        # Local inference: zero marginal cost.
        return Transcription(text=text, in_tokens=it, out_tokens=ot, cost=0.0)

    def extract_structured(self, prompt: str, schema: dict, *, opts=None) -> StructuredResult:
        import json

        httpx = _load_httpx()
        url = f"{self.endpoint}/api/generate"
        r = httpx.post(
            url,
            json={
                "model": self.metadata_model,
                "prompt": prompt,
                "format": schema,  # Ollama accepts a JSON Schema here
                "stream": False,
            },
            timeout=600,
        )
        _raise_for_ollama(r, model=self.metadata_model, url=url)
        payload = r.json()
        raw = payload.get("response", "{}")
        data = json.loads(raw) if isinstance(raw, str) else raw
        it = payload.get("prompt_eval_count", 0)
        ot = payload.get("eval_count", 0)
        return StructuredResult(data=data, in_tokens=it, out_tokens=ot, cost=0.0)
