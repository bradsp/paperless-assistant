"""Prompt customization + model selection tests (prompt 010).

Fully OFFLINE / in-process. Proves:

  * DEFAULTS ARE BYTE-IDENTICAL: with no customization the resolved metadata /
    OCR prompts equal the original constants, and default models are unchanged
    (characterization).
  * prompt resolution: extra appends, override replaces, clearing resets; the
    /api/prompts "effective" preview matches what the engine sends.
  * the model catalog derives from pricing, carries a vision flag, and tolerates a
    custom (uncatalogued) configured model without crashing.
  * /api/models + /api/prompts are auth-gated (401 without token) and return the
    right shapes; the re-OCR picker's vision info is present.
  * config POST accepts the prompt fields (NOT secret-flagged) and persists them,
    merging with the model fields in the same block.
  * a CUSTOM metadata prompt still produces schema-valid writes, and a response
    that fails the fixed schema is still caught and NEVER written.
"""
from __future__ import annotations

import io
import json
import re

import pytest

from paperless_assistant import webui, webui_data, config as cfgmod
from paperless_assistant import prompts as prompts_mod
from paperless_assistant.config import (
    Settings, TaskProvider, SpendCaps, UiSettings, PromptCustomization,
    load_settings,
)
from paperless_assistant.metadata import (
    build_prompt, METADATA_SCHEMA, MetadataExtractor,
)
from paperless_assistant.providers import model_catalog, is_vision_model
from paperless_assistant.providers.base import SchemaValidationError

from fakes import (
    FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)


TOKEN = "ui-secret-token"


# ---------------------------------------------------------------------------
# In-process handler driver (mirrors tests/test_webui.py — no socket bound).
# ---------------------------------------------------------------------------
class _FakeConn:
    def __init__(self, raw: bytes):
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()

    def makefile(self, mode, *a, **k):
        return self.rfile if "r" in mode else self.wfile


class _DummyServer:
    def __init__(self):
        self.server_name = "test"
        self.server_port = 0


class Resp:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _parse_response(raw: bytes) -> Resp:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return Resp(status, headers, body)


def _drive(handler_cls, method, path, *, token=None, body=None):
    payload = b""
    headers = {"Host": "test"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    lines = [f"{method} {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + payload
    conn = _FakeConn(raw)

    class _H(handler_cls):
        def setup(self):
            self.connection = conn
            self.rfile = conn.rfile
            self.wfile = conn.wfile

        def finish(self):
            pass

    _H(conn, ("127.0.0.1", 12345), _DummyServer())
    return _parse_response(conn.wfile.getvalue())


def _settings(tmp_path, **over):
    s = Settings(
        base_url="http://paperless.test:8000", paperless_token="tok",
        anthropic_api_key="sk-ant-test",
        data_dir=str(tmp_path / "data"),
        metadata_task=TaskProvider(provider="anthropic", model="claude-sonnet-4-6"),
        ocr_task=TaskProvider(provider="anthropic", model="claude-opus-4-8"),
        spend=SpendCaps(per_run=1.0, per_period=5.0, period="monthly"),
        ui=UiSettings(enabled=True, host="127.0.0.1", port=0, token=TOKEN),
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _handler(settings, *, config_file=None, environ=None):
    rm = webui.RunManager(settings)
    rm.set_config_file(config_file)
    return webui.make_handler(
        settings, token=settings.ui.token, run_manager=rm,
        config_file=config_file, environ=environ,
    )


# ===========================================================================
# r1 — DEFAULTS BYTE-IDENTICAL (characterization).
# ===========================================================================
# The exact instruction the pre-010 build_prompt emitted, inlined here as the
# golden reference so the test fails if the default ever drifts.
_ORIGINAL_METADATA_PREAMBLE = (
    "You are classifying a scanned/OCR'd document for a personal document "
    "management system. Generate metadata for it.\n\n"
    "STRONGLY PREFER reusing entries from these existing lists. Only invent a "
    "new value when none reasonably fits, and when you do, set the matching "
    "*_is_new flag / list so it can be reviewed."
)
_ORIGINAL_OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text from this document exactly as "
    "it appears, preserving reading order, line breaks, numbers, dates, and "
    "punctuation. Do not summarize, interpret, translate, or add commentary. "
    "If a region is illegible, write [illegible]. Output only the transcribed text."
)


def test_metadata_default_prompt_is_byte_identical():
    doc = {"title": "old", "content": "invoice text"}
    original = (
        _ORIGINAL_METADATA_PREAMBLE + "\n\n"
        "EXISTING CORRESPONDENTS:\nAcme\n\n"
        "EXISTING DOCUMENT TYPES:\nInvoice\n\n"
        "EXISTING TAGS:\nbilling\n\n"
        "CURRENT TITLE: old\n\n"
        "DOCUMENT TEXT:\ninvoice text"
    )
    # No instruction passed -> uses the default. Must equal the original string.
    resolved = build_prompt(doc, ["billing"], ["Acme"], ["Invoice"])
    assert resolved == original


def test_default_instruction_constants_byte_identical():
    assert prompts_mod.METADATA_INSTRUCTION_DEFAULT == _ORIGINAL_METADATA_PREAMBLE
    assert prompts_mod.OCR_INSTRUCTION_DEFAULT == _ORIGINAL_OCR_PROMPT
    # resolve_instruction with nothing customized returns the default UNCHANGED.
    assert prompts_mod.resolve_instruction(_ORIGINAL_OCR_PROMPT) is _ORIGINAL_OCR_PROMPT
    assert prompts_mod.resolve_instruction(
        _ORIGINAL_METADATA_PREAMBLE, prompt_override="", extra_instructions=""
    ) == _ORIGINAL_METADATA_PREAMBLE


def test_default_settings_resolve_to_none_and_default_models():
    s = Settings()
    # Nothing customized -> the engine gets None (byte-identical default path).
    assert s.resolved_instruction_or_none("metadata") is None
    assert s.resolved_instruction_or_none("ocr") is None
    # Default models unchanged.
    assert s.metadata_task.model == cfgmod.METADATA_MODEL == "claude-sonnet-4-6"
    assert s.ocr_task.model == cfgmod.OCR_MODEL == "claude-opus-4-8"


# ===========================================================================
# r2 — prompt resolution: extra appends, override replaces, reset.
# ===========================================================================
def test_extra_instructions_appends():
    out = prompts_mod.resolve_instruction("BASE", extra_instructions="also do X")
    assert out == "BASE\n\nalso do X"


def test_prompt_override_replaces():
    out = prompts_mod.resolve_instruction("BASE", prompt_override="REPLACED")
    assert out == "REPLACED"


def test_override_then_extra_compose():
    out = prompts_mod.resolve_instruction(
        "BASE", prompt_override="REPLACED", extra_instructions="+more")
    assert out == "REPLACED\n\n+more"


def test_blank_fields_reset_to_default():
    assert prompts_mod.resolve_instruction("BASE", prompt_override="   ") == "BASE"
    assert prompts_mod.resolve_instruction("BASE", extra_instructions="  \n ") == "BASE"


def test_settings_resolved_instruction_composes():
    s = Settings()
    s.metadata_prompts = PromptCustomization(
        extra_instructions="Dates as YYYY-MM-DD.", prompt_override="")
    resolved = s.resolved_instruction("metadata")
    assert resolved.startswith(prompts_mod.METADATA_INSTRUCTION_DEFAULT)
    assert resolved.endswith("Dates as YYYY-MM-DD.")
    assert s.resolved_instruction_or_none("metadata") == resolved


# ===========================================================================
# r4 — model catalog shape, pricing-derived, vision flag, custom tolerance.
# ===========================================================================
def test_model_catalog_shape_and_pricing():
    cat = model_catalog()
    assert "anthropic" in cat and "openai" in cat and "ollama" in cat
    opus = [m for m in cat["anthropic"] if m["id"] == "claude-opus-4-8"][0]
    assert opus["vision"] is True
    # Pricing hint derived from the per-token table: 5/1e6 * 1000 = 0.005 per 1K.
    assert opus["in_price_per_1k"] == pytest.approx(0.005)
    assert opus["out_price_per_1k"] == pytest.approx(0.025)
    # Ollama = local/zero-cost -> pricing hint is None (not zero-priced paid).
    llava = [m for m in cat["ollama"] if m["id"] == "llava"][0]
    assert llava["in_price_per_1k"] is None and llava["vision"] is True
    # A text-only local model is flagged non-vision.
    txt = [m for m in cat["ollama"] if m["id"] == "llama3.1"][0]
    assert txt["vision"] is False


def test_model_catalog_is_a_fuller_listing():
    """The catalog lists more than the original two Anthropic models, and the
    added OpenAI vision models are correctly flagged."""
    cat = model_catalog()
    anth_ids = {m["id"] for m in cat["anthropic"]}
    assert {"claude-fable-5", "claude-sonnet-5", "claude-haiku-4-5"} <= anth_ids
    assert len(anth_ids) >= 5
    # exactly one recommended-for-OCR and one recommended-for-metadata default,
    # so the defaults stay stable even as the list grows.
    assert sum(m["recommended_ocr"] for m in cat["anthropic"]) == 1
    assert sum(m["recommended_metadata"] for m in cat["anthropic"]) == 1
    gpt41 = [m for m in cat["openai"] if m["id"] == "gpt-4.1"][0]
    assert gpt41["vision"] is True and gpt41["in_price_per_1k"] == pytest.approx(0.002)


def test_is_vision_model_predicates():
    assert is_vision_model("anthropic", "claude-opus-4-8") is True
    assert is_vision_model("openai", "gpt-4o") is True
    assert is_vision_model("openai", "gpt-3.5-turbo") is False  # not vision
    assert is_vision_model("ollama", "llava:13b") is True       # substring hint
    assert is_vision_model("ollama", "llama3.1") is False


def test_models_payload_tolerates_custom_uncatalogued_model(tmp_path):
    s = _settings(tmp_path)
    s.ocr_task = TaskProvider(provider="anthropic", model="some-unlisted-model-x")
    body = webui_data.models_payload(s)  # must not raise
    assert body["current"]["ocr"]["in_catalog"] is False
    assert body["current"]["ocr"]["model"] == "some-unlisted-model-x"
    # metadata still resolves to a catalogued model.
    assert body["current"]["metadata"]["in_catalog"] is True


# ===========================================================================
# /api/models + /api/prompts — auth + shape.
# ===========================================================================
def test_models_and_prompts_require_token(tmp_path):
    h = _handler(_settings(tmp_path))
    for path in ("/api/models", "/api/prompts"):
        assert _drive(h, "GET", path, token=None).status == 401
        assert _drive(h, "GET", path, token="wrong").status == 401


def test_models_endpoint_shape(tmp_path):
    h = _handler(_settings(tmp_path))
    body = _drive(h, "GET", "/api/models", token=TOKEN).json()
    assert "catalog" in body and "current" in body
    assert body["current"]["ocr"]["vision"] is True  # opus is vision-capable


def test_provider_selection_consolidated_into_models_section(tmp_path):
    """The General settings form no longer carries free-text provider fields;
    provider + model are chosen consistently from the Models section dropdowns."""
    h = _handler(_settings(tmp_path))
    html = _drive(h, "GET", "/", token=None).text
    # The removed free-text General fields must be gone...
    assert '"metadata_provider","metadata provider"' not in html
    assert 'field("ocr_provider"' not in html
    # ...while the consistent per-task provider dropdown remains.
    assert "mp-'+task+'-provider" in html


def test_metadata_provider_editable_when_not_env_locked(tmp_path):
    """With no PA_METADATA_PROVIDER in the environment the provider is NOT
    env-locked, so it can be changed (provider + model) via /api/config."""
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg, environ={})
    cfgp = _drive(h, "GET", "/api/config", token=TOKEN).json()
    assert cfgp["env_locked"].get("metadata_provider") in (None, False)
    res = _drive(h, "POST", "/api/config", token=TOKEN,
                 body={"metadata": {"provider": "openai", "model": "gpt-4o-mini"}})
    assert res.status == 200
    body = _drive(h, "GET", "/api/models", token=TOKEN).json()
    assert body["current"]["metadata"]["provider"] == "openai"
    assert body["current"]["metadata"]["model"] == "gpt-4o-mini"


def test_metadata_provider_env_lock_still_reported(tmp_path):
    """Pinning PA_METADATA_PROVIDER still env-locks the field (env beats YAML)."""
    s = _settings(tmp_path)
    h = _handler(s, environ={"PA_METADATA_PROVIDER": "anthropic"})
    cfgp = _drive(h, "GET", "/api/config", token=TOKEN).json()
    assert cfgp["env_locked"].get("metadata_provider") is True


def test_reocr_vision_warning_data_for_non_vision_model(tmp_path):
    """The re-OCR picker warns when the configured OCR model isn't vision-capable;
    that warning is driven by the payload's `current.ocr.vision` flag."""
    s = _settings(tmp_path)
    # A text-only local model selected for OCR.
    s.ocr_task = TaskProvider(provider="ollama", model="llama3.1")
    body = webui_data.models_payload(s)
    assert body["current"]["ocr"]["vision"] is False  # -> UI shows the warning


def test_prompts_endpoint_effective_matches_engine(tmp_path):
    # The handler reloads settings from the persisted config (production reality:
    # prompts live in /data/config.yml), so persist via POST then read it back.
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    _drive(h, "POST", "/api/config", token=TOKEN,
           body={"metadata": {"extra_instructions": "Prefer my tags."}})
    body = _drive(h, "GET", "/api/prompts", token=TOKEN).json()
    md = body["metadata"]
    assert md["default"] == prompts_mod.METADATA_INSTRUCTION_DEFAULT
    assert md["extra_instructions"] == "Prefer my tags."
    # The effective preview equals what the engine will send (from the same loader).
    loaded = load_settings(config_file=cfg, require_token=False)
    assert md["effective"] == loaded.resolved_instruction("metadata")
    assert md["effective"].endswith("Prefer my tags.")
    # OCR uncustomized -> effective == default.
    assert body["ocr"]["effective"] == prompts_mod.OCR_INSTRUCTION_DEFAULT
    assert "fixed" in body["note"].lower()


# ===========================================================================
# r3 — config POST accepts prompt fields (NOT secret-flagged) + persists.
# ===========================================================================
def test_config_post_writes_prompt_fields(tmp_path):
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    r = _drive(h, "POST", "/api/config", token=TOKEN, body={
        "metadata": {"extra_instructions": "Dates as YYYY-MM-DD.",
                     "prompt_override": ""},
        "ocr": {"prompt_override": "Transcribe verbatim."},
    })
    assert r.status == 200, r.text
    import yaml
    written = yaml.safe_load(open(cfg, encoding="utf-8"))
    assert written["metadata"]["extra_instructions"] == "Dates as YYYY-MM-DD."
    assert written["ocr"]["prompt_override"] == "Transcribe verbatim."
    # And the loader reads them back (round-trips through the layered config).
    loaded = load_settings(config_file=cfg, require_token=False)
    assert loaded.metadata_prompts.extra_instructions == "Dates as YYYY-MM-DD."
    assert loaded.ocr_prompts.prompt_override == "Transcribe verbatim."


def test_prompt_fields_not_rejected_as_secrets(tmp_path):
    """The words prompt_override / extra_instructions must NOT trip the secret
    guard; only true secret keys are refused."""
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    r = _drive(h, "POST", "/api/config", token=TOKEN,
               body={"metadata": {"prompt_override": "x", "extra_instructions": "y"}})
    assert r.status == 200


def test_saving_models_preserves_prompt_fields(tmp_path):
    """Saving the model block must NOT drop previously-written prompt fields in the
    same block (they share the `metadata`/`ocr` YAML block)."""
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    _drive(h, "POST", "/api/config", token=TOKEN,
           body={"metadata": {"extra_instructions": "keep me"}})
    _drive(h, "POST", "/api/config", token=TOKEN,
           body={"metadata": {"provider": "anthropic", "model": "claude-opus-4-8"}})
    import yaml
    written = yaml.safe_load(open(cfg, encoding="utf-8"))
    assert written["metadata"]["extra_instructions"] == "keep me"  # not dropped
    assert written["metadata"]["model"] == "claude-opus-4-8"


def test_prompt_env_var_locks_field(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_METADATA_EXTRA_INSTRUCTIONS", "from env")
    locked = cfgmod.env_overridden_fields({"PA_METADATA_EXTRA_INSTRUCTIONS": "from env"})
    assert locked["metadata_extra_instructions"] is True
    assert locked["ocr_prompt_override"] is False


# ===========================================================================
# r2 guarantee — a CUSTOM prompt still goes through schema validation.
# ===========================================================================
def _wire_extractor(session_docs, responder, monkeypatch, tmp_path, *, instruction):
    from paperless_assistant.providers.anthropic import AnthropicProvider
    from paperless_assistant.fields import CustomFieldResolver
    from paperless_assistant.taxonomy import TaxonomyResolver
    from paperless_assistant.safety import SafetyLayer
    from paperless_assistant.spend import SpendGovernor
    from paperless_assistant.client import PaperlessClient

    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(),
                         docs=session_docs)
    client = fake.client()
    install_stub_anthropic(monkeypatch, responder)
    prov = AnthropicProvider(api_key="x", ocr_model="claude-opus-4-8",
                             metadata_model="claude-sonnet-4-6", max_ocr_tokens=8000)
    resolver = CustomFieldResolver(client)
    taxonomy = TaxonomyResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=str(tmp_path / "snap"))
    ext = MetadataExtractor(client, resolver, taxonomy, safety, SpendGovernor(),
                            new_tax_tag_id=555, provider=prov, instruction=instruction)
    return fake, ext


VALID = {"title": "Payment", "correspondent": "Acme", "document_type": "Letter",
         "tags": ["billing"], "correspondent_is_new": True,
         "document_type_is_new": True, "new_tags": ["billing"]}
MALFORMED = {"title": "Payment"}  # missing required fields


def test_custom_prompt_reaches_model_and_still_validates(monkeypatch, tmp_path):
    seen = {}

    def responder(**kw):
        # The custom instruction must be present in the prompt the model receives.
        seen["prompt"] = kw["messages"][0]["content"]
        return StubMessage([StubToolUseBlock(dict(VALID))])

    docs = [{"id": 7, "title": "old", "content": "invoice", "tags": [11],
             "custom_fields": [{"field": 2, "value": "opt-t"}]}]
    custom = "MY CUSTOM CLASSIFIER INSTRUCTION"
    fake, ext = _wire_extractor(docs, responder, monkeypatch, tmp_path,
                                instruction=custom)
    status, _, _, cost = ext.process_one(docs[0], dry_run=False)
    assert status.startswith("done")
    assert custom in seen["prompt"]              # the custom prompt was used
    assert len(fake.patches) == 1                # exactly one validated write


def test_custom_prompt_bad_output_is_never_written(monkeypatch, tmp_path):
    """A custom prompt that yields an off-schema response is caught by the FIXED
    engine schema and never produces a write (retry-then-error)."""
    def responder(**kw):
        return StubMessage([StubToolUseBlock(dict(MALFORMED))])

    docs = [{"id": 9, "title": "old", "content": "invoice", "tags": [],
             "custom_fields": []}]
    fake, ext = _wire_extractor(docs, responder, monkeypatch, tmp_path,
                                instruction="anything goes here")
    with pytest.raises(SchemaValidationError):
        ext.process_one(docs[0], dry_run=False)
    assert fake.patches == []  # NEVER written


# ===========================================================================
# Self-contained UI still holds with the new panels.
# ===========================================================================
def test_page_still_self_contained_with_new_panels():
    html = webui.PAGE_HTML
    assert html.startswith("<!DOCTYPE html>")
    assert not re.search(r'(?:src|href)\s*=\s*["\'][^"\']+["\']', html)
    assert "http://" not in html and "https://" not in html
    assert "cdn" not in html.lower()
    # The new panels are present.
    assert "AI models" in html and "Prompts" in html
