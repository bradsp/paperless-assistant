"""Phase 5 — hosted-mode outbound-only agent + minimal control plane.

Everything runs OFFLINE: the agent pull-loop is driven against an IN-PROCESS
control plane (no real cloud, no real Paperless, no real keys). Disconnects and
restarts are simulated programmatically. This file proves every item in the
prompt's <verification> list except the inherently manual firewall acceptance
(documented in docs/phase5-acceptance.md).

Layout of proofs:
  * enrollment -> persisted, rotatable agent credential under /data (never YAML)
  * pull a dispatched job -> execute via the engine -> push a result -> heartbeat
  * NO inbound listener / no host port (structural proof of outbound-only)
  * forced disconnect mid-flow resumes with NO duplicate/partial writes
  * at-least-once redelivery is de-duped (no double-write / double-spend)
  * restart resumes from /data without reprocessing
  * NO control-plane payload contains the Paperless token or an AI key
"""
from __future__ import annotations

import json
import pathlib

import pytest

from paperless_assistant.config import Settings, HostedSettings, TaskProvider, SpendCaps
from paperless_assistant.transport import InProcessTransport, TransportError
from paperless_assistant.hosted import (
    HostedAgent, EnrollmentError, RevokedError, build_result, _assert_no_secrets,
)
from paperless_control_plane.store import ControlPlaneStore
from paperless_control_plane.app import ControlPlane


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeReport:
    """Minimal engine-report double: what build_result reads."""

    def __init__(self, counts=None, spend=0.0):
        self._counts = counts or {"wrote": 1}
        self._spend = spend

    def merged_counts(self):
        return self._counts

    def total_spend(self):
        return self._spend


def _mk(tmp_path, *, visibility_timeout=1000.0, poll_timeout=0.05,
        enrollment_token=None, mode="hosted"):
    """Build (store, control_plane, settings) sharing a fresh /data dir."""
    store = ControlPlaneStore(visibility_timeout=visibility_timeout)
    cp = ControlPlane(store, poll_timeout=poll_timeout)
    tok = enrollment_token if enrollment_token is not None else store.mint_enrollment_token(tenant="t1")
    s = Settings(
        base_url="http://paperless.test:8000",
        paperless_token="SECRET-PAPERLESS-TOKEN",
        anthropic_api_key="sk-ant-SECRET-KEY",
        openai_api_key="sk-oai-SECRET-KEY",
        data_dir=str(tmp_path / "data"),
        mode=mode,
        metadata_task=TaskProvider(provider="anthropic", model="claude-sonnet-4-6"),
        spend=SpendCaps(per_run=1.0, per_period=5.0),
    )
    s.hosted = HostedSettings(
        control_plane_url="http://inproc.test",
        enrollment_token=tok or "",
        reconnect_backoff_min=0.001,
        reconnect_backoff_max=0.002,
        heartbeat_interval_seconds=0,  # heartbeat every loop for tests
    )
    return store, cp, s


def _agent(store, cp, settings, *, runner=None, transport=None):
    calls = []

    def _default_runner(job):
        calls.append(job["job_id"])
        return _FakeReport()

    a = HostedAgent(
        settings,
        transport=transport or InProcessTransport(cp),
        job_runner=runner or _default_runner,
        sleep=lambda _x: None,
        now=lambda: 0.0,  # freeze time; heartbeat_interval 0 -> always due
    )
    a._executed_calls = calls  # type: ignore[attr-defined]
    return a


# ---------------------------------------------------------------------------
# Enrollment -> persisted, rotatable credential under /data (never YAML/logs)
# ---------------------------------------------------------------------------
def test_enrollment_persists_agent_credential_under_data(tmp_path):
    store, cp, s = _mk(tmp_path)
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    assert ident["agent_id"].startswith("agt_")
    assert ident["agent_credential"].startswith("agc_")

    # Persisted under /data as JSON (not YAML), reloadable.
    cred_file = pathlib.Path(s.data_dir, "agent-credential.json")
    assert cred_file.exists()
    saved = json.loads(cred_file.read_text())
    assert saved["agent_credential"] == ident["agent_credential"]
    assert saved["tenant"] == "t1"


def test_enrollment_is_one_time(tmp_path):
    store, cp, s = _mk(tmp_path)
    a = _agent(store, cp, s)
    a.ensure_enrolled()
    # A brand-new agent trying to reuse the SAME (now-consumed) token fails.
    _, _, s2 = _mk(tmp_path / "other", enrollment_token=s.hosted.enrollment_token)
    s2.hosted.control_plane_url = "http://inproc.test"
    a2 = HostedAgent(s2, transport=InProcessTransport(cp),
                     job_runner=lambda j: _FakeReport(), sleep=lambda _x: None)
    with pytest.raises(EnrollmentError):
        a2.ensure_enrolled()


def test_restart_reuses_stored_credential_no_reenroll(tmp_path):
    store, cp, s = _mk(tmp_path)
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    # "Restart": a fresh agent over the same /data must NOT need the token again.
    s.hosted.enrollment_token = ""  # token gone after first use
    a2 = _agent(store, cp, s)
    ident2 = a2.ensure_enrolled()
    assert ident2["agent_id"] == ident["agent_id"]
    assert ident2["agent_credential"] == ident["agent_credential"]


def test_credential_is_rotatable_and_revocable(tmp_path):
    store, cp, s = _mk(tmp_path)
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    # Rotation server-side invalidates the old credential.
    new_cred = store.rotate_credential(ident["agent_id"])
    assert new_cred and new_cred != ident["agent_credential"]
    assert store.authenticate(ident["agent_id"], ident["agent_credential"]) is None
    assert store.authenticate(ident["agent_id"], new_cred) is not None
    # Revocation makes the agent's calls unauthenticated -> RevokedError, and the
    # local credential is cleared so it must re-enroll.
    store.revoke(ident["agent_id"])
    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")
    with pytest.raises(RevokedError):
        a.poll_once()
    assert not pathlib.Path(s.data_dir, "agent-credential.json").exists()


def test_credential_never_logged(tmp_path):
    import io
    from paperless_assistant.obs import JsonLogger

    buf = io.StringIO()
    store, cp, s = _mk(tmp_path)
    logger = JsonLogger(stream=buf, path=None)
    a = HostedAgent(s, transport=InProcessTransport(cp),
                    job_runner=lambda j: _FakeReport(), logger=logger,
                    sleep=lambda _x: None)
    ident = a.ensure_enrolled()
    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")
    a.poll_once()
    a.maybe_heartbeat(force=True)
    logtext = buf.getvalue()
    assert ident["agent_credential"] not in logtext
    assert "agent_enrolled" in logtext  # identity IS logged, just not the secret


# ---------------------------------------------------------------------------
# Pull -> execute via engine -> push result -> heartbeat
# ---------------------------------------------------------------------------
def test_pull_execute_push_and_heartbeat(tmp_path):
    store, cp, s = _mk(tmp_path)
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"],
                        job_type="run_sweep", payload={"limit": 5})

    processed = a.poll_once()
    assert processed["job_id"] == job["job_id"]
    # The engine (our runner) actually ran exactly once.
    assert a._executed_calls == [job["job_id"]]
    # Result landed on the control plane and the job was acked (queue drained).
    result = store.result_for(job["job_id"])
    assert result and result["outcome"] == "completed"
    assert store.queue_depth(tenant="t1", agent_id=ident["agent_id"]) == 0

    # Heartbeat is sent and recorded server-side.
    assert a.maybe_heartbeat(force=True) is True
    agent_rec = store.agent(ident["agent_id"])
    assert agent_rec["last_status"]["mode"] == "hosted"


def test_no_work_returns_none(tmp_path):
    store, cp, s = _mk(tmp_path, poll_timeout=0.02)
    a = _agent(store, cp, s)
    a.ensure_enrolled()
    assert a.poll_once() is None  # 204 -> re-poll; no crash


def test_default_runner_uses_the_existing_engine(tmp_path, monkeypatch):
    """The default job_runner must call into Sweep (the SAME engine other triggers
    use) — NOT a forked pipeline. We assert it dispatches to Sweep.run_once /
    Sweep.process_nudge."""
    store, cp, s = _mk(tmp_path)
    # Build an agent with NO explicit runner so the default (Sweep-backed) is used.
    a = HostedAgent(s, transport=InProcessTransport(cp), sleep=lambda _x: None)

    seen = {}

    class _StubSweep:
        def __init__(self, *args, **kw):
            pass

        def run_once(self, *, limit=None, source=None):
            seen["run_once"] = limit
            return _FakeReport()

        def process_nudge(self, doc_id):
            seen["nudge"] = doc_id
            return _FakeReport()

    monkeypatch.setattr("paperless_assistant.sweep.Sweep", _StubSweep)

    ident = a.ensure_enrolled()
    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep",
                  payload={"limit": 3})
    a.poll_once()
    assert seen.get("run_once") == 3

    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="process_document",
                  payload={"document_id": 42})
    a.poll_once()
    assert seen.get("nudge") == 42


# ---------------------------------------------------------------------------
# OUTBOUND-ONLY: no inbound listener / no host port (structural proof, §7 pt 2)
# ---------------------------------------------------------------------------
def test_hosted_pull_loop_opens_no_inbound_listener(tmp_path, monkeypatch):
    """Structural proof of outbound-only: while the agent enrolls, pulls, executes,
    pushes and heartbeats, it must NEVER create a LISTENING socket (no bind+listen,
    no server). We spy on socket.socket and forbid any listen()/bind-to-accept."""
    import socket as _socket

    listened = []
    real_socket = _socket.socket

    class _SpySocket(real_socket):
        def listen(self, *a, **kw):  # a listening server would call this
            listened.append(("listen", self.getsockname() if self.fileno() != -1 else None))
            return super().listen(*a, **kw)

    monkeypatch.setattr(_socket, "socket", _SpySocket)

    store, cp, s = _mk(tmp_path)
    # Use the in-process transport: there is literally no socket server object in
    # the agent's transport. Drive the full lifecycle.
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")
    a.poll_once()
    a.maybe_heartbeat(force=True)

    assert listened == [], f"hosted pull-loop opened an inbound listener: {listened}"

    # And structurally: the agent + its transport expose no server/bind surface.
    assert not hasattr(a, "server")
    assert not hasattr(a.transport, "server")
    for banned in ("bind", "listen", "accept", "serve_forever"):
        assert not hasattr(a.transport, banned), (
            f"transport must not expose inbound-server method {banned!r}")


def test_hosted_serve_binds_no_port_via_cli(tmp_path, monkeypatch, capsys):
    """`pa serve` in hosted mode runs the pull-loop and binds NO port — asserted by
    spying socket.listen across the CLI path (with iterations bounded)."""
    import socket as _socket
    from paperless_assistant import cli

    listened = []
    real = _socket.socket

    class _Spy(real):
        def listen(self, *a, **kw):
            listened.append(True)
            return super().listen(*a, **kw)

    monkeypatch.setattr(_socket, "socket", _Spy)

    store = ControlPlaneStore()
    cp = ControlPlane(store, poll_timeout=0.01)
    tok = store.mint_enrollment_token(tenant="t1")

    # Point the CLI at settings via env; drive it with an in-process transport by
    # patching HostedAgent's transport construction.
    monkeypatch.setenv("PA_MODE", "hosted")
    monkeypatch.setenv("PA_CONTROL_PLANE_URL", "http://inproc.test")
    monkeypatch.setenv("PA_ENROLLMENT_TOKEN", tok)
    monkeypatch.setenv("PA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPERLESS_TOKEN", "tok")

    import paperless_assistant.hosted as hosted_mod
    orig_init = hosted_mod.HostedAgent.__init__

    def patched_init(self, settings, **kw):
        kw.setdefault("transport", InProcessTransport(cp))
        kw.setdefault("sleep", lambda _x: None)
        orig_init(self, settings, **kw)

    monkeypatch.setattr(hosted_mod.HostedAgent, "__init__", patched_init)

    cli.main(["serve", "--iterations", "1"])
    out = capsys.readouterr().out
    assert "HOSTED mode" in out and "outbound-only" in out
    assert listened == [], "hosted `pa serve` must not bind an inbound listener"


# ---------------------------------------------------------------------------
# Forced disconnect mid-flow resumes with NO duplicate/partial writes
# ---------------------------------------------------------------------------
def test_disconnect_after_execute_queues_result_and_flushes_on_reconnect(tmp_path):
    store, cp, s = _mk(tmp_path)  # long visibility: no redelivery in this test
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")

    t = a.transport
    real_request = t.request

    def drop_results(method, path, **kw):
        if path == "/agent/results":
            raise TransportError("connection dropped before ack")
        return real_request(method, path, **kw)

    t.request = drop_results  # type: ignore[method-assign]
    # The push failure is handled internally (flush is best-effort): the job is
    # executed, its result durably queued, and NO exception propagates — the loop
    # keeps going and retries the flush next reconnect.
    a.poll_once()

    # Executed exactly once; result durably queued under /data; cursor at 'executed'.
    assert a._executed_calls == [job["job_id"]]
    assert a.results.depth() == 1
    assert a.cursor.stage(job["job_id"]) == "executed"
    assert store.result_for(job["job_id"]) is None  # never landed yet

    # Reconnect: flush. No re-execution; result now lands; queue drains.
    t.request = real_request  # type: ignore[method-assign]
    a.flush_results()
    assert a._executed_calls == [job["job_id"]]  # NOT re-run
    assert store.result_for(job["job_id"]) is not None
    assert a.results.depth() == 0
    assert a.cursor.stage(job["job_id"]) == "acked"


def test_in_flight_work_continues_while_control_plane_unreachable(tmp_path):
    """Once a job is held, executing it does not depend on control-plane
    reachability (§3.2). Even if the result push fails, the engine still ran."""
    store, cp, s = _mk(tmp_path)
    ran = []

    def runner(job):
        ran.append(job["job_id"])
        return _FakeReport(counts={"wrote": 3})

    a = _agent(store, cp, s, runner=runner)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")

    t = a.transport
    real = t.request
    t.request = lambda m, p, **k: (
        (_ for _ in ()).throw(TransportError("down")) if p == "/agent/results"
        else real(m, p, **k))
    a.poll_once()  # push fails but is handled internally; local work still ran
    assert ran == [job["job_id"]]  # local work completed despite CP unreachable
    assert a.results.depth() == 1  # result durably queued for later flush


# ---------------------------------------------------------------------------
# At-least-once REDELIVERY is de-duped (no double-write / double-spend)
# ---------------------------------------------------------------------------
def test_redelivered_job_is_deduped_no_double_execute(tmp_path):
    # visibility_timeout=0 -> a dispatched-but-unacked job is immediately eligible
    # for redelivery, so a second poll re-leases the SAME job id.
    store, cp, s = _mk(tmp_path, visibility_timeout=0.0)
    spend_calls = []

    def runner(job):
        spend_calls.append(job["job_id"])
        return _FakeReport(spend=0.10)  # each real execution would "spend" $0.10

    a = _agent(store, cp, s, runner=runner)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")

    t = a.transport
    real = t.request

    # First delivery: execute, but the ACK is lost -> CP still holds the job.
    def lose_ack(method, path, **kw):
        if path == "/agent/results":
            raise TransportError("ack lost")
        return real(method, path, **kw)

    t.request = lose_ack  # type: ignore[method-assign]
    a.poll_once()  # executes, push fails, result queued (no exception propagates)
    assert spend_calls == [job["job_id"]]  # executed once

    # Redelivery: transport healthy again; poll re-leases the same job id. It MUST
    # NOT re-execute (no double-spend) — the stage machine de-dupes it, and the
    # queued result is simply (re)pushed and acked.
    t.request = real  # type: ignore[method-assign]
    a.poll_once()
    assert spend_calls == [job["job_id"]], "redelivered job must not double-execute"
    assert store.result_for(job["job_id"]) is not None
    assert store.queue_depth(tenant="t1", agent_id=ident["agent_id"]) == 0


def test_redelivery_after_full_completion_is_noop(tmp_path):
    store, cp, s = _mk(tmp_path, visibility_timeout=0.0)
    a = _agent(store, cp, s)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")
    a.poll_once()  # fully completes + acks
    assert store.queue_depth(tenant="t1", agent_id=ident["agent_id"]) == 0
    # Force a redelivery by re-enqueuing the SAME job id (simulating an at-least-
    # once duplicate the CP might emit). The agent recognises it as done.
    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep",
                  job_id=job["job_id"])
    a.poll_once()
    assert a._executed_calls == [job["job_id"]]  # still exactly one execution


# ---------------------------------------------------------------------------
# Restart resumes from /data without reprocessing
# ---------------------------------------------------------------------------
def test_restart_resumes_from_data_without_reprocessing(tmp_path):
    store, cp, s = _mk(tmp_path)
    runs = []

    def runner(job):
        runs.append(job["job_id"])
        return _FakeReport()

    a = _agent(store, cp, s, runner=runner)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")

    # Execute, then crash before the ack lands (result left queued under /data).
    t = a.transport
    real = t.request
    t.request = lambda m, p, **k: (
        (_ for _ in ()).throw(TransportError("crash")) if p == "/agent/results"
        else real(m, p, **k))
    a.poll_once()  # executes; push fails; result queued under /data
    assert runs == [job["job_id"]]
    assert a.results.depth() == 1

    # "Restart": a brand-new agent + fresh transport over the SAME /data + CP.
    a2 = _agent(store, cp, s, runner=runner)
    # It must NOT re-enroll and must NOT reprocess the job; it just flushes the
    # queued result.
    a2.ensure_enrolled()
    assert a2._identity["agent_id"] == ident["agent_id"]
    a2.flush_results()
    assert runs == [job["job_id"]], "restart must not reprocess a done job"
    assert store.result_for(job["job_id"]) is not None
    assert a2.results.depth() == 0


# ---------------------------------------------------------------------------
# SECURITY: no control-plane payload contains the Paperless token or an AI key
# ---------------------------------------------------------------------------
def test_no_secret_egress_in_any_control_plane_request(tmp_path):
    """Capture EVERY request the agent sends to the control plane over a full
    lifecycle and assert none contains the Paperless token or an AI provider key.
    The agent credential MAY appear (it is the control-plane secret) but the
    Paperless token / AI keys must NEVER."""
    store, cp, s = _mk(tmp_path)

    sent = []

    class _RecordingTransport(InProcessTransport):
        def request(self, method, path, *, headers=None, body=None, timeout=None):
            sent.append({"method": method, "path": path,
                         "headers": headers or {}, "body": body or {}})
            return super().request(method, path, headers=headers, body=body,
                                   timeout=timeout)

    a = _agent(store, cp, s, transport=_RecordingTransport(cp))
    ident = a.ensure_enrolled()
    store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep",
                  payload={"document_id": 7})
    a.poll_once()
    a.maybe_heartbeat(force=True)

    blob = json.dumps(sent)
    assert s.paperless_token not in blob, "Paperless token leaked to control plane"
    assert s.anthropic_api_key not in blob, "Anthropic key leaked to control plane"
    assert s.openai_api_key not in blob, "OpenAI key leaked to control plane"
    # We DID make outbound requests (the test is meaningful).
    assert any(r["path"] == "/agent/work" for r in sent)
    assert any(r["path"] == "/agent/results" for r in sent)
    assert any(r["path"] == "/agent/heartbeat" for r in sent)
    # The agent credential is the ONLY secret that crosses (as a bearer header).
    assert any("Bearer" in json.dumps(r["headers"]) for r in sent)


def test_build_result_rejects_forbidden_secret_keys():
    job = {"job_id": "j1", "type": "run_sweep"}
    # A well-formed result is fine.
    ok = build_result(job, _FakeReport())
    assert ok["job_id"] == "j1" and "usage" in ok
    # If a forbidden key ever sneaks into a payload we refuse to send it.
    with pytest.raises(AssertionError):
        _assert_no_secrets({"job_id": "j1", "paperless_token": "leak"})
    with pytest.raises(AssertionError):
        _assert_no_secrets({"nested": {"anthropic_api_key": "leak"}})


# ---------------------------------------------------------------------------
# Reconnect with bounded backoff + jitter
# ---------------------------------------------------------------------------
def test_reconnect_loop_backs_off_then_resumes(tmp_path):
    """The pull-loop tolerates a disconnected transport (bounded backoff+jitter)
    and resumes pulling once the transport reconnects — proving it survives
    network loss without crashing or busy-spinning."""
    store, cp, s = _mk(tmp_path)
    sleeps = []
    runs = []

    def runner(job):
        runs.append(job["job_id"])
        return _FakeReport()

    t = InProcessTransport(cp)
    a = HostedAgent(s, transport=t, job_runner=runner,
                    sleep=lambda d: sleeps.append(d), now=lambda: 0.0)
    ident = a.ensure_enrolled()
    job = store.enqueue(tenant="t1", agent_id=ident["agent_id"], job_type="run_sweep")

    # Start disconnected: the loop should back off (sleep) without crashing.
    t.disconnect()
    a.run(iterations=3)
    assert sleeps, "a disconnected loop must back off (sleep), not busy-spin"
    # Every backoff delay is bounded by reconnect_backoff_max (full jitter in
    # [0, base]) — proving the jitter is bounded.
    assert all(0.0 <= d <= s.hosted.reconnect_backoff_max for d in sleeps)
    assert runs == []  # nothing pulled while down

    # Reconnect: the loop resumes and pulls the waiting job.
    t.reconnect()
    a.run(iterations=2)
    assert runs == [job["job_id"]]
    assert store.result_for(job["job_id"]) is not None
