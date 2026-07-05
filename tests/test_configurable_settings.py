"""Prompt 011 — configurable hardcoded settings.

Proves:
  * DEFAULTS BYTE-IDENTICAL: garbage_score with default coefficients reproduces
    the exact stock scores; default field/stage names + eligibility + timeouts +
    content window reproduce today's behavior.
  * Each normal-tier setting takes effect at its use site (timeouts, page_size,
    content window, eligibility incl. reocr_done, metadata max_tokens).
  * Field/stage NAMES flow end-to-end: setup provisions the configured names,
    doctor checks them, and a sweep resolves + processes with non-default names.
  * Advanced garbage-heuristic changes scores; reset (defaults) restores exact
    stock scores.
  * The new fields are editable via the config-write path and NOT rejected as
    secrets; the served dashboard page is self-contained.
"""
import io
import json

import pytest

from paperless_assistant import config
from paperless_assistant.config import (
    Settings, TaskProvider, SpendCaps, FieldNames, StageNames, GarbageHeuristic,
    HttpSettings, load_settings,
)
from paperless_assistant.ocr import garbage_score
from paperless_assistant.client import PaperlessClient
from paperless_assistant.fields import CustomFieldResolver
from paperless_assistant.stages import StageOrchestrator
from paperless_assistant.provision import Provisioner, required_fields
from paperless_assistant.doctor import run_doctor
from paperless_assistant.sweep import Sweep

from fakes import (
    FakeSession, FakeResponse, FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)

from conftest import SAMPLE_TEXTS


# ===========================================================================
# 1. DEFAULTS BYTE-IDENTICAL — garbage_score
# ===========================================================================
# The exact scores the POC/pre-011 heuristic produced for the shared fixtures.
def _stock_score(text):
    """The pre-011 formula, inlined, as an independent oracle."""
    import re
    if not text or len(text.strip()) < 40:
        return 1.0
    words = re.findall(r"[A-Za-z]{2,}", text)
    if not words:
        return 1.0
    nonspace = re.sub(r"\s", "", text)
    wordchars = sum(len(w) for w in words)
    word_ratio = wordchars / max(len(nonspace), 1)
    plausible = [w for w in words if len(w) >= 3 and re.search(r"[aeiouAEIOU]", w)]
    plausible_ratio = len(plausible) / max(len(words), 1)
    avg = wordchars / max(len(words), 1)
    frag = 1.0 if avg < 2.5 else 0.0
    score = 1.0 - (0.45 * word_ratio + 0.45 * plausible_ratio + 0.10 * (1 - frag))
    return round(max(0.0, min(1.0, score)), 3)


@pytest.mark.parametrize("name", list(SAMPLE_TEXTS))
def test_garbage_score_default_byte_identical(name):
    text = SAMPLE_TEXTS[name]
    # No heuristic arg -> byte-identical default path.
    assert garbage_score(text)[0] == _stock_score(text)
    # Explicit default coefficients -> identical too.
    assert garbage_score(text, GarbageHeuristic())[0] == _stock_score(text)


def test_garbage_heuristic_advanced_changes_then_reset_restores():
    text = SAMPLE_TEXTS["clean"]
    stock = garbage_score(text)[0]
    # Tuning a coefficient changes the score.
    tuned = GarbageHeuristic(word_ratio_weight=0.20, plausible_weight=0.20)
    assert garbage_score(text, tuned)[0] != stock
    # "Reset" = default coefficients -> exact stock score again (byte-identical).
    assert garbage_score(text, GarbageHeuristic())[0] == stock


# ===========================================================================
# 2. DEFAULTS BYTE-IDENTICAL — eligibility
# ===========================================================================
def test_eligible_stage_labels_default_matches_legacy():
    s = Settings()
    # Default reproduces ELIGIBLE_STAGES = {None, "", "triaged"}.
    assert s.eligible_stage_labels() == config.ELIGIBLE_STAGES == {None, "", "triaged"}


def test_eligibility_can_include_reocr_done():
    s = Settings(metadata_eligible_roles=["", "triaged", "reocr_done"])
    labels = s.eligible_stage_labels()
    assert "reocr_done" in labels and "triaged" in labels and None in labels


def _resolver_with(session, fields):
    session.add_json("GET", "/api/custom_fields/", {"results": fields, "next": None})
    return CustomFieldResolver(PaperlessClient("http://x", "t", session=session))


def test_metadata_eligible_includes_reocr_done_when_configured():
    s = FakeSession()
    r = _resolver_with(s, make_custom_fields())
    settings = Settings(metadata_eligible_roles=["", "triaged", "reocr_done"])
    orch = StageOrchestrator(r.client, r, settings)
    # A reocr_done doc (opt-r), clean score -> eligible now.
    doc = {"id": 5, "custom_fields": [
        {"field": 2, "value": "opt-r"}, {"field": 1, "value": 0.1}]}
    assert orch.metadata_eligible(doc) is True
    # Default eligibility would exclude it.
    orch_default = StageOrchestrator(r.client, r, Settings())
    assert orch_default.metadata_eligible(doc) is False


# ===========================================================================
# 3. field/stage NAME resolution byte-identical + non-default
# ===========================================================================
def test_resolver_default_names_byte_identical():
    s = FakeSession()
    r = _resolver_with(s, make_custom_fields())
    assert r.score_field_id() == 1
    assert r.stage_field_id() == 2
    assert r.notes_field_id() == 3
    assert r.stage_option_for_role(config.STAGE_TRIAGED) == "opt-t"
    assert r.role_for_value("opt-m") == config.STAGE_METADATA_DONE


def test_resolver_custom_names():
    fields = [
        {"id": 11, "name": "scan_quality", "data_type": "float"},
        {"id": 12, "name": "pipeline_state", "data_type": "select",
         "extra_data": {"select_options": [
             {"id": "s-scored", "label": "scored"},
             {"id": "s-reocr", "label": "reocr"},
             {"id": "s-meta", "label": "meta"}]}},
        {"id": 13, "name": "pipeline_notes", "data_type": "string"},
    ]
    s = FakeSession()
    s.add_json("GET", "/api/custom_fields/", {"results": fields, "next": None})
    r = CustomFieldResolver(
        PaperlessClient("http://x", "t", session=s),
        field_names=FieldNames(score="scan_quality", stage="pipeline_state",
                               notes="pipeline_notes"),
        stage_names=StageNames(triaged="scored", reocr_done="reocr",
                               metadata_done="meta"),
    )
    assert r.score_field_id() == 11
    assert r.stage_field_id() == 12
    assert r.stage_option_for_role(config.STAGE_TRIAGED) == "s-scored"
    assert r.role_for_value("s-meta") == config.STAGE_METADATA_DONE


# ===========================================================================
# 4. setup -> doctor -> process end-to-end with NON-DEFAULT names
# ===========================================================================
CUSTOM_FN = FieldNames(score="scan_quality", stage="pipeline_state",
                       notes="pipeline_notes")
CUSTOM_SN = StageNames(triaged="scored", reocr_done="reocr", metadata_done="meta")


def _settings_custom_names(tmp_path):
    return Settings(
        base_url="http://paperless.test:8000", paperless_token="tok",
        anthropic_api_key="sk-ant-test", data_dir=str(tmp_path / "data"),
        metadata_task=TaskProvider(provider="anthropic", model="claude-sonnet-4-6"),
        spend=SpendCaps(per_run=1.0, per_period=5.0, period="monthly"),
        field_names=CUSTOM_FN, stage_names=CUSTOM_SN,
    )


def test_setup_doctor_process_with_custom_names(tmp_path, monkeypatch):
    fake = FakePaperless(fields=[], tags=[])
    settings = _settings_custom_names(tmp_path)

    # --- setup provisions the CONFIGURED names ---
    prov = Provisioner(fake.client(), field_names=CUSTOM_FN, stage_names=CUSTOM_SN)
    report = prov.run()
    assert report.ok
    assert set(report.created_fields) == {"scan_quality", "pipeline_state",
                                          "pipeline_notes"}
    stage = next(f for f in fake.custom_fields if f["name"] == "pipeline_state")
    labels = {o["label"] for o in stage["extra_data"]["select_options"]}
    assert labels == {"scored", "reocr", "meta"}
    # Real Paperless assigns each select option an id on creation; the fake stores
    # extra_data verbatim, so synthesize the server-assigned option ids here.
    for o in stage["extra_data"]["select_options"]:
        o["id"] = "opt-" + o["label"]

    # --- doctor checks the CONFIGURED names (green) ---
    result = run_doctor(settings, fake.client(), check_providers=False)
    assert not result.failed, [c.message for c in result.checks if c.status == "fail"]
    field_checks = [c for c in result.checks if c.name.startswith("field:")]
    assert {c.name for c in field_checks} == {
        "field:scan_quality", "field:pipeline_state", "field:pipeline_notes"}
    assert all(c.status == "ok" for c in field_checks)

    # --- process: a sweep triages using the custom names (write) ---
    fake.documents = {1: {"id": 1, "title": "clean",
                          "content": SAMPLE_TEXTS["clean"], "tags": [],
                          "custom_fields": []}}
    _install_meta_stub(monkeypatch)
    settings.dry_run = False
    sweep = Sweep(settings, client=fake.client())
    sweep.run_once()
    # The triage PATCH wrote the configured stage option id + score field id.
    patched = [b for (_id, b) in fake.patches if "custom_fields" in b]
    assert patched, "expected a triage write"
    stage_fid = next(f["id"] for f in fake.custom_fields if f["name"] == "pipeline_state")
    score_fid = next(f["id"] for f in fake.custom_fields if f["name"] == "scan_quality")
    scored_opt = "opt-scored"
    wrote_fields = {cf["field"] for b in patched for cf in b["custom_fields"]}
    assert stage_fid in wrote_fields and score_fid in wrote_fields
    assert any(cf["field"] == stage_fid and cf["value"] == scored_opt
               for b in patched for cf in b["custom_fields"])


def _install_meta_stub(monkeypatch):
    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock({
        "title": "Payment", "correspondent": "Acme", "document_type": "Letter",
        "tags": ["billing"], "correspondent_is_new": True,
        "document_type_is_new": True, "new_tags": ["billing"]})]))


# ===========================================================================
# 5. normal-tier settings take effect at their use site
# ===========================================================================
def test_http_timeouts_and_page_size_take_effect():
    calls = []
    s = FakeSession()

    def handler(m, u, **kw):
        calls.append((u, kw.get("timeout")))
        return FakeResponse(200, {"results": [], "next": None})

    s.add("GET", "/api/documents/", handler)
    http = HttpSettings(request_timeout=17, page_size=42)
    client = PaperlessClient("http://x", "t", session=s, http=http)
    list(client.iter_documents("id"))
    # The configured page_size is in the URL and the configured timeout is passed.
    assert any("page_size=42" in u for (u, _t) in calls)
    assert any(t == 17 for (_u, t) in calls)


def test_download_and_post_timeouts_from_config():
    s = FakeSession()
    seen = {}

    def _dl(m, u, **kw):
        seen["dl"] = kw.get("timeout")
        return FakeResponse(200, content=b"pdf")

    def _post(m, u, **kw):
        seen["post"] = kw.get("timeout")
        return FakeResponse(200, {"task": "uuid"})

    s.add("GET", "/download/", _dl)
    s.add("POST", "/post_document/", _post)
    http = HttpSettings(download_timeout=11, post_document_timeout=222)
    client = PaperlessClient("http://x", "t", session=s, http=http)
    client.download_original(1)
    client.post_document(b"pdf", {"title": "t"}, "f.pdf")
    assert seen["dl"] == 11
    assert seen["post"] == 222


def test_content_window_truncates_differently():
    from paperless_assistant.metadata import build_prompt
    doc = {"title": "t", "content": "A" * 100 + "B" * 100}
    # Small window truncates; the marker "\n...\n" appears in the DOCUMENT TEXT.
    p_small = build_prompt(doc, [], [], [], content_head=10, content_tail=5)
    body_small = p_small.split("DOCUMENT TEXT:\n", 1)[1]
    assert body_small == "A" * 10 + "\n...\n" + "B" * 5
    # Default window (> content length) does not truncate.
    p_full = build_prompt(doc, [], [], [])
    body_full = p_full.split("DOCUMENT TEXT:\n", 1)[1]
    assert body_full == "A" * 100 + "B" * 100


def test_metadata_max_tokens_threaded_to_provider():
    from paperless_assistant.providers.anthropic import AnthropicProvider
    prov = AnthropicProvider(api_key="k", ocr_model="o", metadata_model="m",
                             max_ocr_tokens=8000, metadata_max_tokens=333)
    assert prov.metadata_max_tokens == 333
    # And byte-identical default when omitted.
    prov2 = AnthropicProvider(api_key="k", ocr_model="o", metadata_model="m",
                              max_ocr_tokens=8000)
    assert prov2.metadata_max_tokens == 1024


# ===========================================================================
# 6. layered config: env + YAML wiring for the new keys
# ===========================================================================
def test_yaml_and_env_layering_for_new_keys(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "field_names:\n  score: scan_quality\n"
        "http:\n  request_timeout: 45\n  page_size: 250\n"
        "metadata_window:\n  content_head: 3000\n"
        "metadata_eligible_roles: ['', 'triaged', 'reocr_done']\n"
        "garbage_heuristic:\n  min_length: 60\n",
        encoding="utf-8",
    )
    s = load_settings(config_file=str(cfg))
    assert s.field_names.score == "scan_quality"
    assert s.http.request_timeout == 45
    assert s.http.page_size == 250
    assert s.metadata_window.content_head == 3000
    assert "reocr_done" in s.metadata_eligible_roles
    assert s.garbage_heuristic.min_length == 60

    # env beats YAML and locks the field.
    monkeypatch.setenv("PA_HTTP_REQUEST_TIMEOUT", "99")
    monkeypatch.setenv("PA_FIELD_SCORE", "envname")
    s2 = load_settings(config_file=str(cfg))
    assert s2.http.request_timeout == 99
    assert s2.field_names.score == "envname"
    locked = config.env_overridden_fields()
    assert locked["http_request_timeout"] is True
    assert locked["field_score"] is True


# ===========================================================================
# 7. config-write path: new fields accepted, NOT secret-flagged; page self-contained
# ===========================================================================
def test_config_write_accepts_new_nonsecret_blocks():
    from paperless_assistant.webui import validate_and_build_yaml
    payload = {
        "field_names": {"score": "scan_quality"},
        "http": {"request_timeout": 45, "retries": 8},
        "metadata_window": {"content_head": 3000},
        "garbage_heuristic": {"min_length": 55, "word_ratio_weight": 0.4},
        "metadata_eligible_roles": ["", "triaged", "reocr_done"],
        "superseded_tag_color": "#123456",
    }
    merged = validate_and_build_yaml(payload)
    assert merged["field_names"]["score"] == "scan_quality"
    assert merged["http"]["request_timeout"] == 45.0
    assert merged["http"]["retries"] == 8
    assert merged["garbage_heuristic"]["min_length"] == 55
    assert merged["metadata_eligible_roles"] == ["", "triaged", "reocr_done"]
    assert merged["superseded_tag_color"] == "#123456"


def test_config_write_still_refuses_secrets_and_delete():
    from paperless_assistant.webui import validate_and_build_yaml, ConfigValidationError
    with pytest.raises(ConfigValidationError):
        validate_and_build_yaml({"anthropic_api_key": "sk-secret"})
    with pytest.raises(ConfigValidationError):
        validate_and_build_yaml({"delete_originals": True})
    # A secret nested in a new block is also refused.
    with pytest.raises(ConfigValidationError):
        validate_and_build_yaml({"http": {"request_timeout": 10}, "token": "x"})


def test_dashboard_page_is_self_contained():
    from paperless_assistant.webui import PAGE_HTML
    low = PAGE_HTML.lower()
    # No external assets (CDN scripts/styles/fonts/images).
    for bad in ("http://", "https://", "src=\"//", "cdn", "<link"):
        assert bad not in low, f"external asset marker '{bad}' found in page"
    # The new Advanced section + names form are present.
    assert "advanced" in low
    assert "names-form" in low
    assert "advanced-garbage" in low


# ===========================================================================
# 8. snapshot retention enforced
# ===========================================================================
def test_snapshot_retention_prunes_expired(tmp_path, monkeypatch):
    import os
    import time
    settings = Settings(
        base_url="http://paperless.test:8000", paperless_token="tok",
        anthropic_api_key="sk-ant-test", data_dir=str(tmp_path / "data"),
        metadata_enabled=False, triage_enabled=False,
        snapshot_retention_days=1, dry_run=True,
    )
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=[])
    sweep = Sweep(settings, client=fake.client())
    snap_dir = tmp_path / "data" / "snapshots" / "triage"
    snap_dir.mkdir(parents=True)
    old = snap_dir / "1.json"
    old.write_text("{}")
    fresh = snap_dir / "2.json"
    fresh.write_text("{}")
    # Age the old snapshot 3 days.
    three_days = time.time() - 3 * 86400
    os.utime(old, (three_days, three_days))
    sweep._enforce_snapshot_retention()
    assert not old.exists()   # pruned
    assert fresh.exists()     # kept


def test_snapshot_retention_zero_keeps_forever(tmp_path):
    settings = Settings(data_dir=str(tmp_path / "data"), snapshot_retention_days=0)
    fake = FakePaperless(fields=[], tags=[], docs=[])
    sweep = Sweep(settings, client=fake.client())
    snap_dir = tmp_path / "data" / "snapshots" / "triage"
    snap_dir.mkdir(parents=True)
    old = snap_dir / "1.json"
    old.write_text("{}")
    import os, time
    ancient = time.time() - 999 * 86400
    os.utime(old, (ancient, ancient))
    sweep._enforce_snapshot_retention()
    assert old.exists()  # 0 = keep forever
