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

"""Data assembly for the local web dashboard (prompt 009).

Pure READ helpers that build the dashboard's JSON payloads by REUSING the existing
modules — obs (`build_status` / `Cursor` / `SpendLedger`), stages + client (library
stats), the persisted /data run-reports, and the /data JSONL log. Nothing here
mutates a document, a queue, or config; it only surfaces what already exists.

Kept separate from `webui.py` (the server/handler/HTML) so the data shapes are
easy to test in isolation and the server file stays about transport + auth.
"""
from __future__ import annotations

import json
import pathlib

from . import config
from .obs import Cursor, SpendLedger, build_status
from .client import PaperlessClient
from .fields import CustomFieldResolver
from .stages import StageOrchestrator


# Log events that count as errors for the "errors only" log filter (r3).
ERROR_EVENTS = frozenset({"failure", "stage_aborted", "sweep_error", "nudge_error"})


def _data(settings, *parts) -> pathlib.Path:
    return settings.data_path(*parts)


# ---------------------------------------------------------------------------
# status — connectivity, last run, spend-vs-cap, run-in-progress.
# ---------------------------------------------------------------------------
def status_payload(settings, *, run_state=None) -> dict:
    """Reuse obs.build_status (the same surface `pa status` shows) and add the
    in-progress manual-run state so the page can poll it. No Paperless call — this
    is the local status file surface, so it never blocks on connectivity."""
    cursor = Cursor(str(_data(settings, "cursor.json")))
    ledger = SpendLedger(
        str(_data(settings, "spend-ledger.json")), period=settings.spend.period
    )
    status = build_status(settings, cursor, ledger)
    spend = status.get("spend") or {}
    period_spend = spend.get("period_spend") or 0.0
    per_period = spend.get("per_period_cap") or 0.0
    status["spend"]["over_period_cap"] = bool(per_period and period_spend >= per_period)
    status["spend"]["period_pct"] = (
        round(100.0 * period_spend / per_period, 1) if per_period else None
    )
    status["run_in_progress"] = bool(run_state and run_state.get("in_progress"))
    status["current_run"] = run_state or {"in_progress": False}
    return status


def progress_payload() -> dict:
    """Live snapshot of the current / most-recent sweep run (stage progress +
    per-document outcomes), for the Overview page's auto-refreshing panel. Reads
    the process-wide progress tracker the Sweep updates as it runs; purely
    observational and non-secret (doc id/title, outcome summary, public URL)."""
    from .progress import tracker

    return tracker().snapshot()


# ---------------------------------------------------------------------------
# stats — library/queue view from Paperless (reuses StageOrchestrator predicates).
# ---------------------------------------------------------------------------
def stats_payload(settings, *, client=None) -> dict:
    """Counts by ai_stage, flagged (ocr_quality >= triage_threshold), and the two
    review-queue counts (superseded / ai-new-taxonomy). Paperless-unreachable is
    handled GRACEFULLY: return {"error": ...} instead of crashing (r3)."""
    try:
        cfg = settings.to_config()
        client = client or PaperlessClient(
            cfg.base_url, cfg.paperless_token, http=settings.http)
        resolver = CustomFieldResolver(
            client, field_names=settings.field_names, stage_names=settings.stage_names)
        orch = StageOrchestrator(client, resolver, settings)

        stage_fid = resolver.stage_field_id()
        score_fid = resolver.score_field_id()
        threshold = settings.triage_threshold

        # Count by state-machine ROLE so configured stage labels still bucket
        # correctly (the dashboard shows role-named counts).
        by_stage = {"triaged": 0, "reocr_done": 0, "metadata_done": 0, "none": 0}
        flagged = 0
        total = 0
        fields = "id,title,content,custom_fields"
        for doc in client.iter_documents(fields):
            total += 1
            cf = {c["field"]: c.get("value") for c in (doc.get("custom_fields") or [])}
            role = resolver.role_for_value(cf.get(stage_fid))
            if role in by_stage:
                by_stage[role] += 1
            else:
                by_stage["none"] += 1
            score = cf.get(score_fid)
            try:
                if score is not None and float(score) >= threshold:
                    flagged += 1
            except (TypeError, ValueError):
                pass

        review = _review_queue_counts(client, settings)
        return {
            "total_documents": total,
            "by_stage": by_stage,
            "flagged_ocr_quality": flagged,
            "triage_threshold": threshold,
            "review_queues": review,
        }
    except Exception as e:  # noqa: BLE001 — never crash the read endpoint (r3)
        return {"error": _safe_error(e)}


def _review_queue_counts(client, settings) -> dict:
    """Count docs carrying the superseded / ai-new-taxonomy review tags. Best-effort
    (a failure bubbles up to stats_payload's error handler)."""
    def _count_tag(tag_name):
        tag = client.request(
            "GET", f"{client.base}/api/tags/?name__iexact={tag_name}"
        ).json()
        results = tag.get("results") or []
        if not results:
            return {"configured": False, "count": 0}
        tag_id = results[0]["id"]
        data = client.request(
            "GET", f"{client.base}/api/documents/?tags__id__all={tag_id}&fields=id"
        ).json()
        return {"configured": True, "count": int(data.get("count", 0))}

    return {
        "superseded": _count_tag(settings.superseded_tag),
        "ai_new_taxonomy": _count_tag(settings.new_taxonomy_tag),
    }


# ---------------------------------------------------------------------------
# runs — recent persisted run reports (list + single detail).
# ---------------------------------------------------------------------------
def runs_payload(settings, *, limit=25) -> dict:
    """List the most-recent persisted run reports under /data/run-reports. Each
    entry is a compact summary; the full report is fetched by run detail."""
    reports_dir = _data(settings, "run-reports")
    items = []
    for path in _report_paths(reports_dir):
        rep = _read_json(path)
        if rep is None:
            continue
        items.append(_run_summary(rep, path))
    items.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return {"runs": items[:limit], "count": len(items)}


def run_detail(settings, run_id: str) -> dict | None:
    """Return the FULL persisted report for `run_id` (per-stage breakdown, counts,
    spend, new_taxonomy, superseded, dry_run, timestamps), or None if not found."""
    reports_dir = _data(settings, "run-reports")
    for path in _report_paths(reports_dir):
        rep = _read_json(path)
        if rep is None:
            continue
        if rep.get("run_id") == run_id:
            rep["_file"] = path.name
            return rep
    return None


def _report_paths(reports_dir):
    d = pathlib.Path(reports_dir)
    if not d.exists():
        return []
    return sorted(d.glob("*.json"))


def _run_summary(rep: dict, path: pathlib.Path) -> dict:
    return {
        "run_id": rep.get("run_id"),
        "kind": rep.get("kind", "run"),
        "started_at": rep.get("started_at"),
        "finished_at": rep.get("finished_at"),
        "dry_run": rep.get("dry_run"),
        "counts": rep.get("counts", {}),
        "spend_total": rep.get("spend_total", 0.0),
        "period_spend": rep.get("period_spend"),
        "period_cap": rep.get("period_cap"),
        "new_taxonomy": rep.get("new_taxonomy", []),
        "superseded": rep.get("superseded", []),
        "file": path.name,
    }


# ---------------------------------------------------------------------------
# logs — tail of /data/logs/pa.jsonl (optionally errors-only).
# ---------------------------------------------------------------------------
def logs_payload(settings, *, limit=100, errors_only=False) -> dict:
    """Return the most-recent N JSONL log events. `errors_only` keeps only error
    events (failure / ERROR / stage_aborted / sweep_error) so operators see
    problems (r3)."""
    path = _data(settings, "logs", "pa.jsonl")
    events = _tail_jsonl(path, max_lines=5000)
    if errors_only:
        events = [e for e in events if _is_error_event(e)]
    events = events[-limit:]
    return {"events": events, "count": len(events), "errors_only": bool(errors_only)}


def _is_error_event(ev: dict) -> bool:
    if str(ev.get("level", "")).lower() in ("error", "critical"):
        return True
    if ev.get("event") in ERROR_EVENTS:
        return True
    return str(ev.get("status", "")).upper() == "ERROR"


def _tail_jsonl(path, *, max_lines=5000) -> list:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    out = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ---------------------------------------------------------------------------
# config — the non-secret tunable surface, with env-locked flags.
# ---------------------------------------------------------------------------
def config_payload(settings, *, environ=None) -> dict:
    """The resolved, NON-SECRET tunables (config.example.yml surface), each field
    flagged whether an env var currently overrides it (env beats YAML), plus a
    secrets block that reports ONLY whether each secret is set — never its value."""
    pub = settings.to_public_dict()
    env_locked = config.env_overridden_fields(environ)
    # Prompt 011: expose the byte-identical DEFAULTS (from a fresh Settings) so the
    # dashboard can render inputs and drive per-field "reset to default" for the
    # Advanced knobs generically, without hardcoding numbers in the HTML.
    defaults = config.Settings().to_public_dict()
    return {
        "values": pub,
        "defaults": defaults,
        "env_locked": env_locked,
        "secrets": {
            # Presence only — NEVER the value. Secrets come from the environment.
            "paperless_token": bool(settings.paperless_token),
            "anthropic_api_key": bool(settings.anthropic_api_key),
            "openai_api_key": bool(settings.openai_api_key),
            "webhook_secret": bool(settings.webhook.secret),
            "ui_token": bool(settings.ui.token),
            "agent_token": bool(settings.agent_token),
            "enrollment_token": bool(settings.hosted.enrollment_token),
        },
    }


# ---------------------------------------------------------------------------
# models — the curated, pricing-annotated catalog + the currently-configured ids.
# ---------------------------------------------------------------------------
def models_payload(settings) -> dict:
    """The per-provider model catalog (id/label/pricing/vision/recommended) DERIVED
    from pricing.py, plus the currently-configured model per task and whether it is
    in the catalog. The dashboard uses this for the model dropdowns + the "other…"
    free-text option + the re-OCR vision warning. Tolerant of a configured custom
    (uncatalogued) model: it is reported with `in_catalog: False`, never rejected."""
    from .providers import model_catalog, is_vision_model

    catalog = model_catalog()

    def _task(provider, model):
        prov_models = catalog.get(provider, [])
        in_catalog = any(m["id"] == model for m in prov_models)
        return {
            "provider": provider,
            "model": model,
            "in_catalog": in_catalog,
            "vision": is_vision_model(provider, model),
        }

    return {
        "catalog": catalog,
        "current": {
            "metadata": _task(settings.metadata_task.provider,
                              settings.metadata_task.model or config.METADATA_MODEL),
            "ocr": _task(settings.ocr_task.provider,
                         settings.ocr_task.model or config.OCR_MODEL),
        },
    }


# ---------------------------------------------------------------------------
# prompts — per task: the built-in default, the current override/extra, and the
# EFFECTIVE composed prompt (what the engine will send). READ-only preview.
# ---------------------------------------------------------------------------
def prompts_payload(settings) -> dict:
    """Per task ("metadata"/"ocr"): the built-in `default` instruction (read-only),
    the current `extra_instructions` + `prompt_override`, and the `effective`
    composed instruction (default -> override -> +extra) — computed by the SAME
    single resolver the engine uses, so the preview is byte-for-byte what the engine
    sends. The engine-owned JSON schema is deliberately NOT included: it is fixed and
    never customizable."""
    from . import prompts as _prompts

    def _task(task, pc):
        default = _prompts.default_instruction(task)
        return {
            "default": default,
            "extra_instructions": pc.extra_instructions,
            "prompt_override": pc.prompt_override,
            "effective": _prompts.resolve_instruction(
                default,
                prompt_override=pc.prompt_override,
                extra_instructions=pc.extra_instructions,
            ),
            "customized": bool(
                str(pc.prompt_override).strip() or str(pc.extra_instructions).strip()
            ),
        }

    return {
        "metadata": _task("metadata", settings.metadata_prompts),
        "ocr": _task("ocr", settings.ocr_prompts),
        "note": (
            "Customizing changes only the natural-language instruction. The "
            "structured-output schema is fixed and every write is still validated, "
            "so a custom prompt can change quality but can never corrupt Paperless."
        ),
    }


# ---------------------------------------------------------------------------
# activity — the per-document audit log (prompt 013): filtered + paginated read
# and a manual purge. REUSES the ActivityStore under /data/activity.db. No secret
# is stored in or returned from activity data (doc metadata + non-secret URL only).
# ---------------------------------------------------------------------------
def _activity_store(settings):
    """Open the shared ActivityStore for reads. Returns None when the log is
    disabled or the DB can't be opened (graceful — the endpoint reports empty)."""
    if not settings.activity_enabled:
        return None
    try:
        from .activity import ActivityStore

        return ActivityStore(str(_data(settings, "activity.db")))
    except Exception:  # noqa: BLE001 — never crash the read endpoint
        return None


def activity_payload(settings, *, doc_id=None, since=None, until=None,
                     dry_run=None, stage=None, status=None, search=None,
                     limit=50, offset=0) -> dict:
    """Filtered, server-side-paginated activity rows + a total (for pagination),
    plus lightweight store stats + the configured retention. Paperless is NOT
    contacted here — the audit log is a local /data surface, so it never blocks on
    connectivity."""
    store = _activity_store(settings)
    if store is None:
        return {
            "rows": [], "total": 0, "limit": limit, "offset": offset,
            "enabled": bool(settings.activity_enabled),
            "retention_days": settings.activity_retention_days,
            "stats": {"count": 0},
        }
    try:
        res = store.query(
            doc_id=doc_id, since=since, until=until, dry_run=dry_run,
            stage=stage, status=status, search=search, limit=limit, offset=offset,
        )
        stats = store.stats()
    finally:
        store.close()
    # Build each doc link from the EXTERNAL/public base URL at read time, so links
    # are browser-reachable even for rows recorded before a public URL was set
    # (older rows may hold the in-stack `webserver:8000` URL). Falls back to base_url.
    from .activity import paperless_doc_url
    public = settings.public_base_url()
    for row in res.get("rows", []):
        if row.get("doc_id") is not None:
            row["paperless_url"] = paperless_doc_url(public, row["doc_id"])
    res["enabled"] = True
    res["retention_days"] = settings.activity_retention_days
    res["stats"] = stats
    return res


def activity_purge(settings, *, older_than_days=None) -> dict:
    """Manually purge activity older than `older_than_days` (default = the
    configured `activity_retention_days`). `0`/None retention = keep forever, in
    which case an explicit `older_than_days` is still honored. Returns the deleted
    count. Best-effort; never raises out to the caller."""
    import time as _time

    store = _activity_store(settings)
    if store is None:
        return {"purged": 0, "enabled": bool(settings.activity_enabled)}
    days = (settings.activity_retention_days if older_than_days is None
            else older_than_days)
    try:
        if not days or days <= 0:
            # Keep-forever + no explicit override -> nothing to purge.
            return {"purged": 0, "retention_days": settings.activity_retention_days,
                    "note": "retention is keep-forever (0); pass older_than_days to "
                            "purge an explicit window."}
        cutoff = _time.time() - float(days) * 86400
        purged = store.purge(cutoff)
        return {"purged": int(purged), "older_than_days": days,
                "retention_days": settings.activity_retention_days}
    finally:
        store.close()


def _read_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _safe_error(e: Exception) -> str:
    """A short, safe error string for an endpoint response — never leak a token or
    secret. Strip anything that looks like an Authorization header value."""
    msg = str(e)
    # Defensive: our client puts the token in a session header, not the message,
    # but never echo an obvious 'Token <...>' if a library surfaced one.
    for marker in ("Token ", "Bearer "):
        if marker in msg:
            msg = msg.split(marker, 1)[0] + marker + "***"
    return msg[:400]
