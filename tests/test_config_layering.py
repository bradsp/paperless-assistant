"""Layered config tests (plan §7.1): default < YAML < env < CLI override, plus
the hard rule that secrets are REFUSED from the YAML file.
"""
import pytest

from paperless_assistant import config
from paperless_assistant.config import load_settings, ConfigError, Settings


def _clear_pa_env(monkeypatch):
    """Strip PA_* / provider env so a test sees only what it sets."""
    for k in list(__import__("os").environ):
        if k.startswith("PA_") or k in (
            "PAPERLESS_URL", "PAPERLESS_TOKEN", "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY", "OPENAI_BASE_URL",
        ):
            monkeypatch.delenv(k, raising=False)


def test_safe_defaults(monkeypatch):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    s = load_settings()
    # §7.2 safe defaults, verified exactly.
    assert s.mode == "conservative"
    assert s.triage_enabled and s.metadata_enabled
    assert s.reocr_enabled is False           # auto re-OCR DISABLED by default
    assert s.taxonomy_policy == "reuse-first"
    assert s.triage_threshold == 0.55
    assert 3 <= s.workers <= 4
    assert s.spend.per_run > 0 and s.spend.per_period > 0   # low, non-zero
    assert s.metadata_task.provider == "anthropic"
    assert s.ocr_task.model == config.OCR_MODEL
    assert s.delete_originals is False        # never automated
    assert s.dry_run is None                  # -> first-run dry-run (I7)


def test_activity_settings_default_yaml_env(monkeypatch, tmp_path):
    """Prompt 013: activity_enabled / activity_retention_days are layered
    (default < YAML < env) and env-lockable like the other tunables."""
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    # default: enabled, 90-day retention.
    s = load_settings()
    assert s.activity_enabled is True
    assert s.activity_retention_days == 90

    # YAML overrides.
    cfg = tmp_path / "config.yml"
    cfg.write_text("activity_enabled: false\nactivity_retention_days: 30\n",
                   encoding="utf-8")
    s2 = load_settings(config_file=str(cfg))
    assert s2.activity_enabled is False
    assert s2.activity_retention_days == 30

    # env beats YAML + is reported as env-locked.
    monkeypatch.setenv("PA_ACTIVITY_RETENTION_DAYS", "7")
    s3 = load_settings(config_file=str(cfg))
    assert s3.activity_retention_days == 7
    locked = config.env_overridden_fields()
    assert locked["activity_retention_days"] is True
    assert locked["activity_enabled"] is False   # PA_ACTIVITY_ENABLED not set


def test_yaml_over_default(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "triage_threshold: 0.7\nworkers: 4\nspend:\n  per_run: 2.5\n"
        "metadata:\n  provider: openai\n  model: gpt-4o-mini\n"
    )
    s = load_settings(config_file=cfg)
    assert s.triage_threshold == 0.7
    assert s.workers == 4
    assert s.spend.per_run == 2.5
    assert s.metadata_task.provider == "openai"
    assert s.metadata_task.model == "gpt-4o-mini"


def test_env_over_yaml(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("triage_threshold: 0.7\nworkers: 4\n")
    monkeypatch.setenv("PA_TRIAGE_THRESHOLD", "0.9")
    monkeypatch.setenv("PA_WORKERS", "2")
    s = load_settings(config_file=cfg)
    assert s.triage_threshold == 0.9          # env beats YAML
    assert s.workers == 2


def test_cli_override_beats_env(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PA_WORKERS", "2")
    s = load_settings(overrides={"workers": 8})
    assert s.workers == 8                      # CLI override is highest precedence


def test_override_none_ignored(monkeypatch):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PA_WORKERS", "2")
    # argparse passes None for unset flags; those must not clobber env/YAML.
    s = load_settings(overrides={"workers": None})
    assert s.workers == 2


def test_secret_refused_from_yaml_paperless_token(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("paperless_token: leaked-in-yaml\n")
    with pytest.raises(ConfigError) as ei:
        load_settings(config_file=cfg)
    assert "secret" in str(ei.value).lower()
    assert "paperless_token" in str(ei.value)


def test_secret_refused_nested(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("metadata:\n  provider: openai\n  api_key: sk-leak\n")
    with pytest.raises(ConfigError) as ei:
        load_settings(config_file=cfg)
    assert "api_key" in str(ei.value)


def test_secret_refused_anthropic_key(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("anthropic_api_key: sk-ant-leak\n")
    with pytest.raises(ConfigError):
        load_settings(config_file=cfg)


def test_delete_originals_refused_from_yaml(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("delete_originals: true\n")
    with pytest.raises(ConfigError) as ei:
        load_settings(config_file=cfg)
    assert "never automated" in str(ei.value).lower()


def test_missing_token_raises(monkeypatch):
    _clear_pa_env(monkeypatch)
    with pytest.raises(ConfigError) as ei:
        load_settings(require_token=True)
    assert "PAPERLESS_TOKEN" in str(ei.value)


def test_to_config_projection(monkeypatch):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "tok")
    monkeypatch.setenv("PAPERLESS_URL", "http://p:8000")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    s = load_settings()
    cfg = s.to_config()
    assert cfg.base_url == "http://p:8000"
    assert cfg.paperless_token == "tok"
    assert cfg.anthropic_api_key == "sk-ant"
    assert cfg.metadata_model == config.METADATA_MODEL


def test_public_dict_has_no_secrets(monkeypatch):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "supersecret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("PA_WEBHOOK_SECRET", "webhook-supersecret")
    s = load_settings()
    pub = s.to_public_dict()
    blob = str(pub)
    assert "supersecret" not in blob
    assert "sk-ant-secret" not in blob
    assert "webhook-supersecret" not in blob   # webhook secret must not leak
    assert pub["spend"]["per_run_cap"] > 0
    assert pub["webhook"]["secret_configured"] is True  # presence only


# ---------------------------------------------------------------------------
# Phase 4 webhook config layering (plan §6.2, §7.1)
# ---------------------------------------------------------------------------
def test_webhook_defaults_off(monkeypatch):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    s = load_settings()
    # OFF by default: the scheduled sweep is authoritative; webhook is opt-in.
    assert s.webhook.enabled is False
    assert s.webhook.port == 8765
    assert s.webhook.path == "/hooks/paperless"
    assert s.webhook.secret == ""


def test_webhook_secret_from_env_only(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PA_WEBHOOK_ENABLED", "true")
    monkeypatch.setenv("PA_WEBHOOK_SECRET", "from-env")
    monkeypatch.setenv("PA_WEBHOOK_PORT", "9999")
    s = load_settings()
    assert s.webhook.enabled is True
    assert s.webhook.secret == "from-env"
    assert s.webhook.port == 9999


def test_webhook_non_secret_tunables_from_yaml(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "webhook:\n  enabled: true\n  port: 8888\n  path: /hooks/pl\n"
        "  debounce_seconds: 5\n"
    )
    s = load_settings(config_file=cfg)
    assert s.webhook.enabled is True
    assert s.webhook.port == 8888
    assert s.webhook.path == "/hooks/pl"
    assert s.webhook.debounce_seconds == 5.0
    # The secret was NOT set in YAML (and could not be); it's empty.
    assert s.webhook.secret == ""


def test_webhook_secret_refused_from_yaml(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("webhook:\n  enabled: true\n  secret: leaked-in-yaml\n")
    with pytest.raises(ConfigError) as ei:
        load_settings(config_file=cfg)
    assert "secret" in str(ei.value).lower()


# ---------------------------------------------------------------------------
# Phase 5 hosted-mode config (plan/connectivity-design.md §3, §4.1)
# ---------------------------------------------------------------------------
def test_hosted_non_secret_url_from_yaml(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "mode: hosted\nhosted:\n  control_plane_url: https://cp.example\n"
        "  heartbeat_interval_seconds: 30\n"
    )
    s = load_settings(config_file=cfg)
    assert s.hosted_mode() is True
    assert s.hosted.control_plane_url == "https://cp.example"
    assert s.hosted.heartbeat_interval_seconds == 30
    # The one-time enrollment token is a SECRET; it is NOT set from YAML.
    assert s.hosted.enrollment_token == ""


def test_hosted_enrollment_token_refused_from_yaml(monkeypatch, tmp_path):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    cfg = tmp_path / "config.yml"
    cfg.write_text("hosted:\n  enrollment_token: leaked-in-yaml\n")
    with pytest.raises(ConfigError) as ei:
        load_settings(config_file=cfg)
    assert "enrollment_token" in str(ei.value).lower() or "secret" in str(ei.value).lower()


def test_hosted_enrollment_token_from_env_only(monkeypatch):
    _clear_pa_env(monkeypatch)
    monkeypatch.setenv("PAPERLESS_TOKEN", "t")
    monkeypatch.setenv("PA_MODE", "hosted")
    monkeypatch.setenv("PA_CONTROL_PLANE_URL", "https://cp.example/")
    monkeypatch.setenv("PA_ENROLLMENT_TOKEN", "enr_secret")
    s = load_settings()
    assert s.hosted_mode() is True
    assert s.hosted.control_plane_url == "https://cp.example"  # trailing / stripped
    assert s.hosted.enrollment_token == "enr_secret"
    # The public (secret-free) dict never exposes the token.
    pub = s.to_public_dict()
    assert pub["hosted"]["enabled"] is True
    assert pub["hosted"]["enrollment_token_configured"] is True
    assert "enr_secret" not in str(pub)
