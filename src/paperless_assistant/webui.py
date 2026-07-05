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

"""Local web dashboard for the Mode A / BYO-key agent (prompt 009).

A Python standard-library HTTP server + JSON endpoints + ONE self-contained HTML
page (inline CSS/JS, no framework, no external assets) that lets a self-hoster
observe + operate the agent from a browser:

  * READ:  status / stats / runs / logs  (assembled by `webui_data`, reusing obs,
           stages, client, and the persisted /data reports + JSONL log).
  * RUN:   POST a manual sweep that runs the EXISTING `Sweep` engine in a background
           thread (single-flight; 409 on a concurrent POST). Dry-run default, spend
           caps, snapshots, review gates and fail-fast all hold — it's the same
           engine, not a fork.
  * CONFIG: GET the non-secret tunables (env-overridden fields flagged locked; no
           secret ever included) and POST edits to /data/config.yml, REUSING the
           loader's guards (refuse secret-looking keys + delete_originals).

AUTH (r2): EVERY route — reads and writes — requires the token (PA_UI_TOKEN, env
only), compared with `hmac.compare_digest`. Unauthenticated -> 401. The token is
never logged nor placed in any response or the served HTML. If the UI is enabled
with NO token the server refuses to start (fail closed) — mirror the webhook.

Unlike the outbound-only agent + the in-network webhook, the UI is a deliberately
PUBLISHED host port; the token is what protects it.
"""
from __future__ import annotations

import hmac
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import config
from . import webui_data


# ---------------------------------------------------------------------------
# Manual-run manager: single-flight background Sweep over the EXISTING engine.
# ---------------------------------------------------------------------------
class RunManager:
    """Runs a manual sweep in a background thread using the real `Sweep` engine.

    SINGLE-FLIGHT: only one manual run at a time. `start()` returns (run_id, None)
    when it launched a run, or (None, "reason") when one is already in progress
    (the handler maps that to 409). The in-progress state is exposed via `state()`
    so the page can poll it. All safety invariants are the engine's — this class
    only reloads settings (so a saved config applies), maps options to overrides,
    and records start/finish."""

    def __init__(self, settings, *, sweep_factory=None, logger=None):
        # Base settings are reloaded per run so a config save takes effect; keep the
        # data_dir / config_file so the reload finds the same /data + YAML.
        self._base_settings = settings
        self._config_file = None
        self._sweep_factory = sweep_factory  # test seam: () -> Sweep-like
        self._logger = logger
        self._lock = threading.Lock()
        self._thread = None
        self._state = {"in_progress": False}

    def set_config_file(self, path):
        self._config_file = path

    def state(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, options: dict):
        """Launch a manual run. Returns (run_id, None) or (None, reason)."""
        with self._lock:
            if self._state.get("in_progress"):
                return None, "a manual run is already in progress"
            run_id = uuid.uuid4().hex[:12]
            self._state = {
                "in_progress": True,
                "run_id": run_id,
                "options": _public_options(options),
                "started_at": _now_iso(),
                "finished_at": None,
                "error": None,
                "result": None,
            }
            self._thread = threading.Thread(
                target=self._run, args=(run_id, options),
                name="pa-webui-run", daemon=True,
            )
            self._thread.start()
            return run_id, None

    # -- background worker -------------------------------------------------
    def _run(self, run_id, options):
        try:
            sweep = self._build_sweep(options)
            multi = sweep.run_once(limit=options.get("limit"), source="manual")
            result = {
                "sweep_run_id": getattr(multi, "run_id", None),
                "dry_run": getattr(multi, "dry_run", None),
                "counts": multi.merged_counts() if hasattr(multi, "merged_counts") else {},
                "spend_total": multi.total_spend() if hasattr(multi, "total_spend") else 0.0,
                "new_taxonomy": multi.all_new_taxonomy() if hasattr(multi, "all_new_taxonomy") else [],
                "superseded": multi.all_superseded() if hasattr(multi, "all_superseded") else [],
            }
            with self._lock:
                self._state.update(
                    in_progress=False, finished_at=_now_iso(), result=result,
                )
        except Exception as e:  # noqa: BLE001 — a failed manual run must not crash the UI
            if self._logger:
                self._logger.event("webui_run_error", level="error", error=str(e))
            with self._lock:
                self._state.update(
                    in_progress=False, finished_at=_now_iso(),
                    error=webui_data._safe_error(e),
                )

    def _build_sweep(self, options):
        # RELOAD settings so a saved /data/config.yml applies to this run, then map
        # the UI options to per-run overrides (same surface as the CLI flags).
        overrides = _overrides_from_options(options)
        # Preserve the process's data_dir (where /data + the YAML live) across the
        # reload, so the run persists to the SAME volume the UI reads from. In the
        # container this equals /data; in tests it's the injected data_dir.
        overrides.setdefault("data_dir", self._base_settings.data_dir)
        try:
            settings = config.load_settings(
                config_file=self._config_file, overrides=overrides,
            )
        except config.ConfigError:
            # If a token isn't in the environment (e.g. offline test wiring), fall
            # back to a COPY of the injected base settings + these overrides so the
            # run still uses the reloaded tunables where possible.
            import copy

            settings = copy.deepcopy(self._base_settings)
            config._apply_overrides(settings, overrides)
        if self._sweep_factory is not None:
            return self._sweep_factory(settings)
        from .sweep import Sweep

        return Sweep(settings, logger=self._logger)


def _overrides_from_options(options: dict) -> dict:
    """Map validated UI run options to config per-run overrides (CLI-equivalent).
    `write` else dry-run; `reocr` else off; `limit`; `max_spend`."""
    ov = {}
    # dry_run: default True (safe) unless the user asked to write.
    ov["dry_run"] = not bool(options.get("write"))
    if options.get("reocr"):
        ov["reocr_enabled"] = True
    if options.get("limit") is not None:
        ov["limit"] = int(options["limit"])
    if options.get("max_spend") is not None:
        ov["per_run_cap"] = float(options["max_spend"])
    return ov


def _public_options(options: dict) -> dict:
    return {
        "write": bool(options.get("write")),
        "reocr": bool(options.get("reocr")),
        "limit": options.get("limit"),
        "max_spend": options.get("max_spend"),
    }


# ---------------------------------------------------------------------------
# Config write (POST /api/config): validate + write /data/config.yml, reusing the
# loader's guards (refuse secret keys + delete_originals).
# ---------------------------------------------------------------------------
# The editable, non-secret tunables the UI may write to the YAML layer. Anything
# not here is ignored; secret-looking keys and delete_originals are REFUSED.
_EDITABLE = {
    "paperless_public_url": str,   # external browser URL for UI links (non-secret)
    "mode": str,
    "triage_enabled": bool,
    "metadata_enabled": bool,
    "reocr_enabled": bool,
    "triage_threshold": float,
    "garbage_threshold": float,
    "workers": int,
    "limit": int,  # max docs processed per stage per run (0 = all eligible)
    "schedule_interval_seconds": int,
    "taxonomy_policy": str,
    "superseded_tag": str,
    "new_taxonomy_tag": str,
    "dry_run": bool,
    # --- prompt 011: non-secret tunables (normal + advanced) --------------
    "snapshot_retention_days": int,
    # --- prompt 013: activity/audit log retention -------------------------
    "activity_enabled": bool,
    "activity_retention_days": int,
    "max_ocr_tokens": int,
    "superseded_tag_color": str,
    "new_taxonomy_tag_color": str,
}

# Prompt 011: nested config blocks the UI may write (merged onto existing). These
# are NON-secret tunables — the secret guards below still run over the result.
_EDITABLE_BLOCKS = ("field_names", "stage_names", "http", "metadata_window",
                    "garbage_heuristic")

# Per-block field coercion for the prompt-011 blocks.
_BLOCK_FIELDS = {
    "field_names": {"score": str, "stage": str, "notes": str},
    "stage_names": {"triaged": str, "reocr_done": str, "metadata_done": str},
    "http": {
        "request_timeout": float, "download_timeout": float,
        "post_document_timeout": float, "task_poll_timeout": float,
        "task_poll_interval": float, "page_size": int,
        "retries": int, "backoff_initial": float, "backoff_cap": float,
    },
    "metadata_window": {"content_head": int, "content_tail": int, "max_tokens": int},
    "garbage_heuristic": {
        "min_length": int, "word_ratio_weight": float, "plausible_weight": float,
        "fragment_weight": float, "fragment_threshold": float, "plausible_min_len": int,
    },
}


class ConfigValidationError(ValueError):
    """A user-facing config-write validation error (mapped to 400)."""


def validate_and_build_yaml(payload: dict, *, existing: dict | None = None) -> dict:
    """Validate an incoming config edit and return the YAML mapping to write.

    REUSES the loader's guards: any secret-looking key (config.SECRET_YAML_KEYS)
    and `delete_originals` are REFUSED here, exactly as the YAML loader refuses
    them, so a secret can never be written to /data/config.yml via the UI. Unknown
    keys are rejected with a clear message; known keys are type-coerced.
    """
    if not isinstance(payload, dict):
        raise ConfigValidationError("config body must be a JSON object")

    merged = dict(existing or {})
    errors = []

    for key, raw in payload.items():
        low = str(key).lower()
        # Guard 1: never accept a secret-looking key into the YAML (§7.1).
        if low in config.SECRET_YAML_KEYS:
            raise ConfigValidationError(
                f"refusing to write secret-looking key '{key}' to config.yml. "
                f"Secrets (Paperless token, AI keys, UI/webhook/agent tokens) come "
                f"from the environment only, never the YAML."
            )
        # Guard 2: delete_originals can NEVER be enabled from config (I4).
        if low == "delete_originals":
            raise ConfigValidationError(
                "delete_originals cannot be set from config — deletion of originals "
                "is never automated (I4). Use the 'superseded' review set instead."
            )

    for key, raw in payload.items():
        if key in ("spend", "ocr", "metadata", "webhook", "ui") or key in _EDITABLE_BLOCKS:
            # MERGE onto any existing block so a partial edit (e.g. saving models)
            # does NOT drop sibling keys previously written (e.g. prompt fields).
            base = dict(merged.get(key) or {}) if isinstance(merged.get(key), dict) else {}
            base.update(_coerce_block(key, raw, errors))
            merged[key] = base
            continue
        if key == "metadata_eligible_roles":
            # A list of state-machine roles ("" / triaged / reocr_done / metadata_done).
            if not isinstance(raw, (list, tuple)):
                errors.append("metadata_eligible_roles must be a list")
            else:
                merged[key] = ["" if r is None else str(r) for r in raw]
            continue
        if key not in _EDITABLE:
            errors.append(f"unknown or non-editable setting '{key}'")
            continue
        try:
            merged[key] = _coerce_scalar(key, raw)
        except (TypeError, ValueError):
            errors.append(f"invalid value for '{key}': {raw!r}")

    if errors:
        raise ConfigValidationError("; ".join(errors))

    # Final guard: run it through the SAME secret/delete rejection the loader uses,
    # so nothing slips past (e.g. a secret nested inside a block).
    config._reject_secrets(merged, "config.yml (web edit)")
    if merged.get("delete_originals"):
        raise ConfigValidationError(
            "delete_originals cannot be enabled via config (I4)."
        )
    return merged


def _coerce_scalar(key, raw):
    cast = _EDITABLE[key]
    if cast is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


def _coerce_block(key, raw, errors):
    if not isinstance(raw, dict):
        errors.append(f"'{key}' must be an object")
        return {}
    out = {}
    if key == "spend":
        for k, cast in (("per_run", float), ("per_period", float), ("period", str)):
            if raw.get(k) is not None:
                try:
                    out[k] = cast(raw[k])
                except (TypeError, ValueError):
                    errors.append(f"invalid spend.{k}: {raw[k]!r}")
    elif key in ("ocr", "metadata"):
        for k in ("provider", "model"):
            if raw.get(k) is not None:
                out[k] = str(raw[k])
        # Prompt customization (prompt 010): non-secret multi-line instruction text.
        # `prompt_override` replaces the built-in default; "" resets to default.
        # `extra_instructions` is appended. These are NOT secrets and must be
        # writable to the YAML (the secret guard below allows them).
        for k in ("extra_instructions", "prompt_override"):
            if raw.get(k) is not None:
                out[k] = str(raw[k])
    elif key == "webhook":
        for k, cast in (("enabled", bool), ("host", str), ("port", int),
                        ("path", str)):
            if raw.get(k) is not None:
                out[k] = _cast_val(cast, raw[k])
    elif key == "ui":
        for k, cast in (("enabled", bool), ("host", str), ("port", int)):
            if raw.get(k) is not None:
                out[k] = _cast_val(cast, raw[k])
    elif key in _BLOCK_FIELDS:
        # Prompt 011 blocks (field_names / stage_names / http / metadata_window /
        # garbage_heuristic): all NON-secret tunables, type-coerced per field.
        for k, cast in _BLOCK_FIELDS[key].items():
            if raw.get(k) is not None:
                try:
                    out[k] = _cast_val(cast, raw[k])
                except (TypeError, ValueError):
                    errors.append(f"invalid {key}.{k}: {raw[k]!r}")
    return out


def _cast_val(cast, raw):
    if cast is bool:
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


def write_config_yaml(path, mapping: dict) -> None:
    """Persist the validated mapping to /data/config.yml (the YAML layer)."""
    import pathlib
    import yaml

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(mapping, sort_keys=True), encoding="utf-8")


def read_config_yaml(path) -> dict:
    import pathlib
    import yaml

    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}
    return raw if isinstance(raw, dict) else {}


# ---------------------------------------------------------------------------
# Auth (r2): env-only token, every route, hmac.compare_digest, 401 on failure.
# ---------------------------------------------------------------------------
def _extract_token(headers, path) -> str | None:
    auth = headers.get("Authorization", "") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    provided = headers.get("X-PA-UI-Token")
    if provided:
        return provided
    qs = parse_qs(urlparse(path).query)
    vals = qs.get("token")
    return vals[0] if vals else None


def _authenticated(headers, path, token) -> bool:
    provided = _extract_token(headers, path)
    if not provided or not token:
        return False
    return hmac.compare_digest(str(provided), str(token))


# ---------------------------------------------------------------------------
# The request handler.
# ---------------------------------------------------------------------------
def make_handler(settings, *, token, run_manager, logger=None, config_file=None,
                 environ=None):
    """Build a BaseHTTPRequestHandler bound to this UI's settings + auth + run
    manager. The handler is the transport; data assembly lives in `webui_data`."""

    def _current_settings():
        # Reload so a saved config.yml is reflected in reads, but PRESERVE the
        # process's data_dir (where /data + the YAML live) and the injected secrets
        # (which come from env/injection, never YAML). Fall back to the injected
        # settings if the reload fails so the UI never goes dark.
        try:
            reloaded = config.load_settings(
                config_file=config_file,
                overrides={"data_dir": settings.data_dir},
                require_token=False,
            )
        except Exception:  # noqa: BLE001
            return settings
        # Re-inject secrets the reload may not have seen (env-only; the reload picks
        # them up when the env is set, but tests/inject provide them on `settings`).
        for attr in ("paperless_token", "anthropic_api_key", "openai_api_key"):
            if not getattr(reloaded, attr) and getattr(settings, attr):
                setattr(reloaded, attr, getattr(settings, attr))
        if not reloaded.ui.token and settings.ui.token:
            reloaded.ui.token = settings.ui.token
        if not reloaded.webhook.secret and settings.webhook.secret:
            reloaded.webhook.secret = settings.webhook.secret
        return reloaded

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: N802 — no stderr access log
            return

        # -- helpers -------------------------------------------------------
        def _send(self, code, body=None, *, text=None, content_type=None):
            if text is not None:
                payload = text.encode("utf-8")
                ctype = content_type or "text/plain; charset=utf-8"
            else:
                payload = json.dumps(body if body is not None else {}).encode("utf-8")
                ctype = "application/json"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                self.wfile.write(payload)

        def _auth_ok(self) -> bool:
            if _authenticated(self.headers, self.path, token):
                return True
            if logger:
                logger.event("webui_rejected", level="warning",
                             reason="unauthenticated",
                             path=urlparse(self.path).path)
            self._send(401, {"error": "unauthenticated"})
            return False

        def _read_json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None  # signal malformed

        # -- GET -----------------------------------------------------------
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            qs = parse_qs(urlparse(self.path).query)

            # The root HTML page is served WITHOUT auth (it contains no secret and
            # no data); it then logs in and sends the token on every fetch below.
            if path == "/" or path == "/index.html":
                self._send(200, text=PAGE_HTML, content_type="text/html; charset=utf-8")
                return

            if not path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            if not self._auth_ok():
                return

            s = _current_settings()
            if path == "/api/status":
                self._send(200, webui_data.status_payload(s, run_state=run_manager.state()))
            elif path == "/api/run/current":
                self._send(200, run_manager.state())
            elif path == "/api/progress":
                self._send(200, webui_data.progress_payload())
            elif path == "/api/stats":
                self._send(200, webui_data.stats_payload(s))
            elif path == "/api/runs":
                self._send(200, webui_data.runs_payload(s))
            elif path == "/api/run":
                run_id = (qs.get("id") or [None])[0]
                if not run_id:
                    self._send(400, {"error": "missing run id"})
                    return
                detail = webui_data.run_detail(s, run_id)
                if detail is None:
                    self._send(404, {"error": "run not found"})
                else:
                    self._send(200, detail)
            elif path == "/api/logs":
                errors_only = (qs.get("errors") or ["0"])[0] in ("1", "true", "yes")
                limit = _int_or(qs.get("limit"), 100)
                self._send(200, webui_data.logs_payload(
                    s, limit=limit, errors_only=errors_only))
            elif path == "/api/config":
                self._send(200, webui_data.config_payload(s, environ=environ))
            elif path == "/api/models":
                self._send(200, webui_data.models_payload(s))
            elif path == "/api/prompts":
                self._send(200, webui_data.prompts_payload(s))
            elif path == "/api/doctor":
                self._get_doctor(s)
            elif path == "/api/activity":
                self._get_activity(s, qs)
            elif path == "/api/onboarding":
                self._send(200, onboarding_payload(s))
            else:
                self._send(404, {"error": "not found", "path": path})

        # -- POST ----------------------------------------------------------
        def do_POST(self):  # noqa: N802
            path = urlparse(self.path).path.rstrip("/") or "/"
            if not path.startswith("/api/"):
                self._send(404, {"error": "not found"})
                return
            if not self._auth_ok():
                return
            body = self._read_json_body()
            if body is None:
                self._send(400, {"error": "malformed JSON body"})
                return

            if path == "/api/run":
                self._post_run(body)
            elif path == "/api/config":
                self._post_config(body)
            elif path == "/api/setup":
                self._post_setup()
            elif path == "/api/pause":
                self._post_pause(True)
            elif path == "/api/resume":
                self._post_pause(False)
            elif path == "/api/activity/purge":
                self._post_activity_purge(body)
            else:
                self._send(404, {"error": "not found", "path": path})

        # -- setup / doctor / pause / resume (prompt 012) ------------------
        def _post_setup(self):
            s = _current_settings()
            try:
                report, reason = run_setup(s)
            except Exception as e:  # noqa: BLE001 — a setup failure must not crash the UI
                if logger:
                    logger.event("webui_setup_error", level="error",
                                 error=webui_data._safe_error(e))
                self._send(502, {"error": webui_data._safe_error(e)})
                return
            if report is None:
                # Single-flight: a concurrent setup is a 409.
                self._send(409, {"error": reason})
                return
            if logger:
                logger.event("webui_setup", ok=report.get("ok"),
                             noop=report.get("noop"))
            self._send(200, {"report": report})

        def _get_doctor(self, s):
            try:
                payload = run_doctor_payload(s)
            except Exception as e:  # noqa: BLE001 — doctor must not crash the UI
                self._send(200, {"healthy": False, "checks": [{
                    "name": "doctor", "status": "fail",
                    "message": webui_data._safe_error(e),
                    "fix": "Verify PAPERLESS_URL/PAPERLESS_TOKEN and reachability.",
                }]})
                return
            self._send(200, payload)

        def _get_activity(self, s, qs):
            """GET /api/activity — filtered + server-side-paginated audit rows.
            Query params: doc_id, since, until (epoch seconds), dry_run (0/1),
            stage, status, q (free-text search), limit, offset. All optional."""
            def _one(key):
                v = qs.get(key)
                return v[0] if v else None

            dry = _one("dry_run")
            dry_val = None if dry in (None, "") else (dry in ("1", "true", "yes"))
            payload = webui_data.activity_payload(
                s,
                doc_id=_opt_int(_one("doc_id")),
                since=_opt_float(_one("since")),
                until=_opt_float(_one("until")),
                dry_run=dry_val,
                stage=_one("stage") or None,
                status=_one("status") or None,
                search=_one("q") or None,
                limit=_int_or(qs.get("limit"), 50),
                offset=_int_or(qs.get("offset"), 0),
            )
            self._send(200, payload)

        def _post_activity_purge(self, body):
            s = _current_settings()
            older = _opt_int((body or {}).get("older_than_days"))
            result = webui_data.activity_purge(s, older_than_days=older)
            if logger:
                logger.event("webui_activity_purged", purged=result.get("purged"))
            self._send(200, result)

        def _post_pause(self, paused):
            s = _current_settings()
            from .obs import pause_flag_for

            state = pause_flag_for(s).set_paused(paused)
            if logger:
                logger.event("webui_paused" if paused else "webui_resumed",
                             paused=state)
            self._send(200, {"paused": state})

        def _post_run(self, body):
            options = {
                "write": bool(body.get("write")),
                "reocr": bool(body.get("reocr")),
                "limit": _opt_int(body.get("limit")),
                "max_spend": _opt_float(body.get("max_spend")),
            }
            run_id, reason = run_manager.start(options)
            if run_id is None:
                # Single-flight: a concurrent run is a 409 (r4).
                self._send(409, {"error": reason, "current_run": run_manager.state()})
                return
            if logger:
                logger.event("webui_run_started", run_id=run_id,
                             write=options["write"], reocr=options["reocr"])
            self._send(202, {"started": True, "run_id": run_id,
                             "options": _public_options(options)})

        def _post_config(self, body):
            existing = read_config_yaml(config_file or str(settings.data_path("config.yml")))
            try:
                mapping = validate_and_build_yaml(body, existing=existing)
            except ConfigValidationError as e:
                self._send(400, {"error": str(e)})
                return
            except config.ConfigError as e:
                self._send(400, {"error": str(e)})
                return
            target = config_file or str(settings.data_path("config.yml"))
            write_config_yaml(target, mapping)
            if logger:
                # Log the KEYS written, never the values (some may be sensitive-ish).
                logger.event("webui_config_saved", keys=sorted(body.keys()))
            self._send(200, {"saved": True, "written_to": target,
                             "note": "env-overridden fields are unaffected until the "
                                     "env var is removed; some settings apply on the "
                                     "next run/restart."})

    return _Handler


def _int_or(vals, default):
    try:
        return int(vals[0]) if vals else default
    except (TypeError, ValueError):
        return default


def _opt_int(v):
    if v in (None, ""):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _opt_float(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _now_iso():
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Setup / Doctor / Onboarding (prompt 012): auth-gated endpoints that REUSE the
# engine's own Provisioner / run_doctor / initcmd — no logic is reimplemented.
# ---------------------------------------------------------------------------
# Single-flight guard for POST /api/setup. Provisioning is idempotent, but firing
# two provisioners at once could race on create-if-missing, so we refuse a
# concurrent invocation (409) exactly as the run manager refuses a concurrent run.
_SETUP_LOCK = threading.Lock()
_SETUP_IN_PROGRESS = {"running": False}


def run_setup(settings, *, client=None) -> tuple[dict | None, str | None]:
    """Run the idempotent Provisioner against Paperless, reusing the engine's
    `Provisioner` (never reimplemented). Returns (report_dict, None) or
    (None, "reason") when a concurrent setup is already running."""
    from .provision import Provisioner

    with _SETUP_LOCK:
        if _SETUP_IN_PROGRESS["running"]:
            return None, "a setup is already in progress"
        _SETUP_IN_PROGRESS["running"] = True
    try:
        cfg = settings.to_config()
        client = client or _paperless_client(cfg, settings)
        prov = Provisioner(
            client,
            superseded_tag=settings.superseded_tag,
            new_taxonomy_tag=settings.new_taxonomy_tag,
            field_names=settings.field_names,
            stage_names=settings.stage_names,
            superseded_tag_color=settings.superseded_tag_color,
            new_taxonomy_tag_color=settings.new_taxonomy_tag_color,
        )
        report = prov.run()
        return report.to_dict(), None
    finally:
        with _SETUP_LOCK:
            _SETUP_IN_PROGRESS["running"] = False


def run_doctor_payload(settings, *, client=None) -> dict:
    """Run the engine's `run_doctor` (never reimplemented) and return its checks as
    OK/WARN/FAIL + fix. Read-only against Paperless."""
    from .doctor import run_doctor

    cfg = settings.to_config()
    client = client or _paperless_client(cfg, settings)
    return run_doctor(settings, client).to_dict()


def onboarding_payload(settings, *, client=None) -> dict:
    """The guided first-run checklist + the compose snippet.

    The compose block is reused verbatim from `initcmd` (never re-authored). Each
    checklist step reflects LIVE state: setup done (required fields + review tags
    present), doctor status, first dry-run done (the /data cursor), and whether
    writes are enabled. Paperless-unreachable is handled gracefully so the wizard
    still renders (the Paperless-dependent steps report unknown)."""
    from . import initcmd
    from .obs import Cursor
    from .provision import required_fields

    cursor = Cursor(str(settings.data_path("cursor.json")))
    first_run_done = cursor.first_run_done

    # writes enabled? dry_run None => first-run dry-run default (I7); explicit False
    # means the engine will write from now on.
    writes_enabled = settings.dry_run is False or (
        settings.dry_run is None and first_run_done
    )

    setup_done = None
    setup_detail = "could not reach Paperless"
    doctor_status = None
    doctor_detail = "could not reach Paperless"
    try:
        cfg = settings.to_config()
        client = client or _paperless_client(cfg, settings)
        # setup done = every required field + both review tags present.
        field_specs = required_fields(settings.field_names, settings.stage_names)
        have_fields = {f["name"] for f in client.get_all("custom_fields")}
        missing_fields = [n for n in field_specs if n not in have_fields]
        missing_tags = []
        for tag in (settings.superseded_tag, settings.new_taxonomy_tag):
            data = client.request(
                "GET", f"{client.base}/api/tags/?name__iexact={tag}"
            ).json()
            if not data.get("results"):
                missing_tags.append(tag)
        setup_done = not missing_fields and not missing_tags
        if setup_done:
            setup_detail = "all required custom fields and review tags are present."
        else:
            bits = []
            if missing_fields:
                bits.append("missing fields: " + ", ".join(missing_fields))
            if missing_tags:
                bits.append("missing tags: " + ", ".join(missing_tags))
            setup_detail = "; ".join(bits) + ". Run Setup to provision them."
        # doctor status = healthy/unhealthy from the real run_doctor.
        from .doctor import run_doctor

        doc = run_doctor(settings, client)
        doctor_status = "healthy" if not doc.failed else "unhealthy"
        fails = [c.name for c in doc.checks if c.status == "fail"]
        warns = [c.name for c in doc.checks if c.status == "warn"]
        if doctor_status == "healthy":
            doctor_detail = (
                "all checks pass" + (f" ({len(warns)} warning(s))" if warns else "")
            )
        else:
            doctor_detail = "failing checks: " + ", ".join(fails)
    except Exception as e:  # noqa: BLE001 — the wizard must render even offline
        setup_detail = f"could not reach Paperless: {webui_data._safe_error(e)}"
        doctor_detail = setup_detail

    steps = [
        {
            "key": "setup", "title": "Provision fields & tags",
            "done": setup_done, "action": "setup", "action_label": "Run setup",
            "detail": setup_detail,
        },
        {
            "key": "doctor", "title": "Health check",
            "done": (doctor_status == "healthy") if doctor_status else None,
            "status": doctor_status, "action": "doctor", "action_label": "Run doctor",
            "detail": doctor_detail,
        },
        {
            "key": "first_run", "title": "First dry-run (preview proposed changes)",
            "done": bool(first_run_done), "action": "run", "action_label": "Run now",
            "detail": ("a first sweep has completed; proposed changes were reported."
                       if first_run_done else
                       "no sweep yet — the first run is a bounded DRY-RUN that only "
                       "reports proposed changes (nothing is written)."),
        },
        {
            "key": "writes", "title": "Enable writes",
            "done": bool(writes_enabled), "action": "run", "action_label": "Run now",
            "detail": ("writes are enabled — sweeps will apply changes."
                       if writes_enabled else
                       "still in dry-run. Run a write sweep (Runs tab) or set dry_run "
                       "off in Settings once you're happy with the preview."),
        },
    ]
    return {
        "compose": initcmd.COMPOSE_BLOCK,
        "next_steps": initcmd.NEXT_STEPS,
        "steps": steps,
        "first_run_done": bool(first_run_done),
        "writes_enabled": bool(writes_enabled),
    }


def _paperless_client(cfg, settings):
    """Build a PaperlessClient with the configured HTTP tunables (mirrors the
    engine's own construction; a test seam replaces this via monkeypatch)."""
    from .client import PaperlessClient

    return PaperlessClient(cfg.base_url, cfg.paperless_token, http=settings.http)


# ---------------------------------------------------------------------------
# The server: fail closed without a token; bind the published host port.
# ---------------------------------------------------------------------------
class WebUIServer:
    """Owns the stdlib ThreadingHTTPServer + the RunManager. Started by `pa web`
    and, when PA_UI_ENABLED, as a background thread inside `pa serve`.

    FAIL CLOSED: if the UI is enabled with no token, `start()` raises — we never
    serve an unauthenticated dashboard (mirror the webhook's secret handling)."""

    def __init__(self, settings, *, logger=None, config_file=None, run_manager=None):
        self.settings = settings
        self.ui = settings.ui
        self.logger = logger
        self.config_file = config_file
        self.run_manager = run_manager or RunManager(settings, logger=logger)
        self.run_manager.set_config_file(config_file)
        self._httpd = None
        self._thread = None

    def _make_httpd(self):
        if not self.ui.token:
            raise RuntimeError(
                "web UI is enabled but PA_UI_TOKEN is not set. Refusing to start an "
                "unauthenticated dashboard. Set PA_UI_TOKEN in the environment "
                "(never the YAML config)."
            )
        handler = make_handler(
            self.settings, token=self.ui.token, run_manager=self.run_manager,
            logger=self.logger, config_file=self.config_file,
        )
        return ThreadingHTTPServer((self.ui.host, self.ui.port), handler)

    def start(self):
        """Start listening in a daemon thread. Raises if fail-closed (no token)."""
        self._httpd = self._make_httpd()
        if self.logger:
            self.logger.event("webui_start", host=self.ui.host, port=self.ui.port)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="pa-webui", daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            if self.logger:
                self.logger.event("webui_stop")

    def serve_forever(self):
        """Blocking run (used by `pa web`). Raises if fail-closed (no token)."""
        self._httpd = self._make_httpd()
        if self.logger:
            self.logger.event("webui_start", host=self.ui.host, port=self.ui.port)
        try:
            self._httpd.serve_forever()
        finally:
            self._httpd.server_close()

    @property
    def address(self):
        if self._httpd is not None:
            return self._httpd.server_address
        return (self.ui.host, self.ui.port)


# ---------------------------------------------------------------------------
# The single self-contained HTML page (inline CSS + vanilla JS, NO external
# assets — no CDN scripts/styles/fonts/images). Token login + polling.
# ---------------------------------------------------------------------------
PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paperless Assistant — Dashboard</title>
<style>
  :root { --bg:#0f1115; --panel:#181b22; --ink:#e6e8ec; --muted:#9aa4b2;
          --line:#2a2f3a; --accent:#5b9dff; --warn:#f0b429; --bad:#f05f5f;
          --ok:#4ec98a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:16px 20px; border-bottom:1px solid var(--line);
           display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  header h1 { font-size:17px; margin:0; font-weight:650; }
  header .sub { color:var(--muted); font-size:12px; }
  header .right { margin-left:auto; display:flex; gap:8px; align-items:center; }
  main { padding:18px 20px; display:grid; gap:18px; max-width:1080px; }
  section { background:var(--panel); border:1px solid var(--line);
            border-radius:10px; padding:14px 16px; }
  section h2 { font-size:14px; margin:0 0 4px; font-weight:640; }
  section .note { color:var(--muted); font-size:12px; margin:0 0 10px; }
  .kpis { display:flex; gap:22px; flex-wrap:wrap; margin-bottom:8px; }
  .kpi .n { font-size:20px; font-weight:680; }
  .kpi .l { color:var(--muted); font-size:11px; text-transform:uppercase;
            letter-spacing:.04em; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:6px 9px; border-bottom:1px solid var(--line);
          white-space:nowrap; }
  th { color:var(--muted); font-weight:600; font-size:11px;
       text-transform:uppercase; letter-spacing:.03em; }
  .scroll { overflow-x:auto; }
  .pill { font-size:11px; padding:2px 8px; border-radius:20px;
          border:1px solid var(--line); }
  .pill.ok { color:var(--ok); } .pill.bad { color:var(--bad); }
  .pill.warn { color:var(--warn); }
  .bar { height:7px; background:var(--line); border-radius:4px; overflow:hidden;
         min-width:90px; display:inline-block; vertical-align:middle; }
  .bar > span { display:block; height:100%; background:var(--accent); }
  .bar > span.warn { background:var(--warn); } .bar > span.bad { background:var(--bad); }
  .empty { color:var(--muted); font-style:italic; padding:6px 0; }
  .err { color:var(--bad); }
  button { background:var(--panel); color:var(--ink); border:1px solid var(--line);
           border-radius:7px; padding:6px 12px; cursor:pointer; font-size:12.5px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); color:#08101f; border-color:var(--accent);
                   font-weight:640; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  input,select { background:#0d0f14; color:var(--ink); border:1px solid var(--line);
                 border-radius:6px; padding:5px 8px; font-size:13px; }
  input:disabled,select:disabled { opacity:.55; }
  label { font-size:12.5px; color:var(--muted); display:inline-flex; gap:6px;
          align-items:center; }
  .row { display:flex; gap:14px; flex-wrap:wrap; align-items:center; }
  .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px 18px; }
  @media (max-width:640px){ .grid2 { grid-template-columns:1fr; } }
  .locked { font-size:10.5px; color:var(--warn); margin-left:6px; }
  .field { display:flex; flex-direction:column; gap:3px; }
  .field > span { font-size:11.5px; color:var(--muted); }
  code { background:#0d0f14; padding:1px 5px; border-radius:4px; font-size:12px; }
  #login { max-width:420px; margin:60px auto; }
  #app { display:none; }
  .msg { font-size:12.5px; margin-top:8px; }
  .msg.ok { color:var(--ok); } .msg.bad { color:var(--bad); }
  .logline { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:12px;
             white-space:pre-wrap; padding:2px 0; border-bottom:1px solid var(--line); }
  .logline.error { color:var(--bad); }
  textarea { background:#0d0f14; color:var(--ink); border:1px solid var(--line);
             border-radius:6px; padding:6px 8px; font-size:12.5px; width:100%;
             font-family:ui-monospace,Menlo,Consolas,monospace; resize:vertical; }
  textarea:disabled { opacity:.55; }
  .taskblock { border:1px solid var(--line); border-radius:8px; padding:12px 14px;
               margin-bottom:14px; }
  .taskblock h3 { font-size:13px; margin:0 0 8px; font-weight:640; }
  .readonly { background:#0d0f14; border:1px solid var(--line); border-radius:6px;
              padding:8px 10px; font-family:ui-monospace,Menlo,Consolas,monospace;
              font-size:12px; white-space:pre-wrap; color:var(--muted);
              max-height:160px; overflow:auto; }
  details { margin-top:8px; } details > summary { cursor:pointer; color:var(--accent);
            font-size:12.5px; }
  .warn-inline { color:var(--warn); font-size:12px; margin-top:6px; }
  .hint { color:var(--muted); font-size:11.5px; }
  .subtle-btn { font-size:11.5px; padding:3px 8px; }
  /* --- Tabs (prompt 012): vanilla-JS tab controller, no framework --------- */
  nav.tabs { display:flex; gap:4px; flex-wrap:wrap; padding:0 20px;
             border-bottom:1px solid var(--line); background:var(--bg);
             position:sticky; top:0; z-index:5; }
  nav.tabs button.tab { background:transparent; border:none; border-bottom:2px solid transparent;
             color:var(--muted); padding:11px 14px; font-size:13.5px; border-radius:0;
             cursor:pointer; }
  nav.tabs button.tab:hover { color:var(--ink); }
  nav.tabs button.tab[aria-selected="true"] { color:var(--ink); border-bottom-color:var(--accent);
             font-weight:640; }
  .tabpanel { display:none; }
  .tabpanel.active { display:block; }
  .subnav { display:flex; gap:6px; flex-wrap:wrap; margin-bottom:14px; }
  .subnav button { font-size:12.5px; }
  .subnav button[aria-selected="true"] { background:var(--accent); color:#08101f;
             border-color:var(--accent); font-weight:640; }
  .subpanel { display:none; } .subpanel.active { display:block; }
  .banner { margin:14px 20px 0; padding:12px 16px; border-radius:10px;
            border:1px solid var(--warn); background:rgba(240,180,41,.12);
            color:var(--warn); font-size:13.5px; display:none; align-items:center;
            gap:12px; flex-wrap:wrap; }
  .banner.show { display:flex; }
  .banner strong { color:var(--warn); }
  .check { display:flex; gap:10px; align-items:flex-start; padding:7px 0;
           border-bottom:1px solid var(--line); }
  .check .st { flex:0 0 auto; }
  .check .body { flex:1; }
  .check .fix { color:var(--muted); font-size:11.5px; margin-top:2px; }
  .step { display:flex; gap:12px; align-items:flex-start; padding:10px 0;
          border-bottom:1px solid var(--line); }
  .step .mark { flex:0 0 24px; font-size:16px; text-align:center; }
  .step .body { flex:1; }
  .step .body .t { font-weight:620; font-size:13px; }
  .step .body .d { color:var(--muted); font-size:12px; margin-top:2px; }
  .step .act { flex:0 0 auto; }
</style>
</head>
<body>
<div id="login">
  <section>
    <h2>Paperless Assistant — Dashboard</h2>
    <p class="note">Enter the dashboard token (set as <code>PA_UI_TOKEN</code> in
       the environment). It is stored only in this browser and sent as a header on
       every request.</p>
    <div class="row">
      <input id="token" type="password" placeholder="dashboard token"
             style="flex:1" autocomplete="current-password">
      <button class="primary" id="login-btn" type="button">Log in</button>
    </div>
    <div class="msg bad" id="login-msg"></div>
  </section>
</div>

<div id="app">
<header>
  <h1>Paperless Assistant</h1>
  <span class="sub" id="ts">loading…</span>
  <div class="right">
    <span class="pill" id="health">—</span>
    <button id="refresh" type="button">Refresh</button>
    <button id="logout" type="button">Log out</button>
  </div>
</header>
<nav class="tabs" role="tablist" aria-label="Dashboard sections">
  <button class="tab" id="tab-overview" role="tab" data-tab="overview"
          aria-selected="true" aria-controls="panel-overview">Overview</button>
  <button class="tab" id="tab-runs" role="tab" data-tab="runs"
          aria-selected="false" aria-controls="panel-runs">Runs</button>
  <button class="tab" id="tab-activity" role="tab" data-tab="activity"
          aria-selected="false" aria-controls="panel-activity">Activity</button>
  <button class="tab" id="tab-setup" role="tab" data-tab="setup"
          aria-selected="false" aria-controls="panel-setup">Setup &amp; Health</button>
  <button class="tab" id="tab-settings" role="tab" data-tab="settings"
          aria-selected="false" aria-controls="panel-settings">Settings</button>
</nav>

<div class="banner" id="paused-banner" role="status">
  <strong>⏸ PAUSED</strong>
  <span>Automatic processing (scheduled sweeps &amp; webhook nudges) is paused. The
    container is still running; a manual “Run now” still works.</span>
  <button id="banner-resume" type="button" class="subtle-btn">Resume</button>
</div>

<main>
  <!-- ============================= OVERVIEW ============================= -->
  <div class="tabpanel active" id="panel-overview" role="tabpanel"
       aria-labelledby="tab-overview">
  <section>
    <h2>Status</h2>
    <p class="note">Connectivity, last run, and spend vs. cap. Secrets come from the
       environment and are never shown here.</p>
    <div class="kpis" id="status-kpis"></div>
    <div id="spend-bar"></div>
  </section>

  <section>
    <h2>Live run</h2>
    <p class="note">Auto-refreshing view of the sweep in progress (scheduled or a
       manual “Run now”) — per-stage progress and what it did with each document,
       as it happens. Updates every few seconds; no refresh needed.</p>
    <div id="live-progress"></div>
  </section>

  <section>
    <h2>Library &amp; review queues</h2>
    <p class="note">Documents by ai_stage, count flagged for re-OCR, and the two
       human-review queues.</p>
    <div class="kpis" id="stats-kpis"></div>
    <div id="stats-err" class="err"></div>
  </section>
  </div>

  <!-- ============================== RUNS =============================== -->
  <div class="tabpanel" id="panel-runs" role="tabpanel" aria-labelledby="tab-runs">
  <section>
    <h2>Run now</h2>
    <p class="note">Starts the SAME sweep engine in the background (single-flight).
       Dry-run by default — all spend caps and review gates still apply. A manual run
       works even while automatic processing is paused.</p>
    <div class="row">
      <label><input type="checkbox" id="opt-write"> write (else dry-run)</label>
      <label><input type="checkbox" id="opt-reocr"> re-OCR</label>
      <label>limit <input type="number" id="opt-limit" min="0" style="width:80px"></label>
      <label>max spend $<input type="number" id="opt-maxspend" min="0" step="0.01"
             style="width:90px"></label>
      <button class="primary" id="run-btn" type="button">Run now</button>
    </div>
    <div class="msg" id="run-msg"></div>
  </section>

  <section>
    <h2>Run history</h2>
    <p class="note">Recent persisted run reports. Click a row for the per-stage
       detail.</p>
    <div class="scroll"><table id="runs-table"></table></div>
    <div id="run-detail"></div>
  </section>

  <section>
    <h2>Recent errors</h2>
    <p class="note">The tail of the log, filtered to failures / aborted stages.</p>
    <div id="errors"></div>
  </section>
  </div>

  <!-- ============================ ACTIVITY ============================= -->
  <div class="tabpanel" id="panel-activity" role="tabpanel"
       aria-labelledby="tab-activity">
  <section>
    <h2>Activity log</h2>
    <p class="note">Exactly what the assistant changed on each document —
       field-level before → after per change (and proposed changes for dry-runs).
       Records only real changes/errors, never no-ops. Filter, search, and page
       through history; expand a row for the full diff. Secrets are never stored
       here — only document metadata and the Paperless link.</p>
    <div class="row" style="margin-bottom:10px">
      <label>doc id <input type="number" id="act-doc" min="0" style="width:90px"></label>
      <label>stage
        <select id="act-stage">
          <option value="">any</option>
          <option value="triage">triage</option>
          <option value="metadata">metadata</option>
          <option value="reocr">reocr</option>
        </select></label>
      <label>mode
        <select id="act-dry">
          <option value="">any</option>
          <option value="0">applied</option>
          <option value="1">dry-run</option>
        </select></label>
      <label>status <input type="text" id="act-status" placeholder="e.g. done, ERROR" style="width:110px"></label>
    </div>
    <div class="row" style="margin-bottom:10px">
      <label>from <input type="date" id="act-from"></label>
      <label>to <input type="date" id="act-to"></label>
      <label>search <input type="text" id="act-q" placeholder="title / changed values" style="width:180px"></label>
      <button class="primary" id="act-apply" type="button">Apply</button>
      <button id="act-clear" type="button">Clear</button>
    </div>
    <div class="scroll"><table id="activity-table"></table></div>
    <div class="row" style="margin-top:10px">
      <button id="act-prev" type="button">‹ Prev</button>
      <span class="hint" id="act-page">—</span>
      <button id="act-next" type="button">Next ›</button>
    </div>
    <div id="activity-detail" style="margin-top:12px"></div>
  </section>

  <section>
    <h2>Retention &amp; purge</h2>
    <p class="note">Activity older than the retention window is purged automatically
       after each sweep (<code>activity_retention_days</code>, editable in Settings;
       0 = keep forever). You can also purge now.</p>
    <div class="row">
      <span class="pill" id="act-retention">—</span>
      <span class="pill" id="act-stats">—</span>
      <button class="primary" id="act-purge-btn" type="button">Purge now</button>
    </div>
    <div class="msg" id="act-purge-msg"></div>
  </section>
  </div>

  <!-- ========================= SETUP &amp; HEALTH ======================= -->
  <div class="tabpanel" id="panel-setup" role="tabpanel" aria-labelledby="tab-setup">
  <section>
    <h2>Guided first-run checklist</h2>
    <p class="note">Complete first-run setup from the browser — no
       <code>docker compose exec</code> needed. Each step reflects live state.</p>
    <div id="onboarding-steps"><div class="empty">loading…</div></div>
    <details style="margin-top:10px">
      <summary>Show the docker-compose service block</summary>
      <div class="readonly" id="onboarding-compose" style="max-height:none;margin-top:8px"></div>
    </details>
  </section>

  <section>
    <h2>Setup — provision fields &amp; tags</h2>
    <p class="note">Runs the idempotent provisioner (the same <code>pa setup</code>):
       creates the required custom fields and review tags if missing, never
       duplicating, and reports any incompatible existing field without changing it.</p>
    <div class="row">
      <button class="primary" id="setup-btn" type="button">Run setup</button>
    </div>
    <div class="msg" id="setup-msg"></div>
    <div id="setup-result"></div>
  </section>

  <section>
    <h2>Doctor — health check</h2>
    <p class="note">Runs the same <code>pa doctor</code> checks (connectivity, token
       scope, required fields/tags, provider credentials, spend caps). Read-only.</p>
    <div class="row">
      <button class="primary" id="doctor-btn" type="button">Run doctor</button>
    </div>
    <div class="msg" id="doctor-msg"></div>
    <div id="doctor-result"></div>
  </section>

  <section>
    <h2>Pause / Resume automatic processing</h2>
    <p class="note">A persisted switch that halts scheduled sweeps and webhook nudges
       without stopping the container. It survives a restart. A manual “Run now” is an
       explicit action and always works.</p>
    <div class="row">
      <span class="pill" id="pause-state">—</span>
      <button class="primary" id="pause-btn" type="button">Pause sweeps</button>
      <button id="resume-btn" type="button">Resume sweeps</button>
    </div>
    <div class="msg" id="pause-msg"></div>
  </section>
  </div>

  <!-- ============================= SETTINGS ============================= -->
  <div class="tabpanel" id="panel-settings" role="tabpanel"
       aria-labelledby="tab-settings">
  <div class="subnav" role="tablist" aria-label="Settings sections">
    <button class="tab" role="tab" data-sub="general" aria-selected="true"
            aria-controls="sub-general">General</button>
    <button class="tab" role="tab" data-sub="models" aria-selected="false"
            aria-controls="sub-models">Models</button>
    <button class="tab" role="tab" data-sub="prompts" aria-selected="false"
            aria-controls="sub-prompts">Prompts</button>
    <button class="tab" role="tab" data-sub="advanced" aria-selected="false"
            aria-controls="sub-advanced">Advanced</button>
  </div>

  <div class="subpanel active" id="sub-general" role="tabpanel">
  <section>
    <h2>Settings</h2>
    <p class="note">Editable tunables written to <code>/data/config.yml</code>.
       Secrets come from the environment (shown as set/unset only). Fields locked by
       an environment variable are disabled — env always beats YAML.</p>
    <div class="grid2" id="settings-form"></div>
    <div class="row" style="margin-top:12px">
      <button class="primary" id="save-btn" type="button">Save settings</button>
    </div>
    <div class="msg" id="settings-msg"></div>
    <div class="note" id="secrets-note" style="margin-top:10px"></div>
  </section>

  <section>
    <h2>Field &amp; stage names, performance</h2>
    <p class="note">Match your Paperless custom-field / <code>ai_stage</code> option
       names, tune HTTP timeouts for a slow server or big PDFs, and set the metadata
       text window &amp; token limits. Changing a field/stage NAME requires re-running
       <code>pa setup</code> and <code>pa doctor</code> so the new names are
       provisioned. Fields locked by an environment variable are disabled.</p>
    <div class="grid2" id="names-form"></div>
    <div class="row" style="margin-top:12px">
      <button class="primary" id="save-names-btn" type="button">Save</button>
    </div>
    <div class="msg" id="names-msg"></div>
  </section>
  </div><!-- /sub-general -->

  <div class="subpanel" id="sub-models" role="tabpanel">
  <section>
    <h2>AI models</h2>
    <p class="note">Pick the model per task from a pricing-annotated list, or choose
       <em>other…</em> to type a custom id. Prices are USD per 1K tokens (hints).
       Fields locked by an environment variable are disabled — env always beats YAML.</p>
    <div id="models-form"></div>
    <div class="row" style="margin-top:12px">
      <button class="primary" id="save-models-btn" type="button">Save models</button>
    </div>
    <div class="msg" id="models-msg"></div>
  </section>
  </div><!-- /sub-models -->

  <div class="subpanel" id="sub-prompts" role="tabpanel">
  <section>
    <h2>Prompts</h2>
    <p class="note">View and customize the instruction each task sends. Two levers:
       <em>extra instructions</em> are appended to the built-in default; the advanced
       <em>full override</em> replaces it (empty = default). Customizing changes only
       the instruction — the structured-output schema is fixed and every write is
       still validated, so a custom prompt can never corrupt Paperless.</p>
    <div id="prompts-form"></div>
    <div class="row" style="margin-top:12px">
      <button class="primary" id="save-prompts-btn" type="button">Save prompts</button>
    </div>
    <div class="msg" id="prompts-msg"></div>
  </section>
  </div><!-- /sub-prompts -->

  <div class="subpanel" id="sub-advanced" role="tabpanel">
  <section>
    <h2>Advanced</h2>
    <p class="note">Power-user tuning. The defaults are strong and reproduce the
       stock behavior exactly.</p>
    <details id="advanced-details">
      <summary>Show advanced tuning (expert only)</summary>
      <div class="warn-inline" style="margin-top:8px">⚠ These change how documents
        are flagged for re-OCR and how the client retries. Misconfiguring can
        degrade quality or mask outages. Each field has a Reset to default.</div>
      <h3 style="margin-top:14px;font-size:13px">OCR-quality (garbage) heuristic</h3>
      <div class="grid2" id="advanced-garbage"></div>
      <h3 style="margin-top:14px;font-size:13px">HTTP retry / backoff</h3>
      <div class="grid2" id="advanced-http"></div>
      <div class="row" style="margin-top:12px">
        <button class="primary" id="save-advanced-btn" type="button">Save advanced</button>
        <button id="reset-advanced-btn" type="button">Reset all to default</button>
      </div>
      <div class="msg" id="advanced-msg"></div>
    </details>
  </section>
  </div><!-- /sub-advanced -->
  </div><!-- /panel-settings -->
</main>
</div>

<script>
"use strict";
var TOKEN = null;
try { TOKEN = window.localStorage.getItem("pa_ui_token"); } catch(e){}

function esc(s){ return String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function kpi(n,l){ return '<div class="kpi"><div class="n">'+esc(n)+
  '</div><div class="l">'+esc(l)+'</div></div>'; }
function pill(t,c){ return '<span class="pill '+(c||"")+'">'+esc(t)+'</span>'; }

function api(path, opts){
  opts = opts || {};
  opts.headers = opts.headers || {};
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  if (opts.body && typeof opts.body !== "string"){
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(opts.body);
  }
  return fetch(path, opts).then(function(r){
    if (r.status === 401){ throw {code:401, msg:"unauthorized"}; }
    return r.json().then(function(j){ return {status:r.status, body:j}; });
  });
}

function showApp(){
  document.getElementById("login").style.display = "none";
  document.getElementById("app").style.display = "block";
}
function showLogin(msg){
  document.getElementById("app").style.display = "none";
  document.getElementById("login").style.display = "block";
  if (msg) document.getElementById("login-msg").textContent = msg;
}

function doLogin(){
  var v = document.getElementById("token").value.trim();
  if (!v){ document.getElementById("login-msg").textContent = "enter a token"; return; }
  TOKEN = v;
  try { window.localStorage.setItem("pa_ui_token", v); } catch(e){}
  // Probe an authed endpoint to confirm the token before showing the app.
  api("/api/status").then(function(res){
    if (res.status === 200){ document.getElementById("login-msg").textContent=""; showApp(); loadAll(); startPolling(); }
    else showLogin("login failed");
  }).catch(function(){ showLogin("invalid token"); });
}
function doLogout(){
  TOKEN = null;
  try { window.localStorage.removeItem("pa_ui_token"); } catch(e){}
  showLogin("");
}

// -- STATUS ---------------------------------------------------------------
function renderStatus(s){
  document.getElementById("ts").textContent = "updated " + new Date().toLocaleString();
  var sp = s.spend || {};
  var last = s.last_run;
  var lastTxt = last ? (new Date(last.ts).toLocaleString() +
     (last.dry_run ? " (dry-run)" : " (write)")) : "none yet";
  document.getElementById("status-kpis").innerHTML =
    kpi((s.stages_enabled||[]).join(", ")||"—","stages") +
    kpi(lastTxt,"last run") +
    kpi("$"+(sp.period_spend||0).toFixed(4),"spend this "+(sp.period||"period")) +
    kpi(sp.per_period_cap?("$"+Number(sp.per_period_cap).toFixed(2)):"∞","period cap");
  var pct = sp.period_pct;
  var bar = "";
  if (pct != null){
    var cls = sp.over_period_cap ? "bad" : (pct>=80 ? "warn" : "");
    bar = '<div class="bar"><span class="'+cls+'" style="width:'+Math.min(100,pct)+
      '%"></span></div> '+esc(pct)+"% of period cap";
  }
  document.getElementById("spend-bar").innerHTML = bar;
  // Paused banner (Overview) + Setup-&-Health pause controls reflect live state.
  renderPauseState(!!s.paused);
  var h = document.getElementById("health");
  if (s.paused){ h.className="pill warn"; h.textContent="paused"; }
  else if (s.run_in_progress){ h.className="pill warn"; h.textContent="run in progress"; }
  else if (sp.over_period_cap){ h.className="pill bad"; h.textContent="over cap"; }
  else { h.className="pill ok"; h.textContent="idle"; }
  // reflect an in-progress run into the run panel
  var cur = s.current_run || {};
  var rb = document.getElementById("run-btn");
  rb.disabled = !!s.run_in_progress;
  if (s.run_in_progress){
    document.getElementById("run-msg").className="msg";
    document.getElementById("run-msg").textContent = "run "+(cur.run_id||"")+" in progress…";
  } else if (cur.finished_at && cur.result){
    var r = cur.result;
    document.getElementById("run-msg").className="msg ok";
    document.getElementById("run-msg").textContent =
      "last manual run "+(r.dry_run?"(dry-run) ":"")+"done: "+
      JSON.stringify(r.counts)+"  spend $"+Number(r.spend_total||0).toFixed(4);
  } else if (cur.finished_at && cur.error){
    document.getElementById("run-msg").className="msg bad";
    document.getElementById("run-msg").textContent = "last manual run error: "+cur.error;
  }
}

// -- LIVE PROGRESS --------------------------------------------------------
// Map a per-document outcome status to a pill colour for the live view.
function progStatusCls(s){
  s = (s||"").toLowerCase();
  if (s.indexOf("error")>=0) return "bad";
  if (s.indexOf("skip")>=0 || s.indexOf("spend_cap")>=0) return "warn";
  return "ok";  // wrote / done / dry / reocr_done / metadata_done ...
}
function stageBar(st){
  var total = st.total||0, done = st.processed||0;
  var pct = total>0 ? Math.min(100, Math.round(100*done/total)) : (done>0?100:0);
  var label = total>0 ? (done+" / "+total) : (done>0? (done+" processed") : "0 eligible");
  var counts = st.counts||{};
  var ctxt = Object.keys(counts).map(function(k){ return esc(k)+":"+counts[k]; }).join("  ");
  return '<div class="field"><span>'+esc(st.stage)+
    (st.spend?(' <span class="hint">$'+Number(st.spend).toFixed(4)+'</span>'):'')+'</span>'+
    '<div><div class="bar"><span style="width:'+pct+'%"></span></div> '+
    esc(label)+(ctxt?('  <span class="hint">'+ctxt+'</span>'):'')+'</div></div>';
}
function renderProgress(p){
  var el = document.getElementById("live-progress");
  if (!p || !p.run_id){ el.innerHTML = '<div class="empty">no run yet</div>'; return; }
  // Fatal provider error (out of credits / bad key) that STOPPED the run — the
  // most important thing to show, so it goes at the top and stays until re-run.
  var errBox = "";
  if (p.error){
    var head = p.error.kind==="billing"
      ? "Run stopped — AI account out of credits or over quota"
      : (p.error.kind==="auth" ? "Run stopped — AI provider rejected the API key"
      : (p.error.kind==="config" ? "Stopped — selected AI provider isn't usable"
                                 : "Run stopped — AI provider error"));
    errBox = '<div class="msg bad" style="margin-bottom:8px">⚠ '+esc(head)+
      (p.error.stage?(' (during '+esc(p.error.stage)+')'):'')+'.'+
      (p.error.help?(' '+esc(p.error.help)):'')+
      (p.error.message?('<div class="hint" style="margin-top:4px">provider said: '+
        esc(p.error.message)+'</div>'):'')+'</div>';
  }
  var badge = p.active ? pill("running","warn")
    : pill(p.finished_at?"done":"idle", p.finished_at?"ok":"");
  var mode = p.dry_run ? pill("dry-run","warn") : pill("write","ok");
  var src = p.source ? (' <span class="hint">'+esc(p.source)+'</span>') : '';
  var when = p.updated_at ? (" · updated "+new Date(p.updated_at*1000).toLocaleTimeString()) : "";
  var head = '<p class="note">'+badge+" "+mode+src+
    ' run <code>'+esc(p.run_id||"")+'</code>'+
    ' · spend $'+Number(p.spend_total||0).toFixed(4)+esc(when)+'</p>';
  var bars = (p.stages||[]).map(stageBar).join("") ||
    '<div class="empty">waiting for the first stage…</div>';
  var rows = (p.recent||[]).map(function(r){
    var title = r.doc_title || ("doc "+(r.doc_id!=null?r.doc_id:"?"));
    var titleHtml = r.paperless_url
      ? '<a href="'+esc(r.paperless_url)+'" target="_blank" rel="noopener">'+esc(title)+'</a>'
      : esc(title);
    var t = r.ts ? new Date(r.ts*1000).toLocaleTimeString() : "";
    return '<tr><td class="hint">'+esc(t)+'</td><td>'+esc(r.stage||"")+'</td><td>'+
      pill(r.status||"", progStatusCls(r.status))+'</td><td>'+titleHtml+'</td><td>'+
      esc(r.summary||"")+'</td></tr>';
  }).join("");
  var recent = rows
    ? ('<div class="scroll"><table><tr><th>time</th><th>stage</th><th>result</th>'+
       '<th>document</th><th>detail</th></tr>'+rows+'</table></div>')
    : '<div class="empty">no documents processed yet</div>';
  el.innerHTML = errBox + head + bars +
    '<h3 style="margin:10px 0 4px">Documents (latest first, last '+
    (p.recent_max||0)+')</h3>' + recent;
}

// -- STATS ----------------------------------------------------------------
function renderStats(s){
  var errEl = document.getElementById("stats-err");
  if (s.error){ errEl.textContent = "Paperless unreachable: "+s.error;
    document.getElementById("stats-kpis").innerHTML=""; return; }
  errEl.textContent = "";
  var bs = s.by_stage||{}; var rq = s.review_queues||{};
  document.getElementById("stats-kpis").innerHTML =
    kpi(s.total_documents!=null?s.total_documents:"—","documents") +
    kpi(bs.triaged||0,"triaged") + kpi(bs.reocr_done||0,"reocr done") +
    kpi(bs.metadata_done||0,"metadata done") + kpi(bs.none||0,"untouched") +
    kpi(s.flagged_ocr_quality||0,"flagged (>= "+s.triage_threshold+")") +
    kpi((rq.superseded||{}).count||0,"superseded") +
    kpi((rq.ai_new_taxonomy||{}).count||0,"ai-new-taxonomy");
}

// -- RUNS -----------------------------------------------------------------
function renderRuns(d){
  var rows = d.runs||[];
  var t = "<tr><th>started</th><th>mode</th><th>counts</th><th>spend</th>"+
          "<th>new tax</th><th>superseded</th></tr>";
  if (!rows.length){ t += '<tr><td colspan="6" class="empty">no runs yet</td></tr>'; }
  rows.forEach(function(x){
    t += '<tr style="cursor:pointer" data-run="'+esc(x.run_id)+'"><td>'+
      esc(x.started_at?new Date(x.started_at).toLocaleString():"—")+"</td><td>"+
      (x.dry_run?pill("dry-run","warn"):pill("write","ok"))+"</td><td>"+
      esc(JSON.stringify(x.counts||{}))+"</td><td>$"+Number(x.spend_total||0).toFixed(4)+
      "</td><td>"+esc((x.new_taxonomy||[]).length)+"</td><td>"+
      esc((x.superseded||[]).length)+"</td></tr>";
  });
  var tbl = document.getElementById("runs-table");
  tbl.innerHTML = t;
  Array.prototype.forEach.call(tbl.querySelectorAll("[data-run]"), function(tr){
    tr.addEventListener("click", function(){ loadRunDetail(tr.getAttribute("data-run")); });
  });
}
function loadRunDetail(id){
  api("/api/run?id="+encodeURIComponent(id)).then(function(res){
    var el = document.getElementById("run-detail");
    if (res.status !== 200){ el.textContent = "detail unavailable"; return; }
    var r = res.body;
    var html = '<div class="scroll"><table><tr><th>stage</th><th>counts</th>'+
      "<th>spend</th></tr>";
    (r.stages||[]).forEach(function(st){
      html += "<tr><td>"+esc(st.stage)+"</td><td>"+esc(JSON.stringify(st.counts||{}))+
        "</td><td>$"+Number(st.spend_total||0).toFixed(4)+"</td></tr>";
    });
    html += "</table></div>";
    el.innerHTML = "<p class='note'>Run "+esc(r.run_id)+" — "+
      (r.dry_run?"dry-run":"write")+", spend $"+Number(r.spend_total||0).toFixed(4)+
      "</p>"+html;
  });
}

// -- ERRORS ---------------------------------------------------------------
function renderErrors(d){
  var evs = d.events||[];
  var el = document.getElementById("errors");
  if (!evs.length){ el.innerHTML = '<div class="empty">no recent errors</div>'; return; }
  el.innerHTML = evs.slice().reverse().map(function(e){
    return '<div class="logline error">'+esc(e.ts||"")+"  "+esc(e.event||"")+
      "  "+esc(e.error||e.detail||e.reason||"")+"</div>";
  }).join("");
}

// -- CONFIG / SETTINGS ----------------------------------------------------
var LAST_CONFIG = null;
function renderConfig(c){
  LAST_CONFIG = c;
  var v = c.values||{}; var locked = c.env_locked||{};
  // Share env-locks with the models + prompts editors so their env-locked fields
  // disable consistently (env always beats YAML).
  MODELS_LOCKED = {
    metadata_provider: !!locked.metadata_provider, metadata_model: !!locked.metadata_model,
    ocr_provider: !!locked.ocr_provider, ocr_model: !!locked.ocr_model
  };
  PROMPTS_LOCKED = {
    metadata_extra_instructions: !!locked.metadata_extra_instructions,
    metadata_prompt_override: !!locked.metadata_prompt_override,
    ocr_extra_instructions: !!locked.ocr_extra_instructions,
    ocr_prompt_override: !!locked.ocr_prompt_override
  };
  var f = document.getElementById("settings-form");
  function field(key, label, value, type, opts){
    var isLocked = !!locked[key];
    var id = "set-"+key;
    var input;
    if (type === "checkbox"){
      input = '<input type="checkbox" id="'+id+'" '+(value?"checked":"")+
        (isLocked?" disabled":"")+'>';
    } else if (type === "select"){
      input = '<select id="'+id+'"'+(isLocked?" disabled":"")+'>'+
        opts.map(function(o){ return '<option '+(o===value?"selected":"")+'>'+
          esc(o)+"</option>"; }).join("")+"</select>";
    } else {
      input = '<input type="'+(type||"text")+'" id="'+id+'" value="'+esc(value)+'"'+
        (isLocked?" disabled":"")+'>';
    }
    return '<div class="field"><span>'+esc(label)+
      (isLocked?'<span class="locked">env-locked</span>':"")+"</span>"+input+"</div>";
  }
  var sp = v.spend||{};
  f.innerHTML =
    field("paperless_public_url","public Paperless URL for browser links (blank = use PAPERLESS_URL)",v.paperless_public_url,"text") +
    field("mode","mode",v.mode,"text") +
    field("workers","workers",v.workers,"number") +
    field("limit","per-run document limit (0 = all eligible, per stage)",v.limit,"number") +
    field("triage_enabled","triage enabled",v.stages_enabled&&v.stages_enabled.indexOf("triage")>=0,"checkbox") +
    field("metadata_enabled","metadata enabled",v.stages_enabled&&v.stages_enabled.indexOf("metadata")>=0,"checkbox") +
    field("reocr_enabled","re-OCR enabled",v.reocr_enabled,"checkbox") +
    field("triage_threshold","triage threshold",v.triage_threshold,"number") +
    field("schedule_interval_seconds","schedule interval (s)",v.schedule_interval_seconds,"number") +
    field("activity_enabled","activity log enabled",v.activity_enabled,"checkbox") +
    field("activity_retention_days","activity retention (days, 0=forever)",v.activity_retention_days,"number") +
    field("spend_per_run","spend cap per run ($)",sp.per_run_cap,"number") +
    field("spend_per_period","spend cap per period ($)",sp.per_period_cap,"number") +
    // AI provider + model for both tasks are chosen together under the "Models"
    // section (consistent dropdowns), not here — see renderModels().
    field("superseded_tag","superseded tag",v.review_tags&&v.review_tags.superseded,"text") +
    field("new_taxonomy_tag","new-taxonomy tag",v.review_tags&&v.review_tags.new_taxonomy,"text");
  var sec = c.secrets||{};
  document.getElementById("secrets-note").innerHTML =
    "Secrets (set via environment only): " +
    Object.keys(sec).map(function(k){ return esc(k)+": "+(sec[k]?"yes":"no"); }).join(" · ");
  renderNames(c);
  renderAdvanced(c);
}

// -- NAMES / PERFORMANCE (prompt 011, normal tier) ------------------------
// A small field spec so inputs + reset are driven generically. `path` is the
// nested location in `values`/`defaults`; `key` is the flat env-lock key.
var NAMES_SPEC = [
  {id:"field_names.score", label:"custom field: OCR quality", key:"field_score", type:"text"},
  {id:"field_names.stage", label:"custom field: AI stage", key:"field_stage", type:"text"},
  {id:"field_names.notes", label:"custom field: AI notes", key:"field_notes", type:"text"},
  {id:"stage_names.triaged", label:"stage label: triaged", key:"stage_triaged", type:"text"},
  {id:"stage_names.reocr_done", label:"stage label: reocr_done", key:"stage_reocr_done", type:"text"},
  {id:"stage_names.metadata_done", label:"stage label: metadata_done", key:"stage_metadata_done", type:"text"},
  {id:"http.request_timeout", label:"HTTP request timeout (s)", key:"http_request_timeout", type:"number"},
  {id:"http.download_timeout", label:"download timeout (s)", key:"http_download_timeout", type:"number"},
  {id:"http.post_document_timeout", label:"upload/consume timeout (s)", key:"http_post_document_timeout", type:"number"},
  {id:"http.task_poll_timeout", label:"consume-task poll timeout (s)", key:"http_task_poll_timeout", type:"number"},
  {id:"http.task_poll_interval", label:"consume-task poll interval (s)", key:"http_task_poll_interval", type:"number"},
  {id:"http.page_size", label:"API page size", key:"http_page_size", type:"number"},
  {id:"metadata_window.content_head", label:"metadata window: head (chars)", key:"metadata_content_head", type:"number"},
  {id:"metadata_window.content_tail", label:"metadata window: tail (chars)", key:"metadata_content_tail", type:"number"},
  {id:"metadata_window.max_tokens", label:"metadata output tokens", key:"metadata_max_tokens", type:"number"},
  {id:"max_ocr_tokens", label:"re-OCR output tokens", key:null, type:"number"},
  {id:"review_tags.superseded_color", label:"superseded tag color", key:null, type:"text"},
  {id:"review_tags.new_taxonomy_color", label:"new-taxonomy tag color", key:null, type:"text"}
];
function dig(obj, path){ var p=path.split("."); var v=obj;
  for (var i=0;i<p.length;i++){ if(v==null) return undefined; v=v[p[i]]; } return v; }
function fieldHtml(dom, val, type, locked){
  return '<input type="'+(type||"text")+'" id="'+dom+'" value="'+esc(val==null?"":val)+'"'+
    (locked?" disabled":"")+'>';
}
function renderNames(c){
  var v=c.values||{}; var locked=c.env_locked||{};
  var el=document.getElementById("names-form");
  el.innerHTML = NAMES_SPEC.map(function(f){
    var dom="nm-"+f.id.replace(/\./g,"_");
    var isLocked = f.key ? !!locked[f.key] : false;
    return '<div class="field"><span>'+esc(f.label)+
      (isLocked?'<span class="locked">env-locked</span>':"")+"</span>"+
      fieldHtml(dom, dig(v,f.id), f.type, isLocked)+"</div>";
  }).join("") +
    '<div class="field"><span>metadata-eligible stages (comma-separated roles: '+
    'triaged, reocr_done, metadata_done; blank = untouched)</span>'+
    '<input type="text" id="nm-eligible" value="'+
    esc((v.metadata_eligible_roles||[]).map(function(r){return r===""?"(none)":r;}).join(", "))+
    '"></div>';
}
function saveNames(){
  var msg=document.getElementById("names-msg");
  var locked=(LAST_CONFIG&&LAST_CONFIG.env_locked)||{};
  var payload={field_names:{}, stage_names:{}, http:{}, metadata_window:{}};
  NAMES_SPEC.forEach(function(f){
    if (f.key && locked[f.key]) return;
    var dom="nm-"+f.id.replace(/\./g,"_"); var el=document.getElementById(dom);
    if (!el) return;
    var val = f.type==="number" ? parseFloat(el.value) : el.value;
    if (f.type==="number" && isNaN(val)) return;
    var p=f.id.split(".");
    if (p.length===2 && payload[p[0]]!==undefined){ payload[p[0]][p[1]]=val; }
    else if (f.id==="max_ocr_tokens"){ payload.max_ocr_tokens=val; }
    else if (f.id==="review_tags.superseded_color"){ payload.superseded_tag_color=val; }
    else if (f.id==="review_tags.new_taxonomy_color"){ payload.new_taxonomy_tag_color=val; }
  });
  // eligible roles
  var raw=document.getElementById("nm-eligible").value;
  var roles=raw.split(",").map(function(s){return s.trim();})
    .filter(function(s){return s!=="";})
    .map(function(s){return s==="(none)"?"":s;});
  // Always include "" (untouched) if the user left an explicit (none) or blank.
  payload.metadata_eligible_roles = roles.length?roles:[""];
  // Drop empty blocks so we don't send noise.
  ["field_names","stage_names","http","metadata_window"].forEach(function(b){
    if (payload[b] && !Object.keys(payload[b]).length) delete payload[b];
  });
  api("/api/config",{method:"POST",body:payload}).then(function(res){
    if (res.status===200){ msg.className="msg ok"; msg.textContent="saved. "+(res.body.note||""); loadConfig(); }
    else { msg.className="msg bad"; msg.textContent="error: "+(res.body.error||res.status); }
  }).catch(function(){ msg.className="msg bad"; msg.textContent="save failed"; });
}

// -- ADVANCED (prompt 011): garbage heuristic + HTTP retry, reset per field --
var ADV_GARBAGE = [
  {k:"min_length", label:"min length (chars)", type:"number"},
  {k:"word_ratio_weight", label:"word-ratio weight", type:"number"},
  {k:"plausible_weight", label:"plausible-word weight", type:"number"},
  {k:"fragment_weight", label:"fragment weight", type:"number"},
  {k:"fragment_threshold", label:"fragment threshold (avg word len)", type:"number"},
  {k:"plausible_min_len", label:"plausible min word len", type:"number"}
];
var ADV_HTTP = [
  {k:"retries", label:"retry attempts", type:"number"},
  {k:"backoff_initial", label:"initial backoff (s)", type:"number"},
  {k:"backoff_cap", label:"backoff cap (s)", type:"number"}
];
function advFieldHtml(block, spec, val, def){
  var dom="adv-"+block+"-"+spec.k;
  return '<div class="field"><span>'+esc(spec.label)+
    ' <span class="hint">(default '+esc(def)+')</span></span>'+
    '<div class="row" style="gap:6px"><input type="number" step="any" id="'+dom+'" value="'+
    esc(val==null?"":val)+'" style="flex:1">'+
    '<button class="subtle-btn" type="button" data-reset="'+dom+'" data-def="'+esc(def)+'">Reset</button></div></div>';
}
function renderAdvanced(c){
  var gh=(c.values||{}).garbage_heuristic||{}; var ghd=(c.defaults||{}).garbage_heuristic||{};
  var ht=(c.values||{}).http||{}; var htd=(c.defaults||{}).http||{};
  document.getElementById("advanced-garbage").innerHTML =
    ADV_GARBAGE.map(function(s){ return advFieldHtml("garbage",s,gh[s.k],ghd[s.k]); }).join("");
  document.getElementById("advanced-http").innerHTML =
    ADV_HTTP.map(function(s){ return advFieldHtml("http",s,ht[s.k],htd[s.k]); }).join("");
  Array.prototype.forEach.call(document.querySelectorAll("[data-reset]"), function(b){
    b.addEventListener("click", function(){
      document.getElementById(b.getAttribute("data-reset")).value = b.getAttribute("data-def");
    });
  });
}
function saveAdvanced(){
  var msg=document.getElementById("advanced-msg");
  var payload={garbage_heuristic:{}, http:{}};
  ADV_GARBAGE.forEach(function(s){ var el=document.getElementById("adv-garbage-"+s.k);
    if (el && el.value!==""){ var n=parseFloat(el.value); if(!isNaN(n)) payload.garbage_heuristic[s.k]=n; } });
  ADV_HTTP.forEach(function(s){ var el=document.getElementById("adv-http-"+s.k);
    if (el && el.value!==""){ var n=parseFloat(el.value); if(!isNaN(n)) payload.http[s.k]=n; } });
  api("/api/config",{method:"POST",body:payload}).then(function(res){
    if (res.status===200){ msg.className="msg ok"; msg.textContent="saved. "+(res.body.note||""); loadConfig(); }
    else { msg.className="msg bad"; msg.textContent="error: "+(res.body.error||res.status); }
  }).catch(function(){ msg.className="msg bad"; msg.textContent="save failed"; });
}
function resetAdvancedAll(){
  if (!LAST_CONFIG) return;
  var ghd=(LAST_CONFIG.defaults||{}).garbage_heuristic||{};
  var htd=(LAST_CONFIG.defaults||{}).http||{};
  ADV_GARBAGE.forEach(function(s){ var el=document.getElementById("adv-garbage-"+s.k); if(el) el.value=ghd[s.k]; });
  ADV_HTTP.forEach(function(s){ var el=document.getElementById("adv-http-"+s.k); if(el) el.value=htd[s.k]; });
}

function saveSettings(){
  if (!LAST_CONFIG) return;
  var locked = LAST_CONFIG.env_locked||{};
  var payload = {};
  function val(key){ var el=document.getElementById("set-"+key); return el; }
  function put(key){ if (locked[key]) return; var el=val(key); if(!el) return;
    if (el.type==="checkbox") payload[key]=el.checked;
    else if (el.type==="number") payload[key]=parseFloat(el.value);
    else payload[key]=el.value; }
  ["mode","workers","limit","triage_enabled","metadata_enabled","reocr_enabled",
   "triage_threshold","schedule_interval_seconds","activity_enabled",
   "activity_retention_days","superseded_tag","new_taxonomy_tag"].forEach(put);
  var spend = {};
  if (!locked.spend_per_run){ var a=val("spend_per_run"); if(a) spend.per_run=parseFloat(a.value); }
  if (!locked.spend_per_period){ var b=val("spend_per_period"); if(b) spend.per_period=parseFloat(b.value); }
  if (Object.keys(spend).length) payload.spend = spend;
  // Provider/model are saved from the Models section (saveModels), not here.

  var msg = document.getElementById("settings-msg");
  api("/api/config",{method:"POST",body:payload}).then(function(res){
    if (res.status===200){ msg.className="msg ok"; msg.textContent="saved. "+(res.body.note||""); loadConfig(); }
    else { msg.className="msg bad"; msg.textContent="error: "+(res.body.error||res.status); }
  }).catch(function(){ msg.className="msg bad"; msg.textContent="save failed"; });
}

// -- MODELS ---------------------------------------------------------------
var MODELS = null; var MODELS_LOCKED = {};
function priceHint(m){
  if (m.in_price_per_1k==null && m.out_price_per_1k==null) return "local / $0";
  return "$"+Number(m.in_price_per_1k||0).toFixed(4)+" in / $"+
    Number(m.out_price_per_1k||0).toFixed(4)+" out per 1K";
}
function renderModels(d){
  MODELS = d;
  var cat = d.catalog||{}; var cur = d.current||{};
  var providers = Object.keys(cat).sort();
  var f = document.getElementById("models-form");
  function taskBlock(task, label){
    var c = cur[task]||{}; var provLocked = !!MODELS_LOCKED[task+"_provider"];
    var modelLocked = !!MODELS_LOCKED[task+"_model"];
    var provSel = '<select id="mp-'+task+'-provider"'+(provLocked?" disabled":"")+'>'+
      providers.map(function(p){ return '<option '+(p===c.provider?"selected":"")+'>'+
        esc(p)+"</option>"; }).join("")+"</select>";
    return '<div class="taskblock" data-task="'+task+'"><h3>'+esc(label)+
      (provLocked||modelLocked?'<span class="locked"> env-locked</span>':"")+'</h3>'+
      '<div class="row"><label>provider '+provSel+'</label>'+
      '<label>model <select id="mp-'+task+'-model"'+(modelLocked?" disabled":"")+
      '></select></label>'+
      '<label class="hint" id="mp-'+task+'-custom-wrap" style="display:none">custom id '+
      '<input type="text" id="mp-'+task+'-custom"'+(modelLocked?" disabled":"")+
      ' style="width:220px"></label></div>'+
      '<div class="hint" id="mp-'+task+'-price"></div>'+
      '<div class="warn-inline" id="mp-'+task+'-vwarn" style="display:none"></div></div>';
  }
  f.innerHTML = taskBlock("metadata","Metadata task") + taskBlock("ocr","Re-OCR task (needs vision)");
  ["metadata","ocr"].forEach(function(task){
    var c = cur[task]||{};
    fillModelOptions(task, c.provider, c.model, !c.in_catalog);
    document.getElementById("mp-"+task+"-provider").addEventListener("change", function(){
      fillModelOptions(task, this.value, null, false); onModelChange(task);
    });
    document.getElementById("mp-"+task+"-model").addEventListener("change", function(){ onModelChange(task); });
    var custom = document.getElementById("mp-"+task+"-custom");
    if (custom) custom.addEventListener("input", function(){ onModelChange(task); });
    onModelChange(task);
  });
}
function fillModelOptions(task, provider, selectModel, forceCustom){
  var cat = (MODELS&&MODELS.catalog&&MODELS.catalog[provider])||[];
  var sel = document.getElementById("mp-"+task+"-model");
  var opts = cat.map(function(m){
    var star = m.recommended ? " ★" : "";
    return '<option value="'+esc(m.id)+'" '+((m.id===selectModel)?"selected":"")+'>'+
      esc(m.label)+star+"</option>"; }).join("");
  opts += '<option value="__other__"'+((forceCustom||( selectModel && !cat.some(function(m){return m.id===selectModel;})))?" selected":"")+'>other…</option>';
  sel.innerHTML = opts;
  var customWrap = document.getElementById("mp-"+task+"-custom-wrap");
  var isOther = sel.value==="__other__";
  customWrap.style.display = isOther ? "inline-flex" : "none";
  if (isOther && selectModel){ document.getElementById("mp-"+task+"-custom").value = selectModel; }
}
function currentModelFor(task){
  var sel = document.getElementById("mp-"+task+"-model");
  if (!sel) return "";
  if (sel.value==="__other__"){ var c=document.getElementById("mp-"+task+"-custom"); return c?c.value.trim():""; }
  return sel.value;
}
function onModelChange(task){
  var sel = document.getElementById("mp-"+task+"-model");
  var customWrap = document.getElementById("mp-"+task+"-custom-wrap");
  customWrap.style.display = sel.value==="__other__" ? "inline-flex" : "none";
  var provider = document.getElementById("mp-"+task+"-provider").value;
  var model = currentModelFor(task);
  var cat = (MODELS&&MODELS.catalog&&MODELS.catalog[provider])||[];
  var entry = cat.filter(function(m){ return m.id===model; })[0];
  var priceEl = document.getElementById("mp-"+task+"-price");
  priceEl.textContent = entry ? priceHint(entry) : (model?("custom model — pricing unknown; spend cap still applies"):"");
  // Re-OCR vision warning: warn when the selected model isn't vision-capable.
  var vwarn = document.getElementById("mp-"+task+"-vwarn");
  if (task==="ocr"){
    var vision = entry ? !!entry.vision : false;
    if (model && !vision){
      vwarn.style.display="block";
      vwarn.textContent = "⚠ '"+model+"' is not known to be vision-capable. Re-OCR needs vision; the run will refuse this model.";
    } else { vwarn.style.display="none"; vwarn.textContent=""; }
  }
}
function saveModels(){
  var msg = document.getElementById("models-msg");
  var payload = {};
  ["metadata","ocr"].forEach(function(task){
    if (MODELS_LOCKED[task+"_provider"] && MODELS_LOCKED[task+"_model"]) return;
    var block = {};
    if (!MODELS_LOCKED[task+"_provider"]) block.provider = document.getElementById("mp-"+task+"-provider").value;
    if (!MODELS_LOCKED[task+"_model"]) block.model = currentModelFor(task);
    if (Object.keys(block).length) payload[task] = block;
  });
  if (!Object.keys(payload).length){ msg.className="msg"; msg.textContent="nothing to save (all env-locked)"; return; }
  api("/api/config",{method:"POST",body:payload}).then(function(res){
    if (res.status===200){ msg.className="msg ok"; msg.textContent="saved. "+(res.body.note||""); loadModels(); }
    else { msg.className="msg bad"; msg.textContent="error: "+(res.body.error||res.status); }
  }).catch(function(){ msg.className="msg bad"; msg.textContent="save failed"; });
}

// -- PROMPTS --------------------------------------------------------------
var PROMPTS = null; var PROMPTS_LOCKED = {};
function renderPrompts(d){
  PROMPTS = d;
  var f = document.getElementById("prompts-form");
  function block(task, label){
    var t = d[task]||{};
    var extraLocked = !!PROMPTS_LOCKED[task+"_extra_instructions"];
    var ovLocked = !!PROMPTS_LOCKED[task+"_prompt_override"];
    return '<div class="taskblock" data-task="'+task+'"><h3>'+esc(label)+'</h3>'+
      '<div class="field"><span>built-in default (read-only)</span>'+
      '<div class="readonly">'+esc(t.default)+"</div></div>"+
      '<div class="field" style="margin-top:8px"><span>extra instructions (appended)'+
      (extraLocked?'<span class="locked">env-locked</span>':"")+'</span>'+
      '<textarea id="pr-'+task+'-extra" rows="3"'+(extraLocked?" disabled":"")+'>'+
      esc(t.extra_instructions||"")+"</textarea>"+
      '<button class="subtle-btn" type="button" data-reset-extra="'+task+'" style="margin-top:5px;align-self:flex-start"'+
      (extraLocked?" disabled":"")+'>Reset to default</button></div>'+
      '<details'+(t.prompt_override?" open":"")+'><summary>Advanced: full instruction override</summary>'+
      '<div class="field" style="margin-top:6px"><span>full override (replaces the default; empty = default)'+
      (ovLocked?'<span class="locked">env-locked</span>':"")+'</span>'+
      '<textarea id="pr-'+task+'-override" rows="5"'+(ovLocked?" disabled":"")+'>'+
      esc(t.prompt_override||"")+"</textarea>"+
      '<button class="subtle-btn" type="button" data-reset-override="'+task+'" style="margin-top:5px;align-self:flex-start"'+
      (ovLocked?" disabled":"")+'>Reset to default</button></div></details>'+
      '<div class="field" style="margin-top:8px"><span>effective prompt (what the engine will send)</span>'+
      '<div class="readonly" id="pr-'+task+'-effective">'+esc(t.effective)+"</div></div></div>";
  }
  f.innerHTML = block("metadata","Metadata instruction") + block("ocr","Re-OCR instruction");
  ["metadata","ocr"].forEach(function(task){
    var ex = document.getElementById("pr-"+task+"-extra");
    var ov = document.getElementById("pr-"+task+"-override");
    if (ex) ex.addEventListener("input", function(){ updateEffective(task); });
    if (ov) ov.addEventListener("input", function(){ updateEffective(task); });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-reset-extra]"), function(b){
    b.addEventListener("click", function(){ var t=b.getAttribute("data-reset-extra");
      document.getElementById("pr-"+t+"-extra").value=""; updateEffective(t); });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-reset-override]"), function(b){
    b.addEventListener("click", function(){ var t=b.getAttribute("data-reset-override");
      document.getElementById("pr-"+t+"-override").value=""; updateEffective(t); });
  });
}
// Compose the effective preview client-side with the SAME rule as the server
// (override-or-default, then append extra) so the preview updates as you type.
function composeEffective(deflt, override, extra){
  var base = (override && override.trim()) ? override : deflt;
  return (extra && extra.trim()) ? (base + "\n\n" + extra.trim()) : base;
}
function updateEffective(task){
  var t = (PROMPTS&&PROMPTS[task])||{};
  var extra = document.getElementById("pr-"+task+"-extra").value;
  var ov = document.getElementById("pr-"+task+"-override").value;
  document.getElementById("pr-"+task+"-effective").textContent =
    composeEffective(t.default||"", ov, extra);
}
function savePrompts(){
  var msg = document.getElementById("prompts-msg");
  var payload = {};
  ["metadata","ocr"].forEach(function(task){
    var block = {};
    if (!PROMPTS_LOCKED[task+"_extra_instructions"]) block.extra_instructions = document.getElementById("pr-"+task+"-extra").value;
    if (!PROMPTS_LOCKED[task+"_prompt_override"]) block.prompt_override = document.getElementById("pr-"+task+"-override").value;
    if (Object.keys(block).length) payload[task] = block;
  });
  if (!Object.keys(payload).length){ msg.className="msg"; msg.textContent="nothing to save (all env-locked)"; return; }
  api("/api/config",{method:"POST",body:payload}).then(function(res){
    if (res.status===200){ msg.className="msg ok"; msg.textContent="saved. "+(res.body.note||""); loadPrompts(); }
    else { msg.className="msg bad"; msg.textContent="error: "+(res.body.error||res.status); }
  }).catch(function(){ msg.className="msg bad"; msg.textContent="save failed"; });
}

// -- SETUP & HEALTH (prompt 012) ------------------------------------------
function statusMark(st){
  if (st==="ok") return pill("OK","ok");
  if (st==="warn") return pill("WARN","warn");
  if (st==="fail") return pill("FAIL","bad");
  return pill(esc(st||"?"),"");
}
function doSetup(){
  var msg=document.getElementById("setup-msg");
  var btn=document.getElementById("setup-btn");
  btn.disabled=true; msg.className="msg"; msg.textContent="running setup…";
  api("/api/setup",{method:"POST",body:{}}).then(function(res){
    btn.disabled=false;
    if (res.status!==200){
      msg.className="msg bad";
      msg.textContent="error: "+((res.body&&res.body.error)||res.status);
      return;
    }
    var r=res.body.report||{};
    msg.className="msg "+(r.ok?"ok":"bad");
    msg.textContent = r.ok ? (r.noop?"already provisioned (no changes).":"setup complete.")
      : "setup reported incompatibilities — see below.";
    renderSetupReport(r);
    loadOnboarding();
  }).catch(function(){ btn.disabled=false; msg.className="msg bad"; msg.textContent="setup failed"; });
}
function renderSetupReport(r){
  function list(label,arr,cls){
    if (!arr||!arr.length) return "";
    return '<div style="margin-top:6px"><strong>'+esc(label)+':</strong> '+
      arr.map(function(x){return '<span class="pill '+(cls||"")+'">'+esc(x)+'</span>';}).join(" ")+'</div>';
  }
  var el=document.getElementById("setup-result");
  var html = list("created fields", r.created_fields, "ok") +
    list("existing fields", r.existing_fields) +
    list("created tags", r.created_tags, "ok") +
    list("existing tags", r.existing_tags);
  if (r.incompatible && r.incompatible.length){
    html += '<div style="margin-top:8px"><strong class="err">incompatible:</strong>'+
      r.incompatible.map(function(m){return '<div class="warn-inline">'+esc(m)+'</div>';}).join("")+'</div>';
  }
  el.innerHTML = html || '<div class="empty">no changes</div>';
}
function doDoctor(){
  var msg=document.getElementById("doctor-msg");
  var btn=document.getElementById("doctor-btn");
  btn.disabled=true; msg.className="msg"; msg.textContent="running checks…";
  api("/api/doctor").then(function(res){
    btn.disabled=false;
    if (res.status!==200){ msg.className="msg bad"; msg.textContent="error: "+res.status; return; }
    var d=res.body||{};
    msg.className="msg "+(d.healthy?"ok":"bad");
    msg.textContent = d.healthy ? "healthy — all checks pass." : "unhealthy — one or more checks failed.";
    var el=document.getElementById("doctor-result");
    el.innerHTML = (d.checks||[]).map(function(c){
      return '<div class="check"><div class="st">'+statusMark(c.status)+'</div>'+
        '<div class="body"><div>'+esc(c.name)+' — '+esc(c.message)+'</div>'+
        (c.fix?'<div class="fix">fix: '+esc(c.fix)+'</div>':'')+'</div></div>';
    }).join("") || '<div class="empty">no checks</div>';
    loadOnboarding();
  }).catch(function(){ btn.disabled=false; msg.className="msg bad"; msg.textContent="doctor failed"; });
}
function setPaused(paused){
  var msg=document.getElementById("pause-msg");
  msg.className="msg"; msg.textContent = paused?"pausing…":"resuming…";
  api(paused?"/api/pause":"/api/resume",{method:"POST",body:{}}).then(function(res){
    if (res.status!==200){ msg.className="msg bad"; msg.textContent="error: "+res.status; return; }
    msg.className="msg ok";
    msg.textContent = res.body.paused ? "paused — automatic processing halted." : "resumed — sweeps will run on schedule.";
    loadStatus();
  }).catch(function(){ msg.className="msg bad"; msg.textContent="failed"; });
}
function renderPauseState(paused){
  var banner=document.getElementById("paused-banner");
  if (banner){ banner.className = "banner"+(paused?" show":""); }
  var ps=document.getElementById("pause-state");
  if (ps){ ps.className="pill "+(paused?"warn":"ok"); ps.textContent = paused?"paused":"running"; }
  var pb=document.getElementById("pause-btn"), rb=document.getElementById("resume-btn");
  if (pb) pb.disabled = paused;
  if (rb) rb.disabled = !paused;
}
function loadOnboarding(){
  return api("/api/onboarding").then(function(res){
    if (res.status!==200) return;
    var d=res.body||{};
    document.getElementById("onboarding-compose").textContent =
      (d.compose||"") + "\n" + (d.next_steps||"");
    var mark=function(done){ return done===true?"✅":(done===false?"⬜":"❔"); };
    document.getElementById("onboarding-steps").innerHTML =
      (d.steps||[]).map(function(st){
        var act="";
        if (st.action==="setup") act='<button class="subtle-btn" type="button" data-ob="setup">'+esc(st.action_label)+'</button>';
        else if (st.action==="doctor") act='<button class="subtle-btn" type="button" data-ob="doctor">'+esc(st.action_label)+'</button>';
        else if (st.action==="run") act='<button class="subtle-btn" type="button" data-ob="run">'+esc(st.action_label)+'</button>';
        return '<div class="step"><div class="mark">'+mark(st.done)+'</div>'+
          '<div class="body"><div class="t">'+esc(st.title)+'</div>'+
          '<div class="d">'+esc(st.detail||"")+'</div></div>'+
          '<div class="act">'+act+'</div></div>';
      }).join("");
    Array.prototype.forEach.call(document.querySelectorAll("[data-ob]"), function(b){
      b.addEventListener("click", function(){
        var a=b.getAttribute("data-ob");
        if (a==="setup") doSetup();
        else if (a==="doctor") doDoctor();
        else if (a==="run"){ activateTab("runs"); }
      });
    });
  });
}

// -- ACTIVITY (prompt 013): filter + paginate + expand-to-diff -------------
var ACT_OFFSET = 0; var ACT_LIMIT = 25; var ACT_TOTAL = 0;
function actFilters(){
  var f = {};
  var doc = document.getElementById("act-doc").value;
  if (doc!=="") f.doc_id = doc;
  var stage = document.getElementById("act-stage").value; if (stage) f.stage = stage;
  var dry = document.getElementById("act-dry").value; if (dry!=="") f.dry_run = dry;
  var status = document.getElementById("act-status").value.trim(); if (status) f.status = status;
  var q = document.getElementById("act-q").value.trim(); if (q) f.q = q;
  var from = document.getElementById("act-from").value;
  if (from){ var d=new Date(from+"T00:00:00"); if(!isNaN(d)) f.since = Math.floor(d.getTime()/1000); }
  var to = document.getElementById("act-to").value;
  if (to){ var d2=new Date(to+"T23:59:59"); if(!isNaN(d2)) f.until = Math.floor(d2.getTime()/1000); }
  return f;
}
function loadActivity(){
  var f = actFilters();
  f.limit = ACT_LIMIT; f.offset = ACT_OFFSET;
  var qs = Object.keys(f).map(function(k){ return encodeURIComponent(k)+"="+encodeURIComponent(f[k]); }).join("&");
  return api("/api/activity?"+qs).then(function(res){
    if (res.status===200) renderActivity(res.body);
  });
}
function tsFmt(ts){ if (ts==null) return "—"; try { return new Date(ts*1000).toLocaleString(); } catch(e){ return String(ts); } }
function diffHtml(ch){
  if (!ch) return '<span class="empty">(no detail)</span>';
  var out = [];
  var fields = ch.fields||{};
  Object.keys(fields).forEach(function(k){
    var b=fields[k].before, a=fields[k].after;
    out.push('<div><code>'+esc(k)+'</code>: '+esc(b==null?"(none)":b)+' → '+esc(a==null?"(none)":a)+'</div>');
  });
  var tags = ch.tags||{};
  if (tags.added && tags.added.length) out.push('<div><code>tags</code>: '+tags.added.map(function(t){return "+"+esc(t);}).join(" ")+'</div>');
  if (tags.removed && tags.removed.length) out.push('<div><code>tags</code>: '+tags.removed.map(function(t){return "−"+esc(t);}).join(" ")+'</div>');
  if (ch.supersede){ out.push('<div><code>supersede</code>: old doc '+esc(ch.supersede.old_doc_id)+
    ' → new doc '+esc(ch.supersede.new_doc_id==null?"(pending)":ch.supersede.new_doc_id)+'</div>'); }
  (ch.flags||[]).forEach(function(fl){ out.push('<div>'+pill(fl,"warn")+'</div>'); });
  if (ch.error) out.push('<div class="err">error: '+esc(ch.error)+'</div>');
  return out.length ? out.join("") : '<span class="empty">(no field changes)</span>';
}
function renderActivity(d){
  ACT_TOTAL = d.total||0;
  document.getElementById("act-retention").textContent =
    "retention: "+(d.retention_days? (d.retention_days+" days") : "keep forever");
  var st = d.stats||{};
  document.getElementById("act-stats").textContent =
    (st.count||0)+" rows"+(st.size_bytes? (" · "+Math.round(st.size_bytes/1024)+" KB") : "");
  var rows = d.rows||[];
  var t = "<tr><th>time</th><th>doc</th><th>stage</th><th>mode</th><th>status</th>"+
          "<th>change summary</th></tr>";
  if (!rows.length){ t += '<tr><td colspan="6" class="empty">no activity matches</td></tr>'; }
  rows.forEach(function(r){
    var docCell = esc(r.doc_id);
    if (r.paperless_url){ docCell = '<a href="'+esc(r.paperless_url)+'" target="_blank" rel="noopener">'+esc(r.doc_id)+'</a>'; }
    if (r.doc_title) docCell += " " + esc(r.doc_title);
    var modeBadge = r.dry_run ? pill("dry-run","warn") : pill("applied","ok");
    t += '<tr style="cursor:pointer" data-act="'+esc(r.id)+'"><td>'+esc(tsFmt(r.ts))+"</td><td>"+
      docCell+"</td><td>"+esc(r.stage||"")+"</td><td>"+modeBadge+"</td><td>"+
      (r.status==="ERROR"?pill("ERROR","bad"):esc(r.status||""))+"</td><td>"+esc(r.summary||"")+"</td></tr>";
  });
  var tbl = document.getElementById("activity-table");
  tbl.innerHTML = t;
  var byId = {}; rows.forEach(function(r){ byId[r.id]=r; });
  Array.prototype.forEach.call(tbl.querySelectorAll("[data-act]"), function(tr){
    tr.addEventListener("click", function(){
      var r = byId[tr.getAttribute("data-act")];
      var el = document.getElementById("activity-detail");
      el.innerHTML = '<section><h2>Doc '+esc(r.doc_id)+' — '+esc(r.stage||"")+
        ' ('+(r.dry_run?"dry-run":"applied")+')</h2>'+
        '<p class="note">'+esc(tsFmt(r.ts))+(r.run_id?(" · run "+esc(r.run_id)):"")+'</p>'+
        diffHtml(r.changes)+'</section>';
    });
  });
  var pages = Math.max(1, Math.ceil(ACT_TOTAL/ACT_LIMIT));
  var page = Math.floor(ACT_OFFSET/ACT_LIMIT)+1;
  document.getElementById("act-page").textContent = "page "+page+" / "+pages+" ("+ACT_TOTAL+" total)";
  document.getElementById("act-prev").disabled = ACT_OFFSET<=0;
  document.getElementById("act-next").disabled = (ACT_OFFSET+ACT_LIMIT)>=ACT_TOTAL;
}
function actApply(){ ACT_OFFSET=0; loadActivity(); }
function actClear(){
  ["act-doc","act-status","act-q","act-from","act-to"].forEach(function(id){ document.getElementById(id).value=""; });
  document.getElementById("act-stage").value=""; document.getElementById("act-dry").value="";
  ACT_OFFSET=0; loadActivity();
}
function actPurge(){
  var msg=document.getElementById("act-purge-msg");
  msg.className="msg"; msg.textContent="purging…";
  api("/api/activity/purge",{method:"POST",body:{}}).then(function(res){
    if (res.status!==200){ msg.className="msg bad"; msg.textContent="error: "+res.status; return; }
    msg.className="msg ok";
    msg.textContent = "purged "+(res.body.purged||0)+" row(s). "+(res.body.note||"");
    loadActivity();
  }).catch(function(){ msg.className="msg bad"; msg.textContent="purge failed"; });
}

// -- TAB CONTROLLER (prompt 012): vanilla JS, hash deep-link, keyboard ------
var TAB_ORDER = ["overview","runs","activity","setup","settings"];
function activateTab(name){
  if (TAB_ORDER.indexOf(name)<0) name="overview";
  TAB_ORDER.forEach(function(t){
    var btn=document.getElementById("tab-"+t);
    var panel=document.getElementById("panel-"+t);
    var on = (t===name);
    if (btn){ btn.setAttribute("aria-selected", on?"true":"false"); btn.tabIndex = on?0:-1; }
    if (panel){ panel.className = "tabpanel"+(on?" active":""); }
  });
  if (window.location.hash !== "#"+name){
    try { history.replaceState(null,"","#"+name); } catch(e){ window.location.hash=name; }
  }
}
function activateSub(name){
  ["general","models","prompts","advanced"].forEach(function(s){
    var panel=document.getElementById("sub-"+s);
    if (panel) panel.className = "subpanel"+(s===name?" active":"");
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-sub]"), function(b){
    b.setAttribute("aria-selected", b.getAttribute("data-sub")===name?"true":"false");
  });
}
function initTabs(){
  var tabs = Array.prototype.slice.call(document.querySelectorAll("nav.tabs .tab"));
  tabs.forEach(function(btn){
    btn.addEventListener("click", function(){ activateTab(btn.getAttribute("data-tab")); });
    btn.addEventListener("keydown", function(e){
      var i = tabs.indexOf(btn);
      if (e.key==="ArrowRight" || e.key==="ArrowLeft"){
        e.preventDefault();
        var ni = e.key==="ArrowRight" ? (i+1)%tabs.length : (i-1+tabs.length)%tabs.length;
        tabs[ni].focus(); activateTab(tabs[ni].getAttribute("data-tab"));
      } else if (e.key==="Home"){ e.preventDefault(); tabs[0].focus(); activateTab(tabs[0].getAttribute("data-tab")); }
      else if (e.key==="End"){ e.preventDefault(); tabs[tabs.length-1].focus(); activateTab(tabs[tabs.length-1].getAttribute("data-tab")); }
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-sub]"), function(b){
    b.addEventListener("click", function(){ activateSub(b.getAttribute("data-sub")); });
  });
  var hash = (window.location.hash||"").replace("#","");
  activateTab(hash || "overview");
  activateSub("general");
  window.addEventListener("hashchange", function(){
    activateTab((window.location.hash||"").replace("#",""));
  });
}

function doRun(){
  var payload = {
    write: document.getElementById("opt-write").checked,
    reocr: document.getElementById("opt-reocr").checked,
    limit: document.getElementById("opt-limit").value || null,
    max_spend: document.getElementById("opt-maxspend").value || null
  };
  var msg = document.getElementById("run-msg");
  api("/api/run",{method:"POST",body:payload}).then(function(res){
    if (res.status===202){ msg.className="msg ok"; msg.textContent="started run "+res.body.run_id+"…"; loadStatus(); }
    else if (res.status===409){ msg.className="msg bad"; msg.textContent="a run is already in progress"; }
    else { msg.className="msg bad"; msg.textContent="error: "+(res.body.error||res.status); }
  }).catch(function(){ msg.className="msg bad"; msg.textContent="run failed"; });
}

// -- loaders --------------------------------------------------------------
function guard(res){ return res; }
function loadStatus(){ return api("/api/status").then(function(r){ if(r.status===200) renderStatus(r.body); }); }
function loadProgress(){ return api("/api/progress").then(function(r){ if(r.status===200) renderProgress(r.body); }); }
function loadStats(){ return api("/api/stats").then(function(r){ if(r.status===200) renderStats(r.body); }); }
function loadRuns(){ return api("/api/runs").then(function(r){ if(r.status===200) renderRuns(r.body); }); }
function loadErrors(){ return api("/api/logs?errors=1&limit=50").then(function(r){ if(r.status===200) renderErrors(r.body); }); }
function loadConfig(){ return api("/api/config").then(function(r){ if(r.status===200) renderConfig(r.body); }); }
function loadModels(){ return api("/api/models").then(function(r){ if(r.status===200) renderModels(r.body); }); }
function loadPrompts(){ return api("/api/prompts").then(function(r){ if(r.status===200) renderPrompts(r.body); }); }
function loadAll(){
  loadStatus().catch(handleAuthErr);
  loadProgress();
  loadStats(); loadRuns(); loadErrors(); loadOnboarding(); loadActivity();
  // Load config first (populates env-locks), then the model + prompt editors.
  loadConfig().then(function(){ loadModels(); loadPrompts(); });
}
function handleAuthErr(e){ if (e && e.code===401) doLogout(); }

var pollTimer = null;
function startPolling(){
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(function(){
    loadStatus().catch(handleAuthErr); loadProgress(); loadErrors();
  }, 4000);
}

document.getElementById("login-btn").addEventListener("click", doLogin);
document.getElementById("token").addEventListener("keydown", function(e){ if(e.key==="Enter") doLogin(); });
document.getElementById("logout").addEventListener("click", doLogout);
document.getElementById("refresh").addEventListener("click", loadAll);
document.getElementById("run-btn").addEventListener("click", doRun);
document.getElementById("save-btn").addEventListener("click", saveSettings);
document.getElementById("save-models-btn").addEventListener("click", saveModels);
document.getElementById("save-prompts-btn").addEventListener("click", savePrompts);
document.getElementById("save-names-btn").addEventListener("click", saveNames);
document.getElementById("save-advanced-btn").addEventListener("click", saveAdvanced);
document.getElementById("reset-advanced-btn").addEventListener("click", resetAdvancedAll);
document.getElementById("setup-btn").addEventListener("click", doSetup);
document.getElementById("doctor-btn").addEventListener("click", doDoctor);
document.getElementById("pause-btn").addEventListener("click", function(){ setPaused(true); });
document.getElementById("resume-btn").addEventListener("click", function(){ setPaused(false); });
document.getElementById("banner-resume").addEventListener("click", function(){ setPaused(false); });
document.getElementById("act-apply").addEventListener("click", actApply);
document.getElementById("act-clear").addEventListener("click", actClear);
document.getElementById("act-purge-btn").addEventListener("click", actPurge);
document.getElementById("act-prev").addEventListener("click", function(){ ACT_OFFSET=Math.max(0,ACT_OFFSET-ACT_LIMIT); loadActivity(); });
document.getElementById("act-next").addEventListener("click", function(){ if((ACT_OFFSET+ACT_LIMIT)<ACT_TOTAL){ ACT_OFFSET+=ACT_LIMIT; loadActivity(); } });
document.getElementById("act-q").addEventListener("keydown", function(e){ if(e.key==="Enter") actApply(); });
initTabs();

// If a token is already stored, try to resume the session.
if (TOKEN){
  api("/api/status").then(function(res){
    if (res.status===200){ showApp(); loadAll(); startPolling(); }
    else showLogin("");
  }).catch(function(){ showLogin(""); });
} else { showLogin(""); }
</script>
</body>
</html>
"""
