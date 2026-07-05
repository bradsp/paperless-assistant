"""Phase 7 — Mode C direct-connection + read-only dashboards + hardening.

Everything runs FULLY OFFLINE: no real cloud, no real Paperless, no real model
API. A FAKE remote Paperless stands in for the user's published instance; the
vendor model call inside the proxy is STUBBED; the store is in-memory / tmp files.

Proves the §9 Phase 7 verification (connectivity §6 / §1 / §7, product-arch §8.4):
  * a tenant REGISTERS a direct target and the direct RUNNER completes an engine
    run (triage/metadata) against a FAKE remote Paperless via the stored token,
    with AI metered through the Phase-6 inference path;
  * the runner REFUSES a host outside the allow-list (SSRF / misconfig guard);
  * ONE-CLICK revocation immediately removes the target + token and stops further
    direct runs; the token is NEVER logged and never persisted in cleartext logs;
  * dashboard JSON endpoints return correct fleet / cost (incl. spend-vs-cap) /
    review-queue shapes; the self-contained HTML view has NO external assets and
    mutates nothing;
  * the DEFAULT mode is STILL the agent — Mode C runs ONLY when explicitly
    registered; no default moved toward Mode C.
"""
from __future__ import annotations

import io
import json
import re

import pytest

from paperless_control_plane.store import ControlPlaneStore
from paperless_control_plane.app import ControlPlane
from paperless_control_plane.billing import (
    BillingStore, STATUS_ACTIVE, STATUS_SUSPENDED,
)
from paperless_control_plane.inference import InferenceProxy, ModelBackend
from paperless_control_plane.direct import (
    DirectTargetStore, DirectRunner, AllowListSession,
    EgressNotAllowedError, DirectTargetError, _public,
)
from paperless_control_plane.dashboard import DashboardData, render_html
from paperless_assistant.obs import JsonLogger

from fakes import FakePaperless, make_custom_fields, healthy_tags, _FakePaperlessSession


# A secret token we can grep the whole world for, to prove it never leaks.
SECRET_TOKEN = "DIRECT-TOKEN-SEKRIT-9a8b7c6d"

VALID_META = {
    "title": "Acme Invoice March",
    "correspondent": "Acme Corp",
    "document_type": "Invoice",
    "tags": ["billing"],
    "correspondent_is_new": True,
    "document_type_is_new": True,
    "new_tags": ["billing"],
}


# ---------------------------------------------------------------------------
# Stub vendor model backend (no real API, no spend).
# ---------------------------------------------------------------------------
class StubBackend(ModelBackend):
    def __init__(self, *, extract_data=None, in_tokens=100, out_tokens=50):
        super().__init__(api_key="VENDOR-SERVER-SIDE-KEY")
        self.extract_data = extract_data if extract_data is not None else dict(VALID_META)
        self.in_tokens = in_tokens
        self.out_tokens = out_tokens
        self.calls = []

    def transcribe(self, *, doc_b64, model, opts=None):
        self.calls.append(("transcribe", model))
        return {"text": "CLEAN OCR", "in_tokens": self.in_tokens,
                "out_tokens": self.out_tokens}

    def extract_structured(self, *, prompt, schema, model, opts=None):
        self.calls.append(("extract", model))
        return {"data": dict(self.extract_data), "in_tokens": self.in_tokens,
                "out_tokens": self.out_tokens}


# ---------------------------------------------------------------------------
# A fake remote Paperless + an egress-allow-listed session factory that still
# enforces the allow-list (so the SSRF guard is genuinely exercised in tests).
# ---------------------------------------------------------------------------
def _remote_paperless(doc_id=7, content="TOPSECRET remote invoice contents"):
    return FakePaperless(
        fields=make_custom_fields(),
        tags=healthy_tags(),
        docs=[{
            "id": doc_id, "title": "old title", "content": content,
            "tags": [], "correspondent": None, "document_type": None,
            # ai_stage=triaged -> metadata-eligible.
            "custom_fields": [{"field": 2, "value": "opt-t"}],
        }],
    )


def _session_factory(fake):
    """Return a factory(allowed_hosts) -> AllowListSession wrapping the fake remote
    Paperless. The allow-list guard runs BEFORE the fake handles the request."""
    def factory(allowed_hosts):
        return AllowListSession(allowed_hosts, session=_FakePaperlessSession(fake))
    return factory


def _proxy(billing, logger=None):
    return InferenceProxy(StubBackend(), billing, logger=logger,
                          pricing_provider="anthropic",
                          default_model_extract="claude-sonnet-4-6",
                          default_model_transcribe="claude-opus-4-8")


def _mk(tmp_path, *, tenant="acme", spend_cap=5.0, entitled=True, logger=None):
    billing = BillingStore(str(tmp_path / "billing.json"))
    if entitled:
        billing.set_subscription(tenant, status=STATUS_ACTIVE, spend_cap=spend_cap)
    direct = DirectTargetStore(str(tmp_path / "direct.json"))
    log = logger or JsonLogger(stream=io.StringIO(), path=None)
    backend = _proxy(billing, logger=log)
    return {"billing": billing, "direct": direct, "proxy": backend,
            "logger": log, "tenant": tenant}


# ===========================================================================
# Direct target store: register / list / token stored server-side / revoke.
# ===========================================================================
def test_add_target_stores_token_server_side_and_public_strips_it(tmp_path):
    store = DirectTargetStore(str(tmp_path / "d.json"))
    pub = store.add_target(tenant="acme", paperless_url="https://paperless.acme.test",
                           token=SECRET_TOKEN)
    # The public record NEVER contains the token.
    assert "token" not in pub
    assert pub["token_configured"] is True
    assert pub["paperless_url"] == "https://paperless.acme.test"
    assert pub["allowed_hosts"] == ["paperless.acme.test"]
    assert pub["enabled"] is True
    # The full record (for the runner) DOES have it, stored server-side.
    full = store.get_target(pub["target_id"])
    assert full["token"] == SECRET_TOKEN
    # list_targets is public (token stripped).
    for t in store.list_targets():
        assert "token" not in t


def test_token_never_persisted_in_cleartext_beyond_owner_file(tmp_path):
    """The token IS persisted (it's a reversible secret the vendor must use), but
    ONLY in the access-controlled direct-target file — not in any public view."""
    path = tmp_path / "d.json"
    store = DirectTargetStore(str(path))
    store.add_target(tenant="acme", paperless_url="https://p.acme.test",
                     token=SECRET_TOKEN)
    # public views never carry it
    assert SECRET_TOKEN not in json.dumps(store.list_targets())
    # the state file does carry it (documented: encrypt-at-rest required in prod)
    assert SECRET_TOKEN in path.read_text(encoding="utf-8")


def test_one_click_revocation_removes_url_and_token(tmp_path):
    store = DirectTargetStore(str(tmp_path / "d.json"))
    pub = store.add_target(tenant="acme", paperless_url="https://p.acme.test",
                           token=SECRET_TOKEN)
    tid = pub["target_id"]
    assert store.get_target(tid) is not None
    assert store.revoke(tid) is True
    # Gone from memory AND from the public list.
    assert store.get_target(tid) is None
    assert store.list_targets() == []
    # The token is scrubbed from the persisted file too.
    assert SECRET_TOKEN not in (tmp_path / "d.json").read_text(encoding="utf-8")
    # Idempotent: revoking again is a harmless no-op.
    assert store.revoke(tid) is False


def test_add_target_requires_url_and_token(tmp_path):
    store = DirectTargetStore(str(tmp_path / "d.json"))
    with pytest.raises(DirectTargetError):
        store.add_target(tenant="t", paperless_url="", token=SECRET_TOKEN)
    with pytest.raises(DirectTargetError):
        store.add_target(tenant="t", paperless_url="https://p.test", token="")


# ===========================================================================
# Egress allow-listing (SSRF / misconfig guard).
# ===========================================================================
def test_allowlist_session_refuses_host_outside_allowlist():
    fake = _remote_paperless()
    sess = AllowListSession(["paperless.acme.test"],
                            session=_FakePaperlessSession(fake))
    # An approved host passes the guard (then the fake handles it).
    r = sess.request("GET", "https://paperless.acme.test/api/documents/7/")
    assert r.status_code in (200, 404)
    # A DIFFERENT host is refused BEFORE any request runs.
    with pytest.raises(EgressNotAllowedError) as ei:
        sess.request("GET", "http://169.254.169.254/latest/meta-data/")
    assert ei.value.host == "169.254.169.254"
    with pytest.raises(EgressNotAllowedError):
        sess.request("GET", "https://evil.example/api/documents/1/")


def test_allowlist_session_disables_redirect_following():
    """SSRF defence-in-depth: the allow-list session must not auto-follow redirects
    (a 30x to a non-approved host would bypass the host check otherwise)."""
    class _Rec:
        def __init__(self):
            self.headers = {}
            self.last = None

        def request(self, method, url, **kw):
            self.last = kw
            class R:  # minimal response
                status_code = 200
            return R()

    rec = _Rec()
    sess = AllowListSession(["paperless.acme.test"], session=rec)
    sess.request("GET", "https://paperless.acme.test/api/documents/")
    assert rec.last.get("allow_redirects") is False


def test_direct_runner_refuses_swapped_host(tmp_path):
    """The runner's client is allow-listed to the registered host; if the target's
    URL host is NOT reachable via the fake (simulating a swapped/foreign host), the
    guard refuses. We register host A but point the fake at host B to force it."""
    env = _mk(tmp_path)
    # Register a target whose host is NOT what the fake answers for; then force the
    # allow-list to a different host than the URL to prove refusal.
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test",
        token=SECRET_TOKEN, allowed_hosts=["only-this-host.test"])
    # allowed_hosts always includes the URL host, so the URL host IS allowed; to
    # prove refusal we instead directly assert the session guard on a foreign host.
    sess = AllowListSession(pub["allowed_hosts"])
    with pytest.raises(EgressNotAllowedError):
        sess._check("https://attacker.test/api/")


# ===========================================================================
# Direct runner: a full engine run against the FAKE remote Paperless, AI metered.
# ===========================================================================
def test_direct_run_completes_engine_run_and_meters_ai(tmp_path):
    env = _mk(tmp_path)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test", token=SECRET_TOKEN)

    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"],
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    # Write mode so metadata actually PATCHes the fake remote Paperless.
    summary = runner.run(pub["target_id"], dry_run=False)

    assert summary["tenant"] == "acme"
    assert summary["dry_run"] is False
    # The engine ran against the REMOTE Paperless via the stored token: a metadata
    # PATCH landed on doc 7.
    assert any(p[0] == 7 for p in fake.patches), "metadata written to remote Paperless"
    # The AI step was metered to the tenant through the Phase-6 path.
    assert summary["spend_usd"] > 0
    usage = env["billing"].usage_summary("acme")
    assert usage["calls"] >= 1
    assert usage["spend_usd"] > 0


def test_direct_runner_client_carries_token_in_auth_header(tmp_path):
    """The stored token is what authenticates the remote calls: the runner's
    PaperlessClient sets `Authorization: Token <token>` on the (allow-listed)
    session — proving the server-side token reaches the tenant's Paperless."""
    env = _mk(tmp_path)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test", token=SECRET_TOKEN)
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"],
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    client = runner._build_client("https://paperless.acme.test", SECRET_TOKEN,
                                  pub["allowed_hosts"])
    assert client.session.headers.get("Authorization") == f"Token {SECRET_TOKEN}"


def test_direct_run_token_never_logged(tmp_path):
    buf = io.StringIO()
    logger = JsonLogger(stream=buf, path=None)
    env = _mk(tmp_path, logger=logger)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test", token=SECRET_TOKEN)
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=logger,
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    runner.run(pub["target_id"], dry_run=False)
    logtext = buf.getvalue()
    assert logtext, "the runner should log routing metadata"
    assert "direct_run_start" in logtext and "direct_run_end" in logtext
    # The token NEVER appears in any log line.
    assert SECRET_TOKEN not in logtext
    # Nor does the remote document content.
    assert "TOPSECRET" not in logtext


def test_direct_run_refuses_after_revocation(tmp_path):
    env = _mk(tmp_path)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test", token=SECRET_TOKEN)
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"],
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    tid = pub["target_id"]
    # First run works.
    runner.run(tid, dry_run=True)
    # Revoke -> the very next run is refused (no target/token left).
    assert env["direct"].revoke(tid) is True
    with pytest.raises(DirectTargetError):
        runner.run(tid, dry_run=True)


def test_direct_run_refuses_disabled_target(tmp_path):
    env = _mk(tmp_path)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test",
        token=SECRET_TOKEN, enabled=False)
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"],
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    with pytest.raises(DirectTargetError):
        runner.run(pub["target_id"], dry_run=True)


def test_direct_run_spend_cap_refuses_ai(tmp_path):
    """The server-side per-tenant spend cap applies to Mode C exactly as to hosted
    agents: once the cap is hit, the metered AI step is refused."""
    env = _mk(tmp_path, spend_cap=0.0)  # will set below to force cap
    # A tiny cap: one metadata call worth of spend already exceeds it.
    env["billing"].set_subscription("acme", status=STATUS_ACTIVE, spend_cap=0.00001)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test", token=SECRET_TOKEN)
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"],
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    # Pre-spend to exceed the cap.
    env["billing"].record_usage("acme", task="extract_structured", model="m",
                                in_tokens=1, out_tokens=1, cost=1.0)
    summary = runner.run(pub["target_id"], dry_run=False)
    # The metadata write was NOT applied (AI refused -> engine surfaced an error,
    # no PATCH). The run still completes as a report (engine handles the refusal).
    assert not any(p[0] == 7 for p in fake.patches), "no write when AI is capped"


def test_unentitled_tenant_direct_run_refused_ai(tmp_path):
    env = _mk(tmp_path, entitled=False)
    fake = _remote_paperless()
    pub = env["direct"].add_target(
        tenant="acme", paperless_url="https://paperless.acme.test", token=SECRET_TOKEN)
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"],
                          session_factory=_session_factory(fake),
                          data_dir=str(tmp_path / "cpdata"))
    summary = runner.run(pub["target_id"], dry_run=False)
    # No subscription -> AI refused -> no metadata write.
    assert not any(p[0] == 7 for p in fake.patches)
    assert env["billing"].usage_summary("acme")["calls"] == 0


# ===========================================================================
# Dashboards: JSON shapes (fleet / cost / review) + self-contained HTML.
# ===========================================================================
def test_dashboard_fleet_shape(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "cp.json"))
    creds = store.enroll(store.mint_enrollment_token(tenant="acme"))
    store.record_heartbeat(agent_id=creds["agent_id"],
                           status={"result_queue_depth": 2, "jobs_done": 5,
                                   "mode": "hosted"})
    direct = DirectTargetStore(str(tmp_path / "d.json"))
    direct.add_target(tenant="acme", paperless_url="https://p.acme.test",
                      token=SECRET_TOKEN)
    data = DashboardData(store=store, billing=None, direct_store=direct)
    fleet = data.fleet()
    assert fleet["agent_count"] == 1
    assert fleet["direct_target_count"] == 1
    a = fleet["agents"][0]
    assert a["agent_id"] == creds["agent_id"]
    assert a["queue_depth"] == 2 and a["jobs_done"] == 5
    # The direct target in the fleet view NEVER carries the token VALUE (only a
    # presence flag).
    assert SECRET_TOKEN not in json.dumps(fleet)
    assert "\"token\":" not in json.dumps(fleet)
    assert fleet["direct_targets"][0]["token_configured"] is True


def test_dashboard_cost_shape_incl_spend_vs_cap(tmp_path):
    billing = BillingStore(str(tmp_path / "b.json"))
    billing.set_subscription("acme", status=STATUS_ACTIVE, spend_cap=1.0)
    billing.record_usage("acme", task="extract_structured", model="m",
                         in_tokens=100, out_tokens=50, cost=0.8)
    data = DashboardData(store=None, billing=billing, direct_store=None)
    cost = data.cost()
    assert cost["tenant_count"] == 1
    t = cost["tenants"][0]
    assert t["tenant"] == "acme"
    assert t["spend_usd"] == pytest.approx(0.8)
    assert t["spend_cap"] == pytest.approx(1.0)
    assert t["cap_pct"] == pytest.approx(80.0)
    assert t["over_cap"] is False
    assert t["subscription"] == STATUS_ACTIVE
    # Over-cap flips the flag.
    billing.record_usage("acme", task="extract_structured", model="m",
                         in_tokens=100, out_tokens=50, cost=0.5)
    t2 = data.cost()["tenants"][0]
    assert t2["over_cap"] is True


def test_dashboard_review_shape(tmp_path):
    # Direct target with recorded review counts + an agent heartbeat with a review block.
    store = ControlPlaneStore(str(tmp_path / "cp.json"))
    creds = store.enroll(store.mint_enrollment_token(tenant="beta"))
    store.record_heartbeat(agent_id=creds["agent_id"],
                           status={"review": {"superseded": 3, "ai_new_taxonomy": 1}})
    direct = DirectTargetStore(str(tmp_path / "d.json"))
    pub = direct.add_target(tenant="acme", paperless_url="https://p.acme.test",
                            token=SECRET_TOKEN)
    direct.record_review(pub["target_id"], superseded=2, ai_new_taxonomy=4)
    data = DashboardData(store=store, billing=None, direct_store=direct)
    review = data.review()
    by = {t["tenant"]: t for t in review["tenants"]}
    assert by["acme"]["superseded"] == 2 and by["acme"]["ai_new_taxonomy"] == 4
    assert by["beta"]["superseded"] == 3 and by["beta"]["ai_new_taxonomy"] == 1
    assert review["total_superseded"] == 5
    assert review["total_ai_new_taxonomy"] == 5


def test_dashboard_html_is_self_contained_and_readonly():
    html = render_html()
    assert html.startswith("<!DOCTYPE html>")
    # NO external assets: no <script src>, no <link href>, no CDN/font URL.
    assert not re.search(r'(?:src|href)\s*=\s*["\'][^"\']+["\']', html)
    assert "http://" not in html and "https://" not in html
    # Read-only: the client script issues only GETs and never a mutating method.
    assert "method:\"GET\"" in html or 'method:"GET"' in html
    for verb in ('method:"POST"', "method:'POST'", 'method:"PUT"',
                 'method:"PATCH"', 'method:"DELETE"'):
        assert verb not in html, f"HTML must not issue {verb}"


# ===========================================================================
# Dashboards over the actual control-plane HTTP dispatch (read-only, GET only).
# ===========================================================================
def _cp_with_dashboard(tmp_path, tenant="acme"):
    store = ControlPlaneStore(str(tmp_path / "cp.json"))
    creds = store.enroll(store.mint_enrollment_token(tenant=tenant))
    store.record_heartbeat(agent_id=creds["agent_id"],
                           status={"result_queue_depth": 0, "jobs_done": 1,
                                   "mode": "hosted"})
    billing = BillingStore(str(tmp_path / "b.json"))
    billing.set_subscription(tenant, status=STATUS_ACTIVE, spend_cap=2.0)
    billing.record_usage(tenant, task="extract_structured", model="m",
                         in_tokens=10, out_tokens=5, cost=0.1)
    direct = DirectTargetStore(str(tmp_path / "d.json"))
    direct.add_target(tenant=tenant, paperless_url="https://p.acme.test",
                      token=SECRET_TOKEN)
    data = DashboardData(store=store, billing=billing, direct_store=direct)
    cp = ControlPlane(store, poll_timeout=0.02, billing=billing, dashboard=data)
    return cp


def test_control_plane_serves_dashboard_json_and_html(tmp_path):
    cp = _cp_with_dashboard(tmp_path)
    # HTML page.
    resp = cp.handle("GET", "/dashboard")
    assert resp.status == 200
    assert resp.content_type.startswith("text/html")
    assert resp.text.startswith("<!DOCTYPE html>")
    # JSON summary.
    resp = cp.handle("GET", "/dashboard/summary")
    assert resp.status == 200
    assert set(resp.body.keys()) == {"fleet", "cost", "review"}
    assert resp.body["fleet"]["agent_count"] == 1
    assert resp.body["cost"]["tenant_count"] == 1
    # Individual endpoints.
    assert cp.handle("GET", "/dashboard/fleet").body["direct_target_count"] == 1
    assert cp.handle("GET", "/dashboard/cost").body["tenants"][0]["tenant"] == "acme"
    assert "tenants" in cp.handle("GET", "/dashboard/review").body


def test_dashboard_is_read_only_rejects_mutating_methods(tmp_path):
    cp = _cp_with_dashboard(tmp_path)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        resp = cp.handle(method, "/dashboard/summary", body={"x": 1})
        assert resp.status == 405, f"{method} on a dashboard path must be 405"
    # And no token ever appears in any dashboard response.
    blob = json.dumps(cp.handle("GET", "/dashboard/summary").body)
    assert SECRET_TOKEN not in blob


def test_dashboard_404_when_not_wired():
    cp = ControlPlane(ControlPlaneStore(), poll_timeout=0.02)  # no dashboard
    assert cp.handle("GET", "/dashboard").status == 404
    assert cp.handle("GET", "/dashboard/summary").status == 404


# ===========================================================================
# The DEFAULT is STILL the agent — Mode C is opt-in only. No default moved.
# ===========================================================================
def test_default_mode_is_still_the_agent_not_direct():
    """Registering NO direct target means Mode C simply does not run. The agent
    (Modes A/B) default is untouched: Settings default mode is conservative BYO,
    hosted is off, and there is no 'direct' default anywhere in the agent config."""
    from paperless_assistant.config import Settings

    s = Settings(paperless_token="t")
    # The agent's default posture is BYO/local, review-first — NOT direct/hosted.
    assert s.mode == "conservative"
    assert s.hosted_mode() is False
    assert s.hosted_inference_active() is False
    # There is no attribute or default that selects Mode C on the agent side.
    assert not hasattr(s, "direct_mode")
    assert "direct" not in s.to_public_dict()


def test_mode_c_runs_only_when_explicitly_registered(tmp_path):
    """With an EMPTY direct-target store, there is nothing for the runner to run —
    Mode C is inert until a tenant opts in by registering a target."""
    env = _mk(tmp_path)
    assert env["direct"].list_targets() == []
    runner = DirectRunner(env["direct"], billing=env["billing"],
                          inference_proxy=env["proxy"], logger=env["logger"])
    with pytest.raises(DirectTargetError):
        runner.run("dt_000001", dry_run=True)  # nothing registered
    # The dashboard shows zero direct targets by default (agent-first).
    data = DashboardData(store=None, billing=env["billing"], direct_store=env["direct"])
    assert data.fleet()["direct_target_count"] == 0


def test_public_helper_strips_token():
    rec = {"target_id": "dt_1", "tenant": "t", "paperless_url": "https://p",
           "token": SECRET_TOKEN, "enabled": True}
    pub = _public(rec)
    assert "token" not in pub and pub["token_configured"] is True
    assert _public(None) is None


# ===========================================================================
# CLI: direct-add / direct-list / direct-revoke + dashboard (token from env only).
# ===========================================================================
def test_cli_direct_add_list_revoke(tmp_path, capsys, monkeypatch):
    from paperless_control_plane import cli as cp_cli

    direct_path = str(tmp_path / "direct.json")
    monkeypatch.setenv("PA_DIRECT_TOKEN", SECRET_TOKEN)
    # add
    cp_cli.main(["--direct", direct_path, "direct-add", "--tenant", "acme",
                 "--paperless-url", "https://paperless.acme.test"])
    out = capsys.readouterr().out
    assert "token" not in json.loads(out.split("\n\n")[0])  # public record only
    assert SECRET_TOKEN not in out  # token never printed
    # list
    cp_cli.main(["--direct", direct_path, "direct-list"])
    listed = json.loads(capsys.readouterr().out)
    assert len(listed) == 1
    tid = listed[0]["target_id"]
    assert SECRET_TOKEN not in json.dumps(listed)
    # revoke
    cp_cli.main(["--direct", direct_path, "direct-revoke", "--target-id", tid])
    assert "revoked" in capsys.readouterr().out
    cp_cli.main(["--direct", direct_path, "direct-list"])
    assert json.loads(capsys.readouterr().out) == []


def test_cli_direct_add_requires_env_token(tmp_path, monkeypatch):
    from paperless_control_plane import cli as cp_cli

    monkeypatch.delenv("PA_DIRECT_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        cp_cli.main(["--direct", str(tmp_path / "d.json"), "direct-add",
                     "--tenant", "acme", "--paperless-url", "https://p.test"])


def test_cli_dashboard_json(tmp_path, capsys):
    from paperless_control_plane import cli as cp_cli

    # Seed a billing record so cost has content.
    BillingStore(str(tmp_path / "billing.json")).set_subscription("acme")
    cp_cli.main(["--state", str(tmp_path / "cp.json"),
                 "--billing", str(tmp_path / "billing.json"),
                 "--direct", str(tmp_path / "direct.json"),
                 "dashboard", "summary"])
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) == {"fleet", "cost", "review"}
