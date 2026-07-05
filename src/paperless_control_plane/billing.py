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

"""Billing seam for hosted inference (Phase 6, r4) — VENDOR side.

Everything commercial lives HERE, in the control plane, behind a clean seam. The
engine and the agent's `HostedProvider` stay billing-agnostic: they only ever see
"a provider" (§8.5). Three concerns, one lock-guarded store:

  * SubscriptionStore — per-tenant entitlement (active / suspended / over-quota).
    A STUB/in-memory model of what a real billing provider would own. NO external
    payment processor, NO outbound billing calls — a real provider could replace
    this class behind the same tiny interface later (that is the whole point of the
    seam). See <fences>: do NOT integrate Stripe et al.
  * UsageLedger — an append-only, per-tenant record of metered inference (tokens,
    cost, task, timestamp, model). Queryable (CLI/JSON) so usage is VISIBLE (§9
    deliverable). Every record is tagged by tenant (no cross-tenant mixing, §8.5).
  * spend cap — a server-side per-tenant USD ceiling that HALTS inference once
    cumulative metered spend reaches it. This is the vendor-side backstop that
    complements the agent-side SpendGovernor (which still runs agent-side).

PRIVACY (connectivity §5): NOTHING here stores document contents or prompts. A
usage record carries only routing/metadata + token counts + cost. The ledger is
safe to persist and to print.

Persistence: in-memory by default (fine for the prototype). An optional JSON path
lets subscriptions + the ledger survive a control-plane restart — cheap, so we
include it, mirroring ControlPlaneStore.
"""
from __future__ import annotations

import json
import pathlib
import threading
import time


def _now() -> float:
    return time.time()


# Subscription states (the stub's entitlement model).
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"       # e.g. payment failed / manually paused
STATUS_CANCELED = "canceled"

_ENTITLED = frozenset({STATUS_ACTIVE})


class EntitlementError(RuntimeError):
    """Tenant is not entitled to hosted inference (no active subscription, or
    suspended/canceled). The proxy turns this into a clear structured refusal."""

    reason = "unentitled"


class SpendCapError(RuntimeError):
    """Tenant's server-side spend cap is reached; inference is refused until the
    cap is raised or the ledger reset. Complements the agent-side SpendGovernor."""

    reason = "spend_cap"


class BillingStore:
    """The vendor-side billing seam: subscriptions + usage ledger + spend caps.

    Thread-safe (a single lock); optionally file-backed. A tenant that has never
    been provisioned is treated as NOT entitled (fail-closed) — you must create a
    subscription for a tenant before it can use hosted inference.
    """

    def __init__(self, path: str | pathlib.Path | None = None, *, now=_now):
        self._lock = threading.RLock()
        self._path = pathlib.Path(path) if path else None
        self._now = now
        # tenant -> {"status", "spend_cap", "created_at", "updated_at"}
        self._subs: dict[str, dict] = {}
        # tenant -> list[usage record]  (append-only; each tagged by tenant)
        self._ledger: dict[str, list] = {}
        self._load()

    # -- persistence (optional) -------------------------------------------
    def _load(self) -> None:
        if not self._path or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        self._subs = data.get("subscriptions", {})
        self._ledger = data.get("ledger", {})

    def _save(self) -> None:
        if not self._path:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"subscriptions": self._subs, "ledger": self._ledger}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # -- subscription admin (stub for a real billing provider) ------------
    def set_subscription(self, tenant: str, *, status: str = STATUS_ACTIVE,
                         spend_cap: float = 5.0) -> dict:
        """Create or update a tenant's subscription (admin/stub action). In a real
        system this state would be driven by a payment provider's webhooks; here an
        operator sets it directly. `spend_cap` is the per-tenant server-side USD
        ceiling (0 = unlimited, discouraged)."""
        with self._lock:
            rec = self._subs.get(tenant, {"created_at": self._now()})
            rec.update({
                "tenant": tenant,
                "status": status,
                "spend_cap": float(spend_cap),
                "updated_at": self._now(),
            })
            self._subs[tenant] = rec
            self._save()
            return dict(rec)

    def set_status(self, tenant: str, status: str) -> bool:
        """Suspend/reactivate/cancel a tenant. Returns True if the tenant existed."""
        with self._lock:
            rec = self._subs.get(tenant)
            if rec is None:
                return False
            rec["status"] = status
            rec["updated_at"] = self._now()
            self._save()
            return True

    def subscription(self, tenant: str) -> dict | None:
        with self._lock:
            rec = self._subs.get(tenant)
            return dict(rec) if rec else None

    # -- entitlement + cap checks (the order the proxy enforces) ----------
    def check_entitled(self, tenant: str) -> None:
        """Raise EntitlementError unless the tenant has an ACTIVE subscription.
        Fail-closed: an unknown tenant is not entitled."""
        with self._lock:
            rec = self._subs.get(tenant)
        if rec is None:
            raise EntitlementError(
                f"tenant {tenant!r} has no subscription; hosted inference is not "
                f"available. (BYO-key/local remains available with zero egress.)"
            )
        if rec.get("status") not in _ENTITLED:
            raise EntitlementError(
                f"tenant {tenant!r} subscription is '{rec.get('status')}', not "
                f"active; hosted inference is refused until it is reactivated."
            )

    def check_spend_cap(self, tenant: str) -> None:
        """Raise SpendCapError if the tenant's cumulative metered spend has reached
        its server-side cap. A cap of 0 means unlimited (no halt)."""
        with self._lock:
            rec = self._subs.get(tenant) or {}
            cap = float(rec.get("spend_cap") or 0.0)
            spent = self._spend_total(tenant)
        if cap > 0 and spent >= cap:
            raise SpendCapError(
                f"tenant {tenant!r} has reached its server-side spend cap "
                f"(${spent:.4f} >= ${cap:.2f}); inference is halted until the cap "
                f"is raised or usage reset."
            )

    # -- metering ---------------------------------------------------------
    def record_usage(self, tenant: str, *, task: str, model: str,
                     in_tokens: int, out_tokens: int, cost: float) -> dict:
        """Append a metered-usage record for the tenant (tokens/cost/task/model +
        timestamp). Content-free by construction — NO prompt, NO document text is
        ever passed in or stored here (§5)."""
        record = {
            "ts": self._now(),
            "tenant": tenant,
            "task": task,
            "model": model,
            "in_tokens": int(in_tokens),
            "out_tokens": int(out_tokens),
            "cost": round(float(cost), 6),
        }
        with self._lock:
            self._ledger.setdefault(tenant, []).append(record)
            self._save()
        return dict(record)

    def _spend_total(self, tenant: str) -> float:
        return round(sum(r["cost"] for r in self._ledger.get(tenant, [])), 6)

    def spend_total(self, tenant: str) -> float:
        with self._lock:
            return self._spend_total(tenant)

    def usage(self, tenant: str) -> list[dict]:
        """The tenant's usage records (a copy)."""
        with self._lock:
            return [dict(r) for r in self._ledger.get(tenant, [])]

    def usage_summary(self, tenant: str) -> dict:
        """A queryable per-tenant usage summary (CLI/JSON, §9 deliverable). Content-
        free: counts, tokens, cost, and the subscription posture only."""
        with self._lock:
            records = self._ledger.get(tenant, [])
            sub = self._subs.get(tenant)
            in_tok = sum(r["in_tokens"] for r in records)
            out_tok = sum(r["out_tokens"] for r in records)
            total = round(sum(r["cost"] for r in records), 6)
            cap = float(sub.get("spend_cap") or 0.0) if sub else 0.0
            by_task: dict[str, dict] = {}
            for r in records:
                b = by_task.setdefault(r["task"], {"calls": 0, "cost": 0.0})
                b["calls"] += 1
                b["cost"] = round(b["cost"] + r["cost"], 6)
            return {
                "tenant": tenant,
                "subscription": (dict(sub) if sub else None),
                "calls": len(records),
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "spend_usd": total,
                "spend_cap": cap,
                "cap_remaining": (round(cap - total, 6) if cap > 0 else None),
                "by_task": by_task,
            }

    def reset_usage(self, tenant: str) -> None:
        """Clear a tenant's ledger (e.g. new billing period). Admin/stub action."""
        with self._lock:
            self._ledger[tenant] = []
            self._save()

    def tenants(self) -> list[str]:
        with self._lock:
            return sorted(set(self._subs) | set(self._ledger))
