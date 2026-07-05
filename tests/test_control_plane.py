"""Phase 5 — the minimal control plane (vendor side).

Tests the protocol gateway in isolation: enrollment, the parking long-poll,
at-least-once dispatch (visibility-timeout redelivery), results/ack, heartbeat,
the admin enqueue path + `pa-control-plane` CLI, and a REAL localhost HTTP
round-trip proving the agent dials OUT over a socket to the control plane (the
control plane never dials in).
"""
from __future__ import annotations

import json
import pathlib

import pytest

from paperless_control_plane.store import ControlPlaneStore
from paperless_control_plane.app import ControlPlane, Response
from paperless_control_plane.server import ControlPlaneServer
from paperless_control_plane import cli as cp_cli


# ---------------------------------------------------------------------------
# Store semantics
# ---------------------------------------------------------------------------
def test_enroll_is_one_time_and_issues_credential():
    store = ControlPlaneStore()
    tok = store.mint_enrollment_token(tenant="acme")
    creds = store.enroll(tok)
    assert creds["tenant"] == "acme"
    assert creds["agent_credential"].startswith("agc_")
    # One-time: the token cannot be exchanged again.
    assert store.enroll(tok) is None


def test_authenticate_and_revoke():
    store = ControlPlaneStore()
    creds = store.enroll(store.mint_enrollment_token())
    aid, cred = creds["agent_id"], creds["agent_credential"]
    assert store.authenticate(aid, cred) is not None
    assert store.authenticate(aid, "wrong") is None
    store.revoke(aid)
    assert store.authenticate(aid, cred) is None  # revoked -> rejected


def test_at_least_once_redelivery_after_visibility_timeout():
    # A tiny fake clock lets us cross the visibility timeout deterministically.
    clock = {"t": 0.0}
    store = ControlPlaneStore(visibility_timeout=10.0, now=lambda: clock["t"])
    creds = store.enroll(store.mint_enrollment_token())
    aid, tenant = creds["agent_id"], creds["tenant"]
    store.enqueue(tenant=tenant, agent_id=aid, job_type="run_sweep")

    j1 = store.lease_next(tenant=tenant, agent_id=aid)
    assert j1 is not None and j1["deliveries"] == 1
    # Before the timeout: not redelivered.
    clock["t"] = 5.0
    assert store.lease_next(tenant=tenant, agent_id=aid) is None
    # After the timeout: the same job is redelivered (at-least-once).
    clock["t"] = 11.0
    j2 = store.lease_next(tenant=tenant, agent_id=aid)
    assert j2 is not None and j2["job_id"] == j1["job_id"] and j2["deliveries"] == 2
    # Ack removes it so it stops redelivering.
    store.ack(tenant=tenant, agent_id=aid, job_id=j1["job_id"], result={"ok": True})
    clock["t"] = 30.0
    assert store.lease_next(tenant=tenant, agent_id=aid) is None


def test_ack_is_idempotent():
    store = ControlPlaneStore()
    creds = store.enroll(store.mint_enrollment_token())
    aid, tenant = creds["agent_id"], creds["tenant"]
    job = store.enqueue(tenant=tenant, agent_id=aid, job_type="run_sweep")
    store.lease_next(tenant=tenant, agent_id=aid)
    assert store.ack(tenant=tenant, agent_id=aid, job_id=job["job_id"]) is True
    # A second ack (e.g. a redelivered ack after the agent reconnects) is a no-op.
    assert store.ack(tenant=tenant, agent_id=aid, job_id=job["job_id"]) is False


def test_store_persists_across_restart(tmp_path):
    path = tmp_path / "cp-state.json"
    store = ControlPlaneStore(path)
    creds = store.enroll(store.mint_enrollment_token(tenant="t9"))
    store.enqueue(tenant="t9", agent_id=creds["agent_id"], job_type="run_sweep")
    # "Restart" the control plane: a fresh store over the same file.
    store2 = ControlPlaneStore(path)
    assert store2.authenticate(creds["agent_id"], creds["agent_credential"]) is not None
    assert store2.queue_depth(tenant="t9", agent_id=creds["agent_id"]) == 1


# ---------------------------------------------------------------------------
# App-level protocol (transport-agnostic handlers)
# ---------------------------------------------------------------------------
def test_work_endpoint_parks_then_204_when_no_job():
    store = ControlPlaneStore()
    creds = store.enroll(store.mint_enrollment_token())
    cp = ControlPlane(store, poll_timeout=0.02)  # short park for the test
    headers = {"X-Agent-Id": creds["agent_id"],
               "Authorization": f"Bearer {creds['agent_credential']}"}
    resp = cp.handle("GET", "/agent/work", headers=headers)
    assert resp.status == 204


def test_work_endpoint_returns_job_when_available():
    store = ControlPlaneStore()
    creds = store.enroll(store.mint_enrollment_token())
    store.enqueue(tenant=creds["tenant"], agent_id=creds["agent_id"], job_type="run_sweep")
    cp = ControlPlane(store, poll_timeout=1.0)
    headers = {"X-Agent-Id": creds["agent_id"],
               "Authorization": f"Bearer {creds['agent_credential']}"}
    resp = cp.handle("GET", "/agent/work", headers=headers)
    assert resp.status == 200 and resp.body["job"]["type"] == "run_sweep"


def test_unauthenticated_endpoints_rejected():
    cp = ControlPlane(ControlPlaneStore())
    for method, path in [("GET", "/agent/work"), ("POST", "/agent/results"),
                         ("POST", "/agent/heartbeat")]:
        resp = cp.handle(method, path, headers={}, body={"job_id": "x"})
        assert resp.status == 401


def test_admin_enqueue_requires_admin_token_when_set():
    store = ControlPlaneStore()
    creds = store.enroll(store.mint_enrollment_token())
    cp = ControlPlane(store, admin_token="s3cr3t")
    body = {"agent_id": creds["agent_id"], "type": "run_sweep"}
    assert cp.handle("POST", "/admin/enqueue", headers={}, body=body).status == 401
    ok = cp.handle("POST", "/admin/enqueue",
                   headers={"Authorization": "Bearer s3cr3t"}, body=body)
    assert ok.status == 200 and ok.body["enqueued"] is True


# ---------------------------------------------------------------------------
# Real localhost HTTP round-trip (agent dials OUT over a socket)
# ---------------------------------------------------------------------------
def test_http_server_full_protocol_over_localhost(tmp_path):
    from paperless_assistant.transport import HttpTransport
    from paperless_assistant.config import Settings, HostedSettings
    from paperless_assistant.hosted import HostedAgent

    store = ControlPlaneStore(visibility_timeout=1000)
    cp = ControlPlane(store, poll_timeout=0.3)
    server = ControlPlaneServer(cp, host="127.0.0.1", port=0).start()
    try:
        base = server.base_url
        tok = store.mint_enrollment_token(tenant="t1")
        s = Settings(data_dir=str(tmp_path / "data"), mode="hosted",
                     paperless_token="tok")
        s.hosted = HostedSettings(control_plane_url=base, enrollment_token=tok,
                                  heartbeat_interval_seconds=0)
        runs = []

        class _R:
            def merged_counts(self):
                return {"wrote": 1}

            def total_spend(self):
                return 0.0

        a = HostedAgent(s, transport=HttpTransport(base),
                        job_runner=lambda j: runs.append(j["job_id"]) or _R(),
                        sleep=lambda _x: None, now=lambda: 0.0)
        ident = a.ensure_enrolled()
        job = store.enqueue(tenant="t1", agent_id=ident["agent_id"],
                            job_type="run_sweep")
        a.poll_once()
        assert runs == [job["job_id"]]
        assert store.result_for(job["job_id"]) is not None
        assert a.maybe_heartbeat(force=True) is True
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# `pa-control-plane` CLI
# ---------------------------------------------------------------------------
def test_cli_mint_token_and_enqueue(tmp_path, capsys):
    state = str(tmp_path / "state.json")
    # Mint a token (operator action).
    cp_cli.main(["--state", state, "mint-token", "--tenant", "t1"])
    token_line = capsys.readouterr().out.splitlines()[0].strip()
    assert token_line.startswith("enr_")

    # Enroll an agent directly via the store (as an agent would over HTTP).
    store = ControlPlaneStore(state)
    creds = store.enroll(token_line)
    assert creds is not None

    # Enqueue a job via the CLI for that agent (same tenant it enrolled under).
    cp_cli.main(["--state", state, "enqueue", "--tenant", "t1",
                 "--agent-id", creds["agent_id"],
                 "--type", "process_document", "--document-id", "5"])
    out = capsys.readouterr().out
    job = json.loads(out)
    assert job["type"] == "process_document"
    assert job["payload"]["document_id"] == 5

    # A fresh store over the same state sees the queued job (persistence).
    store2 = ControlPlaneStore(state)
    assert store2.queue_depth(tenant="t1", agent_id=creds["agent_id"]) == 1


def test_cli_revoke(tmp_path, capsys):
    state = str(tmp_path / "state.json")
    store = ControlPlaneStore(state)
    creds = store.enroll(store.mint_enrollment_token())
    cp_cli.main(["--state", state, "revoke", "--agent-id", creds["agent_id"]])
    assert "revoked" in capsys.readouterr().out
    assert ControlPlaneStore(state).authenticate(
        creds["agent_id"], creds["agent_credential"]) is None
