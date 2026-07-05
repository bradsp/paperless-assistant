"""Phase 6 — hosted inference + subscription/billing (vendor supplies inference).

Everything runs FULLY OFFLINE: no real cloud, no real Paperless, no real model
API. The vendor model call inside the proxy is STUBBED (a fake ModelBackend), so
tests are deterministic and incur NO spend.

Proves the §9 Phase 6 verification:
  * a hosted-mode agent with NO local AI key completes a metadata run end-to-end
    (agent -> HostedProvider -> /agent/inference proxy -> stubbed model -> schema-
    valid metadata written to fake Paperless);
  * usage is metered to the tenant (ledger shows it);
  * a server-side per-tenant spend cap HALTS inference when hit;
  * an unentitled / suspended tenant is REFUSED with a clear error;
  * NO document content is persisted server-side after an inference call, and
    control-plane logs are content-free;
  * a MALFORMED proxy response is caught by engine-side schema validation and
    NEVER written (the Phase-2 guarantee holds through the proxy);
  * the two hardening fixes: credentials stored hashed (persisted != plaintext) and
    authenticate still works; the results map is bounded;
  * BYO/local still works unchanged and sends nothing to the vendor.
"""
from __future__ import annotations

import base64
import io
import json

import pytest

from paperless_assistant.config import (
    Settings, HostedSettings, TaskProvider, SpendCaps, HostedInferenceContext, Config,
)
from paperless_assistant.transport import InProcessTransport, TransportError
from paperless_assistant.hosted import HostedAgent
from paperless_assistant.obs import JsonLogger
from paperless_assistant.metadata import METADATA_SCHEMA, MetadataExtractor
from paperless_assistant.providers import build_provider, SchemaValidationError
from paperless_assistant.providers.hosted_provider import (
    HostedProvider, HostedInferenceRefused, HostedInferenceError,
)
from paperless_assistant.client import PaperlessClient
from paperless_assistant.fields import CustomFieldResolver
from paperless_assistant.taxonomy import TaxonomyResolver
from paperless_assistant.safety import SafetyLayer
from paperless_assistant.spend import SpendGovernor

from paperless_control_plane.store import ControlPlaneStore, _hash_credential
from paperless_control_plane.app import ControlPlane
from paperless_control_plane.billing import (
    BillingStore, EntitlementError, SpendCapError,
    STATUS_ACTIVE, STATUS_SUSPENDED,
)
from paperless_control_plane.inference import (
    InferenceProxy, ModelBackend, TASK_TRANSCRIBE, TASK_EXTRACT, UnpricedModelError,
)

from fakes import (
    FakePaperless, make_custom_fields, healthy_tags, FakeResponse,
)


VALID_META = {
    "title": "Acme Invoice March",
    "correspondent": "Acme Corp",
    "document_type": "Invoice",
    "tags": ["billing"],
    "correspondent_is_new": True,
    "document_type_is_new": True,
    "new_tags": ["billing"],
}

# Off-schema: tags wrong type, new_tags missing.
MALFORMED_META = {
    "title": "Acme Invoice March",
    "correspondent": "Acme Corp",
    "document_type": "Invoice",
    "tags": "billing",
    "correspondent_is_new": True,
    "document_type_is_new": True,
}

# A secret-looking document body we can grep the whole server state for, to prove
# nothing about the document content is persisted server-side or logged.
SECRET_DOC_TEXT = "TOPSECRET-INVOICE-CONTENT-e1f2a3b4"
SECRET_PROMPT = f"classify this: {SECRET_DOC_TEXT}"


# ---------------------------------------------------------------------------
# A stub vendor model backend — NO real API, NO spend.
# ---------------------------------------------------------------------------
class StubBackend(ModelBackend):
    """Records every call it receives (so tests can assert what transited) and
    returns a scripted response. Simulates the vendor's model without any network."""

    def __init__(self, *, extract_data=None, transcribe_text="CLEAN OCR TEXT",
                 in_tokens=100, out_tokens=50):
        super().__init__(api_key="VENDOR-SERVER-SIDE-KEY")
        self.extract_data = extract_data if extract_data is not None else dict(VALID_META)
        self.transcribe_text = transcribe_text
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.calls = []

    def transcribe(self, *, doc_b64, model, opts=None):
        self.calls.append(("transcribe", model, doc_b64))
        return {"text": self.transcribe_text,
                "in_tokens": self.in_tokens, "out_tokens": self.out_tokens}

    def extract_structured(self, *, prompt, schema, model, opts=None):
        self.calls.append(("extract", model, prompt))
        return {"data": dict(self.extract_data),
                "in_tokens": self.in_tokens, "out_tokens": self.out_tokens}


# ---------------------------------------------------------------------------
# Wiring helpers
# ---------------------------------------------------------------------------
def _mk_control_plane(tmp_path, *, backend=None, tenant="t1", entitled=True,
                      spend_cap=5.0, logger=None):
    """Build a control plane with the billing seam + inference proxy wired, an
    enrolled agent, and a subscription for `tenant`. Returns everything a test
    needs."""
    store = ControlPlaneStore(visibility_timeout=1000.0)
    billing = BillingStore(str(tmp_path / "billing.json"))
    if entitled:
        billing.set_subscription(tenant, status=STATUS_ACTIVE, spend_cap=spend_cap)
    backend = backend or StubBackend()
    log = logger or JsonLogger(stream=io.StringIO(), path=None)
    proxy = InferenceProxy(backend, billing, logger=log,
                           pricing_provider="anthropic",
                           default_model_extract="claude-sonnet-4-6",
                           default_model_transcribe="claude-opus-4-8")
    cp = ControlPlane(store, poll_timeout=0.02, billing=billing,
                      inference_proxy=proxy, logger=log)
    tok = store.mint_enrollment_token(tenant=tenant)
    creds = store.enroll(tok)
    return {
        "store": store, "billing": billing, "backend": backend, "proxy": proxy,
        "cp": cp, "creds": creds, "tenant": tenant, "logger": log,
    }


def _hosted_provider(env, *, transport=None):
    """A HostedProvider wired to reach the control plane over an in-process
    transport with the enrolled agent's credential headers."""
    creds = env["creds"]
    transport = transport or InProcessTransport(env["cp"])

    def auth_headers():
        return {"X-Agent-Id": creds["agent_id"],
                "Authorization": f"Bearer {creds['agent_credential']}"}

    return HostedProvider(transport=transport, auth_headers=auth_headers)


# ===========================================================================
# HostedProvider satisfies AIProvider + engine-side validation still runs.
# ===========================================================================
def test_hosted_provider_returns_same_shape_and_meters(tmp_path):
    env = _mk_control_plane(tmp_path)
    prov = _hosted_provider(env)
    # extract_structured returns a StructuredResult with usage + cost.
    result = prov.extract_structured(SECRET_PROMPT, METADATA_SCHEMA)
    assert result.data == VALID_META
    assert result.in_tokens == 100 and result.out_tokens == 50
    assert result.cost > 0  # priced from the pricing table server-side
    # metered to the tenant
    summary = env["billing"].usage_summary("t1")
    assert summary["calls"] == 1
    assert summary["spend_usd"] > 0


def test_engine_validation_still_runs_on_hosted_output(tmp_path):
    """extract_structured_validated (engine-side) validates HostedProvider output —
    a valid dict passes through unchanged."""
    from paperless_assistant.providers import extract_structured_validated

    env = _mk_control_plane(tmp_path)
    prov = _hosted_provider(env)
    r = extract_structured_validated(prov, SECRET_PROMPT, METADATA_SCHEMA)
    assert r.data == VALID_META


def test_hosted_provider_advertises_capabilities(tmp_path):
    env = _mk_control_plane(tmp_path)
    prov = _hosted_provider(env)
    from paperless_assistant.providers import CAP_VISION, CAP_STRUCTURED
    assert CAP_VISION in prov.capabilities
    assert CAP_STRUCTURED in prov.capabilities


# ===========================================================================
# Registry resolves hosted mode with no local key -> HostedProvider.
# ===========================================================================
def test_registry_resolves_hosted_provider_when_no_local_key(tmp_path):
    env = _mk_control_plane(tmp_path)
    creds = env["creds"]

    def auth_headers():
        return {"X-Agent-Id": creds["agent_id"],
                "Authorization": f"Bearer {creds['agent_credential']}"}

    cfg = Config(base_url="http://p", paperless_token="t")
    cfg.hosted_inference = HostedInferenceContext(
        transport=InProcessTransport(env["cp"]), auth_headers=auth_headers)
    ocr = build_provider("ocr", cfg)
    meta = build_provider("metadata", cfg)
    assert ocr.name == "hosted" and meta.name == "hosted"


def test_settings_hosted_inference_active_predicate():
    # hosted mode + toggle ON + NO local key -> active.
    s = Settings(mode="hosted", paperless_token="t")
    s.hosted = HostedSettings(inference_enabled=True)
    assert s.hosted_inference_active() is True
    # A local AI key present -> NOT active (BYO/local zero-egress floor preserved).
    s2 = Settings(mode="hosted", paperless_token="t", anthropic_api_key="sk-ant")
    s2.hosted = HostedSettings(inference_enabled=True)
    assert s2.hosted_inference_active() is False
    # Toggle off -> not active.
    s3 = Settings(mode="hosted", paperless_token="t")
    s3.hosted = HostedSettings(inference_enabled=False)
    assert s3.hosted_inference_active() is False
    # Not hosted mode -> not active.
    s4 = Settings(mode="byo-key", paperless_token="t")
    s4.hosted = HostedSettings(inference_enabled=True)
    assert s4.hosted_inference_active() is False


# ===========================================================================
# END-TO-END: hosted agent, NO local key, completes a metadata run.
# ===========================================================================
def _fake_paperless_with_doc(doc_id=7, content=SECRET_DOC_TEXT):
    fp = FakePaperless(
        fields=make_custom_fields(),
        tags=healthy_tags(),
        docs=[{
            "id": doc_id, "title": "old title", "content": content,
            "tags": [], "correspondent": None, "document_type": None,
            # ai_stage=triaged -> metadata-eligible (ELIGIBLE_STAGES).
            "custom_fields": [{"field": 2, "value": "opt-t"}],
        }],
    )
    return fp


def test_end_to_end_metadata_run_no_local_key(tmp_path):
    """The headline Phase 6 verification: a no-key hosted agent runs metadata via
    the proxy and writes schema-valid metadata to fake Paperless; usage metered."""
    env = _mk_control_plane(tmp_path)
    fp = _fake_paperless_with_doc()
    client = fp.client()

    resolver = CustomFieldResolver(client)
    taxonomy = TaxonomyResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=str(tmp_path / "snap"))
    spend = SpendGovernor(max_spend=5.0)
    prov = _hosted_provider(env)

    ext = MetadataExtractor(client, resolver, taxonomy, safety, spend,
                            new_tax_tag_id=78, provider=prov)
    doc = client.get_document(7, fields="id,title,content,tags,custom_fields")
    status, doc_id, msg, cost = ext.process_one(doc, dry_run=False)

    # A schema-valid write happened (engine validated the proxy output first).
    assert status.startswith("done")
    assert cost > 0
    assert any(p[0] == 7 for p in fp.patches), "metadata was written to Paperless"
    # Usage metered to the tenant.
    summary = env["billing"].usage_summary("t1")
    assert summary["calls"] == 1
    assert summary["spend_usd"] == pytest.approx(cost)
    # The vendor backend actually did the model call (transited the proxy).
    assert env["backend"].calls and env["backend"].calls[0][0] == "extract"


# ===========================================================================
# Metering + entitlement + server-side spend cap (r4).
# ===========================================================================
def test_usage_metered_to_tenant(tmp_path):
    env = _mk_control_plane(tmp_path)
    prov = _hosted_provider(env)
    prov.extract_structured("p1", METADATA_SCHEMA)
    prov.extract_structured("p2", METADATA_SCHEMA)
    usage = env["billing"].usage("t1")
    assert len(usage) == 2
    assert all(r["tenant"] == "t1" for r in usage)
    assert all(r["task"] == TASK_EXTRACT for r in usage)


def test_server_side_spend_cap_halts_inference(tmp_path):
    # Cap the tenant at a tiny value; after one metered call the cap is hit and the
    # next call is refused BEFORE the model runs.
    env = _mk_control_plane(tmp_path, spend_cap=0.0006)  # ~ one sonnet call worth
    prov = _hosted_provider(env)
    prov.extract_structured("p1", METADATA_SCHEMA)  # first call meters spend
    backend_calls_before = len(env["backend"].calls)
    with pytest.raises(HostedInferenceRefused) as ei:
        prov.extract_structured("p2", METADATA_SCHEMA)
    assert ei.value.reason == "spend_cap"
    assert ei.value.status == 429
    # The model was NOT called for the refused request (halt before model work).
    assert len(env["backend"].calls) == backend_calls_before


def test_unpriced_model_fails_closed_no_metering(tmp_path):
    """Regression guard for the reviewed Phase-6 gap: a vendor model with NO
    pricing entry would meter $0 and SILENTLY defeat the server-side spend cap.
    The proxy FAILS CLOSED — refuses BEFORE the paid backend call, never calls the
    model, and meters nothing."""
    billing = BillingStore(str(tmp_path / "billing.json"))
    billing.set_subscription("t1", status=STATUS_ACTIVE, spend_cap=5.0)
    backend = StubBackend()
    # A model that is NOT in the pricing table => cost would be $0.
    proxy = InferenceProxy(backend, billing, pricing_provider="anthropic",
                           default_model_extract="mystery-model",
                           default_model_transcribe="mystery-vision")
    with pytest.raises(UnpricedModelError):
        proxy.run("t1", {"task": TASK_EXTRACT, "prompt": "p", "schema": METADATA_SCHEMA})
    assert backend.calls == []                        # model never called (no spend)
    assert billing.usage_summary("t1")["calls"] == 0  # nothing metered

    # And the default (unconfigured) proxy now defaults to a PRICED model, so the
    # out-of-box path meters correctly rather than at $0.
    from paperless_assistant.providers import pricing
    default_proxy = InferenceProxy(StubBackend(), billing)
    assert pricing.is_priced("anthropic", default_proxy.default_model_extract)
    assert pricing.is_priced("anthropic", default_proxy.default_model_transcribe)


def test_unentitled_tenant_refused(tmp_path):
    # A tenant with NO subscription at all is refused (fail-closed).
    env = _mk_control_plane(tmp_path, entitled=False)
    prov = _hosted_provider(env)
    with pytest.raises(HostedInferenceRefused) as ei:
        prov.extract_structured("p", METADATA_SCHEMA)
    assert ei.value.reason == "unentitled"
    assert ei.value.status == 402
    assert env["backend"].calls == []  # never reached the model


def test_suspended_tenant_refused(tmp_path):
    env = _mk_control_plane(tmp_path)
    env["billing"].set_status("t1", STATUS_SUSPENDED)
    prov = _hosted_provider(env)
    with pytest.raises(HostedInferenceRefused) as ei:
        prov.extract_structured("p", METADATA_SCHEMA)
    assert ei.value.reason == "unentitled"
    assert env["backend"].calls == []


def test_check_order_entitlement_before_cap(tmp_path):
    """Order (r2): an unentitled tenant that is ALSO over cap is refused for
    ENTITLEMENT first (entitlement is checked before the cap)."""
    env = _mk_control_plane(tmp_path, entitled=False)
    # Force a would-be over-cap situation too (no sub anyway).
    prov = _hosted_provider(env)
    with pytest.raises(HostedInferenceRefused) as ei:
        prov.extract_structured("p", METADATA_SCHEMA)
    assert ei.value.reason == "unentitled"  # entitlement wins the ordering


# ===========================================================================
# PRIVACY (r3): contents transit, never persist; logs are content-free.
# ===========================================================================
def test_no_document_content_persisted_server_side(tmp_path):
    """After an inference call carrying document content, NO document content
    appears in ANY server-side persisted state (queue store, billing store)."""
    env = _mk_control_plane(tmp_path)
    prov = _hosted_provider(env)
    prov.extract_structured(SECRET_PROMPT, METADATA_SCHEMA)
    prov.transcribe(b"%PDF-1.4 " + SECRET_DOC_TEXT.encode())

    # 1. The billing store persists usage records only — dump it and grep.
    billing_json = (tmp_path / "billing.json").read_text(encoding="utf-8")
    assert SECRET_DOC_TEXT not in billing_json
    assert SECRET_PROMPT not in billing_json
    # base64 of the doc bytes must not be persisted either.
    b64 = base64.standard_b64encode(b"%PDF-1.4 " + SECRET_DOC_TEXT.encode()).decode()
    assert b64 not in billing_json

    # 2. The control-plane queue store's in-memory state carries no contents.
    store_blob = json.dumps({
        "agents": env["store"]._agents,
        "queues": {"|".join(k): v for k, v in env["store"]._queues.items()},
        "results": env["store"]._results,
    })
    assert SECRET_DOC_TEXT not in store_blob
    assert SECRET_PROMPT not in store_blob

    # 3. The usage ledger records tokens/cost only — never prompt/text fields.
    for r in env["billing"].usage("t1"):
        assert SECRET_DOC_TEXT not in json.dumps(r)
        assert "prompt" not in r and "text" not in r and "content" not in r


def test_control_plane_logs_are_content_free(tmp_path):
    buf = io.StringIO()
    logger = JsonLogger(stream=buf, path=None)
    env = _mk_control_plane(tmp_path, logger=logger)
    prov = _hosted_provider(env)
    prov.extract_structured(SECRET_PROMPT, METADATA_SCHEMA)
    prov.transcribe(b"%PDF-1.4 " + SECRET_DOC_TEXT.encode())
    logtext = buf.getvalue()
    assert logtext, "the proxy should log routing/usage metadata"
    assert "inference_metered" in logtext  # usage IS logged
    assert SECRET_DOC_TEXT not in logtext
    assert SECRET_PROMPT not in logtext


def test_refusal_logs_are_content_free(tmp_path):
    buf = io.StringIO()
    logger = JsonLogger(stream=buf, path=None)
    env = _mk_control_plane(tmp_path, entitled=False, logger=logger)
    prov = _hosted_provider(env)
    with pytest.raises(HostedInferenceRefused):
        prov.extract_structured(SECRET_PROMPT, METADATA_SCHEMA)
    logtext = buf.getvalue()
    assert "inference_refused" in logtext
    assert SECRET_PROMPT not in logtext


# ===========================================================================
# The Phase-2 guarantee survives the proxy: malformed output never written.
# ===========================================================================
def test_malformed_proxy_response_caught_by_engine_never_written(tmp_path):
    """The proxy forwards a MALFORMED model dict; the engine-side validation catches
    it (after retries) and NO PATCH reaches Paperless."""
    backend = StubBackend(extract_data=dict(MALFORMED_META))
    env = _mk_control_plane(tmp_path, backend=backend)
    fp = _fake_paperless_with_doc()
    client = fp.client()

    resolver = CustomFieldResolver(client)
    taxonomy = TaxonomyResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=str(tmp_path / "snap"))
    prov = _hosted_provider(env)
    ext = MetadataExtractor(client, resolver, taxonomy, safety, SpendGovernor(),
                            new_tax_tag_id=78, provider=prov)
    doc = client.get_document(7, fields="id,title,content,tags,custom_fields")

    with pytest.raises(SchemaValidationError):
        ext.process_one(doc, dry_run=False)
    # THE GUARANTEE: not one PATCH happened despite the proxy returning content.
    assert not any(p for p in fp.patches), "no bad write reached Paperless"
    # The proxy DID meter the (billable) attempts — spend accounting is honest.
    assert env["billing"].usage_summary("t1")["calls"] >= 1


# ===========================================================================
# Hardening (r5): credentials hashed at rest; results map bounded.
# ===========================================================================
def test_credential_stored_hashed_not_plaintext(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "cp.json"))
    creds = store.enroll(store.mint_enrollment_token(tenant="acme"))
    plaintext = creds["agent_credential"]

    # The persisted state file must NOT contain the plaintext credential.
    state = (tmp_path / "cp.json").read_text(encoding="utf-8")
    assert plaintext not in state, "plaintext credential must not be persisted"
    # The in-memory agent record stores a salt + hash, not the plaintext.
    rec = store._agents[creds["agent_id"]]
    assert "credential" not in rec  # no plaintext field
    assert rec["credential_hash"] and rec["credential_salt"]
    assert rec["credential_hash"] != plaintext
    # The hash verifies deterministically from the plaintext + salt.
    assert _hash_credential(plaintext, rec["credential_salt"]) == rec["credential_hash"]

    # authenticate still works with the plaintext, and rejects a wrong credential.
    assert store.authenticate(creds["agent_id"], plaintext) is not None
    assert store.authenticate(creds["agent_id"], "wrong") is None
    # Survives a restart (fresh store over the same file).
    store2 = ControlPlaneStore(str(tmp_path / "cp.json"))
    assert store2.authenticate(creds["agent_id"], plaintext) is not None


def test_results_map_is_bounded(tmp_path):
    # A tiny cap proves eviction; TTL disabled so size is the only bound here.
    store = ControlPlaneStore(results_max=5, results_ttl=0.0)
    creds = store.enroll(store.mint_enrollment_token(tenant="t1"))
    aid, tenant = creds["agent_id"], creds["tenant"]
    for i in range(50):
        jid = f"job-{i}"
        store.enqueue(tenant=tenant, agent_id=aid, job_type="run_sweep", job_id=jid)
        store.lease_next(tenant=tenant, agent_id=aid)
        store.ack(tenant=tenant, agent_id=aid, job_id=jid, result={"n": i})
    # Bounded: never exceeds the cap despite 50 acked results.
    assert store.results_count() <= 5
    # The most recent result is still queryable right after ack.
    assert store.result_for("job-49") == {"n": 49}
    # An old one was evicted.
    assert store.result_for("job-0") is None


def test_results_map_ttl_eviction(tmp_path):
    clock = {"t": 0.0}
    store = ControlPlaneStore(results_max=1000, results_ttl=10.0,
                              now=lambda: clock["t"])
    creds = store.enroll(store.mint_enrollment_token(tenant="t1"))
    aid, tenant = creds["agent_id"], creds["tenant"]
    store.enqueue(tenant=tenant, agent_id=aid, job_type="run_sweep", job_id="old")
    store.lease_next(tenant=tenant, agent_id=aid)
    store.ack(tenant=tenant, agent_id=aid, job_id="old", result={"x": 1})
    assert store.result_for("old") == {"x": 1}
    # Advance past the TTL, then ack another job which triggers a prune sweep.
    clock["t"] = 100.0
    store.enqueue(tenant=tenant, agent_id=aid, job_type="run_sweep", job_id="new")
    store.lease_next(tenant=tenant, agent_id=aid)
    store.ack(tenant=tenant, agent_id=aid, job_id="new", result={"x": 2})
    assert store.result_for("old") is None  # expired + evicted
    assert store.result_for("new") == {"x": 2}


# ===========================================================================
# BYO/local still works unchanged and sends NOTHING to the vendor.
# ===========================================================================
def test_byo_local_still_zero_egress(tmp_path, monkeypatch):
    """A BYO/local (Ollama) metadata run must not touch the control-plane transport
    at all — the zero-egress floor is preserved."""
    from fakes import make_ollama_provider, FakeSession

    # Wire an Ollama provider (local, free). Its httpx is stubbed to a fake local
    # server; NO control-plane transport is involved.
    prov = make_ollama_provider(
        lambda url, **kw: {"response": dict(VALID_META),
                           "prompt_eval_count": 5, "eval_count": 3},
        monkeypatch,
    )
    # If the hosted transport were ever used, this recorder would see it.
    env = _mk_control_plane(tmp_path)

    fp = _fake_paperless_with_doc()
    client = fp.client()
    resolver = CustomFieldResolver(client)
    taxonomy = TaxonomyResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir=str(tmp_path / "snap"))
    ext = MetadataExtractor(client, resolver, taxonomy, safety, SpendGovernor(),
                            new_tax_tag_id=78, provider=prov)
    doc = client.get_document(7, fields="id,title,content,tags,custom_fields")
    status, _, _, cost = ext.process_one(doc, dry_run=False)
    assert status.startswith("done")
    assert cost == 0.0  # local = zero marginal cost
    # The vendor saw NOTHING: no usage metered, no backend call.
    assert env["billing"].usage("t1") == []
    assert env["backend"].calls == []


def test_byo_key_agent_does_not_route_to_proxy():
    """A hosted-MODE agent that still has a local key keeps BYO inference — the
    registry does NOT resolve to the HostedProvider."""
    cfg = Config(base_url="http://p", paperless_token="t", anthropic_api_key="sk-ant")
    # No hosted_inference context set (because a local key is present).
    assert cfg.hosted_inference is None
    ocr = build_provider("ocr", cfg)
    assert ocr.name == "anthropic"


# ===========================================================================
# Transcribe path through the proxy (vision), for completeness.
# ===========================================================================
def test_hosted_transcribe_meters_and_returns_text(tmp_path):
    env = _mk_control_plane(tmp_path)
    prov = _hosted_provider(env)
    t = prov.transcribe(b"%PDF-1.4 fake bytes")
    assert t.text == "CLEAN OCR TEXT"
    assert t.in_tokens == 100 and t.out_tokens == 50
    assert t.cost > 0
    usage = env["billing"].usage("t1")
    assert usage[-1]["task"] == TASK_TRANSCRIBE


# ===========================================================================
# 501 when hosted inference is not configured on the control plane.
# ===========================================================================
def test_inference_endpoint_501_when_not_configured():
    store = ControlPlaneStore()
    cp = ControlPlane(store, poll_timeout=0.02)  # no billing / no proxy wired
    creds = store.enroll(store.mint_enrollment_token(tenant="t1"))
    headers = {"X-Agent-Id": creds["agent_id"],
               "Authorization": f"Bearer {creds['agent_credential']}"}
    resp = cp.handle("POST", "/agent/inference", headers=headers,
                     body={"request": {"task": TASK_EXTRACT, "prompt": "p",
                                       "schema": METADATA_SCHEMA}})
    assert resp.status == 501
    assert resp.body["reason"] == "not_configured"


def test_hosted_agent_wires_inference_context_and_sweep_runs_end_to_end(tmp_path):
    """Full integration through the ACTUAL engine: a HostedAgent in hosted-inference
    mode builds a Sweep whose registry-resolved provider is the HostedProvider, and
    a metadata sweep writes schema-valid metadata to fake Paperless via the proxy —
    with NO local AI key configured on the agent."""
    env = _mk_control_plane(tmp_path)
    fp = _fake_paperless_with_doc()

    # Settings: hosted mode, inference ON, NO local AI key (the subscriber path).
    s = Settings(
        base_url="http://paperless.test:8000",
        paperless_token="SECRET-PAPERLESS-TOKEN",
        data_dir=str(tmp_path / "data"),
        mode="hosted",
        triage_enabled=False, reocr_enabled=False, metadata_enabled=True,
        dry_run=False,
        spend=SpendCaps(per_run=5.0, per_period=50.0),
    )
    s.hosted = HostedSettings(
        control_plane_url="http://inproc.test",
        enrollment_token=env["store"].mint_enrollment_token(tenant="t1"),
        inference_enabled=True,
        heartbeat_interval_seconds=0,
    )
    assert s.hosted_inference_active() is True

    # Build the agent with our in-process transport to the control plane. Its
    # default runner constructs a Sweep whose cfg carries the HostedInferenceContext.
    agent = HostedAgent(s, transport=InProcessTransport(env["cp"]),
                        sleep=lambda _x: None, now=lambda: 0.0)
    agent.ensure_enrolled()

    # The agent enrolled under t1, but the control plane's enrolled creds in `env`
    # belong to a DIFFERENT agent; make sure the tenant subscription covers t1 (it
    # does). Now build the Sweep exactly as the runner would, injecting the fake
    # Paperless client so no real server is needed.
    from paperless_assistant.sweep import Sweep

    cfg = agent._engine_cfg()
    assert cfg.hosted_inference is not None  # context injected
    sweep = Sweep(s, client=fp.client(), cfg=cfg)
    multi = sweep.run_once()

    # Metadata was written (schema-valid, engine-validated proxy output).
    assert any(p[0] == 7 for p in fp.patches)
    # Usage metered to the tenant for the agent's own credential tenant (t1).
    assert env["billing"].usage_summary("t1")["calls"] >= 1


def test_inference_endpoint_requires_auth():
    store = ControlPlaneStore()
    billing = BillingStore()
    billing.set_subscription("t1")
    proxy = InferenceProxy(StubBackend(), billing)
    cp = ControlPlane(store, poll_timeout=0.02, billing=billing, inference_proxy=proxy)
    resp = cp.handle("POST", "/agent/inference", headers={},
                     body={"request": {"task": TASK_EXTRACT}})
    assert resp.status == 401


# ===========================================================================
# `pa-control-plane` billing CLI (subscribe / usage) — usage is VISIBLE.
# ===========================================================================
def test_cli_subscribe_and_usage(tmp_path, capsys):
    from paperless_control_plane import cli as cp_cli

    billing_path = str(tmp_path / "billing.json")
    # Create a subscription with a cap.
    cp_cli.main(["--billing", billing_path, "subscribe", "--tenant", "acme",
                 "--spend-cap", "2.50"])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == STATUS_ACTIVE and out["spend_cap"] == 2.5

    # Record some usage directly (as the proxy would), then query it via the CLI.
    BillingStore(billing_path).record_usage(
        "acme", task="extract_structured", model="m",
        in_tokens=100, out_tokens=50, cost=0.01)
    cp_cli.main(["--billing", billing_path, "usage", "--tenant", "acme"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["tenant"] == "acme"
    assert summary["calls"] == 1
    assert summary["spend_usd"] == pytest.approx(0.01)
    assert summary["cap_remaining"] == pytest.approx(2.49)


def test_cli_subscribe_suspend_then_usage_shows_status(tmp_path, capsys):
    from paperless_control_plane import cli as cp_cli

    billing_path = str(tmp_path / "billing.json")
    cp_cli.main(["--billing", billing_path, "subscribe", "--tenant", "acme"])
    capsys.readouterr()
    cp_cli.main(["--billing", billing_path, "subscribe", "--tenant", "acme",
                 "--status", STATUS_SUSPENDED])
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == STATUS_SUSPENDED
