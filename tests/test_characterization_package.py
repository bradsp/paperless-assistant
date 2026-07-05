"""Characterization tests re-run against the REFACTORED package.

These assertions are identical in spirit to test_characterization_originals.py.
They must pass unchanged - proving the extraction is behavior-preserving.
Everything runs offline (no live Paperless, no real Anthropic key).
"""
import io
import json
import threading

import pytest

from conftest import SAMPLE_TEXTS

from paperless_assistant import config
from paperless_assistant.ocr import garbage_score, build_overlay_pdf, sanitize_filename
from paperless_assistant.fields import CustomFieldResolver
from paperless_assistant.safety import SafetyLayer
from paperless_assistant.spend import SpendGovernor
from paperless_assistant.stages import StageOrchestrator
from paperless_assistant.taxonomy import TaxonomyResolver
from paperless_assistant.metadata import MetadataExtractor

from fakes import FakeSession, FakeResponse


# ===========================================================================
# garbage_score - EXACT values pinned from the originals
# ===========================================================================
EXPECTED = {
    "clean": (0.095, "wr=0.92 pr=0.87 awl=5.2 n=46"),
    "garbage": (0.865, "wr=0.30 pr=0.00 awl=2.0 n=6"),
    "empty": (1.0, "empty_or_tiny"),
    "tiny": (1.0, "empty_or_tiny"),
    "no_alpha": (1.0, "no_alpha_words"),
}


def test_garbage_score_exact_match_originals():
    for key, expected in EXPECTED.items():
        assert garbage_score(SAMPLE_TEXTS[key]) == expected


def test_garbage_score_bounded():
    for text in SAMPLE_TEXTS.values():
        score, _ = garbage_score(text)
        assert 0.0 <= score <= 1.0


# ===========================================================================
# Re-consume filename sanitization.
#
# THE ONE DELIBERATE BEHAVIOR CHANGE of Phase 2 (approved, <r5_filename_fix>):
# the POC built the re-consume filename with `.replace("/", "-")`, sanitizing
# ONLY the forward slash and leaving the other Windows-illegal characters
# (\ : * ? " < > |) intact - which can break the upload on Windows. This test
# locks in the NEW, safer behavior: ALL path-illegal characters are replaced.
# This is the single expected characterization change in the phase.
# ===========================================================================
def test_filename_sanitizes_all_illegal_chars():
    dirty = 'Inv/oice:2024*?"<>|\\March'
    clean = sanitize_filename(dirty + ".pdf")
    # None of the illegal characters survive.
    for ch in r'\/:*?"<>|':
        assert ch not in clean
    # Legal characters and the extension are preserved.
    assert clean.endswith(".pdf")
    assert "Invoice" in clean.replace("-", "")  # letters intact (dashes inserted)
    # The OLD behavior (replace only "/") would have LEFT these in - prove we don't.
    old_behavior = (dirty + ".pdf").replace("/", "-")
    assert any(ch in old_behavior for ch in r':*?"<>|\\')  # old left them
    assert clean != old_behavior                             # new differs


# ---------------------------------------------------------------------------
# Helpers: build resolvers/clients over a fake session
# ---------------------------------------------------------------------------
def _make_client(session=None):
    from paperless_assistant.client import PaperlessClient

    return PaperlessClient("http://paperless.test:8000", "tok", session=session or FakeSession())


CUSTOM_FIELDS_RESULT = {
    "results": [
        {"id": 1, "name": "ocr_quality", "data_type": "float"},
        {
            "id": 2,
            "name": "ai_stage",
            "data_type": "select",
            "extra_data": {
                "select_options": [
                    {"id": "opt-t", "label": "triaged"},
                    {"id": "opt-r", "label": "reocr_done"},
                    {"id": "opt-m", "label": "metadata_done"},
                ]
            },
        },
        {"id": 3, "name": "ai_notes", "data_type": "text"},
    ],
    "next": None,
}


def _make_resolver(session):
    session.add_json("GET", "/api/custom_fields/", CUSTOM_FIELDS_RESULT)
    return CustomFieldResolver(_make_client(session))


# ===========================================================================
# fields: stage option id mapping + coercion
# ===========================================================================
def test_resolver_stage_option_id():
    s = FakeSession()
    r = _make_resolver(s)
    assert r.stage_option_id("triaged") == "opt-t"
    assert r.stage_option_id("reocr_done") == "opt-r"


def test_resolver_coerce_score():
    assert CustomFieldResolver.coerce_score(0.7, "float") == 0.7
    assert CustomFieldResolver.coerce_score(0.7, "integer") == 70
    assert CustomFieldResolver.coerce_score(0.7, "string") == "0.7"
    assert CustomFieldResolver.coerce_score(0.7, "weird") == 0.7


def test_resolver_stage_label_from_value():
    s = FakeSession()
    r = _make_resolver(s)
    assert r.stage_label_from_value("opt-t") == "triaged"
    assert r.stage_label_from_value(None) is None


# ===========================================================================
# I2: snapshot writes once; restore round-trip
# ===========================================================================
def test_snapshot_writes_once(tmp_path):
    s = FakeSession()
    r = _make_resolver(s)
    safety = SafetyLayer(_make_client(s), r, snapshot_dir=tmp_path)
    doc = {"id": 42, "title": "orig"}
    safety.snapshot(doc)
    safety.snapshot({"id": 42, "title": "MUTATED"})
    saved = json.loads((tmp_path / "42.json").read_text())
    assert saved["title"] == "orig"


# ===========================================================================
# I1: already_triaged
#
# DELIBERATE Phase 4 behaviour change (source correctness fix, r5): the package's
# already_triaged now means "at `triaged` OR any later stage", NOT "exactly
# `triaged`". A doc at `metadata_done` (opt-m) is "already handled" and must be
# skipped, so re-triage can never reset an advanced doc and cause metadata to
# re-process + re-bill it (I1/I3). This intentionally DIVERGES from the frozen
# original scripts' characterization (test_characterization_originals.py, which
# still documents the original buggy exact-match behaviour of the untouched POC).
# ===========================================================================
def test_already_triaged():
    s = FakeSession()
    r = _make_resolver(s)
    orch = StageOrchestrator(_make_client(s), r)
    # triaged -> handled.
    assert orch.already_triaged({"id": 1, "custom_fields": [{"field": 2, "value": "opt-t"}]}) is True
    # reocr_done -> handled (advanced past triage).
    assert orch.already_triaged({"id": 1, "custom_fields": [{"field": 2, "value": "opt-r"}]}) is True
    # metadata_done -> handled (the bug fix: was False, now True).
    assert orch.already_triaged({"id": 1, "custom_fields": [{"field": 2, "value": "opt-m"}]}) is True
    # untriaged / no ai_stage -> eligible.
    assert orch.already_triaged({"id": 1}) is False
    assert orch.already_triaged({"id": 1, "custom_fields": [{"field": 2, "value": None}]}) is False


# ===========================================================================
# I4/I5: merge_triage_fields preserves foreign fields
# ===========================================================================
def test_merge_triage_fields_preserves_and_truncates(tmp_path):
    s = FakeSession()
    r = _make_resolver(s)
    safety = SafetyLayer(_make_client(s), r, snapshot_dir=tmp_path)
    existing = [{"field": 99, "value": "human"}, {"field": 1, "value": 0.1}]
    merged = safety.merge_triage_fields(existing, 0.8, "x" * 400)
    by = {c["field"]: c["value"] for c in merged}
    assert by[99] == "human"
    assert by[1] == 0.8
    assert by[2] == "opt-t"
    assert len(by[3]) == 255


# ===========================================================================
# I1: eligibility + garbage-threshold exclusion
# ===========================================================================
def test_metadata_eligible():
    s = FakeSession()
    r = _make_resolver(s)
    orch = StageOrchestrator(_make_client(s), r)
    assert orch.metadata_eligible(
        {"custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.2}]}
    ) is True
    assert orch.metadata_eligible(
        {"custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.9}]}
    ) is False
    assert orch.metadata_eligible(
        {"custom_fields": [{"field": 2, "value": "opt-m"}, {"field": 1, "value": 0.1}]}
    ) is False
    # boundary: exactly at threshold -> excluded
    assert orch.metadata_eligible(
        {"custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.55}]}
    ) is False
    # empty stage -> eligible
    assert orch.metadata_eligible({"custom_fields": [{"field": 1, "value": 0.1}]}) is True


def test_reocr_matches():
    s = FakeSession()
    r = _make_resolver(s)
    orch = StageOrchestrator(_make_client(s), r)
    assert orch.reocr_matches(
        {"custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.9}]}, 0.55
    ) is True
    assert orch.reocr_matches(
        {"custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.2}]}, 0.55
    ) is False
    assert orch.reocr_matches(
        {"custom_fields": [{"field": 2, "value": "opt-m"}, {"field": 1, "value": 0.9}]}, 0.55
    ) is False


# ===========================================================================
# I5: TaxonomyResolver reuse-first, case-insensitive, new-flagging
# ===========================================================================
def _make_taxonomy(session):
    def tags(m, u, **kw):
        return FakeResponse(200, {"results": [{"id": 1, "name": "Invoice"}, {"id": 2, "name": "Bank"}], "next": None})

    def corr(m, u, **kw):
        return FakeResponse(200, {"results": [{"id": 5, "name": "Acme Corp"}], "next": None})

    def types(m, u, **kw):
        return FakeResponse(200, {"results": [{"id": 8, "name": "Statement"}], "next": None})

    session.add("GET", "/api/tags/?page_size=200", tags)
    session.add("GET", "/api/correspondents/?page_size=200", corr)
    session.add("GET", "/api/document_types/?page_size=200", types)
    return TaxonomyResolver(_make_client(session))


def test_taxonomy_reuse_case_insensitive():
    s = FakeSession()
    tax = _make_taxonomy(s)
    assert tax.tag_id("invoice") == (1, False)
    assert tax.correspondent_id("ACME CORP") == (5, False)
    assert tax.doc_type_id("statement") == (8, False)


def test_taxonomy_new_flags_creation():
    s = FakeSession()
    tax = _make_taxonomy(s)
    created = []
    s.add("POST", "/api/tags/", lambda m, u, **kw: (created.append(kw.get("json")), FakeResponse(200, {"id": 999}))[1])
    tid, was_new = tax.tag_id("BrandNewTag")
    assert (tid, was_new) == (999, True)
    # reuse without re-creating
    assert tax.tag_id("brandnewtag") == (999, False)
    assert len(created) == 1


# ===========================================================================
# I3: SpendGovernor cap abort + thread-safe accumulation
# ===========================================================================
def test_spend_should_abort():
    gov = SpendGovernor(max_spend=5.0)
    assert gov.should_abort() is False
    gov.add(6.0)
    assert gov.should_abort() is True


def test_spend_no_cap_never_aborts():
    gov = SpendGovernor(max_spend=0.0)
    gov.add(1000.0)
    assert gov.should_abort() is False


def test_spend_threadsafe():
    gov = SpendGovernor(max_spend=0.0)

    def add():
        for _ in range(1000):
            gov.add(0.001)

    threads = [threading.Thread(target=add) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert round(gov.total, 3) == 8.0


# ===========================================================================
# I4/I5: MetadataExtractor.apply_metadata merge-not-clobber + flag
# ===========================================================================
def test_apply_metadata_merges_and_flags(tmp_path):
    s = FakeSession()
    r = _make_resolver(s)
    tax = _make_taxonomy(s)
    captured = {}
    s.add("PATCH", "/api/documents/", lambda m, u, **kw: (captured.__setitem__("body", kw["json"]), FakeResponse(200, {"id": 1}))[1])
    s.add("POST", "/api/tags/", lambda m, u, **kw: FakeResponse(200, {"id": 777}))

    safety = SafetyLayer(_make_client(s), r, snapshot_dir=tmp_path)
    spend = SpendGovernor()
    ext = MetadataExtractor(_make_client(s), r, tax, safety, spend, api_key="x", new_tax_tag_id=555)

    doc = {"id": 7, "tags": [100, 200], "custom_fields": [{"field": 99, "value": "keep"}, {"field": 2, "value": "opt-t"}]}
    meta = {
        "title": "New Title", "correspondent": "Acme Corp", "document_type": "Statement",
        "tags": ["Invoice", "SomethingNew"], "correspondent_is_new": False,
        "document_type_is_new": False, "new_tags": ["SomethingNew"],
    }
    created = ext.apply_metadata(doc, meta)
    body = captured["body"]
    assert created is True
    assert body["title"] == "New Title"
    assert body["correspondent"] == 5
    assert body["document_type"] == 8
    assert set([100, 200, 1, 555]).issubset(set(body["tags"]))  # human tags + Invoice + flag
    cf = {c["field"]: c["value"] for c in body["custom_fields"]}
    assert cf[99] == "keep"
    assert cf[2] == "opt-m"


def test_apply_metadata_no_new_no_flag(tmp_path):
    s = FakeSession()
    r = _make_resolver(s)
    tax = _make_taxonomy(s)
    captured = {}
    s.add("PATCH", "/api/documents/", lambda m, u, **kw: (captured.__setitem__("body", kw["json"]), FakeResponse(200, {"id": 1}))[1])
    safety = SafetyLayer(_make_client(s), r, snapshot_dir=tmp_path)
    ext = MetadataExtractor(_make_client(s), r, tax, safety, SpendGovernor(), api_key="x", new_tax_tag_id=555)
    doc = {"id": 7, "tags": [100], "custom_fields": []}
    meta = {
        "title": "T", "correspondent": "Acme Corp", "document_type": "Statement",
        "tags": ["Invoice", "Bank"], "correspondent_is_new": False,
        "document_type_is_new": False, "new_tags": [],
    }
    assert ext.apply_metadata(doc, meta) is False
    assert 555 not in captured["body"]["tags"]


# ===========================================================================
# I4: mark_old_superseded
# ===========================================================================
def test_mark_old_superseded(tmp_path):
    s = FakeSession()
    r = _make_resolver(s)
    captured = {}
    s.add("PATCH", "/api/documents/", lambda m, u, **kw: (captured.__setitem__("body", kw["json"]), FakeResponse(200, {}))[1])
    safety = SafetyLayer(_make_client(s), r, snapshot_dir=tmp_path)
    old_doc = {"id": 3, "tags": [10, 20], "custom_fields": [{"field": 1, "value": 0.9}, {"field": 2, "value": "opt-t"}]}
    safety.mark_old_superseded(old_doc, superseded_tag_id=77)
    body = captured["body"]
    assert 77 in body["tags"] and 10 in body["tags"] and 20 in body["tags"]
    cf = {c["field"]: c["value"] for c in body["custom_fields"]}
    assert cf[1] == 0.9
    assert cf[2] == "opt-r"


# ===========================================================================
# Risk #3: build_overlay_pdf
# ===========================================================================
def _make_source_pdf(pagesize, n_pages=1):
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=pagesize)
    for i in range(n_pages):
        c.drawString(72, 72, f"VISUAL PAGE {i}")
        c.showPage()
    c.save()
    return buf.getvalue()


def _extract(pdf_bytes):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages), len(reader.pages)


def test_overlay_extractable_letter():
    from reportlab.lib.pagesizes import letter

    out = build_overlay_pdf(_make_source_pdf(letter), "HELLO OVERLAY WORLD invoice 123")
    text, n = _extract(out)
    assert "HELLO OVERLAY WORLD" in text and n == 1


def test_overlay_multipage_preserves_count():
    from reportlab.lib.pagesizes import letter

    out = build_overlay_pdf(_make_source_pdf(letter, 3), "line one\nline two")
    text, n = _extract(out)
    assert n == 3 and "line one" in text


def test_overlay_odd_page_size():
    out = build_overlay_pdf(_make_source_pdf((200, 900)), "ODDSIZE content")
    text, n = _extract(out)
    assert "ODDSIZE" in text and n >= 1
