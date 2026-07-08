# Paperless Assistant - an AI companion for Paperless-NGX.
# Copyright (C) 2026 BP Technology Advisors LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Regression tests for Ollama vision re-OCR (the 400/405 breakage).

These fail on the pre-fix adapter (which base64'd the raw PDF into `images` and
called a bare `raise_for_status()`), and pass on the fixed adapter:

  * a real (multi-page) PDF is RASTERIZED to PNG before the call — the `images`
    field carries PNG bytes, never the PDF container (the 400 image-decode fix);
  * an already-raster original passes through unrendered;
  * a 400-with-body, a 405 endpoint-misconfig, and a not-pulled model each yield
    a distinct, actionable error carrying the server's message — not a bare code;
  * a custom OCR instruction still reaches Ollama;
  * endpoint normalization strips a `/v1` or `/api` suffix so the effective URL is
    `<root>/api/generate`.
"""
import base64
import io

import pytest

from paperless_assistant.providers.base import CapabilityError, ProviderError
from fakes import make_ollama_provider, ollama_error


# base64 magic prefixes: PNG -> "iVBOR...", "%PDF" -> "JVBER...".
_PNG_B64_PREFIX = "iVBOR"
_PDF_B64_PREFIX = "JVBER"


def _make_pdf(pages=1):
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(pages):
        c.drawString(72, 720, f"scanned text page {i + 1}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _make_png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ROOT CAUSE 1: PDF must be rasterized to PNG (was sent raw -> 400).
# ---------------------------------------------------------------------------
def test_pdf_is_rasterized_to_png_not_sent_raw(monkeypatch):
    seen = {}

    def responder(url, **kw):
        seen["url"] = url
        seen["json"] = kw.get("json")
        return {"response": "CLEAN OCR TEXT", "prompt_eval_count": 3, "eval_count": 9}

    prov = make_ollama_provider(responder, monkeypatch, ocr_model="llava:13b")
    result = prov.transcribe(_make_pdf(pages=2))

    assert result.text == "CLEAN OCR TEXT"
    assert result.cost == 0.0
    images = seen["json"]["images"]
    # One image per page, each a PNG (NOT the raw PDF container).
    assert len(images) == 2
    for img in images:
        assert img.startswith(_PNG_B64_PREFIX)
        assert not img.startswith(_PDF_B64_PREFIX)
        # sanity: decodes to real PNG bytes
        assert base64.standard_b64decode(img)[:4] == b"\x89PNG"
    assert seen["url"].endswith("/api/generate")


def test_raster_image_original_passes_through(monkeypatch):
    seen = {}

    def responder(url, **kw):
        seen["json"] = kw.get("json")
        return {"response": "text", "prompt_eval_count": 1, "eval_count": 1}

    prov = make_ollama_provider(responder, monkeypatch, ocr_model="llava")
    png = _make_png()
    prov.transcribe(png)
    images = seen["json"]["images"]
    assert len(images) == 1
    # The exact original bytes are forwarded (no re-render).
    assert base64.standard_b64decode(images[0]) == png


def test_custom_prompt_still_reaches_ollama(monkeypatch):
    seen = {}

    def responder(url, **kw):
        seen["json"] = kw.get("json")
        return {"response": "ok"}

    prov = make_ollama_provider(responder, monkeypatch, ocr_model="llava")
    prov.transcribe(_make_pdf(), opts={"prompt": "MY CUSTOM OCR INSTRUCTION"})
    assert seen["json"]["prompt"] == "MY CUSTOM OCR INSTRUCTION"


# ---------------------------------------------------------------------------
# ROOT CAUSE 3/4: opaque errors -> actionable, body-carrying messages.
# ---------------------------------------------------------------------------
def test_400_image_error_surfaces_body_and_hint(monkeypatch):
    prov = make_ollama_provider(
        lambda url, **kw: ollama_error(400, "invalid image data"),
        monkeypatch, ocr_model="llava",
    )
    with pytest.raises(ProviderError) as ei:
        prov.transcribe(_make_pdf())
    msg = str(ei.value)
    assert "400" in msg
    assert "invalid image data" in msg  # the server's body, not a bare code
    assert "PNG/JPEG" in msg            # remediation hint


def test_405_endpoint_misconfig_is_actionable(monkeypatch):
    prov = make_ollama_provider(
        lambda url, **kw: ollama_error(405, text="405 method not allowed"),
        monkeypatch, ocr_model="llava",
    )
    with pytest.raises(ProviderError) as ei:
        prov.transcribe(_make_pdf())
    msg = str(ei.value)
    assert "405" in msg
    assert "PA_OLLAMA_ENDPOINT" in msg
    assert "/v1" in msg  # tells the user the likely cause


def test_model_not_pulled_yields_capability_error(monkeypatch):
    prov = make_ollama_provider(
        lambda url, **kw: ollama_error(
            404, 'model "llava:13b" not found, try pulling it first'),
        monkeypatch, ocr_model="llava:13b",
    )
    with pytest.raises(CapabilityError) as ei:
        prov.transcribe(_make_pdf())
    msg = str(ei.value)
    assert "ollama pull llava:13b" in msg
    assert "not found" in msg


def test_metadata_400_surfaces_body(monkeypatch):
    # extract_structured shares the same error surfacing.
    prov = make_ollama_provider(
        lambda url, **kw: ollama_error(400, "unexpected server error"),
        monkeypatch,
    )
    with pytest.raises(ProviderError) as ei:
        prov.extract_structured("p", {"type": "object"})
    assert "unexpected server error" in str(ei.value)


# ---------------------------------------------------------------------------
# ROOT CAUSE 2: endpoint normalization.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("given,expected_root", [
    ("http://ollama:11434", "http://ollama:11434"),
    ("http://ollama:11434/", "http://ollama:11434"),
    ("http://ollama:11434/v1", "http://ollama:11434"),
    ("http://ollama:11434/v1/", "http://ollama:11434"),
    ("http://ollama:11434/api", "http://ollama:11434"),
    ("http://ollama:11434/api/generate", "http://ollama:11434"),
])
def test_endpoint_normalized_to_root(given, expected_root, monkeypatch):
    seen = {}

    def responder(url, **kw):
        seen["url"] = url
        return {"response": "ok"}

    prov = make_ollama_provider(responder, monkeypatch, ocr_model="llava",
                                endpoint=given)
    assert prov.endpoint == expected_root
    prov.transcribe(_make_pdf())
    assert seen["url"] == f"{expected_root}/api/generate"


# ---------------------------------------------------------------------------
# Capability negotiation still holds (vision-less model refuses before any call).
# ---------------------------------------------------------------------------
def test_visionless_model_refuses_before_call(monkeypatch):
    called = {"n": 0}

    def responder(url, **kw):
        called["n"] += 1
        return {"response": "should not happen"}

    prov = make_ollama_provider(responder, monkeypatch, ocr_model="llama3.1")
    with pytest.raises(CapabilityError):
        prov.transcribe(_make_pdf())
    assert called["n"] == 0  # no HTTP call was made
