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

"""OcrPipeline - garbage-score heuristic, AI vision re-OCR, invisible-text
overlay PDF builder, and re-consume + supersede flow.

Extracted from: `garbage_score`, `claude_ocr`, `build_overlay_pdf`,
`process_one` (stage1) plus `garbage_score` (stage0).

The Anthropic vision call is isolated behind `_transcribe()` (a thin internal
seam) so Phase 2 can introduce the provider abstraction later. Per the phase
scope, the abstraction itself is NOT built here - Anthropic is called directly.

The overlay is pure-python (pypdf + reportlab), no system deps (plan §4.2).
"""
from __future__ import annotations

import io
import re
import textwrap

from . import config
from .providers.base import CAP_VISION, CapabilityError


# ===========================================================================
# Garbage-score heuristic (local, free - no API). Byte-identical to the POC.
# ===========================================================================
def garbage_score(text: str, heuristic=None):
    """Return (score, note). 0.0 = clean text, 1.0 = garbage/empty.

    Prompt 011 (Advanced): the coefficients + gates are supplied by `heuristic`
    (a `config.GarbageHeuristic`). When omitted, the DEFAULT coefficients are used,
    which reproduce the pre-011 (POC) scores BYTE-FOR-BYTE — this is the byte-
    identical default path. A power user may tune the weights/gates from the
    Advanced config; misconfiguring changes what gets flagged for re-OCR."""
    h = heuristic or config.GarbageHeuristic()
    if not text or len(text.strip()) < h.min_length:
        return 1.0, "empty_or_tiny"

    words = re.findall(r"[A-Za-z]{2,}", text)
    if not words:
        return 1.0, "no_alpha_words"

    nonspace = re.sub(r"\s", "", text)
    wordchars = sum(len(w) for w in words)
    word_ratio = wordchars / max(len(nonspace), 1)

    plausible = [
        w for w in words
        if len(w) >= h.plausible_min_len and re.search(r"[aeiouAEIOU]", w)
    ]
    plausible_ratio = len(plausible) / max(len(words), 1)

    avg_word_len = wordchars / max(len(words), 1)
    frag_penalty = 1.0 if avg_word_len < h.fragment_threshold else 0.0

    score = 1.0 - (
        h.word_ratio_weight * word_ratio
        + h.plausible_weight * plausible_ratio
        + h.fragment_weight * (1 - frag_penalty)
    )
    score = max(0.0, min(1.0, score))

    note = (
        f"wr={word_ratio:.2f} pr={plausible_ratio:.2f} "
        f"awl={avg_word_len:.1f} n={len(words)}"
    )
    return round(score, 3), note


# ===========================================================================
# Invisible-text overlay builder (pure-python). Byte-identical to the POC.
# ===========================================================================
def build_overlay_pdf(original_bytes, text):
    """Return new PDF bytes = original visual pages + invisible text overlay.
    Text is placed on page 1 (Paperless indexes the whole doc's extracted text,
    so single-page placement is sufficient for searchability)."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    reader = PdfReader(io.BytesIO(original_bytes))
    writer = PdfWriter()

    try:
        box = reader.pages[0].mediabox
        pw, ph = float(box.width), float(box.height)
    except Exception:
        pw, ph = letter

    ov_buf = io.BytesIO()
    co = canvas.Canvas(ov_buf, pagesize=(pw, ph))
    to = co.beginText(20, ph - 20)
    to.setTextRenderMode(3)  # invisible
    to.setFont("Helvetica", 6)
    wrap_width = max(40, int(pw / 4))
    for raw_line in text.splitlines() or [text]:
        for line in (textwrap.wrap(raw_line, wrap_width) or [""]):
            to.textLine(line)
            if to.getY() < 20:
                co.drawText(to)
                co.showPage()
                to = co.beginText(20, ph - 20)
                to.setTextRenderMode(3)
                to.setFont("Helvetica", 6)
    co.drawText(to)
    co.save()
    ov_buf.seek(0)

    ov_reader = PdfReader(ov_buf)
    n_orig = len(reader.pages)
    n_ov = len(ov_reader.pages)
    for i in range(n_orig):
        page = reader.pages[i]
        if i < n_ov:
            page.merge_page(ov_reader.pages[i])
        writer.add_page(page)
    for j in range(n_orig, n_ov):
        writer.add_page(ov_reader.pages[j])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text from this document exactly as "
    "it appears, preserving reading order, line breaks, numbers, dates, and "
    "punctuation. Do not summarize, interpret, translate, or add commentary. "
    "If a region is illegible, write [illegible]. Output only the transcribed text."
)


# Characters that are illegal in filenames on Windows (and unsafe on POSIX).
# Phase 2 deliberate fix (approved): the POC only replaced "/" with "-", leaving
# \ : * ? " < > | intact, which can break the re-consume upload on Windows.
_ILLEGAL_FILENAME_CHARS = r'\/:*?"<>|'


def sanitize_filename(name: str) -> str:
    """Replace ALL path-illegal characters with '-'. Deliberate, safer
    replacement for the POC's `.replace("/", "-")` (see <r5_filename_fix>)."""
    return "".join("-" if c in _ILLEGAL_FILENAME_CHARS else c for c in name)


class OcrPipeline:
    """Drives download -> transcribe -> overlay -> re-consume -> supersede.

    Anthropic is called directly via `_transcribe`; that method is the single
    seam Phase 2 will replace with a provider abstraction.
    """

    def __init__(self, client, resolver, safety, spend, *, api_key=None,
                 built_dir, provider=None, instruction=None):
        import pathlib

        self.client = client
        self.resolver = resolver
        self.safety = safety
        self.spend = spend
        self.api_key = api_key
        # The resolved OCR/vision INSTRUCTION (default -> override -> +extra).
        # None keeps the built-in OCR_PROMPT, so existing callers/tests behave
        # byte-identically. It's passed to the provider via transcribe(opts=...);
        # each adapter uses opts["prompt"] when present, else its OCR_PROMPT.
        self.instruction = instruction
        # Phase 2: the vision seam delegates to an AIProvider. Default to the
        # Anthropic adapter built from api_key so existing callers/tests behave
        # exactly as before.
        if provider is None:
            from .providers.anthropic import AnthropicProvider

            provider = AnthropicProvider(
                api_key=api_key,
                ocr_model=config.OCR_MODEL,
                metadata_model=config.METADATA_MODEL,
                max_ocr_tokens=config.MAX_OCR_TOKENS,
            )
        self.provider = provider
        self.built_dir = pathlib.Path(built_dir)
        self.built_dir.mkdir(parents=True, exist_ok=True)

    # -- vision seam: delegates to the provider (Phase 2 abstraction) ------
    def _transcribe(self, pdf_bytes):
        """Transcribe via the configured provider; return (text, in_tokens,
        out_tokens, cost). Return shape unchanged so the pipeline is untouched.

        Capability negotiation (plan §5.3): if the provider lacks `vision`,
        refuse with a clear error BEFORE any billable call - not a silent
        downgrade."""
        if CAP_VISION not in getattr(self.provider, "capabilities", set()):
            raise CapabilityError(
                f"provider '{getattr(self.provider, 'name', '?')}' lacks the "
                f"'vision' capability required for re-OCR. Configure a vision-"
                f"capable model for the OCR task, or skip re-OCR for this provider."
            )
        # Pass the resolved instruction (when customized) via opts so ANY adapter
        # can honor it without the pipeline knowing the provider. None -> the
        # adapter's built-in OCR_PROMPT (byte-identical default).
        opts = {"prompt": self.instruction} if self.instruction else None
        t = self.provider.transcribe(pdf_bytes, opts=opts)
        self.spend.add(t.cost)
        return t.text, t.in_tokens, t.out_tokens, t.cost

    # -- per-document pipeline (mirrors stage1.process_one) ----------------
    def process_one(self, doc, superseded_tag_id, *, dry_run, on_record=None):
        """Re-OCR one doc; supersede old -> new (or, in dry-run, only build the
        corrected PDF without consuming).

        `on_record` (prompt 013, OPTIONAL) is a best-effort observational hook:
        when provided it is called with the supersession relationship
        (`doc`, `new_doc_id`, `dry_run`) so a caller can record a field-level
        activity diff. Invoked inside a try/except so a recorder failure can NEVER
        affect processing; omitted => byte-identical to before. It is NEVER called
        for a spend-cap / empty-OCR / no-new-id no-op."""
        doc_id = doc["id"]

        # Capability negotiation FIRST (plan §5.3): a vision-less provider must
        # refuse re-OCR before any download/consume happens - no partial work.
        if CAP_VISION not in getattr(self.provider, "capabilities", set()):
            raise CapabilityError(
                f"provider '{getattr(self.provider, 'name', '?')}' lacks the "
                f"'vision' capability required for re-OCR. Configure a vision-"
                f"capable model for the OCR task, or skip re-OCR for this provider."
            )

        self.safety.snapshot(doc)

        # spend guard BEFORE the API call (I3)
        if self.spend.should_abort():
            return ("spend_cap", doc_id, "skipped: spend cap reached", 0.0)

        original = self.client.download_original(doc_id)
        text, it, ot, cost = self._transcribe(original)

        if not text.strip():
            return ("empty_ocr", doc_id, "Claude returned no text", cost)

        corrected = build_overlay_pdf(original, text)
        (self.built_dir / f"{doc_id}_corrected.pdf").write_bytes(corrected)
        (self.built_dir / f"{doc_id}_text.txt").write_text(text)

        if dry_run:
            self._maybe_record(on_record, doc, new_doc_id=None, dry_run=True)
            return (
                "dry",
                doc_id,
                f"OCR ok ({len(text)} chars, in={it} out={ot}); PDF built, NOT consumed",
                cost,
            )

        filename = sanitize_filename(f"{(doc.get('title') or ('doc_' + str(doc_id)))}.pdf")
        task_uuid = self.client.post_document(corrected, doc, filename)
        new_doc_id = self.client.find_new_doc_by_task(task_uuid)
        if not new_doc_id:
            return ("no_new_id", doc_id, f"consumed (task {task_uuid}) but new id unknown", cost)

        self._apply_post_consume_metadata(new_doc_id, doc)
        self.safety.mark_old_superseded(doc, superseded_tag_id)

        self._maybe_record(on_record, doc, new_doc_id=new_doc_id, dry_run=False)
        return ("done", doc_id, f"new doc {new_doc_id}; old tagged superseded", cost)

    @staticmethod
    def _maybe_record(on_record, doc, *, new_doc_id, dry_run):
        """Invoke the optional activity hook, best-effort. Never raises."""
        if on_record is None:
            return
        try:
            on_record(doc=doc, new_doc_id=new_doc_id, dry_run=dry_run)
        except Exception:  # noqa: BLE001 — observational; never affect processing
            pass

    def _apply_post_consume_metadata(self, new_doc_id, old_doc):
        """Carry ocr_quality + notes to the NEW doc, set ai_stage=reocr_done.
        Mirrors stage1.apply_post_consume_metadata exactly."""
        r = self.resolver
        old_cf = {cf["field"]: cf.get("value") for cf in (old_doc.get("custom_fields") or [])}
        score_val = old_cf.get(r.score_field_id())
        notes_val = old_cf.get(r.notes_field_id()) or ""
        cf_payload = [
            {
                "field": r.stage_field_id(),
                "value": r.stage_option_for_role(config.STAGE_REOCR_DONE),
            },
            {
                "field": r.notes_field_id(),
                "value": (f"reocr from doc {old_doc['id']}; " + str(notes_val))[:255],
            },
        ]
        if score_val is not None:
            cf_payload.append({"field": r.score_field_id(), "value": score_val})
        self.client.request(
            "PATCH",
            f"{self.client.base}/api/documents/{new_doc_id}/",
            json={"custom_fields": cf_payload},
        )
