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

"""Read-only operational dashboards (Phase 7, product-architecture §8.4).

Three read surfaces, built ENTIRELY from existing Phase 5–6 data + the Phase 7
direct-target registry — nothing here mutates a document, a queue, a subscription,
or a target:

  * FLEET STATUS   — per-agent liveness / last-heartbeat / queue-depth (Phase 5
                     heartbeats) PLUS registered Mode C direct targets (token
                     stripped).
  * COST / USAGE   — per-tenant metered spend + usage, incl. spend-vs-cap (the
                     Phase 6 billing ledger).
  * REVIEW QUEUES  — per-tenant counts of the human-review gates (`superseded`,
                     `ai-new-taxonomy`) so operators see what needs attention (I4).

Exposed two ways:
  * JSON read endpoints (`DashboardData` methods -> plain dicts), served at
    GET /dashboard/fleet, /dashboard/cost, /dashboard/review, /dashboard/summary.
  * A SINGLE self-contained HTML page (GET /dashboard) with INLINE CSS + JS and NO
    external assets, NO SPA/framework. It fetches the JSON endpoints and renders
    tables client-side. Read-only — it issues only GETs and never POSTs.

REVIEW QUEUES SOURCE: the review-gate counts are reported to the control plane by
agents in their heartbeat status (a `review` block) and by the direct runner's
result summary; the dashboard reads whatever the fleet last reported. It does NOT
reach into any tenant's Paperless (that would require the token / an inbound path)
— it only surfaces what already flowed to the control plane. Tenants with no
reported counts show zeros.
"""
from __future__ import annotations

import json


class DashboardData:
    """Assembles the read-only dashboard payloads from the existing stores. Pure
    reads; holds references but never mutates them."""

    def __init__(self, *, store=None, billing=None, direct_store=None):
        self.store = store              # ControlPlaneStore (agents + heartbeats)
        self.billing = billing          # BillingStore (usage ledger + subs)
        self.direct = direct_store      # DirectTargetStore (Mode C targets)

    # -- fleet status ------------------------------------------------------
    def fleet(self) -> dict:
        """Per-agent liveness + registered direct targets. Read-only."""
        agents = []
        now = None
        if self.store is not None:
            now = self.store._now()
            with self.store._lock:
                recs = list(self.store._agents.values())
            for rec in recs:
                last = rec.get("last_heartbeat")
                status = rec.get("last_status") or {}
                agents.append({
                    "agent_id": rec.get("agent_id"),
                    "tenant": rec.get("tenant"),
                    "revoked": bool(rec.get("revoked")),
                    "enrolled_at": rec.get("enrolled_at"),
                    "last_heartbeat": last,
                    "seconds_since_heartbeat": (
                        round(now - last, 1) if last else None),
                    "queue_depth": status.get("result_queue_depth"),
                    "jobs_done": status.get("jobs_done"),
                    "mode": status.get("mode"),
                })
        targets = []
        if self.direct is not None:
            # PUBLIC targets only — the token is stripped by DirectTargetStore.
            targets = self.direct.list_targets()
        return {
            "generated_at": now,
            "agents": agents,
            "direct_targets": targets,
            "agent_count": len(agents),
            "direct_target_count": len(targets),
        }

    # -- cost / usage ------------------------------------------------------
    def cost(self) -> dict:
        """Per-tenant metered spend/usage incl. spend-vs-cap. Read-only."""
        tenants = []
        total_spend = 0.0
        if self.billing is not None:
            for tenant in self.billing.tenants():
                summary = self.billing.usage_summary(tenant)
                total_spend += summary.get("spend_usd", 0.0)
                cap = summary.get("spend_cap") or 0.0
                spent = summary.get("spend_usd", 0.0)
                tenants.append({
                    "tenant": tenant,
                    "calls": summary.get("calls", 0),
                    "in_tokens": summary.get("in_tokens", 0),
                    "out_tokens": summary.get("out_tokens", 0),
                    "spend_usd": spent,
                    "spend_cap": cap,
                    "cap_remaining": summary.get("cap_remaining"),
                    "cap_pct": (round(100.0 * spent / cap, 1) if cap > 0 else None),
                    "over_cap": bool(cap > 0 and spent >= cap),
                    "subscription": (
                        (summary.get("subscription") or {}).get("status")),
                    "by_task": summary.get("by_task", {}),
                })
        return {
            "tenants": tenants,
            "tenant_count": len(tenants),
            "total_spend_usd": round(total_spend, 6),
        }

    # -- review queues -----------------------------------------------------
    def review(self) -> dict:
        """Per-tenant counts of the human-review gates (superseded /
        ai-new-taxonomy) as last reported by the fleet. Read-only."""
        by_tenant: dict[str, dict] = {}

        def _bump(tenant, superseded, new_tax):
            b = by_tenant.setdefault(
                tenant, {"tenant": tenant, "superseded": 0, "ai_new_taxonomy": 0})
            b["superseded"] += int(superseded or 0)
            b["ai_new_taxonomy"] += int(new_tax or 0)

        if self.store is not None:
            with self.store._lock:
                recs = list(self.store._agents.values())
            for rec in recs:
                status = rec.get("last_status") or {}
                review = status.get("review") or {}
                if review:
                    _bump(rec.get("tenant"),
                          review.get("superseded"), review.get("ai_new_taxonomy"))
        # Direct-target review counts (reported by the direct runner) if the store
        # tracks them. The DirectTargetStore records the last run's counts on the
        # public record under "last_review" when the runner sets it.
        if self.direct is not None:
            for tgt in self.direct.list_targets():
                review = tgt.get("last_review") or {}
                if review:
                    _bump(tgt.get("tenant"),
                          review.get("superseded"), review.get("ai_new_taxonomy"))

        tenants = sorted(by_tenant.values(), key=lambda t: t["tenant"])
        total_super = sum(t["superseded"] for t in tenants)
        total_new = sum(t["ai_new_taxonomy"] for t in tenants)
        return {
            "tenants": tenants,
            "tenant_count": len(tenants),
            "total_superseded": total_super,
            "total_ai_new_taxonomy": total_new,
        }

    # -- combined summary --------------------------------------------------
    def summary(self) -> dict:
        return {
            "fleet": self.fleet(),
            "cost": self.cost(),
            "review": self.review(),
        }


# ---------------------------------------------------------------------------
# The self-contained HTML view (inline CSS + JS, no external assets, read-only)
# ---------------------------------------------------------------------------
def render_html() -> str:
    """Return the single self-contained dashboard HTML page.

    NO external assets: all CSS + JS is inline; no <script src>, no <link href>, no
    web font, no CDN. The page fetches the JSON endpoints (same-origin GETs) and
    renders tables. It is strictly READ-ONLY — it never issues a POST/PUT/PATCH/
    DELETE and never mutates a document. Mode C is labelled 'advanced / opt-in'."""
    return _HTML


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paperless Assistant — Control Plane Dashboard</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --ink:#e6e8ec; --muted:#9aa4b2;
          --line:#2a2f3a; --accent:#5b9dff; --warn:#f0b429; --bad:#f05f5f;
          --ok:#4ec98a; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:18px 22px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:18px; margin:0; font-weight:650; }
  header .sub { color:var(--muted); font-size:12.5px; }
  header .ro { margin-left:auto; color:var(--ok); font-size:12px;
               border:1px solid var(--line); border-radius:20px; padding:3px 10px; }
  main { padding:20px 22px; display:grid; gap:22px; max-width:1100px; }
  section { background:var(--panel); border:1px solid var(--line);
            border-radius:10px; padding:16px 18px; }
  section h2 { font-size:14px; margin:0 0 4px; font-weight:640; }
  section .note { color:var(--muted); font-size:12px; margin:0 0 12px; }
  .kpis { display:flex; gap:26px; flex-wrap:wrap; margin-bottom:12px; }
  .kpi .n { font-size:22px; font-weight:680; }
  .kpi .l { color:var(--muted); font-size:11.5px; text-transform:uppercase;
            letter-spacing:.04em; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line);
          white-space:nowrap; }
  th { color:var(--muted); font-weight:600; font-size:11.5px;
       text-transform:uppercase; letter-spacing:.03em; }
  .scroll { overflow-x:auto; }
  .pill { font-size:11px; padding:2px 8px; border-radius:20px;
          border:1px solid var(--line); }
  .pill.ok { color:var(--ok); } .pill.bad { color:var(--bad); }
  .pill.warn { color:var(--warn); }
  .bar { height:7px; background:var(--line); border-radius:4px; overflow:hidden;
         min-width:80px; }
  .bar > span { display:block; height:100%; background:var(--accent); }
  .bar > span.warn { background:var(--warn); } .bar > span.bad { background:var(--bad); }
  .advanced { border-left:3px solid var(--warn); padding-left:10px; }
  .advanced b { color:var(--warn); }
  .empty { color:var(--muted); font-style:italic; padding:8px 10px; }
  footer { color:var(--muted); font-size:11.5px; padding:6px 22px 24px; }
  button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
           border-radius:7px; padding:5px 12px; cursor:pointer; font-size:12.5px; }
  button:hover { border-color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>Paperless Assistant — Control Plane</h1>
  <span class="sub" id="ts">loading…</span>
  <span class="ro">READ-ONLY · dashboards never mutate documents</span>
  <button id="refresh" type="button">Refresh</button>
</header>
<main>
  <section>
    <h2>Fleet status</h2>
    <p class="note">Agents (Modes A/B, outbound-only) by liveness + queue depth,
       plus registered Mode C direct targets. From Phase-5 heartbeats.</p>
    <div class="kpis" id="fleet-kpis"></div>
    <div class="scroll"><table id="fleet-agents"></table></div>
    <div class="advanced" style="margin-top:14px">
      <h2>Direct targets <b>(Mode C — advanced / opt-in)</b></h2>
      <p class="note">Mode C is the only mode where the vendor holds the Paperless
         token and sees contents. The agent is recommended everywhere. Tokens are
         never shown.</p>
      <div class="scroll"><table id="fleet-targets"></table></div>
    </div>
  </section>

  <section>
    <h2>Cost &amp; usage</h2>
    <p class="note">Per-tenant metered spend vs. server-side cap. From the Phase-6
       billing ledger.</p>
    <div class="kpis" id="cost-kpis"></div>
    <div class="scroll"><table id="cost-table"></table></div>
  </section>

  <section>
    <h2>Review queues</h2>
    <p class="note">Human-review gates that need attention (I4): superseded re-OCR
       and newly-invented taxonomy, as last reported by the fleet.</p>
    <div class="kpis" id="review-kpis"></div>
    <div class="scroll"><table id="review-table"></table></div>
  </section>
</main>
<footer>Read-only view. All data is same-origin JSON from
  <code>/dashboard/summary</code>. No external assets are loaded.</footer>

<script>
"use strict";
// READ-ONLY: this script only ever issues GET requests. It never POSTs, and it
// never mutates any document, target, subscription, or queue.
function esc(s){ return String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function kpi(n,l){ return '<div class="kpi"><div class="n">'+esc(n)+
  '</div><div class="l">'+esc(l)+'</div></div>'; }
function pill(txt,cls){ return '<span class="pill '+(cls||"")+'">'+esc(txt)+'</span>'; }

function renderFleet(f){
  document.getElementById("fleet-kpis").innerHTML =
    kpi(f.agent_count,"agents") + kpi(f.direct_target_count,"direct targets");
  var a = f.agents||[];
  var t = "<tr><th>agent</th><th>tenant</th><th>state</th><th>last heartbeat</th>"+
          "<th>queue</th><th>jobs done</th></tr>";
  if(!a.length){ t += '<tr><td colspan="6" class="empty">no agents enrolled</td></tr>'; }
  a.forEach(function(x){
    var st = x.revoked ? pill("revoked","bad")
      : (x.seconds_since_heartbeat==null ? pill("no heartbeat","warn")
      : (x.seconds_since_heartbeat < 180 ? pill("live","ok") : pill("stale","warn")));
    var hb = x.seconds_since_heartbeat==null ? "—" : x.seconds_since_heartbeat+"s ago";
    t += "<tr><td>"+esc(x.agent_id)+"</td><td>"+esc(x.tenant)+"</td><td>"+st+
      "</td><td>"+esc(hb)+"</td><td>"+esc(x.queue_depth==null?"—":x.queue_depth)+
      "</td><td>"+esc(x.jobs_done==null?"—":x.jobs_done)+"</td></tr>";
  });
  document.getElementById("fleet-agents").innerHTML = t;

  var d = f.direct_targets||[];
  var dt = "<tr><th>target</th><th>tenant</th><th>paperless host</th>"+
           "<th>allowed hosts</th><th>state</th><th>token</th></tr>";
  if(!d.length){ dt += '<tr><td colspan="6" class="empty">no direct targets '+
    '(Mode C is opt-in — the agent is the default)</td></tr>'; }
  d.forEach(function(x){
    var host = "";
    try { host = new URL(x.paperless_url).host; } catch(e){ host = x.paperless_url; }
    var state = x.enabled ? pill("enabled","warn") : pill("disabled","");
    dt += "<tr><td>"+esc(x.target_id)+"</td><td>"+esc(x.tenant)+"</td><td>"+
      esc(host)+"</td><td>"+esc((x.allowed_hosts||[]).join(", "))+"</td><td>"+
      state+"</td><td>"+(x.token_configured?pill("stored (not shown)","ok"):"—")+
      "</td></tr>";
  });
  document.getElementById("fleet-targets").innerHTML = dt;
}

function renderCost(c){
  document.getElementById("cost-kpis").innerHTML =
    kpi("$"+(c.total_spend_usd||0).toFixed(4),"total spend") +
    kpi(c.tenant_count,"tenants");
  var t = "<tr><th>tenant</th><th>sub</th><th>calls</th><th>spend</th>"+
          "<th>cap</th><th>vs cap</th></tr>";
  var rows = c.tenants||[];
  if(!rows.length){ t += '<tr><td colspan="6" class="empty">no metered usage yet</td></tr>'; }
  rows.forEach(function(x){
    var sub = x.subscription ?
      pill(x.subscription, x.subscription==="active"?"ok":"warn") : "—";
    var bar = "—";
    if(x.cap_pct!=null){
      var cls = x.over_cap ? "bad" : (x.cap_pct>=80 ? "warn" : "");
      var w = Math.min(100, x.cap_pct);
      bar = '<div class="bar"><span class="'+cls+'" style="width:'+w+'%"></span></div>'+
            ' '+x.cap_pct+"%";
    }
    t += "<tr><td>"+esc(x.tenant)+"</td><td>"+sub+"</td><td>"+esc(x.calls)+
      "</td><td>$"+(x.spend_usd||0).toFixed(4)+"</td><td>"+
      (x.spend_cap>0?("$"+x.spend_cap.toFixed(2)):"∞")+"</td><td>"+bar+"</td></tr>";
  });
  document.getElementById("cost-table").innerHTML = t;
}

function renderReview(r){
  document.getElementById("review-kpis").innerHTML =
    kpi(r.total_superseded,"superseded") +
    kpi(r.total_ai_new_taxonomy,"ai-new-taxonomy");
  var t = "<tr><th>tenant</th><th>superseded</th><th>ai-new-taxonomy</th></tr>";
  var rows = r.tenants||[];
  if(!rows.length){ t += '<tr><td colspan="3" class="empty">nothing awaiting '+
    'review</td></tr>'; }
  rows.forEach(function(x){
    t += "<tr><td>"+esc(x.tenant)+"</td><td>"+esc(x.superseded)+"</td><td>"+
      esc(x.ai_new_taxonomy)+"</td></tr>";
  });
  document.getElementById("review-table").innerHTML = t;
}

function load(){
  fetch("dashboard/summary", {method:"GET"}).then(function(r){ return r.json(); })
   .then(function(d){
     document.getElementById("ts").textContent =
       "generated " + new Date().toLocaleString();
     renderFleet(d.fleet||{}); renderCost(d.cost||{}); renderReview(d.review||{});
   }).catch(function(e){
     document.getElementById("ts").textContent = "error loading data: " + e;
   });
}
document.getElementById("refresh").addEventListener("click", load);
load();
</script>
</body>
</html>
"""
