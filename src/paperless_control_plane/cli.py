# Paperless Assistant - an AI companion for Paperless-NGX.
# Copyright (C) 2026 BP Technology Advisors LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""`pa-control-plane` — minimal admin CLI for the vendor control plane (Phase 5).

Subcommands (thin prototype gateway — NOT the finished SaaS):

  serve         run the HTTP control plane (agents dial OUT to it)
  mint-token    issue a one-time enrollment token for a tenant (give it to an agent
                as PA_ENROLLMENT_TOKEN)
  enqueue       push a job ("process_document N" / "run_sweep") to an agent
  revoke        revoke an agent credential server-side (§4.2)

State persists to a JSON file under --state so a control-plane restart keeps agents
and queued jobs (cheap; in-memory would also satisfy the prototype). `serve` and
the offline `enqueue`/`mint-token` operate on the SAME state file, so an operator
mints a token, starts the server, enrolls an agent, then enqueues a job for it.

Enqueue/mint/revoke here act directly on the shared state store (an operator's
local admin action). Over HTTP, POST /admin/enqueue is also available (guarded by
--admin-token) for a remote admin.
"""
from __future__ import annotations

import argparse
import json
import os

from .store import ControlPlaneStore
from .app import ControlPlane
from .server import ControlPlaneServer
from .billing import BillingStore, STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_CANCELED
from .direct import DirectTargetStore, DirectRunner, DirectTargetError, EgressNotAllowedError
from .dashboard import DashboardData

DEFAULT_STATE = os.environ.get("PA_CP_STATE", "./control-plane-state.json")
DEFAULT_BILLING = os.environ.get("PA_CP_BILLING", "./control-plane-billing.json")
DEFAULT_DIRECT = os.environ.get("PA_CP_DIRECT", "./control-plane-direct.json")


def _store(args) -> ControlPlaneStore:
    return ControlPlaneStore(args.state)


def _billing(args) -> BillingStore:
    return BillingStore(getattr(args, "billing", None) or DEFAULT_BILLING)


def _direct(args) -> DirectTargetStore:
    return DirectTargetStore(getattr(args, "direct", None) or DEFAULT_DIRECT)


def _build_inference_proxy(billing, logger=None):
    """Build the hosted-inference proxy from the SERVER-SIDE vendor key, if set.

    The vendor's model key comes from server-side env ONLY (PA_VENDOR_ANTHROPIC_KEY),
    never YAML, never the agent. Returns None when no vendor key is configured, in
    which case hosted inference is simply unavailable (the endpoint replies 501)."""
    from .inference import AnthropicModelBackend, InferenceProxy

    vendor_key = os.environ.get("PA_VENDOR_ANTHROPIC_KEY", "")
    if not vendor_key:
        return None
    backend = AnthropicModelBackend(
        api_key=vendor_key,
        ocr_model=os.environ.get("PA_VENDOR_OCR_MODEL", "claude-opus-4-8"),
        metadata_model=os.environ.get("PA_VENDOR_METADATA_MODEL", "claude-sonnet-4-6"),
    )
    return InferenceProxy(backend, billing, logger=logger)


def cmd_serve(args):
    from paperless_assistant.obs import JsonLogger

    store = _store(args)
    billing = _billing(args)
    direct = _direct(args)
    logger = JsonLogger(path=None)  # stderr; content-free routing/usage only
    proxy = _build_inference_proxy(billing, logger=logger)
    # Phase 7: read-only dashboards, assembled from existing stores + direct targets.
    dashboard = DashboardData(store=store, billing=billing, direct_store=direct)
    cp = ControlPlane(store, poll_timeout=args.poll_timeout,
                      admin_token=os.environ.get("PA_CP_ADMIN_TOKEN", ""),
                      billing=billing, inference_proxy=proxy, logger=logger,
                      dashboard=dashboard)
    server = ControlPlaneServer(cp, host=args.host, port=args.port)
    print(f"pa-control-plane: listening on http://{args.host}:{args.port} "
          f"(agents dial OUT to this; it never dials in). state={args.state}")
    inf = "ENABLED (vendor key set)" if proxy is not None else "disabled (no vendor key)"
    print(f"  hosted inference: {inf}; billing state="
          f"{getattr(args, 'billing', None) or DEFAULT_BILLING}")
    print("  endpoints: POST /agent/enroll  GET /agent/work  POST /agent/results  "
          "POST /agent/heartbeat  POST /agent/inference  POST /admin/enqueue")
    print(f"  dashboards (READ-ONLY): GET /dashboard  GET /dashboard/summary  "
          f"/dashboard/fleet  /dashboard/cost  /dashboard/review")
    print("  Mode C direct targets: ADVANCED / opt-in — the local agent is the "
          "recommended default. Register with `direct-add`.")
    server.serve_forever()


def cmd_mint_token(args):
    store = _store(args)
    token = store.mint_enrollment_token(tenant=args.tenant, agent_hint=args.hint)
    print(token)
    print(f"\nGive this to the agent as PA_ENROLLMENT_TOKEN (env only, one-time). "
          f"tenant={args.tenant}", flush=True)


def cmd_enqueue(args):
    store = _store(args)
    payload = json.loads(args.payload) if args.payload else {}
    if args.document_id is not None:
        payload.setdefault("document_id", args.document_id)
    job = store.enqueue(tenant=args.tenant, agent_id=args.agent_id,
                        job_type=args.type, payload=payload)
    print(json.dumps(job, indent=2))


def cmd_revoke(args):
    store = _store(args)
    ok = store.revoke(args.agent_id)
    print(f"revoked {args.agent_id}" if ok else f"no such agent {args.agent_id}")


def cmd_subscribe(args):
    """Create/update a tenant's subscription + server-side spend cap (billing stub;
    NO real payment processor). In production a payment provider would drive this."""
    billing = _billing(args)
    if args.status:
        if billing.subscription(args.tenant) is None and args.status != STATUS_ACTIVE:
            # Creating straight into suspended/canceled is allowed but note the cap.
            billing.set_subscription(args.tenant, status=args.status,
                                     spend_cap=args.spend_cap)
        else:
            billing.set_subscription(args.tenant, status=args.status,
                                     spend_cap=args.spend_cap)
    else:
        billing.set_subscription(args.tenant, spend_cap=args.spend_cap)
    print(json.dumps(billing.subscription(args.tenant), indent=2))


def cmd_usage(args):
    """Show a tenant's metered usage (CLI/JSON — usage is VISIBLE, §9). Content-
    free: counts, tokens, cost, cap, and subscription posture only."""
    billing = _billing(args)
    summary = billing.usage_summary(args.tenant)
    print(json.dumps(summary, indent=2))


def cmd_usage_reset(args):
    """Reset a tenant's usage ledger (e.g. a new billing period). Billing stub."""
    billing = _billing(args)
    billing.reset_usage(args.tenant)
    print(f"usage reset for tenant {args.tenant}")


# -- Phase 7: Mode C direct-connection (ADVANCED / opt-in, NEVER default) -----
def cmd_direct_add(args):
    """Register a Mode C direct target (ADVANCED — for users who ALREADY expose
    Paperless). This is the ONE mode where the vendor holds the Paperless token
    and sees contents; the local AGENT is recommended everywhere else.

    The token is read from PA_DIRECT_TOKEN (env ONLY — never a CLI flag, never
    YAML, never logged), scoped to a Paperless service user (not admin)."""
    token = os.environ.get("PA_DIRECT_TOKEN", "")
    if not token:
        raise SystemExit(
            "Set PA_DIRECT_TOKEN in the environment (a Paperless token scoped to a "
            "SERVICE USER, not admin). It is a reversible secret the vendor stores "
            "server-side; it must NEVER be passed as a CLI flag or put in YAML.")
    direct = _direct(args)
    allowed = None
    if args.allowed_host:
        allowed = list(args.allowed_host)
    try:
        rec = direct.add_target(
            tenant=args.tenant, paperless_url=args.paperless_url, token=token,
            allowed_hosts=allowed, enabled=not args.disabled)
    except DirectTargetError as e:
        raise SystemExit(str(e))
    print(json.dumps(rec, indent=2))   # token stripped by DirectTargetStore
    print("\nMode C is ADVANCED / opt-in. The local agent (Modes A/B) is the "
          "recommended default. Prefer Tailscale / Cloudflare Tunnel over a raw "
          "public reverse proxy; scope the token to a service user; revoke with "
          "`direct-revoke` (one click removes the URL + token).", flush=True)


def cmd_direct_list(args):
    """List registered direct targets (tokens are NEVER shown)."""
    direct = _direct(args)
    tenant = getattr(args, "tenant", None)
    print(json.dumps(direct.list_targets(tenant), indent=2))


def cmd_direct_revoke(args):
    """ONE-CLICK REVOCATION: immediately remove a direct target's stored URL +
    token and disable further direct runs for it (connectivity §6)."""
    direct = _direct(args)
    ok = direct.revoke(args.target_id)
    if ok:
        print(f"revoked {args.target_id}: URL + token removed; no further direct "
              f"runs for this target.")
    else:
        print(f"no such direct target {args.target_id} (already revoked?)")


def cmd_direct_run(args):
    """Run the engine ONCE against a direct target's remote Paperless (Mode C).
    The AI step is metered/capped through the Phase-6 inference path. Egress is
    allow-listed to the target's approved host; a bad host is refused."""
    direct = _direct(args)
    billing = _billing(args)
    from paperless_assistant.obs import JsonLogger

    logger = JsonLogger(path=None)
    proxy = _build_inference_proxy(billing, logger=logger)
    if proxy is None:
        print("WARNING: no vendor key set (PA_VENDOR_ANTHROPIC_KEY); the AI step "
              "cannot run metered. Set it to exercise Mode C inference.", flush=True)
    runner = DirectRunner(direct, billing=billing, inference_proxy=proxy,
                          logger=logger)
    try:
        summary = runner.run(args.target_id, limit=args.limit,
                             dry_run=(True if args.dry_run else None))
    except (DirectTargetError, EgressNotAllowedError) as e:
        raise SystemExit(str(e))
    summary.pop("report", None)   # keep CLI output content-free + JSON-serialisable
    print(json.dumps(summary, indent=2))


def cmd_dashboard(args):
    """Print a read-only dashboard payload (fleet / cost / review / summary) as
    JSON. The self-contained HTML view is served at GET /dashboard by `serve`."""
    store = _store(args)
    billing = _billing(args)
    direct = _direct(args)
    data = DashboardData(store=store, billing=billing, direct_store=direct)
    view = {
        "fleet": data.fleet, "cost": data.cost,
        "review": data.review, "summary": data.summary,
    }[args.view]
    print(json.dumps(view(), indent=2, default=str))


def build_parser():
    ap = argparse.ArgumentParser(
        prog="pa-control-plane",
        description="Minimal vendor control plane for the outbound-only agent "
                    "protocol (Phase 5 prototype gateway).")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help="path to the JSON state file (agents + job queue)")
    ap.add_argument("--billing", default=DEFAULT_BILLING,
                    help="path to the JSON billing state (subscriptions + usage ledger)")
    ap.add_argument("--direct", default=DEFAULT_DIRECT,
                    help="path to the JSON direct-target state (Mode C, token stored "
                         "server-side, owner-only file; encrypt at rest in prod)")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the HTTP control plane")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--poll-timeout", type=float, default=25.0,
                   help="seconds a /agent/work long-poll parks before 204")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("mint-token", help="issue a one-time enrollment token")
    p.add_argument("--tenant", default="t-default")
    p.add_argument("--hint", default=None, help="optional human label for the agent")
    p.set_defaults(func=cmd_mint_token)

    p = sub.add_parser("enqueue", help="push a job to an agent")
    p.add_argument("--tenant", default="t-default")
    p.add_argument("--agent-id", required=True)
    p.add_argument("--type", required=True,
                   choices=["process_document", "run_sweep"],
                   help="job type")
    p.add_argument("--document-id", type=int, default=None,
                   help="for process_document: the Paperless document id")
    p.add_argument("--payload", default=None, help="extra JSON payload")
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("revoke", help="revoke an agent credential (server-side)")
    p.add_argument("--agent-id", required=True)
    p.set_defaults(func=cmd_revoke)

    # -- Phase 6 billing seam (stub; NO real payment processor) ------------
    p = sub.add_parser("subscribe",
                       help="create/update a tenant subscription + spend cap (stub)")
    p.add_argument("--tenant", default="t-default")
    p.add_argument("--status", default=None,
                   choices=[STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_CANCELED],
                   help="subscription status (default: active on create)")
    p.add_argument("--spend-cap", type=float, default=5.0,
                   help="server-side per-tenant USD spend cap (0 = unlimited)")
    p.set_defaults(func=cmd_subscribe)

    p = sub.add_parser("usage", help="show a tenant's metered usage (JSON)")
    p.add_argument("--tenant", default="t-default")
    p.set_defaults(func=cmd_usage)

    p = sub.add_parser("usage-reset", help="reset a tenant's usage ledger (new period)")
    p.add_argument("--tenant", default="t-default")
    p.set_defaults(func=cmd_usage_reset)

    # -- Phase 7: Mode C direct-connection (ADVANCED / opt-in, NEVER default) --
    p = sub.add_parser(
        "direct-add",
        help="[ADVANCED/opt-in] register a Mode C direct target (vendor reaches "
             "the user's PUBLISHED Paperless directly; the AGENT is recommended)")
    p.add_argument("--tenant", default="t-default")
    p.add_argument("--paperless-url", required=True,
                   help="the user's PUBLISHED Paperless URL (RP / Tunnel / Tailscale)")
    p.add_argument("--allowed-host", action="append", default=None,
                   help="host to allow egress to (repeatable; defaults to the "
                        "URL's host). Any other host is REFUSED.")
    p.add_argument("--disabled", action="store_true",
                   help="register the target disabled (enable later)")
    p.set_defaults(func=cmd_direct_add)

    p = sub.add_parser("direct-list",
                       help="list registered direct targets (tokens never shown)")
    p.add_argument("--tenant", default=None, help="filter to one tenant")
    p.set_defaults(func=cmd_direct_list)

    p = sub.add_parser("direct-revoke",
                       help="ONE-CLICK revoke a direct target (removes URL + token)")
    p.add_argument("--target-id", required=True)
    p.set_defaults(func=cmd_direct_revoke)

    p = sub.add_parser("direct-run",
                       help="run the engine once against a direct target (Mode C)")
    p.add_argument("--target-id", required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_direct_run)

    # -- Phase 7: read-only dashboards ----------------------------------------
    p = sub.add_parser("dashboard",
                       help="print a read-only dashboard payload as JSON")
    p.add_argument("view", choices=["fleet", "cost", "review", "summary"],
                   nargs="?", default="summary")
    p.set_defaults(func=cmd_dashboard)

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
