"""Live progress tracker: the in-memory ProgressTracker + its wiring into Sweep.

Covers the tracker's own semantics (counts, stage totals, recent-first ordering,
the active→finished lifecycle) and an end-to-end check that a real Sweep run
drives an injected tracker as it processes documents.
"""
from __future__ import annotations

from paperless_assistant.config import Settings, TaskProvider, SpendCaps
from paperless_assistant.progress import ProgressTracker
from paperless_assistant.sweep import Sweep
from fakes import (
    FakePaperless, make_custom_fields, healthy_tags,
    StubMessage, StubToolUseBlock, install_stub_anthropic,
)


# ---------------------------------------------------------------------------
# ProgressTracker unit semantics
# ---------------------------------------------------------------------------
def test_tracker_lifecycle_and_snapshot():
    tr = ProgressTracker(recent_max=3)
    # Nothing yet.
    assert tr.snapshot()["run_id"] is None

    tr.begin_run("r1", dry_run=False, source="scheduled", now=1000.0)
    snap = tr.snapshot()
    assert snap["active"] is True and snap["run_id"] == "r1"
    assert snap["dry_run"] is False and snap["source"] == "scheduled"
    assert snap["finished_at"] is None

    tr.begin_stage("triage", 2, now=1001.0)
    tr.record_doc("triage", "wrote", 1, title="a", summary="s1", cost=0.0,
                  url="/documents/1/details", now=1002.0)
    tr.record_doc("triage", "skip", 2, title="b", summary="already", cost=0.0, now=1003.0)
    tr.begin_stage("metadata", 1, now=1004.0)
    tr.record_doc("metadata", "wrote", 1, title="a", summary="meta", cost=0.25, now=1005.0)

    snap = tr.snapshot()
    stages = {s["stage"]: s for s in snap["stages"]}
    assert stages["triage"]["total"] == 2 and stages["triage"]["processed"] == 2
    assert stages["triage"]["counts"] == {"wrote": 1, "skip": 1}
    assert stages["metadata"]["processed"] == 1
    assert stages["metadata"]["spend"] == 0.25
    # Aggregate + ordering.
    assert snap["stage"] == "metadata"          # current stage is the last begun
    assert snap["spend_total"] == 0.25
    assert snap["counts"] == {"wrote": 2, "skip": 1}
    # Recent is newest-first.
    assert [r["doc_id"] for r in snap["recent"]] == [1, 2, 1]
    assert snap["recent"][0]["stage"] == "metadata"

    tr.end_run(counts={"wrote": 2, "skip": 1}, spend_total=0.25, now=1006.0)
    snap = tr.snapshot()
    assert snap["active"] is False and snap["finished_at"] == 1006.0
    # Finished snapshot stays visible until the next run begins.
    assert snap["run_id"] == "r1"


def test_tracker_recent_is_bounded():
    tr = ProgressTracker(recent_max=2)
    tr.begin_run("r", now=1.0)
    tr.begin_stage("triage", 5, now=1.0)
    for i in range(5):
        tr.record_doc("triage", "wrote", i, now=2.0 + i)
    snap = tr.snapshot()
    # processed count is EXACT even though the recent list is capped.
    assert snap["stages"][0]["processed"] == 5
    assert len(snap["recent"]) == 2
    assert [r["doc_id"] for r in snap["recent"]] == [4, 3]  # newest first


def test_begin_run_resets_previous_run():
    tr = ProgressTracker()
    tr.begin_run("old", now=1.0)
    tr.begin_stage("triage", 1, now=1.0)
    tr.record_doc("triage", "wrote", 9, now=2.0)
    tr.begin_run("new", now=3.0)
    snap = tr.snapshot()
    assert snap["run_id"] == "new"
    assert snap["stages"] == [] and snap["recent"] == []
    assert snap["counts"] == {} and snap["spend_total"] == 0.0


# ---------------------------------------------------------------------------
# End-to-end: a real Sweep run drives an injected tracker
# ---------------------------------------------------------------------------
def _docs():
    return [
        {"id": 1, "title": "clean",
         "content": "Dear Mr Smith, thank you for your payment of one hundred dollars "
                    "received on March 3. Your balance is now zero. Sincerely, Acme.",
         "tags": [], "custom_fields": []},
        {"id": 2, "title": "garbage", "content": "x q z",
         "tags": [], "custom_fields": []},
    ]


def _settings(tmp_path, **over):
    s = Settings(
        base_url="http://paperless.test:8000", paperless_token="tok",
        anthropic_api_key="sk-ant-test",
        data_dir=str(tmp_path / "data"),
        metadata_task=TaskProvider(provider="anthropic", model="claude-sonnet-4-6"),
        spend=SpendCaps(per_run=1.0, per_period=5.0, period="monthly"),
    )
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_sweep_drives_injected_tracker(tmp_path, monkeypatch):
    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock({
        "title": "Payment Confirmation", "correspondent": "Acme",
        "document_type": "Letter", "tags": ["billing"],
        "correspondent_is_new": True, "document_type_is_new": True,
        "new_tags": ["billing"]})]))
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    tr = ProgressTracker()
    s = _settings(tmp_path, dry_run=True)  # triage + metadata, no writes
    Sweep(s, client=fake.client(), progress=tr).run_once(source="manual")

    snap = tr.snapshot()
    # Run finished and is tagged as the manual run that produced it.
    assert snap["active"] is False and snap["run_id"] is not None
    assert snap["source"] == "manual" and snap["dry_run"] is True
    stages = {st["stage"]: st for st in snap["stages"]}
    # Both enabled stages registered; triage saw both docs.
    assert stages["triage"]["total"] == 2 and stages["triage"]["processed"] == 2
    assert "metadata" in stages
    # Per-document outcomes were captured (what it did with each), with the
    # non-secret public URL for the doc link.
    assert snap["recent"], "expected per-document progress events"
    doc_ids = {r["doc_id"] for r in snap["recent"]}
    assert {1, 2} <= doc_ids
    assert any(r["paperless_url"] and r["paperless_url"].endswith("/documents/1/details")
               for r in snap["recent"])


class _Credit(Exception):
    status_code = 400


def test_run_stops_and_surfaces_billing_error(tmp_path, monkeypatch):
    """Reported bug: when the account ran out of credits mid-run, the sweep kept
    processing the whole batch. Now it STOPS on the first credit error and the
    live dashboard surfaces why (kind='billing' + the provider's message)."""
    calls = []

    def _raise(**kw):
        calls.append(1)
        raise _Credit("Your credit balance is too low to access the Anthropic API.")

    install_stub_anthropic(monkeypatch, _raise)
    # A realistic batch (the bug was "processed every item"); each doc has clean,
    # non-garbage content so metadata actually calls the provider.
    many = [{"id": i, "title": f"doc {i}",
             "content": "Dear Mr Smith, thank you for your payment of one hundred "
                        "dollars received on March 3. Balance zero. Acme.",
             "tags": [], "custom_fields": []} for i in range(1, 41)]
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=many)
    tr = ProgressTracker()
    # metadata only (billable), single worker so the first error stops the rest.
    s = _settings(tmp_path, dry_run=True, triage_enabled=False, workers=1)
    sweep = Sweep(s, client=fake.client(), progress=tr)
    sweep.run_once(source="manual")

    # Did NOT call the provider for every document (fail fast, not the whole batch).
    assert len(calls) < len(many)
    # Run-level latch is set with the billing reason.
    assert sweep._fatal_provider_error["reason"] == "billing"
    # The live dashboard error is set + survives end_run() so the user sees it.
    err = tr.snapshot()["error"]
    assert err and err["kind"] == "billing" and err["stage"] == "metadata"
    assert "credit balance is too low" in err["message"].lower()
    assert err["help"]  # actionable guidance is included


def test_sweep_end_run_clears_active_even_on_error(tmp_path, monkeypatch):
    """A crash mid-run must still clear the active flag (the finally in run_once),
    so the dashboard never shows a permanently 'running' ghost run."""
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), docs=_docs())
    tr = ProgressTracker()
    s = _settings(tmp_path, dry_run=True)
    sweep = Sweep(s, client=fake.client(), progress=tr)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sweep, "_run_triage", _boom)
    try:
        sweep.run_once()
    except RuntimeError:
        pass
    assert tr.snapshot()["active"] is False
