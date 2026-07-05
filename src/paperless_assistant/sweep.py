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

"""Sweep runner - the steady-state "keep my library tidy" trigger (plan §6.2).

`pa run`   = one sweep tick: run the ENABLED stages once over the eligible queue,
             each via the existing orchestrator/pipeline so ALL invariants hold
             unchanged (I1 idempotency, I2 snapshot, I3 spend, I4/I5 gates).
`pa serve` = `pa run` on an interval / simple cron loop, restart-safe (idempotency
             makes re-sweeps harmless - correctness never depends on the schedule).

First-run safety (plan §8.2 step 4, I7): the very first processing run defaults to
a BOUNDED dry-run with a report so the user sees proposed changes before anything
writes. This is the out-of-box behaviour, overridable by explicit config/flag.

Re-OCR is OFF by default (plan §7.2) - it only runs when `reocr_enabled` is set.

This module is a CALLER of the engine (plan §4.2). It does not change any stage's
safety behaviour; it wires the pieces the CLI subcommands used to wire inline.
"""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


def _http_status(e: Exception):
    """Best-effort HTTP status code off a provider SDK exception (Anthropic/OpenAI
    both expose `.status_code`, sometimes only on `.response`)."""
    return (getattr(e, "status_code", None)
            or getattr(getattr(e, "response", None), "status_code", None))


def _is_auth_error(e: Exception) -> bool:
    """True if `e` looks like an AI-provider AUTHENTICATION failure (a bad/blank
    API key), as opposed to a per-document error.

    Checks are provider-agnostic: an HTTP 401/403 status, an *AuthenticationError /
    PermissionDenied exception type (Anthropic/OpenAI SDKs), or the telltale
    message text."""
    if _http_status(e) in (401, 403):
        return True
    name = type(e).__name__
    if "AuthenticationError" in name or "PermissionDenied" in name:
        return True
    msg = str(e).lower()
    return any(s in msg for s in (
        "invalid x-api-key", "authentication_error", "invalid_api_key",
        "invalid api key", "incorrect api key",
    ))


# Message/code markers for an EXHAUSTED account (out of credits / over quota /
# billing limit hit). These are provider-config failures — like a bad key, they
# reject EVERY document — but they are NOT auth errors (Anthropic returns HTTP 400
# "credit balance is too low"; OpenAI a 429 with code `insufficient_quota`), so
# the auth check above misses them and the whole batch would otherwise burn
# through doomed, zero-cost (hence uncapped) calls.
_BILLING_MARKERS = (
    "credit balance is too low",     # Anthropic: out of credits
    "insufficient_quota",            # OpenAI: exhausted quota/credits
    "insufficient quota",
    "exceeded your current quota",
    "billing_hard_limit_reached",
    "billing hard limit",
    "payment required",
    "quota exceeded",
)


def _is_billing_error(e: Exception) -> bool:
    """True if `e` looks like an EXHAUSTED-ACCOUNT failure (out of credits / over
    quota / billing cap). Distinct from a rate limit: a plain 429
    `rate_limit_exceeded` is transient and is NOT matched here."""
    if _http_status(e) == 402:  # Payment Required
        return True
    code = str(getattr(e, "code", "") or getattr(e, "type", "") or "").lower()
    if "insufficient_quota" in code or "billing" in code:
        return True
    msg = str(e).lower()
    return any(m in msg for m in _BILLING_MARKERS)


def _provider_fatal_reason(e: Exception):
    """Return the reason a provider error should FAIL FAST — it rejects every
    document identically, so there is no point calling the provider for the rest:
      * 'billing' — out of credits / over quota (account-level)
      * 'auth'    — bad/blank API key (account-level)
      * 'config'  — provider unavailable in this deployment (its SDK package is
                    not installed) or the model lacks a required capability (e.g.
                    a text-only model selected for vision re-OCR)
    or None for an ordinary, per-document error that should not abort the batch.
    'auth'/'billing' are account-level and stop the WHOLE run; 'config' is
    provider/task-specific and only fails its own stage fast (see run_once)."""
    if _is_billing_error(e):
        return "billing"
    if _is_auth_error(e):
        return "auth"
    if isinstance(e, CapabilityError):
        return "config"
    return None


# Which fatal reasons stop the ENTIRE run (skip remaining billable stages).
# Account-level failures hit every provider call, so the run is doomed. A
# per-provider 'config' error only fails its own stage — a different provider on a
# later stage may still work.
_STOP_RUN_REASONS = frozenset({"auth", "billing"})


# One human line per fatal reason, surfaced in logs + the dashboard so the user
# knows exactly what to fix before re-running.
_FATAL_REASON_HELP = {
    "billing": ("AI provider account is out of credits or over its quota. Add "
                "credits / raise your billing limit with the provider, then re-run."),
    "auth": ("AI provider rejected the API key (auth error). Fix the provider key "
             "(e.g. ANTHROPIC_API_KEY) and re-run. `pa doctor` checks it is present."),
    "config": ("The selected AI provider isn't usable in this deployment — its "
               "package may be missing, or the model lacks a required capability "
               "(e.g. a text-only model chosen for vision re-OCR). Check the "
               "provider/model under Settings -> Models (and its API key), then "
               "re-run. `pa doctor` reports provider readiness."),
}

# Sentinel put on documents short-circuited AFTER a fatal provider error (so they
# are recognised as no-op skips and get no audit row / no per-doc error).
_SKIP_ON_FATAL = "skipped: AI provider error (run stopping)"

from .client import PaperlessClient
from .fields import CustomFieldResolver
from .taxonomy import TaxonomyResolver
from .safety import SafetyLayer
from .spend import SpendGovernor
from .stages import StageOrchestrator
from .ocr import OcrPipeline, garbage_score
from .metadata import MetadataExtractor
from .report import RunReport, MultiStageReport
from .obs import JsonLogger, SpendLedger, Cursor, build_status, PauseFlag
from .providers import build_provider, TASK_OCR, TASK_METADATA
from .providers.base import CapabilityError


class Sweep:
    """Runs the enabled stages once (a tick). Reused by `pa serve` on a loop."""

    def __init__(self, settings, *, logger=None, client=None, cfg=None,
                 progress=None):
        self.settings = settings
        # Live progress tracker (observational). Defaults to the process-wide
        # singleton the dashboard reads; tests may inject their own.
        from .progress import tracker as _global_tracker

        self._progress = progress if progress is not None else _global_tracker()
        # `cfg` lets a caller inject a Config that carries extra runtime wiring
        # (e.g. the Phase 6 hosted-inference context the HostedAgent injects). When
        # omitted we project it from settings exactly as before.
        self.cfg = cfg if cfg is not None else settings.to_config()
        # Prompt 011: pass the configured HTTP tunables (timeouts / pagination /
        # retry) to the client (defaults byte-identical).
        self.client = client or PaperlessClient(
            self.cfg.base_url, self.cfg.paperless_token, http=settings.http)
        self.data = settings.data_path
        self.logger = logger or JsonLogger(
            path=str(self.data("logs", "pa.jsonl"))
        )
        self.cursor = Cursor(str(self.data("cursor.json")))
        self.ledger = SpendLedger(
            str(self.data("spend-ledger.json")), period=settings.spend.period
        )
        self.reports_dir = str(self.data("run-reports"))
        # Prompt 012: the persisted pause switch (under /data). It halts AUTOMATIC
        # processing (scheduled sweeps + webhook nudges) only; an explicit manual
        # "Run now" via the dashboard never consults it.
        self.pause_flag = PauseFlag(str(self.data("paused.json")))
        # Prompt 013: the per-document activity/audit log (SQLite under /data).
        # Constructed lazily + best-effort — it is OBSERVATIONAL, so if the store
        # can't be opened the sweep still runs (recording is simply skipped). Held
        # once per Sweep so the _drain worker threads share one WAL connection.
        self._activity = None
        self._activity_opened = False
        self._activity_lock = threading.Lock()
        # Set (once) when a stage aborts on a FATAL provider error (out of credits
        # / bad key). run_once resets it and, when set, skips the remaining billable
        # stages so the run STOPS instead of failing every doc. Lock-guarded because
        # the _drain worker threads race to set it.
        self._fatal_provider_error = None
        self._fatal_lock = threading.Lock()

    def is_paused(self) -> bool:
        return self.pause_flag.is_paused()

    def _note_fatal_provider_error(self, reason, err, stage):
        """Log a fatal provider error + surface it on the live dashboard. Called
        once per stage (guarded by the caller's abort Event). Account-level reasons
        (auth/billing) also latch the run so run_once skips remaining stages; a
        per-provider 'config' error only fails its own stage."""
        provider_msg = str(err)[:300]
        if reason in _STOP_RUN_REASONS:
            with self._fatal_lock:
                if self._fatal_provider_error is None:
                    self._fatal_provider_error = {
                        "reason": reason, "stage": stage,
                        "provider_message": provider_msg,
                    }
        help_line = _FATAL_REASON_HELP.get(reason, "AI provider error.")
        self.logger.event(
            "stage_aborted", level="error", stage=stage, reason_kind=reason,
            reason=help_line, provider_message=provider_msg,
        )
        # Live progress banner (observational; never raises).
        try:
            self._progress.set_error(reason, provider_msg, stage=stage,
                                     help=help_line)
        except Exception:  # noqa: BLE001 — the dashboard hint must never fail a run
            pass
        label = {"billing": "out of credits / over quota",
                 "auth": "rejected the API key",
                 "config": "is not usable in this deployment"}.get(reason,
                                                                    "error")
        print(
            f"pa: {stage} stopped — AI provider {label}. {help_line}",
            file=sys.stderr, flush=True,
        )

    # -- activity/audit log (prompt 013): OBSERVATIONAL, best-effort -------
    def activity_store(self):
        """Return the shared ActivityStore, or None when disabled / unavailable.
        Opening is best-effort: a failure to open never fails the sweep — it just
        disables recording for this process (logged once)."""
        if not self.settings.activity_enabled:
            return None
        # Thread-safe lazy open: the _drain worker threads all call this; without a
        # lock two of them could both open a fresh connection and one would be
        # discarded (losing its rows). Open exactly once under the lock.
        with self._activity_lock:
            if not self._activity_opened:
                self._activity_opened = True
                try:
                    from .activity import ActivityStore

                    self._activity = ActivityStore(str(self.data("activity.db")))
                except Exception as e:  # noqa: BLE001 — never fail the sweep on the log
                    self._activity = None
                    self.logger.event("activity_store_unavailable", level="warning",
                                      error=str(e))
            return self._activity

    def _record_activity(self, entry: dict):
        """Best-effort insert of one activity row. Catches + logs ANY failure so a
        recording error can NEVER fail the document or the run (prompt 013 r2)."""
        store = self.activity_store()
        if store is None:
            return
        try:
            store.record(entry)
        except Exception as e:  # noqa: BLE001 — observational only
            self.logger.event("activity_record_failed", level="warning",
                              doc_id=entry.get("doc_id"), error=str(e))

    def _doc_url(self, doc_id):
        from .activity import paperless_doc_url

        # User-facing link -> the EXTERNAL/public base URL (falls back to base_url).
        return paperless_doc_url(self.settings.public_base_url(), doc_id)

    def _enforce_activity_retention(self):
        """Purge activity rows older than `activity_retention_days` (0 = keep
        forever). Mirrors snapshot-retention enforcement — best-effort, index-backed
        DELETE WHERE ts < cutoff, logged. Called after a completed sweep tick."""
        days = self.settings.activity_retention_days
        if not days or days <= 0:
            return
        store = self.activity_store()
        if store is None:
            return
        cutoff = time.time() - days * 86400
        try:
            removed = store.purge(cutoff)
        except Exception as e:  # noqa: BLE001 — never fail a run on the audit log
            self.logger.event("activity_retention_error", level="warning", error=str(e))
            return
        if removed:
            self.logger.event("activity_retention", removed=removed,
                              retention_days=days)

    # -- first-run dry-run resolution (I7) ---------------------------------
    def resolve_dry_run(self) -> bool:
        """Explicit config/flag wins; otherwise the FIRST ever processing run is a
        bounded dry-run (I7), and subsequent runs write."""
        if self.settings.dry_run is not None:
            return bool(self.settings.dry_run)
        return not self.cursor.first_run_done

    def _snapshot_dir(self, stage):
        return str(self.data("snapshots", stage))

    def _enforce_snapshot_retention(self):
        """Delete snapshot JSON files older than `snapshot_retention_days`
        (0 = keep forever, the byte-identical default). Best-effort: a filesystem
        error never fails a run. Only prunes the /data/snapshots/* rollback records
        — documents are never touched."""
        import pathlib
        import time as _time

        days = self.settings.snapshot_retention_days
        if not days or days <= 0:
            return
        cutoff = _time.time() - days * 86400
        root = pathlib.Path(self.data("snapshots"))
        if not root.exists():
            return
        removed = 0
        try:
            for snap in root.rglob("*.json"):
                try:
                    if snap.stat().st_mtime < cutoff:
                        snap.unlink()
                        removed += 1
                except OSError:
                    continue
        except OSError:
            return
        if removed:
            self.logger.event("snapshot_retention", removed=removed,
                              retention_days=days)

    # -- a single sweep tick -----------------------------------------------
    def run_once(self, *, limit=None, source="scheduled") -> MultiStageReport:
        dry = self.resolve_dry_run()
        limit = self.settings.limit if limit is None else limit
        multi = MultiStageReport()
        multi.dry_run = dry
        self.logger.event(
            "sweep_start", run_id=multi.run_id, dry_run=dry,
            stages=self.settings.enabled_stages(),
            first_run=not self.cursor.first_run_done,
        )
        # Reset the per-run fatal-error latch (a bad key / no credits from a prior
        # tick must not suppress this run's stages).
        self._fatal_provider_error = None
        # Live progress: mark the run active so the dashboard can show it as it
        # runs. Best-effort + observational — never affects processing.
        self._progress.begin_run(multi.run_id, dry_run=dry, source=source)

        resolver = CustomFieldResolver(
            self.client,
            field_names=self.settings.field_names,
            stage_names=self.settings.stage_names,
        )
        orch = StageOrchestrator(self.client, resolver, self.settings)

        try:
            # Triage is local + free (no AI call) — always safe to run. The
            # billable stages (re-OCR, metadata) are SKIPPED once a fatal provider
            # error (out of credits / bad key) has stopped the run, so we don't
            # fail every remaining document across stages.
            if self.settings.triage_enabled:
                multi.add(self._run_triage(resolver, orch, dry, limit))
            # Re-OCR only when explicitly enabled (plan §7.2). OFF by default.
            if self.settings.reocr_enabled and not self._fatal_provider_error:
                multi.add(self._run_reocr(resolver, orch, dry, limit))
            if self.settings.metadata_enabled and not self._fatal_provider_error:
                multi.add(self._run_metadata(resolver, orch, dry, limit))
            if self._fatal_provider_error:
                fe = self._fatal_provider_error
                self.logger.event(
                    "run_stopped", level="error", run_id=multi.run_id,
                    reason_kind=fe["reason"], stage=fe["stage"],
                    reason=_FATAL_REASON_HELP.get(fe["reason"], "AI provider error."),
                    provider_message=fe.get("provider_message"),
                )
        finally:
            # Always clear the active flag (even on error) with the best final
            # counts/spend we have, so the dashboard doesn't show a stuck run.
            self._progress.end_run(
                counts=multi.merged_counts(), spend_total=multi.total_spend())

        multi.period_spend = self.ledger.current()
        multi.period_cap = self.settings.spend.per_period
        multi.finish()

        # Prompt 011: enforce snapshot retention (0 = keep forever). This prunes ONLY
        # expired snapshot JSON files (the rollback records); it NEVER touches a
        # document (I2/I4 are unchanged — snapshotting itself is never disabled).
        self._enforce_snapshot_retention()
        # Prompt 013: enforce activity-log retention (0 = keep forever). Purges ONLY
        # expired audit rows from /data/activity.db; never touches a document and
        # never weakens an invariant — the log is purely observational.
        self._enforce_activity_retention()

        path = multi.persist(self.reports_dir)
        self.cursor.record_run(
            run_id=multi.run_id, dry_run=dry,
            counts=multi.merged_counts(), spend=multi.total_spend(),
        )
        # Mark first run done only after a completed tick, so a crashed first run
        # still gets the safe dry-run default next time (I7).
        if not self.cursor.first_run_done:
            self.cursor.mark_first_run_done()
        self.logger.event(
            "sweep_end", run_id=multi.run_id, dry_run=dry,
            counts=multi.merged_counts(), spend=multi.total_spend(),
            period_spend=multi.period_spend, report=str(path),
        )
        return multi

    # -- stage: triage (local, free - no spend) ----------------------------
    def _run_triage(self, resolver, orch, dry, limit, docs=None):
        report = RunReport("triage")
        report.dry_run = dry
        safety = SafetyLayer(self.client, resolver, snapshot_dir=self._snapshot_dir("triage"))
        self.logger.stage_transition("triage", "start")
        if docs is None:
            docs = orch.fetch_triage_queue(limit=limit or 0)
        threshold = self.settings.triage_threshold

        def process(doc):
            # I1 for the combined sweep: skip a doc already at ANY point in the
            # ai_stage state machine (triaged / reocr_done / metadata_done), not
            # only exactly 'triaged'. Re-triaging a 'metadata_done' doc would
            # reset its stage and make metadata re-process it every tick (a spend
            # + idempotency violation). This is now enforced at the source in
            # StageOrchestrator.already_triaged (Phase 4 fix); the sweep uses that
            # single authoritative predicate rather than a redundant local guard.
            if not self.settings.force and orch.already_triaged(doc):
                # I1: an already-processed doc is a NO-OP -> record NO activity row.
                return ("skip", doc["id"], "already triaged", 0.0)
            score, note = garbage_score(
                doc.get("content", ""), self.settings.garbage_heuristic)
            flag = score is not None and score >= threshold
            detail = f"score={score} {note}" + (" FLAG-reocr" if flag else "")
            status = "dry" if dry else "wrote"
            # Capture the BEFORE custom fields up front — the PATCH below mutates
            # the live doc dict, so the observational recorder must see pre-write.
            before_cf = [dict(cf) for cf in (doc.get("custom_fields") or [])]
            if not dry:
                safety.snapshot(doc)
                body = {"custom_fields": safety.merge_triage_fields(
                    doc.get("custom_fields"), score, note)}
                self.client.request(
                    "PATCH", f"{self.client.base}/api/documents/{doc['id']}/", json=body)
            # Record the field-level triage diff (proposed in dry-run, applied
            # otherwise). This is a REAL change/proposal, so it gets a row.
            self._record_triage(report.run_id, doc, resolver, score, note, flag,
                                 dry_run=dry, status=status, before_cf=before_cf)
            return (status, doc["id"], detail, 0.0)

        self._drain(process, docs, report, stage="triage",
                    run_id=report.run_id, dry_run=dry)
        report.finish()
        self.logger.stage_transition("triage", "end", counts=report.counts())
        return report

    def _record_triage(self, run_id, doc, resolver, score, note, flag, *,
                       dry_run, status, before_cf=None):
        """Build + record the triage field-level diff (ocr_quality / ai_stage /
        ai_notes: before -> after). Best-effort; before comes from the pre-write
        custom fields, after from the computed score/note + the 'triaged' stage."""
        from .activity import diff_fields, changes_summary

        try:
            source_cf = before_cf if before_cf is not None else (
                doc.get("custom_fields") or [])
            cf = {c["field"]: c.get("value") for c in source_cf}
            before = {
                "ocr_quality": cf.get(resolver.score_field_id()),
                "ai_stage": resolver.stage_label_from_value(
                    cf.get(resolver.stage_field_id())),
                "ai_notes": cf.get(resolver.notes_field_id()),
            }
            after = {
                "ocr_quality": score,
                "ai_stage": self.settings.stage_names.triaged,
                "ai_notes": note[:255] if note else note,
            }
            fields = diff_fields(before, after)
            changes = {"fields": fields}
            if flag:
                changes.setdefault("flags", []).append("flagged-reocr")
        except Exception:  # noqa: BLE001 — never let diff-building affect processing
            changes = {"fields": {}}
        self._record_activity({
            "run_id": run_id,
            "doc_id": doc.get("id"),
            "doc_title": doc.get("title"),
            "stage": "triage",
            "dry_run": dry_run,
            "status": status,
            "changes": changes,
            "summary": changes_summary(changes),
            "paperless_url": self._doc_url(doc.get("id")),
        })

    # -- stage: re-OCR (opt-in; billable) ----------------------------------
    def _run_reocr(self, resolver, orch, dry, limit, docs=None):
        report = RunReport("reocr")
        report.dry_run = dry
        taxonomy = TaxonomyResolver(self.client)
        safety = SafetyLayer(self.client, resolver, snapshot_dir=self._snapshot_dir("reocr"))
        spend = SpendGovernor(max_spend=self.settings.spend.per_run)
        provider = build_provider(TASK_OCR, self.cfg)
        pipeline = OcrPipeline(
            self.client, resolver, safety, spend,
            api_key=self.cfg.anthropic_api_key,
            built_dir=str(self.data("built_pdfs")), provider=provider,
            # Resolved OCR instruction (default -> override -> +extra); None when
            # nothing is customized, so the adapter uses its built-in OCR_PROMPT.
            instruction=self.settings.resolved_instruction_or_none("ocr"),
        )
        superseded_tag_id = taxonomy.get_or_create_tag(self.settings.superseded_tag)
        self.logger.stage_transition("reocr", "start")
        if docs is None:
            queue = orch.fetch_reocr_queue(self.settings.triage_threshold, limit or 0)
        else:
            # Nudge path: filter the single pulled doc through the SAME eligibility
            # predicate the sweep uses (client-side, plan §4.2).
            queue = [d for d in docs if orch.reocr_matches(d, self.settings.triage_threshold)]

        def _on_record(*, doc, new_doc_id, dry_run):
            self._record_reocr(report.run_id, doc, new_doc_id, dry_run=dry_run)

        def process(doc):
            if not dry and self._period_cap_hit(spend):
                # No-op (cap): no re-OCR happened -> record NO activity row.
                return ("spend_cap", doc["id"], "per-period cap reached", 0.0)
            status, doc_id, msg, cost = pipeline.process_one(
                doc, superseded_tag_id, dry_run=dry, on_record=_on_record)
            if not dry and cost:
                self.ledger.add(cost)
            if status.startswith("done") or status == "reocr_done":
                report.note_superseded(doc["id"])
            return (status, doc_id, msg, cost)

        self._drain(process, queue, report, stage="reocr",
                    run_id=report.run_id, dry_run=dry)
        report.finish()
        self.logger.stage_transition("reocr", "end", counts=report.counts())
        return report

    def _record_reocr(self, run_id, doc, new_doc_id, *, dry_run):
        """Record the re-OCR supersession: old doc -> superseded, new doc id
        created (proposed in dry-run). Best-effort."""
        from .activity import changes_summary

        old_id = doc.get("id")
        if dry_run:
            changes = {
                "supersede": {"old_doc_id": old_id, "new_doc_id": None},
                "fields": {"ai_stage": {
                    "before": None, "after": self.settings.stage_names.reocr_done}},
                "flags": ["would-supersede"],
            }
            status = "dry"
        else:
            changes = {
                "supersede": {"old_doc_id": old_id, "new_doc_id": new_doc_id},
                "tags": {"added": [self.settings.superseded_tag]},
                "fields": {"ai_stage": {
                    "before": None, "after": self.settings.stage_names.reocr_done}},
            }
            status = "done"
        self._record_activity({
            "run_id": run_id,
            "doc_id": old_id,
            "doc_title": doc.get("title"),
            "stage": "reocr",
            "dry_run": dry_run,
            "status": status,
            "changes": changes,
            "summary": changes_summary(changes),
            "paperless_url": self._doc_url(old_id),
        })

    # -- stage: metadata (billable) ----------------------------------------
    def _run_metadata(self, resolver, orch, dry, limit, docs=None):
        report = RunReport("metadata")
        report.dry_run = dry
        taxonomy = TaxonomyResolver(self.client)
        safety = SafetyLayer(self.client, resolver, snapshot_dir=self._snapshot_dir("metadata"))
        spend = SpendGovernor(max_spend=self.settings.spend.per_run)
        new_tax_tag_id = taxonomy.get_or_create_tag(self.settings.new_taxonomy_tag)
        provider = build_provider(TASK_METADATA, self.cfg)
        extractor = MetadataExtractor(
            self.client, resolver, taxonomy, safety, spend,
            api_key=self.cfg.anthropic_api_key,
            new_tax_tag_id=new_tax_tag_id, provider=provider,
            # Resolved metadata instruction (default -> override -> +extra); None
            # when nothing is customized -> build_prompt uses the default constant.
            instruction=self.settings.resolved_instruction_or_none("metadata"),
            # Prompt 011: configurable content window (defaults byte-identical).
            content_head=self.settings.metadata_window.content_head,
            content_tail=self.settings.metadata_window.content_tail,
        )
        self.logger.stage_transition("metadata", "start")
        if docs is None:
            queue = orch.fetch_metadata_queue(limit or 0)
        else:
            # Nudge path: same metadata-eligibility predicate as the sweep.
            queue = [d for d in docs if orch.metadata_eligible(d)]

        def _on_record(*, doc, meta, dry_run, created):
            self._record_metadata(report.run_id, doc, meta, taxonomy, resolver,
                                  dry_run=dry_run, created=created)

        def process(doc):
            if not dry and self._period_cap_hit(spend):
                # No-op (cap): nothing extracted/applied -> record NO activity row.
                return ("spend_cap", doc["id"], "per-period cap reached", 0.0)
            status, doc_id, msg, cost = extractor.process_one(
                doc, dry_run=dry, on_record=_on_record)
            if not dry and cost:
                self.ledger.add(cost)
            return (status, doc_id, msg, cost)

        self._drain(process, queue, report, stage="metadata",
                    run_id=report.run_id, dry_run=dry)
        report.finish()
        self.logger.stage_transition("metadata", "end", counts=report.counts())
        return report

    def _record_metadata(self, run_id, doc, meta, taxonomy, resolver, *,
                        dry_run, created):
        """Record the metadata field-level diff (title / correspondent /
        document_type / tags added-removed / ai_stage transition), proposed in
        dry-run and applied otherwise. Best-effort: 'before' = the current doc
        (ids resolved back to names via the taxonomy maps), 'after' = the
        proposed/applied meta."""
        from .activity import diff_fields, tag_delta, changes_summary

        try:
            id_to_corr = {v: k for k, v in taxonomy.correspondents.items()}
            id_to_type = {v: k for k, v in taxonomy.doc_types.items()}
            id_to_tag = {v: k for k, v in taxonomy.tags.items()}

            before = {
                "title": doc.get("title"),
                "correspondent": id_to_corr.get(doc.get("correspondent")),
                "document_type": id_to_type.get(doc.get("document_type")),
                "ai_stage": resolver.stage_label_from_value(
                    {c["field"]: c.get("value")
                     for c in (doc.get("custom_fields") or [])}.get(
                        resolver.stage_field_id())),
            }
            after = {
                "title": (meta.get("title") or "").strip() or None,
                "correspondent": (meta.get("correspondent") or "").strip() or None,
                "document_type": (meta.get("document_type") or "").strip() or None,
                "ai_stage": self.settings.stage_names.metadata_done,
            }
            fields = diff_fields(before, after)

            before_tags = [id_to_tag.get(t) for t in (doc.get("tags") or [])]
            before_tags = [t for t in before_tags if t]
            # After tags = existing (kept) + proposed tag names (merge-not-clobber).
            after_tags = list(before_tags)
            for tname in meta.get("tags") or []:
                if tname and tname not in after_tags:
                    after_tags.append(tname)
            if created and not dry_run:
                if self.settings.new_taxonomy_tag not in after_tags:
                    after_tags.append(self.settings.new_taxonomy_tag)
            tags = tag_delta(before_tags, after_tags)

            changes = {"fields": fields}
            if tags:
                changes["tags"] = tags
            flags = []
            if meta.get("correspondent_is_new"):
                flags.append("new-correspondent")
            if meta.get("document_type_is_new"):
                flags.append("new-document-type")
            if meta.get("new_tags"):
                flags.append("new-tags")
            if created:
                flags.append("ai-new-taxonomy")
            if flags:
                changes["flags"] = flags
        except Exception:  # noqa: BLE001 — never let diff-building affect processing
            changes = {"fields": {}}

        self._record_activity({
            "run_id": run_id,
            "doc_id": doc.get("id"),
            "doc_title": doc.get("title"),
            "stage": "metadata",
            "dry_run": dry_run,
            "status": "dry" if dry_run else ("done_new_tax" if created else "done"),
            "changes": changes,
            "summary": changes_summary(changes),
            "paperless_url": self._doc_url(doc.get("id")),
        })

    # -- single-doc nudge path (Phase 4 webhook) ---------------------------
    # Fields the stages need, matching what the sweep's per-stage fetches request.
    _NUDGE_FIELDS = (
        "id,title,content,correspondent,document_type,tags,created,"
        "archive_serial_number,custom_fields"
    )

    def process_nudge(self, doc_id, *, dry=None) -> "MultiStageReport":
        """Process ONE document (identified by a webhook nudge) through the enabled
        stages, reusing the SAME orchestrator/safety/spend/ledger as the sweep so
        every invariant (I1-I5) holds unchanged. The nudge only carried the id; we
        PULL the doc via REST here — content pushed by the webhook is never trusted.

        Handles gracefully:
          * a doc that no longer exists (deleted) -> no-op;
          * a not-yet-OCR'd doc (no extractable content) -> SKIP; the scheduled
            sweep will pick it up once OCR finishes (plan §6.2). Never writes
            garbage.
        Idempotency (I1) makes a duplicate nudge a cheap no-op: an already-handled
        doc is skipped by the stage predicates exactly as in the sweep.
        """
        if dry is None:
            dry = self.resolve_dry_run()
        multi = MultiStageReport()
        multi.dry_run = dry
        # Prompt 012: automatic processing is paused — a webhook nudge is AUTOMATIC,
        # so skip it (the scheduled sweep will pick the doc up once resumed). We stay
        # alive and log a "paused" event; we never write.
        if self.is_paused():
            self.logger.event(
                "nudge_paused", run_id=multi.run_id, doc_id=doc_id,
                reason="automatic processing is paused; resume to process nudges",
            )
            multi.finish()
            return multi
        self.logger.event(
            "nudge_start", run_id=multi.run_id, doc_id=doc_id, dry_run=dry,
            stages=self.settings.enabled_stages(),
        )

        resolver = CustomFieldResolver(
            self.client,
            field_names=self.settings.field_names,
            stage_names=self.settings.stage_names,
        )
        orch = StageOrchestrator(self.client, resolver, self.settings)

        doc = self.client.get_document(doc_id, fields=self._NUDGE_FIELDS)
        if doc is None:
            self.logger.event("nudge_skip", run_id=multi.run_id, doc_id=doc_id,
                              reason="document does not exist")
            multi.finish()
            return multi

        # Not-yet-OCR guard (plan §6.2 / §r3): if content has not been extracted
        # yet (e.g. a nudge slipped through on "Document Added" before OCR), do
        # NOT process — the sweep will get it later. Never write garbage.
        if not (doc.get("content") or "").strip():
            self.logger.event(
                "nudge_skip", run_id=multi.run_id, doc_id=doc_id,
                reason="no OCR text yet (hook on Consumption Finished, not Added); "
                       "the scheduled sweep will process it once text exists",
            )
            multi.finish()
            return multi

        if self.settings.triage_enabled:
            multi.add(self._run_triage(resolver, orch, dry, None, docs=[doc]))
            doc = self.client.get_document(doc_id, fields=self._NUDGE_FIELDS) or doc
        if self.settings.reocr_enabled:
            multi.add(self._run_reocr(resolver, orch, dry, None, docs=[doc]))
            doc = self.client.get_document(doc_id, fields=self._NUDGE_FIELDS) or doc
        if self.settings.metadata_enabled:
            multi.add(self._run_metadata(resolver, orch, dry, None, docs=[doc]))

        multi.period_spend = self.ledger.current()
        multi.period_cap = self.settings.spend.per_period
        multi.finish()
        self.logger.event(
            "nudge_end", run_id=multi.run_id, doc_id=doc_id, dry_run=dry,
            counts=multi.merged_counts(), spend=multi.total_spend(),
            period_spend=multi.period_spend,
        )
        return multi

    # -- shared worker drain -----------------------------------------------
    def _drain(self, process, queue, report, *, stage, run_id=None, dry_run=None):
        # Live progress: register the stage + its document total before draining
        # (even an empty stage registers, so the UI shows 0/0 rather than nothing).
        self._progress.begin_stage(stage, len(queue))
        if not queue:
            return
        workers = max(1, self.settings.workers)
        # Fail fast on a FATAL provider error: a bad key (auth) OR an exhausted
        # account (out of credits / over quota) rejects EVERY doc, so once we see
        # one, short-circuit the rest of the stage instead of making a doomed
        # (zero-cost, so uncapped) call per document. This ALSO escalates to the
        # run so the remaining billable stages are skipped (see run_once).
        abort = threading.Event()

        def guarded(d):
            if abort.is_set():
                return ("skip", d["id"], _SKIP_ON_FATAL, 0.0)
            return process(d)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(guarded, d): d for d in queue}
            for fut in as_completed(futs):
                d = futs[fut]
                try:
                    status, doc_id, msg, cost = fut.result()
                except Exception as e:  # surface the REAL error (I6)
                    status, doc_id, msg, cost = ("ERROR", d["id"], str(e), 0.0)
                    self.logger.failure(doc_id, e, stage=stage)
                    # Fail-fast FIRST: set the abort before any (comparatively slow)
                    # audit-log write, so it short-circuits the rest of the stage
                    # without recording-latency letting more docs run.
                    reason = _provider_fatal_reason(e)
                    if reason and not abort.is_set():
                        abort.set()
                        self._note_fatal_provider_error(reason, e, stage)
                    # Prompt 013: record the REAL error against the doc so
                    # troubleshooting sees it. Best-effort, observational; a
                    # fatal-abort skip is a no-op and gets NO row.
                    if _SKIP_ON_FATAL not in (msg or ""):
                        self._record_activity({
                            "run_id": run_id,
                            "doc_id": doc_id,
                            "doc_title": d.get("title") if isinstance(d, dict) else None,
                            "stage": stage,
                            "dry_run": bool(dry_run),
                            "status": "ERROR",
                            "changes": {"error": str(e)},
                            "summary": "ERROR: " + str(e)[:120],
                            "paperless_url": self._doc_url(doc_id),
                        })
                report.record(status, doc_id, msg, cost)
                report.spend_total += cost
                if "NEW" in (msg or "") or status.endswith("_new_tax"):
                    report.note_new_taxonomy(_parse_new_names(msg))
                self.logger.doc_outcome(doc_id, status, msg, cost, stage=stage)
                # Live progress: push this document's outcome (what it did with it)
                # so the dashboard's auto-refreshing panel updates in near-real-time.
                self._progress.record_doc(
                    stage, status, doc_id,
                    title=d.get("title") if isinstance(d, dict) else None,
                    summary=msg, cost=cost, url=self._doc_url(doc_id))

    def _period_cap_hit(self, spend) -> bool:
        """Both the per-run governor (I3) AND the persisted per-period ledger gate
        starting new billable work (plan §7.2 per-period cap)."""
        if spend.should_abort():
            return True
        return self.ledger.would_exceed(self.settings.spend.per_period)

    # -- status ------------------------------------------------------------
    def status(self, *, queue_depth=None):
        return build_status(self.settings, self.cursor, self.ledger, queue_depth=queue_depth)


def _parse_new_names(msg):
    """Best-effort pull of 'NEW corr=X', 'NEW type=Y', 'NEW tags=[...]' out of the
    metadata detail line so the run report's new_taxonomy list is populated."""
    names = []
    if not msg:
        return names
    for token in ("NEW corr=", "NEW type="):
        i = msg.find(token)
        if i >= 0:
            rest = msg[i + len(token):]
            end = rest.find(";")
            end = end if end >= 0 else rest.find("]")
            names.append(rest[: end if end >= 0 else len(rest)].strip())
    i = msg.find("NEW tags=")
    if i >= 0:
        rest = msg[i + len("NEW tags="):]
        rest = rest.strip().lstrip("[").rstrip("]")
        for part in rest.split(","):
            p = part.strip().strip("'\"")
            if p:
                names.append(p)
    return [n for n in names if n]


def serve(settings, *, iterations=None, sleep_fn=time.sleep, logger=None):
    """Run `run_once` on an interval loop (plan §6.2 scheduled sweep). Restart-safe
    via idempotency + the /data cursor. `iterations` bounds the loop for tests
    (None = forever). Returns the list of sweep reports produced.

    If the on-ingest webhook is enabled (plan §6.2), a stdlib nudge receiver runs
    ALONGSIDE the scheduler (in a background thread, bound inside the compose
    network with no host port). The scheduled sweep remains AUTHORITATIVE — the
    webhook is a latency optimisation and correctness never depends on it."""
    sweep = Sweep(settings, logger=logger)
    interval = max(1, settings.schedule_interval_seconds)
    reports = []
    n = 0
    sweep.logger.event("serve_start", interval_seconds=interval)

    webhook_server = None
    if settings.webhook.enabled:
        from .webhook import WebhookServer

        webhook_server = WebhookServer(settings, sweep, logger=sweep.logger)
        webhook_server.start()
    # A failing sweep (e.g. Paperless briefly unreachable, or a misconfigured
    # PAPERLESS_URL) must NOT crash the long-running agent: an unhandled exception
    # here would exit the process and, under `restart: unless-stopped`, crash-loop
    # the container. Instead we log the real error (I6) and retry. On failure we
    # retry on a SHORT delay (so a Paperless that is merely slow to boot is picked
    # up within ~30s rather than a full interval); a success resets to the interval.
    error_retry = min(interval, 30)
    try:
        while iterations is None or n < iterations:
            # Prompt 012: honor the persisted pause switch. When paused we SKIP the
            # sweep but STAY ALIVE (the container keeps running under
            # `restart: unless-stopped`); a manual "Run now" from the dashboard is an
            # explicit action and is unaffected. Re-read each tick so a resume from
            # the dashboard takes effect on the next tick.
            if sweep.is_paused():
                sweep.logger.event(
                    "sweep_paused",
                    reason="automatic processing is paused (dashboard/CLI pause); "
                           "the scheduler is alive but skipping sweeps until resumed",
                )
                n += 1
                if iterations is not None and n >= iterations:
                    break
                sleep_fn(interval)
                continue
            try:
                reports.append(sweep.run_once())
                delay = interval
            except Exception as e:  # noqa: BLE001 - the loop must survive ANY tick error
                sweep.logger.event("sweep_error", level="error",
                                   error=str(e), retry_seconds=error_retry)
                print(
                    f"pa serve: sweep failed: {e}\n"
                    f"  will retry in {error_retry}s. Check that PAPERLESS_URL points "
                    f"at your Paperless service and it is reachable "
                    f"(run `pa doctor` to diagnose).",
                    file=sys.stderr, flush=True,
                )
                delay = error_retry
            n += 1
            if iterations is not None and n >= iterations:
                break
            sleep_fn(delay)
    finally:
        if webhook_server is not None:
            webhook_server.stop()
    sweep.logger.event("serve_stop", ticks=n)
    return reports
