"""Phase 8 local web dashboard tests (prompt 009).

Fully OFFLINE, in-process: the stdlib request handler is driven directly (a fake
socket feeds a request and captures the response) — no real port bound, no live
Paperless, no real keys, no browser. Proves:

  * every route requires the token (401 without it); the served HTML page is public
    but carries no secret;
  * status / stats / runs / logs return the right shapes from /data fixtures + a
    fake Paperless; Paperless-unreachable is handled gracefully (error field);
  * config GET strips secrets + flags env-locked fields; config POST refuses
    secret-looking keys + delete_originals, validates, and writes /data/config.yml;
  * "run now" starts a background run over the REAL Sweep engine and is
    single-flight (a concurrent POST -> 409);
  * the served HTML is self-contained (no external src=/href=http/CDN);
  * enabling the UI with NO token refuses to start (fail closed).
"""
from __future__ import annotations

import io
import json
import re
import threading
import time

import pytest

from paperless_assistant import webui, webui_data
from paperless_assistant.config import Settings, TaskProvider, SpendCaps, UiSettings
from fakes import (
    FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)


TOKEN = "ui-secret-token"


# ---------------------------------------------------------------------------
# In-process handler driver (no socket bound).
# ---------------------------------------------------------------------------
class _FakeConn:
    """Minimal socket stand-in: serves a single request buffer, captures output."""

    def __init__(self, raw: bytes):
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()

    def makefile(self, mode, *a, **k):
        return self.rfile if "r" in mode else self.wfile


class Resp:
    def __init__(self, status, headers, body_bytes):
        self.status = status
        self.headers = headers
        self._body = body_bytes

    @property
    def text(self):
        return self._body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self._body.decode("utf-8"))


def _drive(handler_cls, method, path, *, token=None, body=None, extra_headers=None):
    """Build a raw HTTP/1.1 request, run it through the handler, parse the response."""
    payload = b""
    headers = {"Host": "test"}
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if extra_headers:
        headers.update(extra_headers)
    lines = [f"{method} {path} HTTP/1.1"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
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


class _DummyServer:
    def __init__(self):
        self.server_name = "test"
        self.server_port = 0


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _settings(tmp_path, **over):
    s = Settings(
        base_url="http://paperless.test:8000", paperless_token="tok",
        anthropic_api_key="sk-ant-test",
        data_dir=str(tmp_path / "data"),
        metadata_task=TaskProvider(provider="anthropic", model="claude-sonnet-4-6"),
        spend=SpendCaps(per_run=1.0, per_period=5.0, period="monthly"),
        ui=UiSettings(enabled=True, host="127.0.0.1", port=0, token=TOKEN),
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _handler(settings, *, run_manager=None, config_file=None, environ=None):
    rm = run_manager or webui.RunManager(settings)
    rm.set_config_file(config_file)
    return webui.make_handler(
        settings, token=settings.ui.token, run_manager=rm,
        config_file=config_file, environ=environ,
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


# ===========================================================================
# Auth (r2)
# ===========================================================================
def test_every_api_route_requires_token(tmp_path):
    h = _handler(_settings(tmp_path))
    for method, path in [("GET", "/api/status"), ("GET", "/api/stats"),
                         ("GET", "/api/runs"), ("GET", "/api/logs"),
                         ("GET", "/api/config"), ("GET", "/api/progress"),
                         ("POST", "/api/run"),
                         ("POST", "/api/config")]:
        r = _drive(h, method, path, token=None, body=({} if method == "POST" else None))
        assert r.status == 401, f"{method} {path} should be 401 without a token"


def test_wrong_token_is_401(tmp_path):
    h = _handler(_settings(tmp_path))
    r = _drive(h, "GET", "/api/status", token="wrong")
    assert r.status == 401


def test_valid_token_authenticates(tmp_path):
    h = _handler(_settings(tmp_path))
    r = _drive(h, "GET", "/api/status", token=TOKEN)
    assert r.status == 200


def test_token_via_x_header(tmp_path):
    h = _handler(_settings(tmp_path))
    r = _drive(h, "GET", "/api/status", extra_headers={"X-PA-UI-Token": TOKEN})
    assert r.status == 200


def test_root_html_is_public_but_has_no_secret(tmp_path):
    h = _handler(_settings(tmp_path))
    r = _drive(h, "GET", "/", token=None)
    assert r.status == 200
    assert "text/html" in r.headers["content-type"]
    assert TOKEN not in r.text  # never embed the token in the page


# ===========================================================================
# Read endpoints (r3)
# ===========================================================================
def test_status_shape(tmp_path):
    h = _handler(_settings(tmp_path))
    body = _drive(h, "GET", "/api/status", token=TOKEN).json()
    assert "spend" in body and "stages_enabled" in body
    assert "run_in_progress" in body
    assert "period_pct" in body["spend"]


def test_stats_shape_from_fake_paperless(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    monkeypatch.setattr(webui_data, "PaperlessClient", lambda *a, **k: fake.client())
    h = _handler(s)
    body = _drive(h, "GET", "/api/stats", token=TOKEN).json()
    assert body.get("error") is None
    assert body["total_documents"] == 2
    assert set(body["by_stage"]) == {"triaged", "reocr_done", "metadata_done", "none"}
    assert "review_queues" in body


def test_stats_handles_paperless_unreachable(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    class _Boom:
        def __init__(self, *a, **k):
            raise ConnectionError("paperless down")

    monkeypatch.setattr(webui_data, "PaperlessClient", _Boom)
    h = _handler(s)
    body = _drive(h, "GET", "/api/stats", token=TOKEN).json()
    assert "error" in body and body["error"]  # graceful, no crash


def test_runs_list_and_detail(tmp_path):
    s = _settings(tmp_path)
    reports = s.data_path("run-reports")
    reports.mkdir(parents=True)
    rep = {
        "kind": "sweep", "run_id": "abc123def456",
        "started_at": "2026-07-01T10:00:00+00:00",
        "finished_at": "2026-07-01T10:01:00+00:00",
        "dry_run": True, "counts": {"dry": 5}, "spend_total": 0.0,
        "new_taxonomy": ["Acme"], "superseded": [7],
        "stages": [{"stage": "triage", "counts": {"dry": 5}, "spend_total": 0.0}],
    }
    (reports / "sweep-x.json").write_text(json.dumps(rep), encoding="utf-8")
    h = _handler(s)
    lst = _drive(h, "GET", "/api/runs", token=TOKEN).json()
    assert lst["count"] == 1 and lst["runs"][0]["run_id"] == "abc123def456"
    detail = _drive(h, "GET", "/api/run?id=abc123def456", token=TOKEN).json()
    assert detail["stages"][0]["stage"] == "triage"
    missing = _drive(h, "GET", "/api/run?id=nope", token=TOKEN)
    assert missing.status == 404


def test_logs_tail_and_errors_filter(tmp_path):
    s = _settings(tmp_path)
    logp = s.data_path("logs", "pa.jsonl")
    logp.parent.mkdir(parents=True)
    lines = [
        {"ts": "t1", "level": "info", "event": "sweep_start"},
        {"ts": "t2", "level": "error", "event": "failure", "error": "boom"},
        {"ts": "t3", "level": "info", "event": "doc_outcome", "status": "wrote"},
        {"ts": "t4", "level": "error", "event": "stage_aborted"},
    ]
    logp.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    h = _handler(s)
    allev = _drive(h, "GET", "/api/logs", token=TOKEN).json()
    assert allev["count"] == 4
    errs = _drive(h, "GET", "/api/logs?errors=1", token=TOKEN).json()
    assert errs["count"] == 2
    assert all(e["event"] in ("failure", "stage_aborted") for e in errs["events"])


# ===========================================================================
# Config endpoints (r5)
# ===========================================================================
def test_config_get_strips_secrets_and_flags_env_locked(tmp_path, monkeypatch):
    monkeypatch.setenv("PA_WORKERS", "7")  # env-locks workers
    s = _settings(tmp_path)
    h = _handler(s, environ={"PA_WORKERS": "7"})
    body = _drive(h, "GET", "/api/config", token=TOKEN).json()
    # No secret value anywhere.
    dump = json.dumps(body)
    assert "sk-ant-test" not in dump and TOKEN not in dump and "tok" not in dump.replace("token", "")
    # Secrets reported as presence only.
    assert body["secrets"]["anthropic_api_key"] is True
    assert body["secrets"]["ui_token"] is True
    # env-locked flag present + set for workers.
    assert body["env_locked"]["workers"] is True
    assert body["env_locked"]["mode"] is False


def test_config_post_writes_yaml(tmp_path):
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    r = _drive(h, "POST", "/api/config", token=TOKEN,
               body={"workers": 5, "triage_threshold": 0.7,
                     "spend": {"per_run": 2.0}})
    assert r.status == 200
    import yaml
    written = yaml.safe_load(open(cfg, encoding="utf-8"))
    assert written["workers"] == 5
    assert written["triage_threshold"] == 0.7
    assert written["spend"]["per_run"] == 2.0


def test_limit_is_configurable_via_dashboard(tmp_path):
    """The per-run document limit is surfaced in the config payload, editable via
    /api/config, and offered as a field in the served settings page."""
    s = _settings(tmp_path, limit=0)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    body = _drive(h, "GET", "/api/config", token=TOKEN).json()
    assert body["values"]["limit"] == 0  # exposed for the General form
    r = _drive(h, "POST", "/api/config", token=TOKEN, body={"limit": 25})
    assert r.status == 200
    import yaml
    assert yaml.safe_load(open(cfg, encoding="utf-8"))["limit"] == 25
    # The General settings form offers the limit field.
    assert 'field("limit"' in webui.PAGE_HTML


def test_progress_endpoint_reports_live_snapshot(tmp_path):
    """GET /api/progress returns the live tracker snapshot (stage progress +
    per-document outcomes) so the Overview panel can auto-refresh."""
    from paperless_assistant import progress as progress_mod

    tr = progress_mod.tracker()
    tr.begin_run("run-xyz", dry_run=True, source="manual")
    tr.begin_stage("triage", 2)
    tr.record_doc("triage", "wrote", 1, title="clean", summary="score=0.1",
                  cost=0.0, url="/documents/1/details")
    h = _handler(_settings(tmp_path))
    body = _drive(h, "GET", "/api/progress", token=TOKEN).json()
    assert body["active"] is True and body["run_id"] == "run-xyz"
    assert body["dry_run"] is True and body["source"] == "manual"
    tri = [s for s in body["stages"] if s["stage"] == "triage"][0]
    assert tri["total"] == 2 and tri["processed"] == 1
    assert body["recent"][0]["doc_id"] == 1
    assert body["recent"][0]["summary"] == "score=0.1"
    tr.end_run(counts={"wrote": 1}, spend_total=0.0)
    body2 = _drive(h, "GET", "/api/progress", token=TOKEN).json()
    assert body2["active"] is False and body2["finished_at"] is not None


def test_progress_endpoint_surfaces_fatal_provider_error(tmp_path):
    """A fatal provider error (out of credits) is carried in the progress payload
    and the page renders it as a banner, so the user is informed."""
    from paperless_assistant import progress as progress_mod

    tr = progress_mod.tracker()
    tr.begin_run("run-oops", dry_run=False, source="scheduled")
    tr.set_error("billing", "Your credit balance is too low.", stage="metadata",
                 help="Add credits and re-run.")
    tr.end_run()
    h = _handler(_settings(tmp_path))
    body = _drive(h, "GET", "/api/progress", token=TOKEN).json()
    assert body["error"]["kind"] == "billing"
    assert body["error"]["stage"] == "metadata"
    assert "credit balance" in body["error"]["message"].lower()
    # The page renders the fatal-error banner from p.error.
    assert "p.error" in webui.PAGE_HTML and "Run stopped" in webui.PAGE_HTML


def test_config_post_refuses_secret_keys(tmp_path):
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    for secret in ("paperless_token", "anthropic_api_key", "api_key", "secret"):
        r = _drive(h, "POST", "/api/config", token=TOKEN, body={secret: "x"})
        assert r.status == 400, f"{secret} must be refused"
        assert "secret" in r.json()["error"].lower()
    # Nothing was written.
    import os
    assert not os.path.exists(cfg)


def test_config_post_refuses_delete_originals(tmp_path):
    s = _settings(tmp_path)
    cfg = str(s.data_path("config.yml"))
    h = _handler(s, config_file=cfg)
    r = _drive(h, "POST", "/api/config", token=TOKEN, body={"delete_originals": True})
    assert r.status == 400
    assert "delete_originals" in r.json()["error"]


def test_config_post_reports_validation_errors(tmp_path):
    s = _settings(tmp_path)
    h = _handler(s, config_file=str(s.data_path("config.yml")))
    r = _drive(h, "POST", "/api/config", token=TOKEN, body={"nonsense_key": 1})
    assert r.status == 400
    assert "unknown" in r.json()["error"].lower()


# ===========================================================================
# Run now (r4) — real Sweep engine, single-flight.
# ===========================================================================
def _wire_run(tmp_path, monkeypatch):
    """Settings + fake Paperless + stubbed metadata so a real Sweep can run offline."""
    s = _settings(tmp_path)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())

    from paperless_assistant import sweep as sweep_mod
    monkeypatch.setattr(sweep_mod, "PaperlessClient", lambda *a, **k: fake.client())
    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock({
        "title": "Payment", "correspondent": "Acme", "document_type": "Letter",
        "tags": ["billing"], "correspondent_is_new": True,
        "document_type_is_new": True, "new_tags": ["billing"]})]))
    return s, fake


def test_run_now_starts_background_run_over_real_sweep(tmp_path, monkeypatch):
    s, fake = _wire_run(tmp_path, monkeypatch)
    rm = webui.RunManager(s)
    h = _handler(s, run_manager=rm)
    r = _drive(h, "POST", "/api/run", token=TOKEN, body={"write": True, "limit": 5})
    assert r.status == 202
    run_id = r.json()["run_id"]
    assert run_id
    # Wait for the background run to finish.
    for _ in range(100):
        st = rm.state()
        if not st["in_progress"] and st.get("finished_at"):
            break
        time.sleep(0.05)
    st = rm.state()
    assert st["in_progress"] is False
    assert st.get("error") is None, st.get("error")
    assert st["result"] is not None
    # It really ran the engine: a persisted run report exists under /data.
    assert list(s.data_path("run-reports").glob("*.json"))


def test_run_now_defaults_to_dry_run(tmp_path, monkeypatch):
    s, fake = _wire_run(tmp_path, monkeypatch)
    rm = webui.RunManager(s)
    h = _handler(s, run_manager=rm)
    _drive(h, "POST", "/api/run", token=TOKEN, body={})  # no write flag
    for _ in range(100):
        if not rm.state()["in_progress"] and rm.state().get("finished_at"):
            break
        time.sleep(0.05)
    assert rm.state()["result"]["dry_run"] is True
    # dry-run never PATCHed a document.
    assert fake.patches == []


def test_run_now_is_single_flight(tmp_path):
    """A concurrent POST while a run is in progress -> 409. Use a blocking fake
    Sweep so the first run is deterministically 'in progress'."""
    s = _settings(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class _BlockingSweep:
        def __init__(self, settings):
            pass

        def run_once(self, *, limit=None, source=None):
            started.set()
            release.wait(timeout=5)

            class _M:
                run_id = "blk"
                dry_run = True
                def merged_counts(self):
                    return {}
                def total_spend(self):
                    return 0.0
                def all_new_taxonomy(self):
                    return []
                def all_superseded(self):
                    return []
            return _M()

    rm = webui.RunManager(s, sweep_factory=lambda st: _BlockingSweep(st))
    h = _handler(s, run_manager=rm)
    first = _drive(h, "POST", "/api/run", token=TOKEN, body={})
    assert first.status == 202
    assert started.wait(timeout=5)
    # Second POST while the first is still running -> 409.
    second = _drive(h, "POST", "/api/run", token=TOKEN, body={})
    assert second.status == 409
    release.set()
    for _ in range(100):
        if not rm.state()["in_progress"]:
            break
        time.sleep(0.05)


# ===========================================================================
# Self-contained HTML (constraint) + fail-closed (r1)
# ===========================================================================
def test_html_page_is_self_contained():
    html = webui.PAGE_HTML
    assert html.startswith("<!DOCTYPE html>")
    # No external assets: no <script src>, no <link href>, no CDN/font URL.
    assert not re.search(r'(?:src|href)\s*=\s*["\'][^"\']+["\']', html)
    assert "http://" not in html and "https://" not in html
    assert "cdn" not in html.lower()


# ===========================================================================
# Prompt 012: tabbed UI markup + Setup / Doctor / Pause / Onboarding endpoints.
# ===========================================================================
def test_html_has_four_tabs_and_settings_subsections():
    html = webui.PAGE_HTML
    for panel in ("panel-overview", "panel-runs", "panel-setup", "panel-settings"):
        assert panel in html, panel
    for sub in ("sub-general", "sub-models", "sub-prompts", "sub-advanced"):
        assert sub in html, sub
    # PAUSED banner + the new action controls are present.
    for el in ("paused-banner", "setup-btn", "doctor-btn", "pause-btn",
               "resume-btn", "onboarding-steps"):
        assert el in html, el
    # role=tablist wiring for accessibility + deep-link controller present.
    assert 'role="tablist"' in html and "activateTab" in html


def test_new_endpoints_require_token(tmp_path):
    h = _handler(_settings(tmp_path))
    for method, path in [("POST", "/api/setup"), ("GET", "/api/doctor"),
                         ("POST", "/api/pause"), ("POST", "/api/resume"),
                         ("GET", "/api/onboarding")]:
        r = _drive(h, method, path, token=None,
                   body=({} if method == "POST" else None))
        assert r.status == 401, f"{method} {path} must be 401 without a token"


def _wire_paperless(tmp_path, monkeypatch, **fake_kw):
    """Point webui's Paperless client builder at a fresh FakePaperless."""
    s = _settings(tmp_path)
    fake = FakePaperless(**fake_kw)
    monkeypatch.setattr(webui, "_paperless_client", lambda cfg, settings: fake.client())
    return s, fake


def test_setup_runs_provisioner_and_is_idempotent(tmp_path, monkeypatch):
    # Empty Paperless: first setup CREATES fields + tags; second is a no-op.
    s, fake = _wire_paperless(tmp_path, monkeypatch, fields=[], tags=[])
    h = _handler(s)
    r1 = _drive(h, "POST", "/api/setup", token=TOKEN, body={}).json()
    rep1 = r1["report"]
    assert rep1["ok"] is True
    assert set(rep1["created_fields"]) == {"ocr_quality", "ai_stage", "ai_notes"}
    assert set(rep1["created_tags"]) == {"superseded", "ai-new-taxonomy"}
    assert rep1["noop"] is False
    # Second call: everything already exists -> verified no-op, no dup creation.
    r2 = _drive(h, "POST", "/api/setup", token=TOKEN, body={}).json()
    rep2 = r2["report"]
    assert rep2["ok"] is True and rep2["noop"] is True
    assert rep2["created_fields"] == [] and rep2["created_tags"] == []
    assert set(rep2["existing_fields"]) == {"ocr_quality", "ai_stage", "ai_notes"}


def test_setup_reports_incompatible_field(tmp_path, monkeypatch):
    # An existing 'ai_notes' with the wrong data_type is REPORTED, never clobbered.
    bad_fields = make_custom_fields(notes_type="integer")
    s, fake = _wire_paperless(tmp_path, monkeypatch, fields=bad_fields,
                              tags=healthy_tags())
    h = _handler(s)
    rep = _drive(h, "POST", "/api/setup", token=TOKEN, body={}).json()["report"]
    assert rep["ok"] is False
    assert any("ai_notes" in m for m in rep["incompatible"])


def test_setup_response_has_no_secret(tmp_path, monkeypatch):
    s, fake = _wire_paperless(tmp_path, monkeypatch, fields=[], tags=[])
    h = _handler(s)
    txt = _drive(h, "POST", "/api/setup", token=TOKEN, body={}).text
    assert "sk-ant-test" not in txt and TOKEN not in txt


def test_doctor_returns_checks_ok_warn_fail(tmp_path, monkeypatch):
    # Healthy fake -> checks include OK connectivity + present fields/tags.
    s, fake = _wire_paperless(tmp_path, monkeypatch,
                              fields=make_custom_fields(), tags=healthy_tags())
    h = _handler(s)
    body = _drive(h, "GET", "/api/doctor", token=TOKEN).json()
    assert "checks" in body and body["checks"]
    statuses = {c["status"] for c in body["checks"]}
    assert statuses <= {"ok", "warn", "fail"}
    names = {c["name"] for c in body["checks"]}
    assert "connectivity" in names
    # A missing field on an empty Paperless yields FAIL checks with a fix.
    s2, _ = _wire_paperless(tmp_path, monkeypatch, fields=[], tags=[])
    h2 = _handler(s2)
    body2 = _drive(h2, "GET", "/api/doctor", token=TOKEN).json()
    assert body2["healthy"] is False
    assert any(c["status"] == "fail" and c["fix"] for c in body2["checks"])


def test_pause_resume_persist_and_surface_in_status(tmp_path):
    from paperless_assistant.obs import PauseFlag

    s = _settings(tmp_path)
    h = _handler(s)
    # Initially not paused.
    st = _drive(h, "GET", "/api/status", token=TOKEN).json()
    assert st["paused"] is False
    # Pause -> persisted flag on disk + reflected in status.
    r = _drive(h, "POST", "/api/pause", token=TOKEN, body={}).json()
    assert r["paused"] is True
    assert PauseFlag(str(s.data_path("paused.json"))).is_paused() is True
    st = _drive(h, "GET", "/api/status", token=TOKEN).json()
    assert st["paused"] is True
    # A NEW handler (simulating a restart) still sees paused (survives restart).
    h2 = _handler(_settings(tmp_path))
    st2 = _drive(h2, "GET", "/api/status", token=TOKEN).json()
    assert st2["paused"] is True
    # Resume clears it.
    r = _drive(h, "POST", "/api/resume", token=TOKEN, body={}).json()
    assert r["paused"] is False
    st = _drive(h, "GET", "/api/status", token=TOKEN).json()
    assert st["paused"] is False


def test_onboarding_returns_snippet_and_live_checklist(tmp_path, monkeypatch):
    s, fake = _wire_paperless(tmp_path, monkeypatch,
                              fields=make_custom_fields(), tags=healthy_tags())
    h = _handler(s)
    body = _drive(h, "GET", "/api/onboarding", token=TOKEN).json()
    # Compose snippet reused from initcmd.
    assert "paperless-assistant:" in body["compose"]
    keys = {st["key"] for st in body["steps"]}
    assert keys == {"setup", "doctor", "first_run", "writes"}
    steps = {st["key"]: st for st in body["steps"]}
    # Healthy Paperless with all fields/tags -> setup step done, doctor healthy.
    assert steps["setup"]["done"] is True
    assert steps["doctor"]["done"] is True
    # No sweep yet -> first-run not done, writes not enabled (first-run dry-run).
    assert steps["first_run"]["done"] is False
    assert body["writes_enabled"] is False


def test_onboarding_reflects_missing_setup(tmp_path, monkeypatch):
    s, fake = _wire_paperless(tmp_path, monkeypatch, fields=[], tags=[])
    h = _handler(s)
    body = _drive(h, "GET", "/api/onboarding", token=TOKEN).json()
    steps = {st["key"]: st for st in body["steps"]}
    assert steps["setup"]["done"] is False
    assert steps["doctor"]["done"] is False


def test_onboarding_graceful_when_paperless_unreachable(tmp_path, monkeypatch):
    s = _settings(tmp_path)

    def _boom(cfg, settings):
        raise ConnectionError("paperless down")

    monkeypatch.setattr(webui, "_paperless_client", _boom)
    h = _handler(s)
    r = _drive(h, "GET", "/api/onboarding", token=TOKEN)
    assert r.status == 200  # still renders the wizard
    body = r.json()
    steps = {st["key"]: st for st in body["steps"]}
    # Paperless-dependent steps report unknown (None), never crash.
    assert steps["setup"]["done"] is None


# ===========================================================================
# Prompt 013: Activity tab + /api/activity + /api/activity/purge.
# ===========================================================================
def _seed_activity(settings, rows):
    from paperless_assistant.activity import ActivityStore

    st = ActivityStore(str(settings.data_path("activity.db")))
    for r in rows:
        st.record(r)
    st.close()


def test_activity_endpoints_require_token(tmp_path):
    h = _handler(_settings(tmp_path))
    r1 = _drive(h, "GET", "/api/activity", token=None)
    assert r1.status == 401
    r2 = _drive(h, "POST", "/api/activity/purge", token=None, body={})
    assert r2.status == 401


def test_activity_html_tab_present_and_self_contained():
    html = webui.PAGE_HTML
    assert "panel-activity" in html and 'data-tab="activity"' in html
    assert "activity-table" in html and "act-purge-btn" in html
    # still self-contained (no external assets, no literal http(s) URLs).
    assert not re.search(r'(?:src|href)\s*=\s*["\'][^"\']+["\']', html)
    assert "http://" not in html and "https://" not in html


def test_activity_query_filters_and_pagination(tmp_path):
    import time as _t
    s = _settings(tmp_path)
    base = _t.time()
    _seed_activity(s, [
        {"run_id": "r", "doc_id": 1, "doc_title": "Invoice", "stage": "triage",
         "dry_run": False, "status": "wrote",
         "changes": {"fields": {"ai_stage": {"before": None, "after": "triaged"}}},
         "summary": "", "paperless_url": "http://p/documents/1/details", "ts": base},
        {"run_id": "r", "doc_id": 2, "doc_title": "Statement", "stage": "metadata",
         "dry_run": True, "status": "dry",
         "changes": {"fields": {"title": {"before": "x", "after": "Bank Statement"}}},
         "summary": "", "paperless_url": "", "ts": base + 1},
        {"run_id": "r", "doc_id": 2, "doc_title": "Statement", "stage": "metadata",
         "dry_run": False, "status": "ERROR",
         "changes": {"error": "boom"}, "summary": "", "paperless_url": "", "ts": base + 2},
    ])
    h = _handler(s)
    allr = _drive(h, "GET", "/api/activity", token=TOKEN).json()
    assert allr["total"] == 3 and allr["enabled"] is True
    # filter by doc + stage + dry-run + status + search
    assert _drive(h, "GET", "/api/activity?doc_id=2", token=TOKEN).json()["total"] == 2
    assert _drive(h, "GET", "/api/activity?stage=triage", token=TOKEN).json()["total"] == 1
    assert _drive(h, "GET", "/api/activity?dry_run=1", token=TOKEN).json()["total"] == 1
    assert _drive(h, "GET", "/api/activity?status=ERROR", token=TOKEN).json()["total"] == 1
    assert _drive(h, "GET", "/api/activity?q=Bank", token=TOKEN).json()["total"] == 1
    # pagination: limit 1 returns 1 row but full total.
    page = _drive(h, "GET", "/api/activity?limit=1&offset=0", token=TOKEN).json()
    assert len(page["rows"]) == 1 and page["total"] == 3


def test_activity_response_has_no_secret(tmp_path):
    s = _settings(tmp_path)
    _seed_activity(s, [
        {"run_id": "r", "doc_id": 1, "doc_title": "Invoice", "stage": "triage",
         "dry_run": False, "status": "wrote", "changes": {"fields": {}},
         "summary": "", "paperless_url": "http://p/documents/1/details"},
    ])
    h = _handler(s)
    txt = _drive(h, "GET", "/api/activity", token=TOKEN).text
    assert "sk-ant-test" not in txt and TOKEN not in txt and "tok" not in txt.replace("token", "")


def test_activity_manual_purge(tmp_path):
    import time as _t
    s = _settings(tmp_path, activity_retention_days=90)
    now = _t.time()
    _seed_activity(s, [
        {"run_id": "old", "doc_id": 1, "doc_title": "old", "stage": "metadata",
         "dry_run": False, "status": "done", "changes": {"fields": {}},
         "summary": "", "paperless_url": "", "ts": now - 200 * 86400},
        {"run_id": "new", "doc_id": 2, "doc_title": "new", "stage": "metadata",
         "dry_run": False, "status": "done", "changes": {"fields": {}},
         "summary": "", "paperless_url": "", "ts": now - 1 * 86400},
    ])
    h = _handler(s)
    r = _drive(h, "POST", "/api/activity/purge", token=TOKEN, body={}).json()
    assert r["purged"] == 1
    # only the recent row remains
    assert _drive(h, "GET", "/api/activity", token=TOKEN).json()["total"] == 1


def test_activity_purge_keep_forever_is_noop(tmp_path):
    s = _settings(tmp_path, activity_retention_days=0)
    _seed_activity(s, [
        {"run_id": "r", "doc_id": 1, "doc_title": "x", "stage": "metadata",
         "dry_run": False, "status": "done", "changes": {"fields": {}},
         "summary": "", "paperless_url": ""},
    ])
    h = _handler(s)
    r = _drive(h, "POST", "/api/activity/purge", token=TOKEN, body={}).json()
    assert r["purged"] == 0                       # 0 = keep forever
    assert _drive(h, "GET", "/api/activity", token=TOKEN).json()["total"] == 1


def test_ui_enabled_without_token_refuses_to_start(tmp_path):
    s = _settings(tmp_path)
    s.ui.token = ""  # enabled but no token
    server = webui.WebUIServer(s)
    with pytest.raises(RuntimeError) as ei:
        server._make_httpd()
    assert "PA_UI_TOKEN" in str(ei.value)


def test_serve_forever_also_fails_closed_without_token(tmp_path):
    s = _settings(tmp_path)
    s.ui.token = ""
    server = webui.WebUIServer(s)
    with pytest.raises(RuntimeError):
        server.serve_forever()


def test_cli_serve_ui_thread_fails_closed_without_token(tmp_path):
    """`pa serve` with PA_UI_ENABLED but no token exits with a clear error."""
    from paperless_assistant import cli
    s = _settings(tmp_path)
    s.ui.token = ""

    class _Args:
        config = None

    with pytest.raises(SystemExit):
        cli._start_ui_thread(s, _Args())
