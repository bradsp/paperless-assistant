"""Phase 2: AIProvider abstraction + the structured-output guarantee.

Everything runs fully offline: no network, no real API keys, no Ollama server.
Each provider's client/HTTP is stubbed.

Covers:
  * engine-side schema validation (good passes, malformed raises)
  * the metadata task through Anthropic AND OpenAI/Ollama on the SAME input,
    both yielding schema-valid metadata
  * a malformed provider response is caught, retried, and NEVER written (no PATCH)
  * capability negotiation: a vision-less provider selected for re-OCR raises a
    clear error and performs no download/consume
  * per-provider pricing feeds the SpendGovernor; the cap still aborts work
"""
import io

import pytest

from paperless_assistant import config
from paperless_assistant.metadata import METADATA_SCHEMA, MetadataExtractor
from paperless_assistant.ocr import OcrPipeline
from paperless_assistant.client import PaperlessClient
from paperless_assistant.fields import CustomFieldResolver
from paperless_assistant.taxonomy import TaxonomyResolver
from paperless_assistant.safety import SafetyLayer
from paperless_assistant.spend import SpendGovernor
from paperless_assistant.providers import (
    CAP_VISION,
    CapabilityError,
    SchemaValidationError,
    StructuredResult,
    Transcription,
    validate_against_schema,
    extract_structured_validated,
    build_provider,
)
from paperless_assistant.providers import pricing

from fakes import (
    FakeSession,
    FakeResponse,
    make_openai_provider,
    make_ollama_provider,
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

VALID_META = {
    "title": "Acme Invoice March",
    "correspondent": "Acme Corp",
    "document_type": "Invoice",
    "tags": ["billing"],
    "correspondent_is_new": True,
    "document_type_is_new": True,
    "new_tags": ["billing"],
}

# Missing required "new_tags" and wrong type on tags -> off-schema.
MALFORMED_META = {
    "title": "Acme Invoice March",
    "correspondent": "Acme Corp",
    "document_type": "Invoice",
    "tags": "billing",              # should be an array
    "correspondent_is_new": True,
    "document_type_is_new": True,
    # "new_tags" missing entirely
}


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
# Engine-side validation: the guarantee in isolation (no adapter).
# ===========================================================================
def test_validate_good_metadata_passes():
    validate_against_schema(VALID_META, METADATA_SCHEMA)  # no raise


def test_validate_malformed_metadata_raises():
    with pytest.raises(SchemaValidationError):
        validate_against_schema(MALFORMED_META, METADATA_SCHEMA)


class _ScriptedProvider:
    """A minimal AIProvider that returns a scripted sequence of dicts."""
    name = "scripted"
    capabilities = {"structured_output"}

    def __init__(self, sequence):
        self._seq = list(sequence)
        self.calls = 0

    def extract_structured(self, prompt, schema, *, opts=None):
        data = self._seq[min(self.calls, len(self._seq) - 1)]
        self.calls += 1
        return StructuredResult(data=data, in_tokens=10, out_tokens=5, cost=0.01)

    def transcribe(self, doc, *, opts=None):  # pragma: no cover
        return Transcription("", 0, 0, 0.0)


def test_extract_validated_retries_then_succeeds():
    prov = _ScriptedProvider([MALFORMED_META, VALID_META])
    result = extract_structured_validated(prov, "p", METADATA_SCHEMA, max_attempts=3)
    assert result.data == VALID_META
    assert prov.calls == 2                 # retried once
    assert result.cost == pytest.approx(0.02)  # cost of BOTH attempts counted


def test_extract_validated_all_malformed_raises_with_spend():
    prov = _ScriptedProvider([MALFORMED_META])
    with pytest.raises(SchemaValidationError) as ei:
        extract_structured_validated(prov, "p", METADATA_SCHEMA, max_attempts=3)
    assert prov.calls == 3
    assert getattr(ei.value, "spent", 0.0) == pytest.approx(0.03)


# ===========================================================================
# The metadata task through >=2 providers on the SAME input -> both valid.
# ===========================================================================
def test_metadata_valid_through_anthropic(monkeypatch, tmp_path):
    from fakes import StubMessage, StubToolUseBlock, install_stub_anthropic
    from paperless_assistant.providers.anthropic import AnthropicProvider

    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock(dict(VALID_META))]))
    prov = AnthropicProvider(api_key="x", ocr_model=config.OCR_MODEL,
                             metadata_model=config.METADATA_MODEL, max_ocr_tokens=8000)
    session = FakeSession()
    # snapshot dir under tmp
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "a")
    spend = SpendGovernor()
    session.add("POST", "/api/correspondents/", lambda m, u, **k: FakeResponse(200, {"id": 5}))
    session.add("POST", "/api/document_types/", lambda m, u, **k: FakeResponse(200, {"id": 8}))
    session.add("POST", "/api/tags/", lambda m, u, **k: FakeResponse(200, {"id": 30}))
    patched = []
    session.add("PATCH", "/api/documents/", lambda m, u, **k: (patched.append(k["json"]), FakeResponse(200, {}))[1])
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend,
                            new_tax_tag_id=555, provider=prov)
    doc = {"id": 7, "title": "old", "content": "invoice", "tags": [11],
           "custom_fields": [{"field": 2, "value": "opt-t"}]}
    status, _, _, cost = ext.process_one(doc, dry_run=False)
    assert status == "done_new_tax"
    assert cost > 0
    validate_against_schema(dict(VALID_META), METADATA_SCHEMA)
    assert len(patched) == 1  # exactly one write, after validation


def test_metadata_valid_through_openai(monkeypatch, tmp_path):
    import json
    prov = make_openai_provider(
        lambda **kw: __import__("fakes")._OAIResponse(json.dumps(VALID_META))
    )
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "o")
    spend = SpendGovernor()
    session.add("POST", "/api/correspondents/", lambda m, u, **k: FakeResponse(200, {"id": 5}))
    session.add("POST", "/api/document_types/", lambda m, u, **k: FakeResponse(200, {"id": 8}))
    session.add("POST", "/api/tags/", lambda m, u, **k: FakeResponse(200, {"id": 30}))
    patched = []
    session.add("PATCH", "/api/documents/", lambda m, u, **k: (patched.append(k["json"]), FakeResponse(200, {}))[1])
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend,
                            new_tax_tag_id=555, provider=prov)
    doc = {"id": 7, "title": "old", "content": "invoice", "tags": [11],
           "custom_fields": [{"field": 2, "value": "opt-t"}]}
    status, _, _, cost = ext.process_one(doc, dry_run=False)
    assert status == "done_new_tax"
    # OpenAI gpt-4o priced -> nonzero cost feeds the governor
    assert cost > 0
    assert len(patched) == 1


def test_openai_strict_schema_translation():
    """OpenAI structured outputs require `additionalProperties: false` + full
    `required` on every object (else a 400). The adapter translates the engine
    schema without mutating the canonical one."""
    from paperless_assistant.providers.openai import _openai_strict_schema

    strict = _openai_strict_schema(METADATA_SCHEMA)
    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == set(METADATA_SCHEMA["properties"].keys())
    # An array-of-strings property is not an object -> left untouched.
    assert "additionalProperties" not in strict["properties"]["tags"]
    # The engine's canonical schema is NOT mutated (it re-validates against THIS).
    assert "additionalProperties" not in METADATA_SCHEMA


def test_openai_request_sends_strict_compatible_schema():
    """The reported 400 ('additionalProperties is required...') is fixed: the
    request carries the strict-dialect schema."""
    import json
    import fakes

    seen = {}

    def responder(**kw):
        seen.update(kw)
        return fakes._OAIResponse(json.dumps(VALID_META))

    prov = make_openai_provider(responder)
    prov.extract_structured("p", METADATA_SCHEMA)
    rf = seen["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["additionalProperties"] is False


def test_openai_client_configures_retry_budget(monkeypatch):
    """Transient TPM 429s should be ridden out by the SDK's retry-after backoff,
    so the client is built with a generous max_retries."""
    from paperless_assistant.providers import openai as oai_mod

    captured = {}

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(oai_mod, "_load_openai", lambda: _FakeOpenAI)
    prov = oai_mod.OpenAIProvider(api_key="k", ocr_model="gpt-4o",
                                  metadata_model="gpt-4o", max_ocr_tokens=8000,
                                  max_retries=6)
    prov._client()
    assert captured["max_retries"] == 6


def test_metadata_valid_through_ollama(monkeypatch, tmp_path):
    prov = make_ollama_provider(
        lambda url, **kw: {"response": dict(VALID_META), "prompt_eval_count": 12, "eval_count": 7},
        monkeypatch,
    )
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "l")
    spend = SpendGovernor()
    session.add("POST", "/api/correspondents/", lambda m, u, **k: FakeResponse(200, {"id": 5}))
    session.add("POST", "/api/document_types/", lambda m, u, **k: FakeResponse(200, {"id": 8}))
    session.add("POST", "/api/tags/", lambda m, u, **k: FakeResponse(200, {"id": 30}))
    patched = []
    session.add("PATCH", "/api/documents/", lambda m, u, **k: (patched.append(k["json"]), FakeResponse(200, {}))[1])
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend,
                            new_tax_tag_id=555, provider=prov)
    doc = {"id": 7, "title": "old", "content": "invoice", "tags": [11],
           "custom_fields": [{"field": 2, "value": "opt-t"}]}
    status, _, _, cost = ext.process_one(doc, dry_run=False)
    assert status == "done_new_tax"
    assert cost == 0.0  # local = zero marginal cost
    assert len(patched) == 1


# ===========================================================================
# THE NEGATIVE PATH: malformed provider output is caught, retried, NEVER written.
# ===========================================================================
def test_malformed_output_never_writes_ollama(monkeypatch, tmp_path):
    # Always returns off-schema output -> validation must fail after retries and
    # NO PATCH may reach Paperless.
    prov = make_ollama_provider(
        lambda url, **kw: {"response": dict(MALFORMED_META), "prompt_eval_count": 1, "eval_count": 1},
        monkeypatch,
    )
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "m")
    spend = SpendGovernor()
    session.add("PATCH", "/api/documents/", lambda m, u, **k: FakeResponse(200, {}))
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend,
                            new_tax_tag_id=555, provider=prov)
    doc = {"id": 7, "title": "old", "content": "invoice", "tags": [11],
           "custom_fields": [{"field": 2, "value": "opt-t"}]}
    with pytest.raises(SchemaValidationError):
        ext.process_one(doc, dry_run=False)
    # The guarantee: not a single PATCH happened.
    assert not any(c[0] == "PATCH" for c in session.calls)


def test_malformed_output_never_writes_anthropic(monkeypatch, tmp_path):
    from fakes import StubMessage, StubToolUseBlock, install_stub_anthropic
    from paperless_assistant.providers.anthropic import AnthropicProvider

    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock(dict(MALFORMED_META))]))
    prov = AnthropicProvider(api_key="x", ocr_model=config.OCR_MODEL,
                             metadata_model=config.METADATA_MODEL, max_ocr_tokens=8000)
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "ma")
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, SpendGovernor(),
                            new_tax_tag_id=555, provider=prov)
    doc = {"id": 7, "title": "old", "content": "invoice", "custom_fields": []}
    with pytest.raises(SchemaValidationError):
        ext.process_one(doc, dry_run=False)
    assert not any(c[0] == "PATCH" for c in session.calls)


# ===========================================================================
# Capability negotiation: vision-less provider for re-OCR -> clear error, no I/O.
# ===========================================================================
def test_reocr_visionless_provider_refuses_no_download(monkeypatch, tmp_path):
    # A text-only Ollama model has no vision capability.
    prov = make_ollama_provider(lambda url, **kw: {}, monkeypatch, ocr_model="llama3.1")
    assert CAP_VISION not in prov.capabilities

    session = FakeSession()
    resolver = _resolver(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "cap")
    # If any download/consume were attempted the fake session has NO route ->
    # it would raise AssertionError("no fake route"), a different error. We assert
    # we get CapabilityError and that download was never called.
    pipeline = OcrPipeline(_client(session), resolver, safety, SpendGovernor(),
                           built_dir=tmp_path / "b", provider=prov)
    doc = {"id": 7, "title": "bad scan", "custom_fields": []}
    with pytest.raises(CapabilityError):
        pipeline.process_one(doc, superseded_tag_id=99, dry_run=False)
    # No download, no post_document, no PATCH.
    assert not any("download" in c[1] for c in session.calls)
    assert not any(c[0] == "POST" for c in session.calls)
    assert not any(c[0] == "PATCH" for c in session.calls)
    # And no snapshot was written (refused before any work).
    assert not (tmp_path / "cap" / "7.json").exists()


def test_vision_provider_allows_reocr(monkeypatch, tmp_path):
    # A llava-class Ollama model IS vision-capable.
    prov = make_ollama_provider(
        lambda url, **kw: {"response": "CLEAN OCR TEXT", "prompt_eval_count": 3, "eval_count": 9},
        monkeypatch, ocr_model="llava:13b",
    )
    assert CAP_VISION in prov.capabilities

    session = FakeSession()
    resolver = _resolver(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "s")

    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 72, "scan")
    c.showPage()
    c.save()
    pdf = buf.getvalue()

    session.add("GET", "/api/documents/7/download/", lambda m, u, **k: FakeResponse(200, content=pdf))
    pipeline = OcrPipeline(_client(session), resolver, safety, SpendGovernor(),
                           built_dir=tmp_path / "b", provider=prov)
    doc = {"id": 7, "title": "bad scan", "custom_fields": []}
    status, _, msg, cost = pipeline.process_one(doc, superseded_tag_id=99, dry_run=True)
    assert status == "dry"
    assert cost == 0.0  # local


# ===========================================================================
# Pricing moved into providers; SpendGovernor still gates.
# ===========================================================================
def test_pricing_table_anthropic_current_rates():
    # Current published per-Mtok rates (Opus 4.8 is $5/$25, not the pre-4.8 $15/$75).
    assert pricing.cost_of("anthropic", "claude-opus-4-8", 1_000_000, 0) == pytest.approx(5.0)
    assert pricing.cost_of("anthropic", "claude-opus-4-8", 0, 1_000_000) == pytest.approx(25.0)
    assert pricing.cost_of("anthropic", "claude-sonnet-4-6", 1_000_000, 0) == pytest.approx(3.0)
    assert pricing.cost_of("anthropic", "claude-sonnet-4-6", 0, 1_000_000) == pytest.approx(15.0)


def test_pricing_table_expanded_current_models():
    # The catalog now lists a fuller set of current models, all priced + gated.
    for m in ("claude-fable-5", "claude-opus-4-7", "claude-sonnet-5", "claude-haiku-4-5"):
        assert pricing.is_priced("anthropic", m), m
    for m in ("gpt-4.1", "gpt-4.1-mini", "o4-mini", "gpt-5.4"):
        assert pricing.is_priced("openai", m), m


def test_pricing_ollama_is_free():
    assert pricing.cost_of("ollama", "llava:13b", 10_000, 10_000) == 0.0


def test_spend_cap_still_aborts_through_provider(monkeypatch, tmp_path):
    prov = make_ollama_provider(
        lambda url, **kw: {"response": dict(VALID_META)}, monkeypatch,
    )
    session = FakeSession()
    resolver = _resolver(session)
    taxonomy = _taxonomy(session)
    safety = SafetyLayer(_client(session), resolver, snapshot_dir=tmp_path / "sc")
    spend = SpendGovernor(max_spend=5.0)
    spend.add(6.0)  # already over cap
    called = {"n": 0}
    prov_wrapped = prov

    def _spy(*a, **k):
        called["n"] += 1
        return StructuredResult(data=dict(VALID_META), in_tokens=1, out_tokens=1, cost=0.0)

    prov_wrapped.extract_structured = _spy  # type: ignore[method-assign]
    ext = MetadataExtractor(_client(session), resolver, taxonomy, safety, spend,
                            new_tax_tag_id=555, provider=prov_wrapped)
    doc = {"id": 7, "content": "t", "custom_fields": []}
    status, _, _, cost = ext.process_one(doc, dry_run=False)
    assert status == "spend_cap"
    assert called["n"] == 0  # provider never invoked once cap is hit (I3)


# ===========================================================================
# Registry / factory resolves config -> provider per task (Anthropic default).
# ===========================================================================
def test_registry_default_is_anthropic():
    cfg = config.Config(base_url=BASE, paperless_token="t", anthropic_api_key="k")
    ocr = build_provider("ocr", cfg)
    meta = build_provider("metadata", cfg)
    assert ocr.name == "anthropic" and meta.name == "anthropic"
    assert CAP_VISION in ocr.capabilities


def test_registry_builds_openai_and_ollama():
    cfg = config.Config(base_url=BASE, paperless_token="t",
                        ocr_provider="ollama", metadata_provider="openai",
                        openai_api_key="k", ocr_model="llava:13b")
    ocr = build_provider("ocr", cfg)
    meta = build_provider("metadata", cfg)
    assert ocr.name == "ollama"
    assert meta.name == "openai"


def test_registry_unknown_provider_raises():
    from paperless_assistant.providers import ProviderError
    cfg = config.Config(base_url=BASE, paperless_token="t", metadata_provider="bogus")
    with pytest.raises(ProviderError):
        build_provider("metadata", cfg)
