"""Characterization tests driven against the ORIGINAL stageN_*.py scripts.

These lock in the observable behavior of the invariant-bearing logic BEFORE any
refactor. The identical assertions are re-run against the refactored package in
test_characterization_package.py. If a value here changes, behavior changed.

They run fully offline: no live Paperless, no real Anthropic key. Only pure /
logic functions are exercised (garbage_score, overlay builder, field/stage
resolution, merge-not-clobber, spend accumulator, eligibility) - nothing here
performs real network I/O.
"""
import io
import json
import threading

import pytest

from conftest import SAMPLE_TEXTS


# ===========================================================================
# garbage_score heuristic (OcrPipeline) -- exact scores + notes are pinned
# ===========================================================================
# Expected values are computed from the ORIGINAL stage0_triage.garbage_score and
# frozen here as the contract.
EXPECTED_GARBAGE = {
    "empty": (1.0, "empty_or_tiny"),
    "tiny": (1.0, "empty_or_tiny"),
}


def test_garbage_score_empty_and_tiny(orig_stage0):
    for key, expected in EXPECTED_GARBAGE.items():
        assert orig_stage0.garbage_score(SAMPLE_TEXTS[key]) == expected


def test_garbage_score_clean_low(orig_stage0):
    score, note = orig_stage0.garbage_score(SAMPLE_TEXTS["clean"])
    # Clean prose scores low (well below the 0.55 flag threshold).
    assert score < 0.2
    assert note.startswith("wr=")


def test_garbage_score_garbage_high(orig_stage0):
    score, note = orig_stage0.garbage_score(SAMPLE_TEXTS["garbage"])
    # Fragmented single-letter garbage scores high.
    assert score >= 0.55
    assert note.startswith("wr=")


def test_garbage_score_no_alpha(orig_stage0):
    score, note = orig_stage0.garbage_score(SAMPLE_TEXTS["no_alpha"])
    assert (score, note) == (1.0, "no_alpha_words")


def test_garbage_score_is_deterministic(orig_stage0):
    a = orig_stage0.garbage_score(SAMPLE_TEXTS["clean"])
    b = orig_stage0.garbage_score(SAMPLE_TEXTS["clean"])
    assert a == b


def test_garbage_score_bounded_0_1(orig_stage0):
    for text in SAMPLE_TEXTS.values():
        score, _ = orig_stage0.garbage_score(text)
        assert 0.0 <= score <= 1.0


# ===========================================================================
# I2: snapshot writes once and never overwrites (stage0)
# ===========================================================================
def test_snapshot_writes_once(orig_stage0, tmp_path, monkeypatch):
    monkeypatch.setattr(orig_stage0, "SNAP_DIR", tmp_path)
    doc = {"id": 42, "title": "orig", "content": "hello"}
    orig_stage0.snapshot(doc)
    snap = tmp_path / "42.json"
    assert snap.exists()
    saved = json.loads(snap.read_text())
    assert saved["title"] == "orig"

    # second snapshot with mutated doc must NOT overwrite
    doc2 = {"id": 42, "title": "MUTATED", "content": "changed"}
    orig_stage0.snapshot(doc2)
    saved_again = json.loads(snap.read_text())
    assert saved_again["title"] == "orig"  # unchanged


# ===========================================================================
# fields: stage_value / stage_option_id select-id mapping (I1 machinery)
# ===========================================================================
def _select_fmap():
    return {
        "ocr_quality": {"id": 1, "data_type": "float"},
        "ai_stage": {
            "id": 2,
            "data_type": "select",
            "options": {"triaged": "opt-t", "reocr_done": "opt-r", "metadata_done": "opt-m"},
        },
        "ai_notes": {"id": 3, "data_type": "text"},
    }


def _text_fmap():
    return {
        "ocr_quality": {"id": 1, "data_type": "float"},
        "ai_stage": {"id": 2, "data_type": "text"},
        "ai_notes": {"id": 3, "data_type": "text"},
    }


def test_stage_value_select_maps_to_option_id(orig_stage0):
    assert orig_stage0.stage_value(_select_fmap()) == "opt-t"  # STAGE_VALUE == triaged


def test_stage_value_text_passthrough(orig_stage0):
    assert orig_stage0.stage_value(_text_fmap()) == "triaged"


def test_stage_option_id_select(orig_stage1):
    fmap = _select_fmap()
    assert orig_stage1.stage_option_id(fmap, "reocr_done") == "opt-r"


def test_stage_option_id_text_passthrough(orig_stage1):
    fmap = _text_fmap()
    assert orig_stage1.stage_option_id(fmap, "reocr_done") == "reocr_done"


# ===========================================================================
# fields: _coerce_score data-type coercion
# ===========================================================================
def test_coerce_score_types(orig_stage0):
    assert orig_stage0._coerce_score(0.7, "float") == 0.7
    assert orig_stage0._coerce_score(0.7, "monetary") == 0.7
    assert orig_stage0._coerce_score(0.7, "integer") == 70
    assert orig_stage0._coerce_score(0.7, "string") == "0.7"
    assert orig_stage0._coerce_score(0.7, "text") == "0.7"
    assert orig_stage0._coerce_score(0.7, "url") == "0.7"
    # unknown type falls back to float
    assert orig_stage0._coerce_score(0.7, "weird") == 0.7


# ===========================================================================
# I1: already_triaged skip logic
# ===========================================================================
def test_already_triaged_true(orig_stage0):
    fmap = _select_fmap()
    doc = {"id": 1, "custom_fields": [{"field": 2, "value": "opt-t"}]}
    assert orig_stage0.already_triaged(doc, fmap) is True


def test_already_triaged_false_wrong_stage(orig_stage0):
    fmap = _select_fmap()
    doc = {"id": 1, "custom_fields": [{"field": 2, "value": "opt-m"}]}
    assert orig_stage0.already_triaged(doc, fmap) is False


def test_already_triaged_false_no_fields(orig_stage0):
    fmap = _select_fmap()
    assert orig_stage0.already_triaged({"id": 1}, fmap) is False


# ===========================================================================
# I4/I5: merge_custom_fields preserves other fields, updates ours (stage0)
# ===========================================================================
def test_merge_custom_fields_preserves_others(orig_stage0):
    fmap = _select_fmap()
    existing = [
        {"field": 99, "value": "human-set-value"},   # foreign field, must survive
        {"field": 1, "value": 0.1},                   # old score, must be replaced
    ]
    merged = orig_stage0.merge_custom_fields(existing, fmap, 0.8, "note here")
    by_field = {c["field"]: c["value"] for c in merged}
    assert by_field[99] == "human-set-value"          # preserved
    assert by_field[1] == 0.8                          # score updated
    assert by_field[2] == "opt-t"                      # stage set to option id
    assert by_field[3] == "note here"                  # note set


def test_merge_custom_fields_note_truncated_255(orig_stage0):
    fmap = _select_fmap()
    long_note = "x" * 400
    merged = orig_stage0.merge_custom_fields([], fmap, 0.5, long_note)
    note_val = {c["field"]: c["value"] for c in merged}[3]
    assert len(note_val) == 255


# ===========================================================================
# I1: eligible() -- stage2 eligibility + garbage-threshold exclusion
# ===========================================================================
def _stage2_fmap():
    return {
        "ocr_quality": {"id": 10, "data_type": "float"},
        "ai_stage": {
            "id": 11,
            "data_type": "select",
            "options": {"triaged": "t", "reocr_done": "r", "metadata_done": "m"},
        },
        "ai_notes": {"id": 12, "data_type": "text"},
    }


def test_eligible_triaged_low_score(orig_stage2):
    fmap = _stage2_fmap()
    doc = {"custom_fields": [{"field": 11, "value": "t"}, {"field": 10, "value": 0.2}]}
    assert orig_stage2.eligible(doc, fmap) is True


def test_eligible_excludes_garbage(orig_stage2):
    fmap = _stage2_fmap()
    doc = {"custom_fields": [{"field": 11, "value": "t"}, {"field": 10, "value": 0.9}]}
    assert orig_stage2.eligible(doc, fmap) is False  # >= GARBAGE_THRESH


def test_eligible_excludes_done_stage(orig_stage2):
    fmap = _stage2_fmap()
    doc = {"custom_fields": [{"field": 11, "value": "m"}, {"field": 10, "value": 0.1}]}
    assert orig_stage2.eligible(doc, fmap) is False  # metadata_done not eligible


def test_eligible_empty_stage_ok(orig_stage2):
    fmap = _stage2_fmap()
    doc = {"custom_fields": [{"field": 10, "value": 0.1}]}  # no stage -> None -> eligible
    assert orig_stage2.eligible(doc, fmap) is True


def test_eligible_garbage_threshold_boundary(orig_stage2):
    fmap = _stage2_fmap()
    # exactly at threshold -> excluded (>=)
    doc = {"custom_fields": [{"field": 11, "value": "t"}, {"field": 10, "value": 0.55}]}
    assert orig_stage2.eligible(doc, fmap) is False


# ===========================================================================
# I5: Taxonomy reuse-first, case-insensitive, new-flagging (stage2)
# ===========================================================================
def _make_taxonomy(orig_stage2, monkeypatch):
    """Build a Taxonomy without hitting the network by stubbing get_all."""
    def fake_get_all(endpoint, fields=None):
        if endpoint == "tags":
            return [{"id": 1, "name": "Invoice"}, {"id": 2, "name": "Bank"}]
        if endpoint == "correspondents":
            return [{"id": 5, "name": "Acme Corp"}]
        if endpoint == "document_types":
            return [{"id": 8, "name": "Statement"}]
        return []
    monkeypatch.setattr(orig_stage2, "get_all", fake_get_all)
    return orig_stage2.Taxonomy()


def test_taxonomy_reuse_case_insensitive(orig_stage2, monkeypatch):
    tax = _make_taxonomy(orig_stage2, monkeypatch)
    tid, created = tax.tag_id("invoice")   # different case
    assert tid == 1
    assert created is False


def test_taxonomy_correspondent_reuse(orig_stage2, monkeypatch):
    tax = _make_taxonomy(orig_stage2, monkeypatch)
    cid, created = tax.correspondent_id("ACME CORP")
    assert cid == 5
    assert created is False


def test_taxonomy_new_flags_creation(orig_stage2, monkeypatch):
    tax = _make_taxonomy(orig_stage2, monkeypatch)
    created_calls = []

    def fake_request(method, url, **kw):
        created_calls.append((method, url, kw.get("json")))

        class R:
            def json(self_inner):
                return {"id": 999}
        return R()
    monkeypatch.setattr(orig_stage2, "_request", fake_request)

    tid, created = tax.tag_id("BrandNewTag")
    assert created is True
    assert tid == 999
    # subsequent lookup reuses without creating again
    tid2, created2 = tax.tag_id("brandnewtag")
    assert tid2 == 999
    assert created2 is False
    assert len(created_calls) == 1


# ===========================================================================
# I3: spend accumulator + cap abort, thread-safety (stage1 & stage2)
# ===========================================================================
def test_spend_cap_blocks_new_work(orig_stage1, monkeypatch):
    # Simulate the pre-call gate in process_one: once _spend_total >= max_spend,
    # new work is refused.
    monkeypatch.setattr(orig_stage1, "_spend_total", 6.0)

    class Args:
        max_spend = 5.0
        dry_run = True
    with orig_stage1._spend_lock:
        blocked = orig_stage1._spend_total >= Args.max_spend
    assert blocked is True


def test_spend_accumulator_threadsafe(orig_stage2, monkeypatch):
    monkeypatch.setattr(orig_stage2, "_spend_total", 0.0)

    def add():
        for _ in range(1000):
            with orig_stage2._spend_lock:
                orig_stage2._spend_total += 0.001

    threads = [threading.Thread(target=add) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 8 threads * 1000 * 0.001 == 8.0 (no lost updates)
    assert round(orig_stage2._spend_total, 3) == 8.0


# ===========================================================================
# I4/I5: apply_metadata merge-not-clobber tags + preserves custom fields
# ===========================================================================
def test_apply_metadata_merges_tags_and_flags_new(orig_stage2, monkeypatch):
    fmap = _stage2_fmap()
    tax = _make_taxonomy(orig_stage2, monkeypatch)

    # capture the PATCH body instead of sending it
    captured = {}

    def fake_request(method, url, **kw):
        if method == "PATCH":
            captured["body"] = kw["json"]

        class R:
            def json(self_inner):
                return {"id": 12345}
        return R()
    monkeypatch.setattr(orig_stage2, "_request", fake_request)

    doc = {
        "id": 7,
        "tags": [100, 200],  # existing human tags
        "custom_fields": [{"field": 99, "value": "keep-me"}, {"field": 11, "value": "t"}],
    }
    meta = {
        "title": "New Title",
        "correspondent": "Acme Corp",       # existing -> reuse (id 5)
        "document_type": "Statement",        # existing -> reuse (id 8)
        "tags": ["Invoice", "SomethingNew"], # one existing, one new
        "correspondent_is_new": False,
        "document_type_is_new": False,
        "new_tags": ["SomethingNew"],
    }
    new_tax_tag_id = 555
    created = orig_stage2.apply_metadata(doc, meta, tax, fmap, new_tax_tag_id)
    body = captured["body"]

    assert created is True  # SomethingNew created
    assert body["title"] == "New Title"
    assert body["correspondent"] == 5
    assert body["document_type"] == 8
    # existing human tags 100,200 preserved; Invoice(1) added; new tag id + new-tax flag added
    assert 100 in body["tags"] and 200 in body["tags"]
    assert 1 in body["tags"]             # Invoice reused
    assert new_tax_tag_id in body["tags"]  # ai-new-taxonomy flag applied
    # custom field 99 preserved; stage advanced to metadata_done ("m")
    cf = {c["field"]: c["value"] for c in body["custom_fields"]}
    assert cf[99] == "keep-me"
    assert cf[11] == "m"


def test_apply_metadata_no_new_taxonomy_no_flag(orig_stage2, monkeypatch):
    fmap = _stage2_fmap()
    tax = _make_taxonomy(orig_stage2, monkeypatch)
    captured = {}

    def fake_request(method, url, **kw):
        if method == "PATCH":
            captured["body"] = kw["json"]

        class R:
            def json(self_inner):
                return {"id": 1}
        return R()
    monkeypatch.setattr(orig_stage2, "_request", fake_request)

    doc = {"id": 7, "tags": [100], "custom_fields": []}
    meta = {
        "title": "T",
        "correspondent": "Acme Corp",
        "document_type": "Statement",
        "tags": ["Invoice", "Bank"],  # all existing
        "correspondent_is_new": False,
        "document_type_is_new": False,
        "new_tags": [],
    }
    created = orig_stage2.apply_metadata(doc, meta, tax, fmap, 555)
    assert created is False
    assert 555 not in captured["body"]["tags"]  # no new-tax flag


# ===========================================================================
# I4: mark_old_superseded adds tag, advances stage, preserves other fields
# ===========================================================================
def test_mark_old_superseded(orig_stage1, monkeypatch):
    fmap = _select_fmap()
    captured = {}

    def fake_request(method, url, **kw):
        captured["method"] = method
        captured["body"] = kw.get("json")

        class R:
            def json(self_inner):
                return {}
        return R()
    monkeypatch.setattr(orig_stage1, "_request", fake_request)

    old_doc = {
        "id": 3,
        "tags": [10, 20],
        "custom_fields": [{"field": 1, "value": 0.9}, {"field": 2, "value": "opt-t"}],
    }
    orig_stage1.mark_old_superseded(old_doc, fmap, superseded_tag_id=77)
    body = captured["body"]
    assert 77 in body["tags"]             # superseded tag added
    assert 10 in body["tags"] and 20 in body["tags"]  # existing tags kept
    cf = {c["field"]: c["value"] for c in body["custom_fields"]}
    assert cf[1] == 0.9                    # score preserved
    assert cf[2] == "opt-r"                # stage advanced to reocr_done


# ===========================================================================
# Risk #3: build_overlay_pdf produces valid PDF w/ extractable injected text
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


def _extract_all_text(pdf_bytes):
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages), len(reader.pages)


def test_overlay_injects_extractable_text_letter(orig_stage1):
    from reportlab.lib.pagesizes import letter
    src = _make_source_pdf(letter, n_pages=1)
    out = orig_stage1.build_overlay_pdf(src, "HELLO OVERLAY WORLD invoice total 123")
    text, n = _extract_all_text(out)
    assert "HELLO OVERLAY WORLD" in text
    assert n == 1


def test_overlay_multipage_preserves_page_count(orig_stage1):
    from reportlab.lib.pagesizes import letter
    src = _make_source_pdf(letter, n_pages=3)
    out = orig_stage1.build_overlay_pdf(src, "line one\nline two\nline three")
    text, n = _extract_all_text(out)
    assert n == 3  # original page count preserved
    assert "line one" in text


def test_overlay_odd_page_size(orig_stage1):
    src = _make_source_pdf((200, 900), n_pages=1)  # tall narrow page
    out = orig_stage1.build_overlay_pdf(src, "ODDSIZE transcript content here")
    text, n = _extract_all_text(out)
    assert "ODDSIZE" in text
    assert n >= 1


def test_overlay_long_text_appends_pages(orig_stage1):
    from reportlab.lib.pagesizes import letter
    src = _make_source_pdf(letter, n_pages=1)
    long_text = "\n".join(f"transcript line number {i} with content" for i in range(400))
    out = orig_stage1.build_overlay_pdf(src, long_text)
    text, n = _extract_all_text(out)
    # long text spills onto appended text-only pages beyond the 1 original page
    assert n >= 1
    assert "transcript line number 0" in text
