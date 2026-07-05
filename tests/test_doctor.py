"""`pa doctor` tests (plan §8.2 step 3): green on a healthy fake Paperless, and
loud, actionable failures for each failure mode (missing/mistyped field, missing
tag, invalid token, admin-token warning, unreachable/unconfigured provider).
"""
import requests

from paperless_assistant.config import Settings, TaskProvider
from paperless_assistant.doctor import run_doctor, OK, WARN, FAIL
from fakes import FakePaperless, make_custom_fields, healthy_tags


def _settings(**over):
    s = Settings(base_url="http://paperless.test:8000", paperless_token="tok",
                 anthropic_api_key="sk-ant-test")
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _status(result, name):
    return next(c.status for c in result.checks if c.name == name)


def _has_fail(result):
    return any(c.status == FAIL for c in result.checks)


def test_green_on_healthy():
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), admin=False)
    result = run_doctor(_settings(), fake.client())
    assert not result.failed
    assert _status(result, "connectivity") == OK
    assert _status(result, "token-scope") == OK
    assert _status(result, "field:ai_stage") == OK
    assert _status(result, "tag:superseded") == OK
    assert _status(result, "provider:metadata") == OK
    assert _status(result, "config") == OK


def test_admin_token_warns_but_not_fatal():
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags(), admin=True)
    result = run_doctor(_settings(), fake.client())
    assert _status(result, "token-scope") == WARN
    # Admin is a WARNING (recommend a scoped user), not a hard failure on its own.
    assert not result.failed
    fix = next(c.fix for c in result.checks if c.name == "token-scope")
    assert "service user" in fix.lower()


def test_missing_field_fails_with_fix():
    fields = [f for f in make_custom_fields() if f["name"] != "ocr_quality"]
    fake = FakePaperless(fields=fields, tags=healthy_tags())
    result = run_doctor(_settings(), fake.client())
    assert result.failed
    c = next(c for c in result.checks if c.name == "field:ocr_quality")
    assert c.status == FAIL
    assert "pa setup" in c.fix


def test_mistyped_field_fails():
    fields = make_custom_fields()
    for f in fields:
        if f["name"] == "ai_stage":
            f["data_type"] = "text"
            f.pop("extra_data", None)
    fake = FakePaperless(fields=fields, tags=healthy_tags())
    result = run_doctor(_settings(), fake.client())
    assert result.failed
    assert _status(result, "field:ai_stage") == FAIL


def test_missing_tag_fails():
    fake = FakePaperless(fields=make_custom_fields(),
                         tags=[{"id": 77, "name": "superseded", "color": "#a0a0a0"}])
    result = run_doctor(_settings(), fake.client())
    assert result.failed
    assert _status(result, "tag:ai-new-taxonomy") == FAIL


def test_invalid_token_fails():
    class _AuthFail(FakePaperless):
        def handle(self, method, url, **kw):
            # Auth fails at the connectivity probe endpoint (/api/ui_settings/).
            if "ui_settings" in url:
                raise requests.HTTPError("401 on GET /api/ui_settings/\n  server says: invalid token")
            return super().handle(method, url, **kw)

    fake = _AuthFail(fields=make_custom_fields(), tags=healthy_tags())
    result = run_doctor(_settings(), fake.client())
    assert result.failed
    c = next(c for c in result.checks if c.name == "connectivity")
    assert c.status == FAIL
    assert "token" in c.fix.lower()


def test_unreachable_paperless_fails():
    class _Down(FakePaperless):
        def handle(self, method, url, **kw):
            if "ui_settings" in url:
                raise requests.ConnectionError("connection refused")
            return super().handle(method, url, **kw)

    fake = _Down(fields=make_custom_fields(), tags=healthy_tags())
    result = run_doctor(_settings(), fake.client())
    assert result.failed
    assert _status(result, "connectivity") == FAIL


def test_unconfigured_provider_fails():
    # metadata uses anthropic but no key -> FAIL with actionable fix.
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    s = _settings(anthropic_api_key="")
    result = run_doctor(s, fake.client())
    assert result.failed
    c = next(c for c in result.checks if c.name == "provider:metadata")
    assert c.status == FAIL
    assert "ANTHROPIC_API_KEY" in c.fix


def test_openai_provider_missing_package_fails(monkeypatch):
    """The reported bug: switching to OpenAI failed because the 'openai' package
    wasn't installed. `pa doctor` now catches that (key present, package absent)
    instead of falsely reporting OK."""
    import paperless_assistant.doctor as doctor
    monkeypatch.setattr(doctor, "_package_installed", lambda name: False)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    s = _settings(metadata_task=TaskProvider(provider="openai", model="gpt-4o-mini"),
                  openai_api_key="sk-openai-test")
    result = run_doctor(s, fake.client())
    c = next(c for c in result.checks if c.name == "provider:metadata")
    assert c.status == FAIL
    assert "openai" in c.fix and "package" in c.message.lower()


def test_openai_provider_ok_when_key_and_package_present(monkeypatch):
    import paperless_assistant.doctor as doctor
    monkeypatch.setattr(doctor, "_package_installed", lambda name: True)
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    s = _settings(metadata_task=TaskProvider(provider="openai", model="gpt-4o-mini"),
                  openai_api_key="sk-openai-test")
    result = run_doctor(s, fake.client())
    assert _status(result, "provider:metadata") == OK


def test_reocr_provider_checked_only_when_enabled():
    # ollama OCR w/ endpoint is fine, but only probed when reocr is enabled.
    fake = FakePaperless(fields=make_custom_fields(), tags=healthy_tags())
    s = _settings(reocr_enabled=True,
                  ocr_task=TaskProvider(provider="ollama", model="llava"),
                  ollama_endpoint="http://ollama:11434")
    result = run_doctor(s, fake.client())
    assert _status(result, "provider:ocr") == OK
    assert not result.failed
