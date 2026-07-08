# Ollama vision re-OCR — root-cause diagnosis & fix

## Summary

Re-OCR through a local Ollama vision model failed for every model with an opaque
HTTP **400** (image documents and PDFs) or **405** (misconfigured endpoint). Three
independent defects combined; the opaque error surfacing hid which one was biting.
All three are fixed.

## Evidence gathered

Traced one document end-to-end:

`OcrPipeline.process_one` → `self.client.download_original(doc_id)` →
`OcrPipeline._transcribe(pdf_bytes)` → `OllamaProvider.transcribe(doc)` → `httpx.post`.

Key observations from the code (pre-fix):

- `client.download_original` issues `GET /api/documents/<id>/download/?original=true`
  and returns `r.content` — the **stored original**, which for the overwhelming
  majority of Paperless documents is a **PDF** container (and for image uploads, a
  raster original).
- `providers/ollama.py::transcribe` did
  `b64 = base64.standard_b64encode(doc)` and posted `images: [b64]` to
  `{endpoint}/api/generate`, then `r.raise_for_status()`.
- Confirmed the bytes handed to `images` began with `%PDF` (base64 prefix
  `JVBER…`), i.e. a **PDF container was placed in a field that expects raster
  image bytes**.
- No PDF renderer was installed in the environment (`pypdfium2`, `pymupdf`,
  `pdf2image` all absent); only `pillow` (can't rasterize PDF) and `httpx`.
- `endpoint.rstrip("/") + "/api/generate"` does **no** path normalization, so a
  `PA_OLLAMA_ENDPOINT` of `…:11434/v1` or `…:11434/api` builds
  `…/v1/api/generate` / `…/api/api/generate`, which Ollama answers with 404/405.
- `r.raise_for_status()` raises `httpx.HTTPStatusError` **without** Ollama's JSON
  error body (`{"error": "…"}`), which is exactly where the actionable reason
  lives (`model "x" not found, try pulling it first`, `invalid image data`, …).

## Hypotheses — confirmed / refuted

| # | Hypothesis | Verdict | Evidence |
|---|-----------|---------|----------|
| 1 | **PDF bytes sent to `images` → 400** | **CONFIRMED (primary)** | `download_original` returns the PDF; adapter base64'd it straight into `images`; verified the payload begins with `%PDF`. Ollama's `images` decodes PNG/JPEG only. |
| 2 | **Endpoint path/method mismatch → 405** | **CONFIRMED (secondary)** | No normalization of `endpoint`; a `/v1` or `/api` suffix yields a non-existent path → 404/405. Reproduced by asserting the built URL. |
| 3 | **Opaque error hides the reason** | **CONFIRMED** | `raise_for_status()` drops the response body; the user only ever saw "error 400/405". |
| 4 | **Model not pulled / not vision** | **CONFIRMED as a distinct case** | Ollama returns 404 `model "…" not found, try pulling it first`; previously indistinguishable from any other 404. The vision-capability gate (`_looks_vision`) already refuses non-vision *names* before any call and is preserved. |

## The failing request vs. the fixed request

**Before (fails):**
```
POST http://host:11434/api/generate           # or …/v1/api/generate on a misconfig
{ "model": "llava:13b",
  "prompt": "...",
  "images": ["JVBERi0xLjQK…"],                 # <-- base64 of a %PDF container
  "stream": false }
→ HTTP 400  (body swallowed by raise_for_status)  → user sees only "error 400"
```

**After (succeeds):**
```
POST http://host:11434/api/generate            # endpoint normalized to the root
{ "model": "llava:13b",
  "prompt": "...",
  "images": ["iVBORw0KGgo…", "iVBORw0KGgo…"],   # <-- one base64 PNG per PDF page
  "stream": false }
→ HTTP 200  { "response": "…transcribed text…", "prompt_eval_count": …, "eval_count": … }
```

## The fix (why it's correct)

1. **Rasterize before the call** (`providers/ollama.py::_doc_to_images`). A PDF is
   rendered to **one PNG per page** at 200 DPI via **`pypdfium2`** (prebuilt
   wheels, bundled PDFium — pure-pip, **no system deps**: no poppler/ghostscript,
   honoring plan §4.2). An already-raster original (PNG/JPEG/…) is detected by
   magic bytes and passed through unchanged. This puts real image data in
   `images`, which is exactly what Ollama's API contract requires. Multi-page PDFs
   send every page. This is **Ollama-specific** (kept in the adapter), so the
   Anthropic `document`-block path and the OpenAI path are untouched — they were
   confirmed to send the container themselves and must not be regressed.

2. **Endpoint normalization** (`_normalize_endpoint`). Strips a single trailing
   well-known suffix (`/v1`, `/api`, `/api/generate`, `/api/chat`, `/api/tags`) so
   the effective URL is always `<root>/api/generate`. We support the **native
   Ollama `/api/generate` contract** (documented), not the OpenAI-compatible
   `/v1` shape — a deliberate single-contract choice.

3. **Actionable errors** (`_raise_for_ollama` + `_error_body`) replace
   `raise_for_status()`. The message now carries the **status code + Ollama's
   response body** plus a targeted remediation hint:
   - 404 / "not found" / "try pulling" → `CapabilityError` "run `ollama pull <model>` …".
   - 405 → `ProviderError` "check PA_OLLAMA_ENDPOINT points at the Ollama ROOT, not a /v1 path".
   - 400 mentioning "image" → `ProviderError` "…needs PNG/JPEG, not a PDF container".
   - anything else → status + body verbatim.

4. **Earlier diagnostics** (`doctor.py`). A cheap, no-network **endpoint-shape**
   check WARNs on a `/v1` or `/api` endpoint; a FAIL if `pypdfium2` is missing for
   an Ollama OCR task. An **opt-in** best-effort `/api/tags` probe
   (`pa doctor --probe-ollama`, free, no inference) confirms reachability and that
   the configured model is pulled — kept opt-in to preserve the "capability, not a
   live network call" default.

## Verification

- Full suite: **399 passed** (385 baseline + 14 new). No Anthropic/OpenAI test
  changed behavior.
- New regression tests (`tests/test_ollama_reocr.py`) assert the `images` payload
  is PNG (`iVBOR…`) not raw PDF (`JVBER…`) — these **fail on the old adapter** and
  pass on the fixed one — plus the 400/405/not-pulled error text and endpoint
  normalization.
- End-to-end demonstration (stubbed transport reproducing real Ollama shapes):
  a 2-page PDF re-OCRs to non-empty text with a normalized URL and PNG images; the
  three failure modes each produce a distinct, body-carrying, actionable error.
