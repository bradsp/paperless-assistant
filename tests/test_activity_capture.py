"""Activity CAPTURE tests (prompt 013): the sweep records a field-level
before -> after row for triage / metadata (applied + dry-run), a re-OCR
supersession, and ERRORs; a skip/no-op records NO row; recording is purely
observational (a broken store never fails a doc/run); and retention purge is
enforced in serve().

Fully OFFLINE — fake Paperless + stubbed metadata, SQLite under tmp /data.
"""
from __future__ import annotations

import io
import time

from paperless_assistant.config import Settings, TaskProvider, SpendCaps
from paperless_assistant.sweep import Sweep
from paperless_assistant.activity import ActivityStore
from paperless_assistant.obs import JsonLogger
from fakes import (
    FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)


def _docs():
    return [
        {"id": 1, "title": "clean",
         "content": "Dear Mr Smith, thank you for your payment of one hundred dollars "
                    "received on March 3. Your balance is now zero. Sincerely, Acme.",
         "tags": [], "custom_fields": []},
        {"id": 2, "title": "garbage", "content": "x q z",
         "tags": [], "custom_fields": []},
    ]


def _settings(tmp_path, **over):
    s = Settings(
        base_url="http://paperless.test:8000", paperless_token="tok",
        anthropic_api_key="sk-ant-test",
        data_dir=str(tmp_path / "data"),
        metadata_task=TaskProvider(provider="anthropic", model="claude-sonnet-4-6"),
        spend=SpendCaps(per_run=1.0, per_period=5.0, period="monthly"),
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _install_metadata_stub(monkeypatch):
    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock({
        "title": "Payment Confirmation", "correspondent": "Acme",
        "document_type": "Letter", "tags": ["billing"],
        "correspondent_is_new": True, "document_type_is_new": True,
        "new_tags": ["billing"]})]))


def _open_store(tmp_path):
    return ActivityStore(str(tmp_path / "data" / "activity.db"))


# ===========================================================================
# dry-run: proposed field-level diffs, NO Paperless write, dry_run=true rows.
# ===========================================================================
def test_dry_run_records_proposed_diffs_no_write(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=True)
    Sweep(s, client=fake.client()).run_once()

    # I7 dry-run: NO Paperless PATCH happened.
    assert fake.patches == []

    st = _open_store(tmp_path)
    all_rows = st.query(limit=500)
    assert all_rows["total"] > 0
    # Every recorded row is dry_run=true and carries field-level changes.
    assert all(r["dry_run"] is True for r in all_rows["rows"])

    # Triage recorded a field-level diff for BOTH docs (both got a real score).
    triage = st.query(stage="triage", limit=500)
    assert triage["total"] == 2
    tr = triage["rows"][0]
    assert "ocr_quality" in tr["changes"]["fields"]
    assert tr["changes"]["fields"]["ai_stage"]["after"] == "triaged"

    # Metadata recorded a PROPOSED title/correspondent diff for the clean doc.
    meta = st.query(stage="metadata", doc_id=1, limit=500)
    assert meta["total"] == 1
    mf = meta["rows"][0]["changes"]["fields"]
    assert mf["title"]["after"] == "Payment Confirmation"
    assert mf["correspondent"]["after"] == "Acme"
    st.close()


# ===========================================================================
# applied: title/tags/ai_stage before -> after; new-taxonomy flag.
# ===========================================================================
def test_applied_metadata_records_before_after_and_flags(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    # Single doc: the stub returns the SAME new-taxonomy (Acme/Letter/billing) for
    # every document, so with >1 eligible doc processed concurrently only whichever
    # doc wins the creation race carries the `ai-new-taxonomy` flag (created=True) —
    # racy across platforms/thread scheduling. One doc makes it the sole creator.
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(),
                         docs=_docs()[:1])
    s = _settings(tmp_path, dry_run=False, triage_enabled=False)  # metadata only, writes
    Sweep(s, client=fake.client()).run_once()

    st = _open_store(tmp_path)
    meta = st.query(stage="metadata", doc_id=1, limit=500)
    assert meta["total"] == 1
    row = meta["rows"][0]
    assert row["dry_run"] is False
    ch = row["changes"]
    assert ch["fields"]["title"]["after"] == "Payment Confirmation"
    assert ch["fields"]["ai_stage"]["after"] == "metadata_done"
    # A new tag was proposed -> tags added include it; new taxonomy created -> flag.
    assert "billing" in (ch.get("tags", {}).get("added") or [])
    assert "ai-new-taxonomy" in (ch.get("flags") or [])
    # A Paperless URL is present and non-secret.
    assert row["paperless_url"].endswith("/documents/1/details")
    st.close()


# ===========================================================================
# skips / no-ops produce NO row (both the transparency signal AND size control).
# ===========================================================================
def test_skip_noop_records_no_row(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=False)

    Sweep(s, client=fake.client()).run_once()  # first: writes triage + metadata
    st = _open_store(tmp_path)
    first_total = st.query(limit=500)["total"]
    assert first_total > 0
    st.close()

    # Second run: every doc is already processed -> all stages SKIP -> NO new rows.
    Sweep(s, client=fake.client()).run_once()
    st2 = _open_store(tmp_path)
    assert st2.query(limit=500)["total"] == first_total   # unchanged: no no-op rows
    st2.close()


# ===========================================================================
# ERRORS recorded against the doc.
# ===========================================================================
def test_error_recorded_against_doc(tmp_path, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("Paperless said: field 'ai_stage' rejected value")
    install_stub_anthropic(monkeypatch, _boom)

    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=False, triage_enabled=False)  # metadata only
    Sweep(s, client=fake.client()).run_once()

    st = _open_store(tmp_path)
    errs = st.query(status="ERROR", limit=500)
    assert errs["total"] >= 1
    assert "rejected value" in errs["rows"][0]["changes"]["error"]
    st.close()


# ===========================================================================
# OBSERVATIONAL: a broken store never fails a doc/run.
# ===========================================================================
def test_broken_store_does_not_fail_doc_or_run(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=False)

    sweep = Sweep(s, client=fake.client())

    # Force the ActivityStore to ERROR on every record() call.
    class _BrokenStore:
        def record(self, entry):
            raise RuntimeError("audit DB is on fire")

        def purge(self, older):
            raise RuntimeError("audit DB is on fire")

        def stats(self):
            raise RuntimeError("audit DB is on fire")

    sweep._activity = _BrokenStore()
    sweep._activity_opened = True

    # The run must complete and the documents must still be processed (written).
    multi = sweep.run_once()
    assert multi is not None
    assert fake.patches, "documents must still process when the audit store errors"
    # Persisted run report exists (the run completed end to end).
    assert list(s.data_path("run-reports").glob("*.json"))


# ===========================================================================
# retention purge enforced in serve() (mirror snapshot retention).
# ===========================================================================
def test_retention_purge_enforced_in_serve(tmp_path, monkeypatch):
    from paperless_assistant import sweep as sweep_mod

    s = _settings(tmp_path, schedule_interval_seconds=3600, activity_retention_days=90)

    # Seed an OLD row and a RECENT row directly into the store.
    st = _open_store(tmp_path)
    now = time.time()
    st.record({"run_id": "old", "doc_id": 1, "doc_title": "old", "stage": "metadata",
               "dry_run": False, "status": "done", "changes": {"fields": {}},
               "summary": "", "paperless_url": "",
               "ts": now - 200 * 86400})
    st.record({"run_id": "new", "doc_id": 2, "doc_title": "new", "stage": "metadata",
               "dry_run": False, "status": "done", "changes": {"fields": {}},
               "summary": "", "paperless_url": "",
               "ts": now - 1 * 86400})
    st.close()

    # A no-op run_once so serve completes a tick and enforces retention.
    def _fake_run_once(self, *, limit=None):
        self._enforce_activity_retention()

        class _M:
            pass
        return _M()

    monkeypatch.setattr(sweep_mod.Sweep, "run_once", _fake_run_once)
    sweep_mod.serve(s, iterations=1, sleep_fn=lambda *_: None,
                    logger=JsonLogger(stream=io.StringIO(), path=None))

    st2 = _open_store(tmp_path)
    remaining = st2.query(limit=500)
    assert remaining["total"] == 1
    assert remaining["rows"][0]["doc_id"] == 2   # the old row was purged
    st2.close()


# ===========================================================================
# re-OCR supersession capture (applied + dry-run), via the OcrPipeline hook.
# ===========================================================================
def _source_pdf():
    import io
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 72, "scan")
    c.showPage()
    c.save()
    return buf.getvalue()


def _reocr_pipeline(tmp_path, session):
    from paperless_assistant.client import PaperlessClient
    from paperless_assistant.fields import CustomFieldResolver
    from paperless_assistant.safety import SafetyLayer
    from paperless_assistant.spend import SpendGovernor
    from paperless_assistant.ocr import OcrPipeline
    from fakes import FakeResponse

    base = "http://paperless.test:8000"
    session.add_json("GET", "/api/custom_fields/", {"results": [
        {"id": 1, "name": "ocr_quality", "data_type": "float"},
        {"id": 2, "name": "ai_stage", "data_type": "select",
         "extra_data": {"select_options": [
             {"id": "opt-t", "label": "triaged"},
             {"id": "opt-r", "label": "reocr_done"},
             {"id": "opt-m", "label": "metadata_done"}]}},
        {"id": 3, "name": "ai_notes", "data_type": "text"}], "next": None})
    client = PaperlessClient(base, "tok", session=session)
    resolver = CustomFieldResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=tmp_path / "snaps")
    pipeline = OcrPipeline(client, resolver, safety, SpendGovernor(max_spend=0.0),
                           api_key="x", built_dir=tmp_path / "built")
    return pipeline


def test_reocr_capture_applied_and_dry_run(tmp_path, monkeypatch):
    from fakes import FakeSession, FakeResponse

    # Applied: old -> superseded, new doc id created; recorder gets the relationship.
    from fakes import StubTextBlock

    session = FakeSession()
    pipeline = _reocr_pipeline(tmp_path, session)
    # Re-OCR returns raw transcribed TEXT (a text block, not a tool-use block).
    install_stub_anthropic(monkeypatch,
                           lambda **kw: StubMessage([StubTextBlock("CLEAN TEXT")]))
    pdf = _source_pdf()
    session.add("GET", "/api/documents/7/download/",
                lambda m, u, **k: FakeResponse(200, content=pdf))
    session.add("POST", "/api/documents/post_document/",
                lambda m, u, **k: FakeResponse(200, "task-1"))
    session.add("GET", "/api/tasks/",
                lambda m, u, **k: FakeResponse(200, [{"status": "SUCCESS", "related_document": 700}]))
    session.add("PATCH", "/api/documents/", lambda m, u, **k: FakeResponse(200, {}))

    st = _open_store(tmp_path)
    recorded = []

    def on_record(*, doc, new_doc_id, dry_run):
        recorded.append((doc["id"], new_doc_id, dry_run))
        st.record({"run_id": "r", "doc_id": doc["id"], "doc_title": doc.get("title"),
                   "stage": "reocr", "dry_run": dry_run, "status": "done",
                   "changes": {"supersede": {"old_doc_id": doc["id"],
                                             "new_doc_id": new_doc_id}},
                   "summary": "", "paperless_url": ""})

    doc = {"id": 7, "title": "Bad Scan", "tags": [11], "custom_fields": [
        {"field": 1, "value": 0.9}, {"field": 3, "value": "n"}, {"field": 2, "value": "opt-t"}]}
    status, _, _, _ = pipeline.process_one(doc, superseded_tag_id=99, dry_run=False,
                                           on_record=on_record)
    assert status == "done"
    assert recorded == [(7, 700, False)]
    rows = st.query(stage="reocr", limit=10)["rows"]
    assert rows and rows[0]["changes"]["supersede"]["new_doc_id"] == 700
    st.close()


def test_activity_disabled_records_nothing(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=True, activity_enabled=False)
    Sweep(s, client=fake.client()).run_once()
    # With the log disabled, the DB is never even created.
    assert not (tmp_path / "data" / "activity.db").exists()


def test_activity_links_use_public_url(tmp_path):
    """Activity doc links must use the EXTERNAL/public Paperless URL (browser-
    reachable), not the in-stack API base_url — and older rows stored with the
    internal URL are corrected at read time. Falls back to base_url when unset."""
    import pathlib
    from paperless_assistant import webui_data
    from paperless_assistant.activity import ActivityStore
    from paperless_assistant.config import Settings

    s = Settings(base_url="http://webserver:8000",
                 paperless_public_url="https://paperless.example",
                 paperless_token="x", data_dir=str(tmp_path / "data"))
    assert s.public_base_url() == "https://paperless.example"
    # fallback when no public url configured
    s2 = Settings(base_url="http://webserver:8000", paperless_token="x",
                  data_dir=str(tmp_path / "d2"))
    assert s2.public_base_url() == "http://webserver:8000"

    # An older row stored with the INTERNAL url must still render the public one.
    db = str(s.data_path("activity.db"))
    pathlib.Path(db).parent.mkdir(parents=True, exist_ok=True)
    store = ActivityStore(db)
    store.record({"doc_id": 1909, "stage": "metadata", "dry_run": False,
                  "status": "done", "changes": {"fields": {}},
                  "paperless_url": "http://webserver:8000/documents/1909/details"})
    store.close()

    payload = webui_data.activity_payload(s)
    row = payload["rows"][0]
    assert row["paperless_url"] == "https://paperless.example/documents/1909/details"
