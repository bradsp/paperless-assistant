"""Sweep tests (plan §6.2, §7.2, §8.4): one `pa run` tick triages + proposes
metadata, does NOT run re-OCR by default, writes a persisted run report to /data,
is idempotent on re-run (I1), defaults the first run to a bounded dry-run (I7),
and emits the asserted JSON log shape.
"""
import io
import json

from paperless_assistant.config import Settings, TaskProvider, SpendCaps
from paperless_assistant.sweep import Sweep
from paperless_assistant.obs import JsonLogger
from fakes import (
    FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)


def _docs():
    # doc 1: clean, untriaged -> triage(clean) then metadata-eligible.
    # doc 2: garbage, untriaged -> triage flags it; metadata skips it (garbage).
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


def test_serve_survives_failing_tick_and_retries(tmp_path, monkeypatch):
    """A failing sweep (e.g. Paperless unreachable) must NOT propagate out of
    serve() — that would exit the process and crash-loop the container under
    `restart: unless-stopped`. Instead it logs the real error and retries on a
    short delay."""
    import requests
    from paperless_assistant import sweep as sweep_mod

    s = _settings(tmp_path, schedule_interval_seconds=3600)

    def boom(self):
        raise requests.exceptions.ConnectionError("cannot reach webserver:8000")

    monkeypatch.setattr(sweep_mod.Sweep, "run_once", boom)

    slept = []
    logbuf = io.StringIO()
    logger = JsonLogger(stream=logbuf, path=None)

    # Bounded to 2 ticks; must return normally (NOT raise).
    reports = sweep_mod.serve(s, iterations=2, sleep_fn=slept.append, logger=logger)

    assert reports == []  # no successful sweeps recorded
    events = [json.loads(ln)["event"]
              for ln in logbuf.getvalue().splitlines() if ln.strip()]
    assert events.count("sweep_error") == 2      # each failure logged, no crash
    assert slept == [30]                          # retried on the SHORT delay, not 3600s


def test_serve_skips_sweep_when_paused_but_stays_alive(tmp_path, monkeypatch):
    """Prompt 012: with the pause flag set, a `serve` tick does NOT call run_once
    and the loop stays alive (logs a paused event). Clearing it restores sweeping."""
    from paperless_assistant import sweep as sweep_mod
    from paperless_assistant.obs import PauseFlag

    s = _settings(tmp_path, schedule_interval_seconds=3600)
    ran = {"count": 0}

    def _fake_run_once(self, *, limit=None):
        ran["count"] += 1

        class _M:
            pass
        return _M()

    monkeypatch.setattr(sweep_mod.Sweep, "run_once", _fake_run_once)

    # Pause first: the flag lives under /data (survives restart by construction).
    PauseFlag(str(s.data_path("paused.json"))).set_paused(True)

    slept = []
    buf = io.StringIO()
    logger = JsonLogger(stream=buf, path=None)
    reports = sweep_mod.serve(s, iterations=2, sleep_fn=slept.append, logger=logger)

    # Paused: NO sweep ran, but the loop completed both ticks (stayed alive).
    assert ran["count"] == 0
    assert reports == []
    events = [json.loads(ln)["event"] for ln in buf.getvalue().splitlines() if ln.strip()]
    assert events.count("sweep_paused") == 2
    # Slept at the normal interval between ticks (last tick breaks before sleeping),
    # never crashed and never the short error-retry.
    assert slept == [3600]

    # Resume: the very next serve tick runs the sweep again.
    PauseFlag(str(s.data_path("paused.json"))).set_paused(False)
    sweep_mod.serve(s, iterations=1, sleep_fn=lambda *_: None, logger=logger)
    assert ran["count"] == 1


def test_process_nudge_honors_pause(tmp_path):
    """The webhook nudge path is AUTOMATIC processing, so it must skip while paused
    (staying alive, never writing)."""
    from paperless_assistant.sweep import Sweep
    from paperless_assistant.obs import PauseFlag

    s = _settings(tmp_path)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    buf = io.StringIO()
    sweep = Sweep(s, client=fake.client(), logger=JsonLogger(stream=buf, path=None))
    PauseFlag(str(s.data_path("paused.json"))).set_paused(True)

    multi = sweep.process_nudge(1)
    # Skipped: no document was PATCHed (nothing processed).
    assert fake.patches == []
    events = [json.loads(ln)["event"] for ln in buf.getvalue().splitlines() if ln.strip()]
    assert "nudge_paused" in events
    assert "nudge_start" not in events


def test_drain_fails_fast_on_provider_auth_error(tmp_path):
    """A provider AUTH error (bad/blank API key) rejects every doc, so the stage
    must fail fast — NOT make a doomed (zero-cost, uncapped) call per document."""
    from paperless_assistant.report import RunReport
    from paperless_assistant.sweep import Sweep

    s = _settings(tmp_path, workers=1)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    buf = io.StringIO()
    sweep = Sweep(s, client=fake.client(), logger=JsonLogger(stream=buf, path=None))

    calls = []

    class AuthErr(Exception):
        status_code = 401

    def process(doc):
        calls.append(doc["id"])
        raise AuthErr("Error code: 401 - invalid x-api-key")

    report = RunReport("metadata")
    queue = [{"id": i} for i in range(1, 101)]
    sweep._drain(process, queue, report, stage="metadata")

    assert len(calls) < len(queue)              # did NOT call every doc (fail fast)
    assert report.counts().get("skip", 0) > 0   # remaining docs short-circuited
    events = [json.loads(ln)["event"]
              for ln in buf.getvalue().splitlines() if ln.strip()]
    assert "stage_aborted" in events


def test_provider_fatal_reason_classification():
    """Out-of-credits / over-quota errors classify as 'billing', bad keys as
    'auth', and ordinary / transient errors as None (do not stop the run)."""
    from paperless_assistant.sweep import _provider_fatal_reason

    class Err(Exception):
        def __init__(self, msg, *, status=None, code=None):
            super().__init__(msg)
            if status is not None:
                self.status_code = status
            if code is not None:
                self.code = code

    # Anthropic: out of credits (HTTP 400, distinctive message).
    assert _provider_fatal_reason(
        Err("Error code: 400 - Your credit balance is too low to access the "
            "Anthropic API.", status=400)) == "billing"
    # OpenAI: exhausted quota (HTTP 429 with code insufficient_quota).
    assert _provider_fatal_reason(
        Err("You exceeded your current quota", status=429,
            code="insufficient_quota")) == "billing"
    # Payment Required.
    assert _provider_fatal_reason(Err("nope", status=402)) == "billing"
    # Bad key.
    assert _provider_fatal_reason(Err("invalid x-api-key", status=401)) == "auth"
    # A PLAIN rate limit is transient, not fatal -> None (must NOT stop the run).
    assert _provider_fatal_reason(
        Err("Rate limit exceeded", status=429, code="rate_limit_exceeded")) is None
    # An ordinary per-doc error.
    assert _provider_fatal_reason(ValueError("bad pdf")) is None


def test_drain_fails_fast_on_billing_error(tmp_path):
    """An out-of-credits error rejects every doc, so the stage must fail fast —
    NOT keep processing the whole batch (the reported bug)."""
    from paperless_assistant.report import RunReport
    from paperless_assistant.sweep import Sweep

    s = _settings(tmp_path, workers=1)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    buf = io.StringIO()
    sweep = Sweep(s, client=fake.client(), logger=JsonLogger(stream=buf, path=None))

    calls = []

    class CreditErr(Exception):
        status_code = 400

    def process(doc):
        calls.append(doc["id"])
        raise CreditErr("Your credit balance is too low to access the Anthropic API.")

    report = RunReport("metadata")
    queue = [{"id": i} for i in range(1, 101)]
    sweep._drain(process, queue, report, stage="metadata")

    assert len(calls) < len(queue)              # did NOT call every doc (fail fast)
    assert report.counts().get("skip", 0) > 0   # remaining docs short-circuited
    # The run-level latch is set so subsequent billable stages are skipped.
    assert sweep._fatal_provider_error["reason"] == "billing"
    events = [json.loads(ln)["event"]
              for ln in buf.getvalue().splitlines() if ln.strip()]
    assert "stage_aborted" in events


def test_capability_error_classifies_as_config():
    """A provider that isn't usable in this deployment (missing SDK package, or a
    model lacking a needed capability) is a fatal 'config' error."""
    from paperless_assistant.sweep import _provider_fatal_reason
    from paperless_assistant.providers.base import CapabilityError

    assert _provider_fatal_reason(
        CapabilityError("The OpenAI provider requires the 'openai' package.")) == "config"
    assert _provider_fatal_reason(
        CapabilityError("model 'gpt-x' is not vision-capable")) == "config"


def test_drain_fails_fast_on_capability_error_without_latching_run(tmp_path):
    """A missing-package / capability error must ALSO fail fast (the reported bug:
    switching to OpenAI errored on every document). But 'config' is provider/task-
    specific, so it fails only its own stage and does NOT stop the whole run — a
    later stage on a different provider can still run."""
    from paperless_assistant.report import RunReport
    from paperless_assistant.sweep import Sweep
    from paperless_assistant.providers.base import CapabilityError

    s = _settings(tmp_path, workers=1)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    buf = io.StringIO()
    sweep = Sweep(s, client=fake.client(), logger=JsonLogger(stream=buf, path=None))

    calls = []

    def process(doc):
        calls.append(doc["id"])
        raise CapabilityError("The OpenAI provider requires the 'openai' package.")

    report = RunReport("metadata")
    queue = [{"id": i} for i in range(1, 101)]
    sweep._drain(process, queue, report, stage="metadata")

    assert len(calls) < len(queue)                 # failed fast within the stage
    assert report.counts().get("skip", 0) > 0
    # 'config' does NOT latch the run (unlike auth/billing).
    assert sweep._fatal_provider_error is None
    events = [json.loads(ln)["event"]
              for ln in buf.getvalue().splitlines() if ln.strip()]
    assert "stage_aborted" in events


def test_drain_does_not_fail_fast_on_ordinary_error(tmp_path):
    """An ordinary per-doc error must NOT abort the stage — every doc is attempted."""
    from paperless_assistant.report import RunReport
    from paperless_assistant.sweep import Sweep

    s = _settings(tmp_path, workers=1)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    sweep = Sweep(s, client=fake.client(), logger=JsonLogger(stream=io.StringIO(), path=None))

    calls = []

    def process(doc):
        calls.append(doc["id"])
        raise ValueError("a transient per-doc problem")

    report = RunReport("metadata")
    queue = [{"id": i} for i in range(1, 6)]
    sweep._drain(process, queue, report, stage="metadata")

    assert len(calls) == 5                       # every doc attempted (no abort)
    assert report.counts().get("ERROR", 0) == 5


def test_first_run_is_dry_run(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path)  # dry_run=None -> first-run auto dry-run (I7)
    sweep = Sweep(s, client=fake.client())
    assert sweep.resolve_dry_run() is True
    multi = sweep.run_once()
    assert multi.dry_run is True
    # I7: a dry first run performs NO writes to Paperless.
    assert fake.patches == []


def test_second_run_writes(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path)
    Sweep(s, client=fake.client()).run_once()          # first: dry-run, no writes
    assert fake.patches == []
    Sweep(s, client=fake.client()).run_once()          # second: writes
    assert fake.patches, "second run should write to Paperless"


def test_run_triages_and_proposes_metadata_no_reocr(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=True)  # explicit dry-run to inspect proposals
    sweep = Sweep(s, client=fake.client())
    multi = sweep.run_once()

    stages = {r.stage for r in multi.stage_reports}
    assert "triage" in stages and "metadata" in stages
    # Re-OCR is OFF by default -> no reocr stage ran.
    assert "reocr" not in stages
    assert s.reocr_enabled is False

    # triage saw both docs; metadata proposed for the clean one (garbage skipped).
    triage = next(r for r in multi.stage_reports if r.stage == "triage")
    assert len(triage.per_doc) == 2
    meta = next(r for r in multi.stage_reports if r.stage == "metadata")
    assert any(pd[0] == "dry" for pd in meta.per_doc)


def test_persisted_run_report_written(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=True)
    Sweep(s, client=fake.client()).run_once()

    reports = list((tmp_path / "data" / "run-reports").glob("sweep-*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text())
    assert payload["kind"] == "sweep"
    assert payload["dry_run"] is True
    assert "counts" in payload and "spend_total" in payload
    assert "period_spend" in payload
    assert isinstance(payload["stages"], list) and payload["stages"]


def test_rerun_skips_done_docs_idempotent(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=False)  # write mode from the start

    Sweep(s, client=fake.client()).run_once()          # writes triage + metadata
    n_patches_first = len(fake.patches)
    assert n_patches_first > 0

    fake.patches.clear()
    multi2 = Sweep(s, client=fake.client()).run_once()  # I1: everything already done
    triage2 = next(r for r in multi2.stage_reports if r.stage == "triage")
    # Both docs now marked triaged -> triage skips them.
    assert all(pd[0] == "skip" for pd in triage2.per_doc)
    # metadata queue is empty (docs advanced past eligibility) -> no new writes.
    assert fake.patches == []


def test_reocr_runs_only_when_enabled(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=True, reocr_enabled=True)
    sweep = Sweep(s, client=fake.client())
    multi = sweep.run_once()
    assert "reocr" in {r.stage for r in multi.stage_reports}


def test_json_log_shape(tmp_path, monkeypatch):
    _install_metadata_stub(monkeypatch)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=True)
    buf = io.StringIO()
    logger = JsonLogger(stream=buf, path=str(tmp_path / "data" / "logs" / "pa.jsonl"))
    Sweep(s, logger=logger, client=fake.client()).run_once()

    lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
    assert lines
    # Every record has the stable shape: ts, level, event.
    for rec in lines:
        assert "ts" in rec and "level" in rec and "event" in rec
    events = {rec["event"] for rec in lines}
    assert "sweep_start" in events and "sweep_end" in events
    assert "doc_outcome" in events            # per-doc outcome
    assert "stage_transition" in events       # stage transitions
    # doc_outcome records carry the outcome fields.
    doc_recs = [r for r in lines if r["event"] == "doc_outcome"]
    assert all("doc_id" in r and "status" in r and "stage" in r for r in doc_recs)
    # The durable JSONL file was also written under /data.
    assert (tmp_path / "data" / "logs" / "pa.jsonl").exists()


def test_failure_logs_real_error(tmp_path, monkeypatch):
    # Make metadata extraction raise; the sweep must record ERROR and log the
    # real error text (I6), not swallow it.
    def _boom(**kw):
        raise RuntimeError("Paperless said: field 'ai_stage' rejected value")
    install_stub_anthropic(monkeypatch, _boom)

    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    s = _settings(tmp_path, dry_run=False, triage_enabled=False)  # metadata only
    buf = io.StringIO()
    logger = JsonLogger(stream=buf, path=str(tmp_path / "data" / "logs" / "pa.jsonl"))
    multi = Sweep(s, logger=logger, client=fake.client()).run_once()

    meta = next(r for r in multi.stage_reports if r.stage == "metadata")
    assert any(pd[0] == "ERROR" for pd in meta.per_doc)
    lines = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
    failures = [r for r in lines if r["event"] == "failure"]
    assert failures and "rejected value" in failures[0]["error"]
