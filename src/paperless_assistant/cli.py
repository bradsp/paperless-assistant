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

"""pa CLI - triage / reocr / metadata subcommands over the extracted engine.

Reproduces the three scripts' flags, semantics and console output. Config
resolution stays minimal for Phase 1: env vars (PAPERLESS_URL/TOKEN,
ANTHROPIC_API_KEY) + CLI flags, exactly matching the scripts' argparse reality.
Dry-run is first-class (I7).
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import json
import sys

from . import config
from .client import PaperlessClient
from .fields import CustomFieldResolver
from .taxonomy import TaxonomyResolver
from .safety import SafetyLayer
from .spend import SpendGovernor
from .stages import StageOrchestrator
from .ocr import OcrPipeline, garbage_score
from .metadata import MetadataExtractor
from .report import RunReport
from .providers import build_provider, TASK_OCR, TASK_METADATA


def _client(require_anthropic=False, **cfg_overrides):
    cfg = config.Config.from_env(require_anthropic=require_anthropic, **cfg_overrides)
    return cfg, PaperlessClient(cfg.base_url, cfg.paperless_token)


def _resolved_instruction(args, task):
    """Resolve the per-task instruction (default -> override -> +extra) from the
    LAYERED settings so the legacy `pa reocr` / `pa metadata` subcommands honor
    prompt customization from /data/config.yml + env. Returns None when nothing is
    customized, so the engine uses its built-in default (byte-identical). Any
    resolution problem degrades gracefully to the default (None)."""
    try:
        settings = config.load_settings(
            config_file=getattr(args, "config", None), require_token=False,
        )
        return settings.resolved_instruction_or_none(task)
    except Exception:  # noqa: BLE001 — never block a run on prompt resolution
        return None


# ===========================================================================
# triage (stage0)
# ===========================================================================
def cmd_triage(args):
    cfg, client = _client()
    resolver = CustomFieldResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir="./snapshots")
    orch = StageOrchestrator(client, resolver)

    fmap = resolver
    print(
        f"Resolved fields: "
        f"{config.FIELD_SCORE}=id{fmap.field_id(config.FIELD_SCORE)}/{fmap.data_type(config.FIELD_SCORE)}  "
        f"{config.FIELD_STAGE}=id{fmap.field_id(config.FIELD_STAGE)}/{fmap.data_type(config.FIELD_STAGE)}  "
        f"{config.FIELD_NOTES}=id{fmap.field_id(config.FIELD_NOTES)}/{fmap.data_type(config.FIELD_NOTES)}"
    )
    if fmap.data_type(config.FIELD_SCORE) not in ("float", "integer", "monetary"):
        print(
            f"  WARNING: '{config.FIELD_SCORE}' is type "
            f"'{fmap.data_type(config.FIELD_SCORE)}'. A Number (float) field is "
            f"recommended so you can filter with >= in the UI."
        )
    print(
        f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}  "
        f"threshold={args.threshold}  workers={args.workers}"
    )

    docs = orch.fetch_triage_queue(limit=args.limit)
    print(f"Fetched {len(docs)} documents.\n")

    def process(doc):
        if not args.force and orch.already_triaged(doc):
            return ("skip", doc["id"], None, None)
        score, note = garbage_score(doc.get("content", ""))
        if args.dry_run:
            return ("dry", doc["id"], score, note)
        safety.snapshot(doc)
        body = {"custom_fields": safety.merge_triage_fields(doc.get("custom_fields"), score, note)}
        client.request("PATCH", f"{client.base}/api/documents/{doc['id']}/", json=body)
        return ("wrote", doc["id"], score, note)

    flagged = wrote = skipped = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, d): d for d in docs}
        for fut in as_completed(futs):
            status, doc_id, score, note = fut.result()
            if status == "skip":
                skipped += 1
                continue
            if status in ("wrote", "dry"):
                if status == "wrote":
                    wrote += 1
                flag = score is not None and score >= args.threshold
                if flag:
                    flagged += 1
                marker = "  <-- FLAG re-OCR" if flag else ""
                print(f"[{status}] doc {doc_id:>5}  score={score}  {note}{marker}")

    dt_s = time.time() - t0
    print("\n--- summary ---")
    print(f"processed : {wrote if not args.dry_run else len(docs) - skipped}")
    print(f"skipped   : {skipped} (already triaged)")
    print(f"flagged   : {flagged} (>= {args.threshold}) for Stage 1 re-OCR")
    print(f"elapsed   : {dt_s:.1f}s")
    print(f"\nSnapshots saved to {safety.snapshot_dir.resolve()}")
    if not args.dry_run:
        print(
            f"\nIn the Paperless UI you can now filter: "
            f"{config.FIELD_SCORE} >= {args.threshold} to see the re-OCR queue."
        )


# ===========================================================================
# reocr (stage1)
# ===========================================================================
def cmd_reocr(args):
    cfg, client = _client(
        require_anthropic=True,
        ocr_provider=getattr(args, "provider", None),
        ocr_model=getattr(args, "model", None),
    )
    resolver = CustomFieldResolver(client)
    taxonomy = TaxonomyResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir="./snapshots_stage1")
    spend = SpendGovernor(max_spend=args.max_spend)
    orch = StageOrchestrator(client, resolver)
    provider = build_provider(TASK_OCR, cfg)
    pipeline = OcrPipeline(
        client, resolver, safety, spend,
        api_key=cfg.anthropic_api_key, built_dir="./built_pdfs", provider=provider,
        instruction=_resolved_instruction(args, "ocr"),
    )

    superseded_tag_id = taxonomy.get_or_create_superseded_tag()
    queue = orch.fetch_reocr_queue(args.threshold, args.limit)
    print(
        f"Queue: {len(queue)} document(s) with ai_stage=triaged and "
        f"{config.FIELD_SCORE} >= {args.threshold}"
    )
    print(
        f"Mode: {'DRY-RUN (no consume)' if args.dry_run else 'FULL'}  "
        f"workers={args.workers}  provider={cfg.ocr_provider}  model={cfg.ocr_model}  "
        f"spend_cap={'$'+format(args.max_spend, '.2f') if args.max_spend else 'none'}\n"
    )
    if not queue:
        print("Nothing to do.")
        return

    report = RunReport("reocr")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(pipeline.process_one, d, superseded_tag_id, dry_run=args.dry_run): d
            for d in queue
        }
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                status, doc_id, msg, cost = fut.result()
            except Exception as e:
                status, doc_id, msg, cost = ("ERROR", d["id"], str(e), 0.0)
            report.record(status, doc_id, msg, cost)
            print(f"[{status}] doc {doc_id}: {msg}  (${cost:.3f})")

    print("\n--- summary ---")
    for k, v in report.counts().items():
        print(f"{k}: {v}")
    print(f"approx spend: ${spend.total:.2f}")
    print(f"\nCorrected PDFs + text in: {pipeline.built_dir.resolve()}")
    print(f"Metadata snapshots in:    {safety.snapshot_dir.resolve()}")
    if not args.dry_run:
        print(
            f"\nReview the re-OCR'd documents, then in the Paperless UI filter "
            f"tag:{config.SUPERSEDED_TAG} and bulk-delete the old originals once you're satisfied."
        )


# ===========================================================================
# metadata (stage2)
# ===========================================================================
def cmd_metadata(args):
    cfg, client = _client(
        require_anthropic=True,
        metadata_provider=getattr(args, "provider", None),
        metadata_model=getattr(args, "model", None),
    )
    resolver = CustomFieldResolver(client)
    taxonomy = TaxonomyResolver(client)
    safety = SafetyLayer(client, resolver, snapshot_dir="./snapshots_stage2")
    spend = SpendGovernor(max_spend=args.max_spend)
    orch = StageOrchestrator(client, resolver)
    new_tax_tag_id = taxonomy.get_or_create_tag(config.NEW_TAXONOMY_TAG)
    provider = build_provider(TASK_METADATA, cfg)
    extractor = MetadataExtractor(
        client, resolver, taxonomy, safety, spend,
        api_key=cfg.anthropic_api_key, new_tax_tag_id=new_tax_tag_id, provider=provider,
        instruction=_resolved_instruction(args, "metadata"),
    )

    queue = orch.fetch_metadata_queue(args.limit)
    print(
        f"Eligible queue: {len(queue)} document(s)  "
        f"(excludes ocr_quality >= {config.GARBAGE_THRESH} and already-done)"
    )
    print(
        f"Existing taxonomy: {len(taxonomy.tags)} tags, "
        f"{len(taxonomy.correspondents)} correspondents, {len(taxonomy.doc_types)} types"
    )
    print(
        f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}  provider={cfg.metadata_provider}  "
        f"model={cfg.metadata_model}  workers={args.workers}  "
        f"spend_cap={'$'+format(args.max_spend, '.2f') if args.max_spend else 'none'}\n"
    )
    if not queue:
        print("Nothing to do.")
        return

    report = RunReport("metadata")
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(extractor.process_one, d, dry_run=args.dry_run): d for d in queue}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                status, doc_id, msg, cost = fut.result()
            except Exception as e:
                status, doc_id, msg, cost = ("ERROR", d["id"], str(e), 0.0)
            report.record(status, doc_id, msg, cost)
            print(f"[{status}] doc {doc_id}: {msg}  (${cost:.4f})")

    print("\n--- summary ---")
    for k, v in report.counts().items():
        print(f"{k}: {v}")
    print(f"approx spend: ${spend.total:.2f}")
    print(f"snapshots: {safety.snapshot_dir.resolve()}")
    if not args.dry_run:
        print(f"\nReview newly-created taxonomy: filter tag:{config.NEW_TAXONOMY_TAG} in the UI.")


# ===========================================================================
# Phase 3 onboarding + sweep subcommands
# ===========================================================================
def _settings(args, *, require_token=True):
    """Resolve the layered Settings (plan §7.1) with CLI flags as per-run
    overrides (highest precedence)."""
    overrides = {}
    for name in ("workers", "limit", "triage_threshold", "reocr_enabled", "force"):
        val = getattr(args, name, None)
        if val is not None:
            overrides[name] = val
    if getattr(args, "dry_run", None):
        overrides["dry_run"] = True
    if getattr(args, "write", None):  # `pa run --write` forces a real write
        overrides["dry_run"] = False
    if getattr(args, "per_run_cap", None) is not None:
        overrides["per_run_cap"] = args.per_run_cap
    return config.load_settings(
        config_file=getattr(args, "config", None),
        overrides=overrides,
        require_token=require_token,
    )


def cmd_init(args):
    from . import initcmd

    print(initcmd.render(write_path=getattr(args, "out", None)))
    if getattr(args, "out", None):
        print(f"\nWrote compose block to {args.out}")


def cmd_setup(args):
    from .provision import Provisioner

    settings = _settings(args)
    client = PaperlessClient(settings.base_url, settings.paperless_token)
    prov = Provisioner(
        client,
        superseded_tag=settings.superseded_tag,
        new_taxonomy_tag=settings.new_taxonomy_tag,
    )
    report = prov.run()

    if report.created_fields:
        print(f"Created custom fields: {', '.join(report.created_fields)}")
    if report.existing_fields:
        print(f"Custom fields already present: {', '.join(report.existing_fields)}")
    if report.created_tags:
        print(f"Created review tags: {', '.join(report.created_tags)}")
    if report.existing_tags:
        print(f"Review tags already present: {', '.join(report.existing_tags)}")
    if report.is_noop:
        print("Nothing to do - all prerequisites already in place (idempotent no-op).")
    for msg in report.incompatible:
        print(f"INCOMPATIBLE: {msg}", file=sys.stderr)
    if not report.ok:
        print("\n`pa setup` found incompatible prerequisites (see above). "
              "Nothing was clobbered.", file=sys.stderr)
        raise SystemExit(2)
    print("\nSetup OK. Next: `pa doctor`.")


def cmd_doctor(args):
    from .doctor import run_doctor, OK, WARN, FAIL
    from .config import ConfigError

    try:
        settings = _settings(args)
    except ConfigError as e:
        print(f"[FAIL] config: {e}", file=sys.stderr)
        raise SystemExit(2)
    client = PaperlessClient(settings.base_url, settings.paperless_token)
    result = run_doctor(settings, client)

    icon = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    for c in result.checks:
        print(f"[{icon[c.status]}] {c.name}: {c.message}")
        if c.fix and c.status != OK:
            print(f"         fix: {c.fix}")
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2))
    if result.failed:
        print("\npa doctor: FAILED - see the fixes above.", file=sys.stderr)
        raise SystemExit(1)
    print("\npa doctor: all green.")


def cmd_run(args):
    from .sweep import Sweep

    settings = _settings(args)
    sweep = Sweep(settings)
    dry = sweep.resolve_dry_run()
    print(
        f"Sweep: stages={settings.enabled_stages()} "
        f"mode={'DRY-RUN' if dry else 'WRITE'} "
        f"reocr={'on' if settings.reocr_enabled else 'OFF (default)'} "
        f"workers={settings.workers} "
        f"spend_cap per_run=${settings.spend.per_run:.2f} "
        f"per_{settings.spend.period}=${settings.spend.per_period:.2f}"
    )
    if dry and settings.dry_run is None:
        print("(first run -> bounded DRY-RUN; you will see proposed changes before "
              "anything is written. Re-run to apply, or set dry_run in config.)")
    multi = sweep.run_once()
    print("\n--- sweep summary ---")
    for k, v in multi.merged_counts().items():
        print(f"{k}: {v}")
    print(f"spend (this run): ${multi.total_spend():.4f}")
    print(f"spend (period {settings.spend.period}): ${(multi.period_spend or 0):.4f} "
          f"/ cap ${settings.spend.per_period:.2f}")
    if multi.all_new_taxonomy():
        print(f"new taxonomy proposed: {multi.all_new_taxonomy()}")
    if multi.all_superseded():
        print(f"superseded docs: {multi.all_superseded()}")
    path = settings.data_path("run-reports")
    print(f"run report written under {path}")


def cmd_serve(args):
    from .sweep import serve

    settings = _settings(args)

    # Phase 5: hosted mode (Mode B) runs the OUTBOUND-ONLY pull-loop instead of the
    # local scheduler. The agent dials OUT to the control plane and pulls work; it
    # binds NO inbound listener and needs NO host port. Inference stays BYO.
    if settings.hosted_mode():
        return _serve_hosted(settings, args)

    # `--webhook` forces the nudge receiver on for this run (env/YAML otherwise).
    if getattr(args, "webhook", False):
        settings.webhook.enabled = True
    wh = settings.webhook
    if wh.enabled:
        if not wh.secret:
            print(
                "pa serve: webhook is enabled but PA_WEBHOOK_SECRET is not set. "
                "Refusing to start an unauthenticated nudge receiver — set "
                "PA_WEBHOOK_SECRET in the environment (never the YAML).",
                file=sys.stderr,
            )
            raise SystemExit(2)
        print(
            f"pa serve: sweeping every {settings.schedule_interval_seconds}s "
            f"(stages={settings.enabled_stages()}); webhook nudge receiver on "
            f"{wh.host}:{wh.port}{wh.path} (in-network, no host port). Ctrl-C to stop."
        )
    else:
        print(
            f"pa serve: sweeping every {settings.schedule_interval_seconds}s "
            f"(stages={settings.enabled_stages()}); webhook OFF (scheduled sweep "
            f"only). Ctrl-C to stop."
        )
    # Opt-in web dashboard alongside the scheduler (one container = scheduler + UI).
    # Fail closed without a token. Bound as the (non-root) pa user; the user maps a
    # host port to reach it.
    ui_server = _start_ui_thread(settings, args)
    iters = getattr(args, "iterations", None)
    try:
        serve(settings, iterations=iters)
    finally:
        if ui_server is not None:
            ui_server.stop()


def _serve_hosted(settings, args):
    """Run the hosted-mode outbound pull-loop (Phase 5). Outbound-only: no inbound
    listener, no host port. Enrolls once (PA_ENROLLMENT_TOKEN -> agent credential
    persisted under /data), then long-polls the control plane for work."""
    from .hosted import HostedAgent, EnrollmentError

    h = settings.hosted
    if not h.control_plane_url:
        print(
            "pa serve: PA_MODE=hosted but PA_CONTROL_PLANE_URL is not set. Set the "
            "control-plane URL the agent should dial OUT to.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(
        f"pa serve (HOSTED mode): dialing OUT to {h.control_plane_url}; pulling "
        f"work via long-poll (outbound-only, NO inbound listener, NO host port). "
        f"Inference stays BYO/agent-side. Ctrl-C to stop."
    )
    agent = HostedAgent(settings)
    try:
        agent.run(iterations=getattr(args, "iterations", None))
    except EnrollmentError as e:
        print(f"pa serve: hosted enrollment failed: {e}", file=sys.stderr)
        raise SystemExit(2)


def _start_ui_thread(settings, args):
    """Start the web dashboard as a background thread (used by `pa serve` when
    PA_UI_ENABLED). Fail closed: no token -> refuse to start (mirror the webhook).
    Returns the WebUIServer, or None if the UI is disabled."""
    if not settings.ui.enabled:
        return None
    from .webui import WebUIServer

    if not settings.ui.token:
        print(
            "pa serve: web UI is enabled (PA_UI_ENABLED) but PA_UI_TOKEN is not set. "
            "Refusing to start an unauthenticated dashboard — set PA_UI_TOKEN in the "
            "environment (never the YAML config).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    server = WebUIServer(settings, config_file=getattr(args, "config", None))
    server.start()
    print(
        f"pa serve: web dashboard on {settings.ui.host}:{settings.ui.port} "
        f"(PUBLISHED host port; token-protected). "
    )
    return server


def cmd_web(args):
    """`pa web` — run ONLY the web dashboard (stdlib HTTP + one self-contained HTML
    page), token-protected. Fail closed without PA_UI_TOKEN."""
    from .webui import WebUIServer

    settings = _settings(args)
    # `pa web` implies the UI is on regardless of PA_UI_ENABLED (the user asked for
    # it explicitly) — but the token is still mandatory (fail closed).
    settings.ui.enabled = True
    if not settings.ui.token:
        print(
            "pa web: PA_UI_TOKEN is not set. Refusing to start an unauthenticated "
            "dashboard. Set PA_UI_TOKEN in the environment (never the YAML config).",
            file=sys.stderr,
        )
        raise SystemExit(2)
    server = WebUIServer(settings, config_file=getattr(args, "config", None))
    print(
        f"pa web: dashboard on {settings.ui.host}:{settings.ui.port} "
        f"(publish this port to reach it in a browser; token-protected). Ctrl-C to stop."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_status(args):
    from .sweep import Sweep

    settings = _settings(args)
    sweep = Sweep(settings)
    status = sweep.status()
    print(json.dumps(status, indent=2))


def build_parser():
    ap = argparse.ArgumentParser(prog="pa", description="Paperless Assistant core engine CLI")
    sub = ap.add_subparsers(dest="command", required=True)

    p_tri = sub.add_parser("triage", help="score OCR quality into custom fields (stage 0)")
    p_tri.add_argument("--dry-run", action="store_true", help="score and report, write nothing")
    p_tri.add_argument("--force", action="store_true", help="re-triage docs already marked triaged")
    p_tri.add_argument("--threshold", type=float, default=config.DEFAULT_TRIAGE_THRESHOLD,
                       help="score above which a doc is flagged for re-OCR")
    p_tri.add_argument("--limit", type=int, default=0, help="process at most N documents (0 = all)")
    p_tri.add_argument("--workers", type=int, default=config.DEFAULT_TRIAGE_WORKERS,
                       help="concurrent requests (keep modest; <= DB pool)")
    p_tri.set_defaults(func=cmd_triage)

    p_re = sub.add_parser("reocr", help="Claude vision re-OCR of garbage scans (stage 1)")
    p_re.add_argument("--dry-run", action="store_true",
                      help="OCR + build corrected PDFs, but DO NOT consume or modify Paperless")
    p_re.add_argument("--threshold", type=float, default=config.DEFAULT_TRIAGE_THRESHOLD)
    p_re.add_argument("--limit", type=int, default=0)
    p_re.add_argument("--workers", type=int, default=config.DEFAULT_REOCR_WORKERS,
                      help="concurrent docs (keep low: <= DB pool and gentle on the API)")
    p_re.add_argument("--max-spend", type=float, default=0.0,
                      help="USD ceiling; abort starting new docs once exceeded (0 = no cap)")
    p_re.add_argument("--provider", default=None,
                      help="AI provider for re-OCR: anthropic (default) | openai | ollama")
    p_re.add_argument("--model", default=None,
                      help="override the vision model for re-OCR")
    p_re.set_defaults(func=cmd_reocr)

    p_md = sub.add_parser("metadata", help="AI metadata refresh (stage 2)")
    p_md.add_argument("--dry-run", action="store_true")
    p_md.add_argument("--limit", type=int, default=0)
    p_md.add_argument("--workers", type=int, default=config.DEFAULT_METADATA_WORKERS)
    p_md.add_argument("--max-spend", type=float, default=0.0)
    p_md.add_argument("--provider", default=None,
                      help="AI provider for metadata: anthropic (default) | openai | ollama")
    p_md.add_argument("--model", default=None,
                      help="override the metadata model")
    p_md.set_defaults(func=cmd_metadata)

    # -- Phase 3 onboarding ------------------------------------------------
    p_init = sub.add_parser("init", help="print the docker-compose service block (plan §8.1)")
    p_init.add_argument("--out", default=None,
                        help="also write the compose block to this file")
    p_init.set_defaults(func=cmd_init)

    p_setup = sub.add_parser(
        "setup", help="idempotently provision required custom fields + review tags")
    p_setup.add_argument("--config", default=None, help="path to a YAML config file")
    p_setup.set_defaults(func=cmd_setup)

    p_doc = sub.add_parser(
        "doctor", help="check connectivity, token scope, fields/tags, providers, config")
    p_doc.add_argument("--config", default=None, help="path to a YAML config file")
    p_doc.add_argument("--json", action="store_true", help="also print machine-readable JSON")
    p_doc.set_defaults(func=cmd_doctor)

    # -- Phase 3 sweep -----------------------------------------------------
    def _add_sweep_flags(p):
        p.add_argument("--config", default=None, help="path to a YAML config file")
        p.add_argument("--dry-run", action="store_true",
                       help="force a dry-run (propose, write nothing)")
        p.add_argument("--write", action="store_true",
                       help="force a real write even on the first run")
        p.add_argument("--force", action="store_true", help="re-process already-done docs")
        p.add_argument("--limit", type=int, default=None,
                       help="process at most N docs per stage (0 = all)")
        p.add_argument("--workers", type=int, default=None)
        p.add_argument("--threshold", dest="triage_threshold", type=float, default=None,
                       help="triage threshold override")
        p.add_argument("--reocr", dest="reocr_enabled", action="store_true", default=None,
                       help="enable the (default-OFF) re-OCR stage for this run")
        p.add_argument("--max-spend", dest="per_run_cap", type=float, default=None,
                       help="per-run USD spend cap override")

    p_run = sub.add_parser("run", help="run one sweep tick over the enabled stages")
    _add_sweep_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_serve = sub.add_parser(
        "serve",
        help="run scheduled sweeps on an interval (restart-safe); optionally the "
             "on-ingest webhook nudge receiver alongside")
    _add_sweep_flags(p_serve)
    p_serve.add_argument("--iterations", type=int, default=None,
                         help="stop after N ticks (default: run forever)")
    p_serve.add_argument(
        "--webhook", action="store_true", default=False,
        help="also run the on-ingest webhook NUDGE receiver (Phase 4): a stdlib "
             "listener bound INSIDE the compose network (default 0.0.0.0:8765, "
             "path /hooks/paperless) with NO published host port. Requires "
             "PA_WEBHOOK_SECRET (env only). Paperless reaches it by service name "
             "via a Workflow->Webhook action on 'Consumption Finished'. The "
             "scheduled sweep stays authoritative.")
    p_serve.set_defaults(func=cmd_serve)

    # `pa agent` is an alias for `pa serve` (plan §6.2 wording).
    p_agent = sub.add_parser("agent", help="alias for `serve`")
    _add_sweep_flags(p_agent)
    p_agent.add_argument("--iterations", type=int, default=None)
    p_agent.add_argument("--webhook", action="store_true", default=False,
                         help="also run the on-ingest webhook nudge receiver")
    p_agent.set_defaults(func=cmd_serve)

    p_status = sub.add_parser("status", help="print a local status snapshot (no heartbeat)")
    p_status.add_argument("--config", default=None, help="path to a YAML config file")
    p_status.set_defaults(func=cmd_status)

    # -- Phase 8 web dashboard --------------------------------------------
    p_web = sub.add_parser(
        "web",
        help="run the token-protected web dashboard (stdlib HTTP + one "
             "self-contained HTML page): view status/stats/runs/errors, start "
             "manual sweeps, and edit tunables. Requires PA_UI_TOKEN (env only); "
             "refuses to start without it. Publishes a host port (unlike the "
             "outbound-only agent) — auth protects it.")
    p_web.add_argument("--config", default=None, help="path to a YAML config file")
    p_web.set_defaults(func=cmd_web)

    return ap


def main(argv=None):
    # In the container: start as root only to make a bind-mounted /data writable,
    # then drop to the non-root pa user. No-op outside the image (see container.py).
    from .container import drop_privileges_if_container_root
    drop_privileges_if_container_root()

    ap = build_parser()
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
