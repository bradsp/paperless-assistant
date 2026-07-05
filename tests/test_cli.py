"""CLI smoke tests: `pa triage`/`metadata`/`reocr` run end-to-end offline.

Paperless HTTP is mocked via `responses`; Anthropic is stubbed. Proves the
subcommands wire the engine together and produce the expected console output
without a live server or real key.
"""
import time

import pytest
import responses

from paperless_assistant import cli
from fakes import StubMessage, StubTextBlock, StubToolUseBlock, install_stub_anthropic

BASE = "http://paperless.test:8000"

CUSTOM_FIELDS = {
    "results": [
        {"id": 1, "name": "ocr_quality", "data_type": "float"},
        {"id": 2, "name": "ai_stage", "data_type": "select",
         "extra_data": {"select_options": [
             {"id": "opt-t", "label": "triaged"},
             {"id": "opt-r", "label": "reocr_done"},
             {"id": "opt-m", "label": "metadata_done"},
         ]}},
        {"id": 3, "name": "ai_notes", "data_type": "text"},
    ],
    "next": None,
}


@responses.activate
def test_cli_triage_dry_run(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses.add(responses.GET, f"{BASE}/api/custom_fields/", json=CUSTOM_FIELDS)
    docs = {
        "results": [
            {"id": 1, "title": "clean", "content": "Dear Sir, thank you for your payment of one hundred dollars received. Regards, Acme.", "custom_fields": []},
            {"id": 2, "title": "garbage", "content": "x", "custom_fields": []},
        ],
        "next": None,
    }
    responses.add(responses.GET, f"{BASE}/api/documents/", json=docs)

    cli.main(["triage", "--dry-run", "--limit", "10"])
    out = capsys.readouterr().out
    assert "Mode: DRY-RUN" in out
    assert "Fetched 2 documents." in out
    assert "--- summary ---" in out
    # garbage doc (content "x") scores 1.0 -> flagged
    assert "FLAG re-OCR" in out
    # no PATCH performed in dry-run
    assert not any(c.request.method == "PATCH" for c in responses.calls)


@responses.activate
def test_cli_triage_writes_and_snapshots(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # snapshots land under tmp
    responses.add(responses.GET, f"{BASE}/api/custom_fields/", json=CUSTOM_FIELDS)
    responses.add(responses.GET, f"{BASE}/api/documents/",
                  json={"results": [{"id": 5, "title": "d", "content": "junk", "custom_fields": []}], "next": None})
    responses.add(responses.PATCH, f"{BASE}/api/documents/5/", json={})

    cli.main(["triage", "--limit", "1", "--workers", "1"])
    out = capsys.readouterr().out
    assert "[wrote] doc" in out
    assert (tmp_path / "snapshots" / "5.json").exists()  # I2


@responses.activate
def test_cli_metadata_dry_run(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    responses.add(responses.GET, f"{BASE}/api/custom_fields/", json=CUSTOM_FIELDS)
    responses.add(responses.GET, f"{BASE}/api/tags/", json={"results": [], "next": None})
    responses.add(responses.GET, f"{BASE}/api/correspondents/", json={"results": [], "next": None})
    responses.add(responses.GET, f"{BASE}/api/document_types/", json={"results": [], "next": None})
    # get_or_create_tag(ai-new-taxonomy): name__iexact GET then create
    responses.add(responses.GET, f"{BASE}/api/tags/", json={"results": [{"id": 555, "name": "ai-new-taxonomy"}]})
    responses.add(responses.GET, f"{BASE}/api/documents/",
                  json={"results": [{"id": 9, "title": "t", "content": "invoice text", "tags": [], "custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.1}]}], "next": None})

    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubToolUseBlock({
        "title": "Nice Title", "correspondent": "Acme", "document_type": "Invoice",
        "tags": ["billing"], "correspondent_is_new": True,
        "document_type_is_new": True, "new_tags": ["billing"]})]))

    cli.main(["metadata", "--dry-run", "--limit", "5", "--workers", "1"])
    out = capsys.readouterr().out
    assert "Mode: DRY-RUN" in out
    assert "Eligible queue: 1 document(s)" in out
    assert "title='Nice Title'" in out
    assert not any(c.request.method == "PATCH" for c in responses.calls)


@responses.activate
def test_cli_reocr_dry_run(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    import io
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(72, 72, "scan")
    c.showPage()
    c.save()
    pdf = buf.getvalue()

    responses.add(responses.GET, f"{BASE}/api/custom_fields/", json=CUSTOM_FIELDS)
    # tags list for taxonomy (superseded lookup via name__iexact)
    responses.add(responses.GET, f"{BASE}/api/tags/", json={"results": [{"id": 77, "name": "superseded"}]})
    responses.add(responses.GET, f"{BASE}/api/correspondents/", json={"results": [], "next": None})
    responses.add(responses.GET, f"{BASE}/api/document_types/", json={"results": [], "next": None})
    responses.add(responses.GET, f"{BASE}/api/documents/",
                  json={"results": [{"id": 3, "title": "bad", "tags": [], "custom_fields": [{"field": 2, "value": "opt-t"}, {"field": 1, "value": 0.9}]}], "next": None})
    responses.add(responses.GET, f"{BASE}/api/documents/3/download/", body=pdf)

    install_stub_anthropic(monkeypatch, lambda **kw: StubMessage([StubTextBlock("CLEAN OCR TEXT")]))

    cli.main(["reocr", "--dry-run", "--limit", "1", "--workers", "1"])
    out = capsys.readouterr().out
    assert "DRY-RUN (no consume)" in out
    assert "[dry] doc 3" in out
    assert (tmp_path / "built_pdfs" / "3_corrected.pdf").exists()


def test_cli_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "triage" in out and "reocr" in out and "metadata" in out
    # Phase 3 onboarding + sweep subcommands are listed too.
    for cmd in ("init", "setup", "doctor", "run", "serve"):
        assert cmd in out


def test_cli_init_prints_compose_block(capsys):
    cli.main(["init"])
    out = capsys.readouterr().out
    assert "paperless-assistant:" in out
    assert "/data" in out
    assert "no `ports:`" in out            # the no-published-ports note


def test_cli_setup_creates_then_noop(capsys, monkeypatch):
    from fakes import FakePaperless
    from paperless_assistant import cli as cli_mod

    fake = FakePaperless(fields=[], tags=[])
    monkeypatch.setattr(cli_mod, "PaperlessClient",
                        lambda base, token: fake.client())

    cli.main(["setup"])
    out = capsys.readouterr().out
    assert "Created custom fields" in out
    assert "Setup OK" in out

    cli.main(["setup"])          # second run: verified no-op
    out2 = capsys.readouterr().out
    assert "idempotent no-op" in out2


def test_cli_doctor_green_exit_zero(capsys, monkeypatch):
    from fakes import FakePaperless, make_custom_fields, healthy_tags
    from paperless_assistant import cli as cli_mod

    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    monkeypatch.setattr(cli_mod, "PaperlessClient",
                        lambda base, token: fake.client())
    cli.main(["doctor"])         # healthy -> no SystemExit
    out = capsys.readouterr().out
    assert "all green" in out


def test_cli_doctor_fails_nonzero(capsys, monkeypatch):
    from fakes import FakePaperless
    from paperless_assistant import cli as cli_mod

    fake = FakePaperless(fields=[], tags=[])  # nothing provisioned
    monkeypatch.setattr(cli_mod, "PaperlessClient",
                        lambda base, token: fake.client())
    with pytest.raises(SystemExit) as ei:
        cli.main(["doctor"])
    assert ei.value.code == 1


def test_cli_serve_help_shows_webhook(capsys):
    with pytest.raises(SystemExit):
        cli.main(["serve", "--help"])
    out = capsys.readouterr().out
    assert "--webhook" in out
    assert "nudge" in out.lower()
    assert "PA_WEBHOOK_SECRET" in out


def test_cli_serve_webhook_refuses_without_secret(capsys, monkeypatch):
    # `pa serve --webhook` with no PA_WEBHOOK_SECRET must exit non-zero and NOT
    # start an unauthenticated receiver.
    monkeypatch.delenv("PA_WEBHOOK_SECRET", raising=False)
    with pytest.raises(SystemExit) as ei:
        cli.main(["serve", "--webhook", "--iterations", "1"])
    assert ei.value.code == 2
    err = capsys.readouterr().err
    assert "PA_WEBHOOK_SECRET" in err


def test_cli_doctor_reports_webhook(capsys, monkeypatch):
    from fakes import FakePaperless, make_custom_fields, healthy_tags
    from paperless_assistant import cli as cli_mod

    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    monkeypatch.setattr(cli_mod, "PaperlessClient",
                        lambda base, token: fake.client())
    cli.main(["doctor"])  # webhook OFF by default -> reported as OK, not a failure
    out = capsys.readouterr().out
    assert "webhook" in out.lower()
