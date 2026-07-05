"""End-to-end pipeline tests with a fully mocked Paperless surface and a stubbed
Anthropic client. No live server, no real API key, deterministic + free.

Covers the OCR re-consume+supersede flow (I2/I3/I4) and the metadata flow
(I3/I4/I5), including the spend-cap abort gate (I3).
"""
import io

import pytest

from paperless_assistant.client import PaperlessClient
from paperless_assistant.fields import CustomFieldResolver
from paperless_assistant.taxonomy import TaxonomyResolver
from paperless_assistant.safety import SafetyLayer
from paperless_assistant.spend import SpendGovernor
from paperless_assistant.ocr import OcrPipeline
from paperless_assistant.metadata import MetadataExtractor

from fakes import (
    FakeSession,
    FakeResponse,
    StubMessage,
    StubTextBlock,
    StubToolUseBlock,
    install_stub_anthropic,
)


BASE = "http://paperless.test:8000"

CUSTOM_FIELDS = {
    "results": [
        {"id": 1, "name": "ocr_quality", "data_type": "float"},
        {"id": 2, "name": "ai_stage", "data_type": "select",
         "extra_data": {"select_options": [
             {"id": "opt-t", "label": "triaged"},
             {"id": "opt-r", "label": "reocr_done"},
             {"id": "opt-m", "label": "metadata_done"},
         ]}},
        {"id": 3, "name": "ai_notes", "data_type": "text"},
    ],
    "next": None,
}


def _source_pdf():
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 72, "scan")
    c.showPage()
    c.save()
    return buf.getvalue()


def _client(session):
    return PaperlessClient(BASE, "tok", session=session)


def _resolver(session):
    session.add_json("GET", "/api/custom_fields/", CUSTOM_FIELDS)
    return CustomFieldResolver(_client(session))


def _taxonomy(session):
    session.add("GET", "/api/tags/?page_size=200", lambda m, u, **k: FakeResponse(200, {"results": [], "next": None}))
    session.add("GET", "/api/correspondents/?page_size=200", lambda m, u, **k: FakeResponse(200, {"results": [], "next": None}))
    session.add("GET", "/api/document_types/?page_size=200", lambda m, u, **k: FakeResponse(200, {"results": [], "next": None}))
    return TaxonomyResolver(_client(session))


# ===========================================================================
# OCR pipeline: full re-consume + supersede (stubbed Claude vision)
# ===========================================================================
def test_ocr_process_one_full_flow(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "snaps")
    spend = SpendGovernor(max_spend=0.0)
    pipeline = OcrPipeline(_client(session), resolver, safety, spend,
                           api_key="x", built_dir=tmp_path / "built")

    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubTextBlock("CLEAN TRANSCRIBED TEXT")]))

    pdf = _source_pdf()
    session.add("GET", "/api/documents/7/download/", lambda m, u, **k: FakeResponse(200, content=pdf))
    session.add("POST", "/api/documents/post_document/", lambda m, u, **k: FakeResponse(200, "task-1"))
    session.add("GET", "/api/tasks/", lambda m, u, **k: FakeResponse(200, [{"status": "SUCCESS", "related_document": 700}]))
    patched = []
    session.add("PATCH", "/api/documents/", lambda m, u, **k: (patched.append((u, k["json"])), FakeResponse(200, {}))[1])

    doc = {"id": 7, "title": "Bad Scan", "tags": [11], "custom_fields": [
        {"field": 1, "value": 0.9}, {"field": 3, "value": "wr=0.1"}, {"field": 2, "value": "opt-t"}]}
    status, doc_id, msg, cost = pipeline.process_one(doc, superseded_tag_id=99, dry_run=False)

    assert status == "done"
    assert cost > 0  # spend accounted
    # snapshot written
    assert (tmp_path / "snaps" / "7.json").exists()
    # corrected PDF + text written
    assert (tmp_path / "built" / "7_corrected.pdf").exists()
    # two PATCHes: new doc metadata (700) + old doc superseded (7)
    urls = [u for (u, _) in patched]
    assert any("/documents/700/" in u for u in urls)
    assert any("/documents/7/" in u for u in urls)
    # old doc PATCH advances stage to reocr_done and adds superseded tag
    old_body = [b for (u, b) in patched if "/documents/7/" in u][0]
    assert 99 in old_body["tags"]
    assert {"field": 2, "value": "opt-r"} in old_body["custom_fields"]


def test_ocr_dry_run_does_not_consume(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")
    pipeline = OcrPipeline(_client(session), resolver, safety, SpendGovernor(),
                           api_key="x", built_dir=tmp_path / "b")
    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubTextBlock("TEXT")]))
    pdf = _source_pdf()
    session.add("GET", "/api/documents/7/download/", lambda m, u, **k: FakeResponse(200, content=pdf))

    doc = {"id": 7, "title": "x", "custom_fields": []}
    status, _, msg, _ = pipeline.process_one(doc, superseded_tag_id=99, dry_run=True)
    assert status == "dry"
    assert "NOT consumed" in msg
    # no post_document / patch routes were needed -> none called
    assert not any(c[0] == "POST" and "post_document" in c[1] for c in session.calls)


def test_ocr_spend_cap_blocks_before_api(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")
    spend = SpendGovernor(max_spend=5.0)
    spend.add(6.0)  # already over cap
    pipeline = OcrPipeline(_client(session), resolver, safety, spend, api_key="x", built_dir=tmp_path / "b")

    called = {"anthropic": False}

    def responder(**kw):
        called["anthropic"] = True
        return StubMessage([StubTextBlock("T")])

    install_stub_anthropic(monkeypatch, responder)

    doc = {"id": 7, "title": "x", "custom_fields": []}
    status, _, msg, cost = pipeline.process_one(doc, superseded_tag_id=99, dry_run=False)
    assert status == "spend_cap"
    assert cost == 0.0
    assert called["anthropic"] is False  # aborted BEFORE the billable call (I3)


def test_ocr_empty_transcription(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")
    pipeline = OcrPipeline(_client(session), resolver, safety, SpendGovernor(), api_key="x", built_dir=tmp_path / "b")
    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubTextBlock("   ")]))
    session.add("GET", "/api/documents/7/download/", lambda m, u, **k: FakeResponse(200, content=_source_pdf()))
    doc = {"id": 7, "title": "x", "custom_fields": []}
    status, _, msg, _ = pipeline.process_one(doc, superseded_tag_id=99, dry_run=False)
    assert status == "empty_ocr"


# ===========================================================================
# Metadata pipeline: full apply (stubbed forced tool use)
# ===========================================================================
def _meta_message():
    return StubMessage([StubToolUseBlock({
        "title": "Acme Invoice March",
        "correspondent": "Acme Corp",
        "document_type": "Invoice",
        "tags": ["billing"],
        "correspondent_is_new": True,
        "document_type_is_new": True,
        "new_tags": ["billing"],
    })])


def test_metadata_process_one_full_flow(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)  # empty taxonomy -> everything is new
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")
    spend = SpendGovernor()
    # taxonomy create + doc patch
    session.add("POST", "/api/correspondents/", lambda m, u, **k: FakeResponse(200, {"id": 5}))
    session.add("POST", "/api/document_types/", lambda m, u, **k: FakeResponse(200, {"id": 8}))
    session.add("POST", "/api/tags/", lambda m, u, **k: FakeResponse(200, {"id": 30}))
    patched = []
    session.add("PATCH", "/api/documents/", lambda m, u, **k: (patched.append(k["json"]), FakeResponse(200, {}))[1])

    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend,
                            api_key="x", new_tax_tag_id=555)
    install_stub_anthropic(monkeypatch, lambda **kw: _meta_message())

    doc = {"id": 7, "title": "old", "content": "some text", "tags": [11], "custom_fields": [{"field": 2, "value": "opt-t"}]}
    status, doc_id, msg, cost = ext.process_one(doc, dry_run=False)
    assert status == "done_new_tax"  # created new taxonomy -> flagged
    assert cost > 0
    body = patched[0]
    assert body["correspondent"] == 5
    assert body["document_type"] == 8
    assert 11 in body["tags"]      # human tag preserved
    assert 30 in body["tags"]      # new billing tag
    assert 555 in body["tags"]     # ai-new-taxonomy flag
    assert {"field": 2, "value": "opt-m"} in body["custom_fields"]


def test_metadata_dry_run_no_write(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, SpendGovernor(),
                            api_key="x", new_tax_tag_id=555)
    install_stub_anthropic(monkeypatch, lambda **kw: _meta_message())
    doc = {"id": 7, "title": "old", "content": "text", "custom_fields": []}
    status, _, msg, _ = ext.process_one(doc, dry_run=True)
    assert status == "dry"
    assert "NEW corr=Acme Corp" in msg
    # no PATCH happened
    assert not any(c[0] == "PATCH" for c in session.calls)


def test_metadata_spend_cap_aborts(tmp_path, monkeypatch):
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")
    spend = SpendGovernor(max_spend=5.0)
    spend.add(10.0)
    called = {"anthropic": False}
    install_stub_anthropic(monkeypatch, lambda **kw: (called.__setitem__("anthropic", True), _meta_message())[1])
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend, api_key="x", new_tax_tag_id=555)
    doc = {"id": 7, "content": "t", "custom_fields": []}
    status, _, _, cost = ext.process_one(doc, dry_run=False)
    assert status == "spend_cap"
    assert called["anthropic"] is False
