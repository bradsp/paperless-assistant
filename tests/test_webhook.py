"""Phase 4 on-ingest webhook NUDGE receiver tests (plan §6.2, §8.1).

Everything runs fully offline: NO live Paperless, NO real keys, NO real inbound
network dependency. The receiver is driven two ways:
  * the persisted queue + payload parser are unit-tested directly;
  * the HTTP handler is exercised end-to-end over an EPHEMERAL localhost port
    (bound within the test on 127.0.0.1:0), against the existing FakePaperless +
    stubbed Anthropic provider.

Proves:
  * a valid {doc_url} nudge processes exactly that document through the pipeline;
  * unauthenticated / malformed / non-integer nudges are rejected (4xx) and do
    nothing;
  * duplicate nudges for the same doc do not double-process / double-spend;
  * a not-yet-OCR'd doc is handled gracefully (no garbage write);
  * nudges/work survive a simulated restart and resume without reprocessing;
  * the receiver publishes no *host* port (it binds a socket the test controls;
    the compose no-ports posture is asserted structurally in test_docker/compose).
"""
from __future__ import annotations

import json
import urllib.request
import urllib.error

import pytest

from paperless_assistant.config import Settings, TaskProvider, SpendCaps, WebhookSettings
from paperless_assistant.sweep import Sweep
from paperless_assistant.webhook import (
    parse_doc_id, NudgeQueue, WebhookServer,
)
from fakes import (
    FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------
def _docs():
    # doc 1: clean, untriaged (metadata-eligible after/at triage).
    # doc 2: garbage, untriaged (triage flags it; metadata skips it).
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
        webhook=WebhookSettings(enabled=True, host="127.0.0.1", port=0,
                                secret="s3cr3t", debounce_seconds=30.0),
        dry_run=False,  # write mode so we can observe real PATCHes
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


def _post(url, body, *, token=None, header_secret=None, raw=None):
    """POST helper returning (status, json_dict). Non-2xx raises are caught so we
    can assert on 4xx codes."""
    data = raw if raw is not None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if header_secret:
        req.add_header("X-PA-Webhook-Secret", header_secret)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


# ===========================================================================
# parse_doc_id — untrusted input, id only
# ===========================================================================
def test_parse_doc_id_from_doc_url():
    assert parse_doc_id({"doc_url": "http://pl:8000/documents/42/"}) == 42
    assert parse_doc_id({"doc_url": "http://pl:8000/api/documents/7"}) == 7
    assert parse_doc_id({"doc_url": "https://x/documents/99/?foo=bar"}) == 99


def test_parse_doc_id_rejects_non_integer():
    assert parse_doc_id({"doc_url": "http://pl/documents/abc/"}) is None
    assert parse_doc_id({"doc_url": "not a url at all"}) is None
    assert parse_doc_id({}) is None
    assert parse_doc_id({"doc_url": None}) is None
    assert parse_doc_id({"doc_id": True}) is None  # bool is not a doc id


def test_parse_doc_id_defensive_fallbacks():
    assert parse_doc_id({"doc_id": 5}) == 5
    assert parse_doc_id({"id": "13"}) == 13


# ===========================================================================
# NudgeQueue — persistence, debounce, restart-safety
# ===========================================================================
def test_queue_enqueue_and_debounce(tmp_path):
    clock = [1000.0]
    q = NudgeQueue(str(tmp_path / "q.json"), debounce_seconds=30.0,
                   now=lambda: clock[0])
    assert q.enqueue(1) is True          # newly queued
    assert q.enqueue(1) is False         # within window -> debounced
    clock[0] += 100                      # advance past window
    assert q.enqueue(1) is True          # queued again (but still one pending id)
    assert q.pending_ids() == [1]


def test_queue_survives_restart(tmp_path):
    path = str(tmp_path / "q.json")
    q1 = NudgeQueue(path)
    q1.enqueue(7)
    q1.enqueue(8)
    # New instance = simulated process restart; state is read from /data.
    q2 = NudgeQueue(path)
    assert q2.pending_ids() == [7, 8]
    q2.mark_done(7)
    q3 = NudgeQueue(path)
    assert q3.pending_ids() == [8]
    assert q3.is_done(7) is True


# ===========================================================================
# End-to-end over an ephemeral localhost port
# ===========================================================================
@pytest.fixture
def server(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path)
    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    srv.start()
    host, port = srv.address
    url = f"http://{host}:{port}{s.webhook.path}"
    yield srv, url, fake, s
    srv.stop()


def test_valid_nudge_processes_exactly_that_doc(server):
    srv, url, fake, s = server
    status, body = _post(url, {"doc_url": f"http://pl/documents/1/"}, token="s3cr3t")
    assert status == 202 and body["doc_id"] == 1
    # doc 1 was pulled + processed: triage + metadata PATCHed it; doc 2 untouched.
    patched_ids = {doc_id for doc_id, _ in fake.patches}
    assert patched_ids == {1}
    # metadata_done advanced (a metadata PATCH carries custom_fields with the stage).
    assert any("title" in b for _, b in fake.patches)


def test_unauthenticated_nudge_rejected_and_does_nothing(server):
    srv, url, fake, s = server
    # No token at all.
    status, body = _post(url, {"doc_url": "http://pl/documents/1/"})
    assert status == 401
    assert fake.patches == []
    # Wrong token.
    status2, _ = _post(url, {"doc_url": "http://pl/documents/1/"}, token="wrong")
    assert status2 == 401
    assert fake.patches == []


def test_malformed_nudge_rejected(server):
    srv, url, fake, s = server
    status, body = _post(url, None, token="s3cr3t", raw=b"{not json")
    assert status == 400
    assert fake.patches == []


def test_non_integer_nudge_rejected(server):
    srv, url, fake, s = server
    status, body = _post(url, {"doc_url": "http://pl/documents/notanid/"}, token="s3cr3t")
    assert status == 400
    assert fake.patches == []


def test_duplicate_nudges_do_not_double_process(server):
    srv, url, fake, s = server
    _post(url, {"doc_url": "http://pl/documents/1/"}, token="s3cr3t")
    n_after_first = len(fake.patches)
    assert n_after_first > 0
    # Immediate duplicate: debounced by the queue -> no new work, no double-spend.
    status, body = _post(url, {"doc_url": "http://pl/documents/1/"}, token="s3cr3t")
    assert status == 202 and body["debounced"] is True
    assert len(fake.patches) == n_after_first


def test_duplicate_after_debounce_is_idempotent_no_op(tmp_path, monkeypatch):
    # Even with debounce bypassed (window elapsed), a second nudge for an
    # already-done doc is a cheap no-op: the stage predicates skip it (I1), so
    # no metadata re-spend.
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, webhook=WebhookSettings(
        enabled=True, host="127.0.0.1", port=0, secret="s3cr3t",
        debounce_seconds=0.0))  # no debounce window
    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    srv.start()
    try:
        host, port = srv.address
        url = f"http://{host}:{port}{s.webhook.path}"
        _post(url, {"doc_url": "http://pl/documents/1/"}, token="s3cr3t")
        n_first = len(fake.patches)
        assert n_first > 0
        fake.patches.clear()
        # Second nudge, no debounce: doc 1 is now metadata_done + triaged ->
        # every stage predicate skips it. No PATCH, no spend.
        _post(url, {"doc_url": "http://pl/documents/1/"}, token="s3cr3t")
        assert fake.patches == []
    finally:
        srv.stop()


def test_not_yet_ocrd_doc_handled_gracefully(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    # doc 3 has NO content yet (nudge slipped in before OCR).
    docs = _docs() + [{"id": 3, "title": "fresh", "content": "",
                       "tags": [], "custom_fields": []}]
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=docs)
    s = _settings(tmp_path)
    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    srv.start()
    try:
        host, port = srv.address
        url = f"http://{host}:{port}{s.webhook.path}"
        status, body = _post(url, {"doc_url": "http://pl/documents/3/"}, token="s3cr3t")
        assert status == 202
        # Gracefully skipped: no garbage written for the not-yet-OCR'd doc.
        assert all(doc_id != 3 for doc_id, _ in fake.patches)
    finally:
        srv.stop()


def test_nudge_for_missing_doc_is_noop(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path)
    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    srv.start()
    try:
        host, port = srv.address
        url = f"http://{host}:{port}{s.webhook.path}"
        status, _ = _post(url, {"doc_url": "http://pl/documents/999/"}, token="s3cr3t")
        assert status == 202
        assert fake.patches == []  # doc 999 doesn't exist -> nothing happened
    finally:
        srv.stop()


# ===========================================================================
# Restart-safety: pending work resumes without reprocessing done docs
# ===========================================================================
def test_restart_resumes_pending_without_reprocessing_done(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path)

    # Pre-seed the persisted queue as if a previous run crashed mid-queue:
    #   doc 1 already fully processed (done) and doc 2 still pending.
    qpath = str(s.data_path("webhook-queue.json"))
    q = NudgeQueue(qpath)
    q.enqueue(1)
    q.mark_done(1)      # doc 1 completed before the crash
    q.enqueue(2)        # doc 2 left pending

    # Also mark doc 1 as already triaged+metadata_done in Paperless so, IF it were
    # reprocessed, it would be a no-op we could detect by absence of a doc-1 PATCH.
    fake.documents[1]["custom_fields"] = [
        {"field": 2, "value": "opt-m"},  # ai_stage=metadata_done
        {"field": 1, "value": 0.1},
    ]

    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    # Starting the server resumes pending work (doc 2), and must NOT touch doc 1.
    srv.start()
    try:
        patched_ids = {doc_id for doc_id, _ in fake.patches}
        # doc 2 (pending) was processed on resume; doc 1 (done) was NOT reprocessed.
        assert 2 in patched_ids
        assert 1 not in patched_ids
        # queue drained: doc 2 now done, nothing pending.
        assert NudgeQueue(qpath).pending_ids() == []
        assert NudgeQueue(qpath).is_done(2) is True
    finally:
        srv.stop()


# ===========================================================================
# Per-period spend cap applies to nudge-triggered work
# ===========================================================================
def test_nudge_respects_period_spend_cap(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    # Cap already exhausted in the persisted ledger -> no billable metadata work.
    s = _settings(tmp_path, spend=SpendCaps(per_run=1.0, per_period=0.0001,
                                            period="monthly"))
    # Pre-fill the ledger above the cap.
    from paperless_assistant.obs import SpendLedger
    SpendLedger(str(s.data_path("spend-ledger.json")), period="monthly").add(1.0)

    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    srv.start()
    try:
        host, port = srv.address
        url = f"http://{host}:{port}{s.webhook.path}"
        status, _ = _post(url, {"doc_url": "http://pl/documents/1/"}, token="s3cr3t")
        assert status == 202
        # Triage (free/local) may still PATCH, but NO metadata write (spend-capped):
        # a metadata PATCH would carry a 'title'. Assert none did.
        assert not any("title" in b for _, b in fake.patches)
    finally:
        srv.stop()


# ===========================================================================
# Refusing to start unauthenticated
# ===========================================================================
def test_server_refuses_to_start_without_secret(tmp_path):
    s = _settings(tmp_path, webhook=WebhookSettings(
        enabled=True, host="127.0.0.1", port=0, secret=""))
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    sweep = Sweep(s, client=fake.client())
    srv = WebhookServer(s, sweep)
    with pytest.raises(RuntimeError, match="PA_WEBHOOK_SECRET"):
        srv.start()
