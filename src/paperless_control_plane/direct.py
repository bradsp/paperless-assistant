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

"""Mode C — the optional DIRECT-CONNECTION escape hatch (Phase 7, connectivity §6).

    ┌──────────────────────────────────────────────────────────────────────┐
    │  Mode C runs NO agent.  It is for users who ALREADY publish Paperless  │
    │  (reverse proxy / Cloudflare Tunnel / Tailscale) and prefer the vendor │
    │  reach their instance directly rather than run a companion container.  │
    │  This is the ONE mode where the vendor holds the Paperless URL + token │
    │  and sees document contents (connectivity §1).  It is OPT-IN, clearly  │
    │  labelled "advanced", and NEVER the default — the agent (Modes A/B) is  │
    │  recommended everywhere.  It does NOT change the agent's outbound-only  │
    │  guarantee or any of the six §7 no-inbound points for Modes A/B.        │
    └──────────────────────────────────────────────────────────────────────┘

Two pieces live here:

  * DirectTargetStore — a per-tenant registry of direct targets:
        {paperless_url, token (SERVER-SIDE), allowed_hosts, enabled}
    with add / list / **revoke** (one-click delete of URL+token). The token is a
    REVERSIBLE secret the vendor must present to Paperless, so — unlike the agent
    credential — it CANNOT be hashed. It is stored access-controlled and is NEVER
    logged. Encryption-at-rest is REQUIRED in production (see the 1.0-readiness
    note); this prototype stores it in a mode-0600 JSON file and flags the gap.

  * DirectRunner — executes the SAME engine (Sweep: triage / metadata / re-OCR)
    against the remote Paperless using the stored token, with the AI step routed
    through the Phase-6 inference path (metered/capped per tenant). It REUSES the
    engine and the Phase-6 billing/inference — it does NOT fork the pipeline.

HARDENING (connectivity §6):
  * EGRESS ALLOW-LISTING — the runner ONLY connects to the tenant's approved
    Paperless host(s). A request to any other host is REFUSED before a socket is
    opened (SSRF / misconfig / swapped-URL guard). Enforced by an allow-listing
    requests session wrapper injected into the PaperlessClient.
  * ONE-CLICK REVOCATION — `revoke` immediately removes the stored URL + token and
    disables further direct runs for that target/tenant.
  * The direct token is NEVER logged and never leaves the control plane except in
    the Authorization header of the allow-listed call to the tenant's own Paperless.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from urllib.parse import urlsplit


def _now() -> float:
    return time.time()


class EgressNotAllowedError(RuntimeError):
    """The direct runner attempted to connect to a host that is NOT on the
    tenant's approved allow-list. Refused BEFORE any socket opens — the defence
    against SSRF, a misconfigured URL, or a swapped/hijacked target host."""

    def __init__(self, host: str, allowed: list[str]):
        self.host = host
        self.allowed = list(allowed)
        super().__init__(
            f"egress to host {host!r} is refused: not on the direct target's "
            f"approved allow-list {sorted(self.allowed)!r}. The direct runner only "
            f"connects to the tenant's registered Paperless host (connectivity §6)."
        )


class DirectTargetError(RuntimeError):
    """A direct target is missing, disabled, or misconfigured. Surfaced clearly so
    an operator sees exactly what is wrong (I6 spirit)."""


def _host_of(url: str) -> str:
    """The lowercased hostname of a URL (no port). Empty string if unparseable."""
    return (urlsplit(url).hostname or "").lower()


# ---------------------------------------------------------------------------
# Egress allow-listing session (SSRF / misconfig guard, connectivity §6)
# ---------------------------------------------------------------------------
class AllowListSession:
    """A thin wrapper over a real `requests.Session` that REFUSES any request whose
    URL host is not on the approved allow-list, BEFORE the underlying request runs.

    This is the structural egress guard: the direct runner's PaperlessClient is
    constructed with one of these, so every REST call the engine makes is checked.
    A swapped base URL, an injected redirect target, or an SSRF attempt to a
    metadata endpoint is refused here rather than dialled.
    """

    def __init__(self, allowed_hosts, *, session=None):
        # Normalise to a lowercase set for O(1), case-insensitive checks.
        self.allowed_hosts = {str(h).lower() for h in allowed_hosts if h}
        if session is not None:
            self._session = session
        else:  # pragma: no cover - exercised only in real (non-test) operation
            import requests

            self._session = requests.Session()
        # Mirror the headers attribute PaperlessClient expects to update.
        self.headers = self._session.headers

    def _check(self, url: str) -> None:
        host = _host_of(url)
        if host not in self.allowed_hosts:
            raise EgressNotAllowedError(host, sorted(self.allowed_hosts))

    def request(self, method, url, **kw):
        self._check(url)
        # SSRF defence-in-depth: never auto-follow a redirect — a 30x to a
        # non-approved host would otherwise be dialled by the underlying session
        # WITHOUT re-checking the allow-list. The engine only ever calls explicit
        # Paperless URLs, so redirect-following is unnecessary here. The `session`
        # kwarg is honoured for tests; a caller may override if it truly needs it.
        kw.setdefault("allow_redirects", False)
        return self._session.request(method, url, **kw)

    def post(self, url, **kw):
        self._check(url)
        kw.setdefault("allow_redirects", False)
        return self._session.post(url, **kw)

    def close(self):  # pragma: no cover - cleanup convenience
        try:
            self._session.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Direct-target store (per-tenant registry; token stored server-side)
# ---------------------------------------------------------------------------
class DirectTargetStore:
    """Per-tenant registry of Mode C direct targets. Thread-safe; optionally
    file-backed (mode-0600) so targets survive a control-plane restart.

    A target record:
        {
          "target_id", "tenant", "paperless_url", "allowed_hosts": [...],
          "enabled": bool, "created_at", "updated_at",
          "token": "<reversible secret, SERVER-SIDE, NEVER logged>",
        }

    SECURITY: `token` is a REVERSIBLE secret (the vendor must present it to
    Paperless), so it is NOT hashed. It is stored access-controlled and NEVER
    logged. `public_target()` / `list_targets()` strip it. Encryption-at-rest is
    required in production (1.0-readiness note) — this in-repo store flags the gap
    and restricts the file to owner-only permissions as a floor.
    """

    def __init__(self, path: str | pathlib.Path | None = None, *, now=_now):
        self._lock = threading.RLock()
        self._path = pathlib.Path(path) if path else None
        self._now = now
        # target_id -> record (with token). Keyed by target_id; a tenant may have
        # more than one target (e.g. two published instances).
        self._targets: dict[str, dict] = {}
        self._seq = 0
        self._load()

    # -- persistence (optional, owner-only file) --------------------------
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        self._targets = data.get("targets", {})
        self._seq = int(data.get("seq", len(self._targets)))

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"targets": self._targets, "seq": self._seq}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        # Restrict to owner-only BEFORE the file carries a token. On POSIX this is
        # 0600; on Windows chmod is a best-effort no-op (documented; use ACLs / an
        # encrypted volume there). Encryption-at-rest is still REQUIRED in prod.
        try:
            os.chmod(tmp, 0o600)
        except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
            pass
        tmp.replace(self._path)

    # -- admin: add / list / get / revoke ---------------------------------
    def add_target(self, *, tenant: str, paperless_url: str, token: str,
                   allowed_hosts: list[str] | None = None,
                   enabled: bool = True) -> dict:
        """Register a direct target for a tenant. `token` is the user-provided,
        service-user-scoped Paperless token stored SERVER-SIDE (never logged).

        `allowed_hosts` defaults to the host of `paperless_url` — the egress guard
        then permits ONLY that host. Passing extra hosts is allowed (e.g. an
        apex + www) but is an explicit widening the operator opts into.

        Returns the PUBLIC record (token stripped)."""
        if not paperless_url:
            raise DirectTargetError("paperless_url is required")
        if not token:
            raise DirectTargetError(
                "a Paperless token is required (scope it to a service user, not "
                "admin — connectivity §6)")
        url = paperless_url.rstrip("/")
        host = _host_of(url)
        if not host:
            raise DirectTargetError(f"paperless_url {paperless_url!r} has no host")
        hosts = [h.lower() for h in (allowed_hosts or [host]) if h]
        if host not in hosts:
            # The target's own host must always be reachable.
            hosts.append(host)
        with self._lock:
            self._seq += 1
            target_id = f"dt_{self._seq:06d}"
            rec = {
                "target_id": target_id,
                "tenant": tenant,
                "paperless_url": url,
                "allowed_hosts": sorted(set(hosts)),
                "enabled": bool(enabled),
                "created_at": self._now(),
                "updated_at": self._now(),
                "token": token,  # SERVER-SIDE secret; stripped from public views
            }
            self._targets[target_id] = rec
            self._save()
            return _public(rec)

    def get_target(self, target_id: str) -> dict | None:
        """Return the FULL record (INCLUDING the token) for the runner. Callers
        must never log this. Returns None if unknown."""
        with self._lock:
            rec = self._targets.get(target_id)
            return dict(rec) if rec else None

    def public_target(self, target_id: str) -> dict | None:
        with self._lock:
            rec = self._targets.get(target_id)
            return _public(rec) if rec else None

    def list_targets(self, tenant: str | None = None) -> list[dict]:
        """PUBLIC list (tokens stripped). Optionally filtered to one tenant."""
        with self._lock:
            recs = self._targets.values()
            return [_public(r) for r in recs
                    if tenant is None or r["tenant"] == tenant]

    def set_enabled(self, target_id: str, enabled: bool) -> bool:
        with self._lock:
            rec = self._targets.get(target_id)
            if rec is None:
                return False
            rec["enabled"] = bool(enabled)
            rec["updated_at"] = self._now()
            self._save()
            return True

    def revoke(self, target_id: str) -> bool:
        """ONE-CLICK REVOCATION (connectivity §6): immediately remove the stored
        URL + token and disable further direct runs for this target. Idempotent —
        revoking an unknown/already-revoked target is a harmless no-op returning
        False. The token is DELETED from memory and (on next save) from disk; it
        is never logged in the process.

        Returns True iff a target was removed."""
        with self._lock:
            rec = self._targets.pop(target_id, None)
            if rec is None:
                return False
            # Best-effort scrub of the secret in the popped dict before it is GC'd.
            if "token" in rec:
                rec["token"] = ""
            self._save()
            return True

    def record_review(self, target_id: str, *, superseded: int,
                      ai_new_taxonomy: int) -> bool:
        """Record the LAST run's review-gate counts on a target so the read-only
        dashboard can surface what needs human attention (I4). Content-free —
        counts only. Returns False if the target is unknown."""
        with self._lock:
            rec = self._targets.get(target_id)
            if rec is None:
                return False
            rec["last_review"] = {
                "superseded": int(superseded),
                "ai_new_taxonomy": int(ai_new_taxonomy),
                "at": self._now(),
            }
            self._save()
            return True

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted({r["tenant"] for r in self._targets.values()})


def _public(rec: dict | None) -> dict | None:
    """A copy of a target record with the token stripped — safe to log / return /
    render in a dashboard. Adds `token_configured` so surfaces can show presence
    without the value."""
    if rec is None:
        return None
    out = {k: v for k, v in rec.items() if k != "token"}
    out["token_configured"] = bool(rec.get("token"))
    return out


# ---------------------------------------------------------------------------
# In-process inference transport (server-side Mode C AI path)
# ---------------------------------------------------------------------------
class _InProcessInferenceTransport:
    """Routes the engine's HostedProvider inference calls straight into the
    control-plane InferenceProxy IN-PROCESS (no socket), so the Mode C AI step is
    metered/capped through the SAME Phase-6 path as a hosted agent — without
    forking the billing/inference logic.

    It speaks the tiny Transport interface (`request(method, path, headers, body)`)
    the HostedProvider already uses. Only POST /agent/inference is handled; the
    proxy performs entitlement + spend-cap checks in the mandated order and meters
    usage to the tenant. Contents transit here ONLY for the model call and are
    never persisted (§5)."""

    PATH_INFERENCE = "/agent/inference"

    def __init__(self, billing, inference_proxy, tenant: str, *, logger=None):
        self.billing = billing
        self.proxy = inference_proxy
        self.tenant = tenant
        self.logger = logger

    def request(self, method, path, *, headers=None, body=None, timeout=None):
        if method.upper() != "POST" or path.rstrip("/") != self.PATH_INFERENCE:
            return 404, {"error": "not found", "path": path}
        # Mirror ControlPlane._inference's mandated check order (auth is implicit —
        # this transport is only ever constructed for an already-resolved tenant on
        # the SERVER side, never exposed to an untrusted caller).
        from .billing import EntitlementError, SpendCapError
        from .inference import InferenceError, UnpricedModelError

        try:
            self.billing.check_entitled(self.tenant)
            self.billing.check_spend_cap(self.tenant)
        except EntitlementError as e:
            self._log_refusal("unentitled")
            return 402, {"error": str(e), "reason": e.reason}
        except SpendCapError as e:
            self._log_refusal("spend_cap")
            return 429, {"error": str(e), "reason": e.reason}

        request = (body or {}).get("request") or {}
        try:
            result = self.proxy.run(self.tenant, request)
        except UnpricedModelError as e:
            self._log_refusal("unpriced_model")
            return 500, {"error": str(e), "reason": "unpriced_model"}
        except InferenceError as e:
            return 400, {"error": str(e), "reason": "inference_error"}
        return 200, {"result": result}

    def _log_refusal(self, reason):
        if self.logger is not None:
            self.logger.event("direct_inference_refused", level="warning",
                              tenant=self.tenant, reason=reason)

    def close(self):  # pragma: no cover - interface completeness
        pass


# ---------------------------------------------------------------------------
# Direct runner — the SAME engine against a remote Paperless (Mode C)
# ---------------------------------------------------------------------------
class DirectRunner:
    """Executes the engine (Sweep) against a tenant's remote Paperless via a stored
    direct target, with the AI step metered/capped through the Phase-6 inference
    path. REUSES the engine and Phase-6 billing/inference — no fork.

    Construction is cheap; `run(target_id, ...)` does the work:
      1. Load the target (must exist + be enabled).
      2. Build an EGRESS-ALLOW-LISTED PaperlessClient pointed at the remote URL
         with the stored token.
      3. Build a Sweep whose Config carries a HostedInferenceContext wired to an
         in-process InferenceProxy transport (metered per tenant).
      4. Run one sweep tick; return the report + a content-free summary.

    NEVER logs the token. Egress is refused for any host outside the allow-list.
    """

    def __init__(self, target_store: DirectTargetStore, *, billing=None,
                 inference_proxy=None, logger=None, session_factory=None,
                 data_dir=None):
        self.targets = target_store
        self.billing = billing
        self.inference_proxy = inference_proxy
        self.logger = logger
        # `session_factory(allowed_hosts) -> AllowListSession` lets tests inject a
        # fake remote Paperless behind the SAME egress guard. Default builds a real
        # allow-listed requests session.
        self._session_factory = session_factory or (
            lambda allowed: AllowListSession(allowed))
        # Where the engine writes durable state (snapshots/reports/logs). Mode C is
        # server-side, so this is a control-plane-side scratch dir per run.
        self._data_dir = data_dir

    def run(self, target_id: str, *, limit: int | None = None,
            dry_run: bool | None = None, settings_overrides: dict | None = None):
        """Run one engine tick against the target's remote Paperless. Returns a
        dict: {target_id, tenant, dry_run, counts, spend_usd, report}. Raises
        DirectTargetError (missing/disabled) or EgressNotAllowedError (bad host)."""
        rec = self.targets.get_target(target_id)
        if rec is None:
            raise DirectTargetError(f"no such direct target {target_id!r}")
        if not rec.get("enabled"):
            raise DirectTargetError(
                f"direct target {target_id!r} is disabled; enable it or register a "
                f"new one before running (it may have been revoked).")
        tenant = rec["tenant"]
        url = rec["paperless_url"]
        token = rec["token"]  # SERVER-SIDE secret; NEVER logged
        allowed = rec.get("allowed_hosts") or [_host_of(url)]

        # Log the RUN with routing metadata only — never the token (connectivity §6).
        if self.logger is not None:
            self.logger.event("direct_run_start", tenant=tenant,
                              target_id=target_id, paperless_host=_host_of(url))

        settings = self._build_settings(tenant, url, token, dry_run, limit,
                                        settings_overrides)
        client = self._build_client(url, token, allowed)
        cfg = self._build_cfg(settings, tenant)

        from paperless_assistant.sweep import Sweep

        sweep = Sweep(settings, client=client, cfg=cfg, logger=self._engine_logger())
        report = sweep.run_once(limit=limit)

        superseded = len(report.all_superseded())
        ai_new_taxonomy = len(report.all_new_taxonomy())
        # Record the review-gate counts on the target so the read-only dashboard
        # surfaces what needs human attention (I4). Content-free — counts only.
        self.targets.record_review(target_id, superseded=superseded,
                                   ai_new_taxonomy=ai_new_taxonomy)

        summary = {
            "target_id": target_id,
            "tenant": tenant,
            "paperless_host": _host_of(url),
            "dry_run": report.dry_run,
            "counts": report.merged_counts(),
            "spend_usd": round(report.total_spend(), 6),
            "review": {"superseded": superseded, "ai_new_taxonomy": ai_new_taxonomy},
            "report": report,
        }
        if self.logger is not None:
            self.logger.event("direct_run_end", tenant=tenant, target_id=target_id,
                              counts=summary["counts"], spend_usd=summary["spend_usd"])
        return summary

    # -- wiring helpers ---------------------------------------------------
    def _build_settings(self, tenant, url, token, dry_run, limit, overrides):
        from paperless_assistant.config import Settings, SpendCaps

        # Mode C uses the SAME safe defaults as the agent: triage + metadata ON,
        # re-OCR OFF, reuse-first taxonomy, low spend caps. Overrides let an
        # operator adjust per run without changing any default.
        s = Settings(
            base_url=url,
            paperless_token=token,
            data_dir=str(self._run_data_dir(tenant)),
            mode="direct",  # a non-agent label; the engine treats it like BYO wiring
            dry_run=dry_run,
            spend=SpendCaps(per_run=1.00, per_period=5.00),
        )
        if overrides:
            for k, v in overrides.items():
                if v is None:
                    continue
                if hasattr(s, k):
                    setattr(s, k, v)
        if limit is not None:
            s.limit = int(limit)
        return s

    def _build_client(self, url, token, allowed_hosts):
        """A PaperlessClient whose session refuses egress outside `allowed_hosts`
        (SSRF / misconfig guard). Same client the engine uses everywhere else."""
        from paperless_assistant.client import PaperlessClient

        session = self._session_factory(allowed_hosts)
        return PaperlessClient(url, token, session=session)

    def _build_cfg(self, settings, tenant):
        """Project settings to a Config, wiring the Phase-6 inference path when the
        billing + proxy are configured. When they are NOT (e.g. an offline probe),
        the engine falls back to BYO wiring exactly as before — but Mode C's whole
        point is metered hosted inference, so the CLI wires them."""
        cfg = settings.to_config()
        if self.billing is not None and self.inference_proxy is not None:
            from paperless_assistant.config import HostedInferenceContext

            transport = _InProcessInferenceTransport(
                self.billing, self.inference_proxy, tenant, logger=self.logger)

            def auth_headers():
                # No agent credential in Mode C — the tenant is already resolved
                # server-side. The in-process transport ignores auth headers.
                return {}

            cfg.hosted_inference = HostedInferenceContext(
                transport=transport, auth_headers=auth_headers)
        return cfg

    def _run_data_dir(self, tenant):
        base = pathlib.Path(self._data_dir) if self._data_dir else pathlib.Path(
            os.environ.get("PA_CP_DIRECT_DATA", "./control-plane-direct-data"))
        return base / tenant

    def _engine_logger(self):
        # Reuse the control plane's logger if provided; the engine logs routing +
        # per-doc outcome metadata (no token). A None here lets Sweep default.
        return self.logger
