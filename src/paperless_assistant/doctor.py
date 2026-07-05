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

"""Doctor - `pa doctor` health/preflight checks (plan §8.2 step 3, §8.4).

Verifies and reports:
  * Paperless connectivity (can we reach the REST API at all?)
  * token validity + scope (per connectivity §4: WARN if the token looks like an
    admin/superuser token and recommend a scoped service user)
  * presence + correct data_type of the required custom fields and review tags
  * configured provider reachability / credentials for the selected task providers
  * the resolved effective config, INCLUDING spend caps

Each check yields a Check(status, message, fix). `pa doctor` exits NON-ZERO on any
failure with an actionable message (I6 spirit: say exactly what's wrong + how to
fix it). WARN does not fail the run; FAIL does.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass

import requests

from . import config
from .provision import REQUIRED_FIELDS, required_fields


def _package_installed(name: str) -> bool:
    """True if an importable module `name` is available, WITHOUT importing it
    (no side effects). Used to verify a selected provider's SDK is present."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str  # OK / WARN / FAIL
    message: str
    fix: str = ""


class DoctorResult:
    def __init__(self):
        self.checks: list[Check] = []

    def add(self, name, status, message, fix=""):
        self.checks.append(Check(name, status, message, fix))

    @property
    def failed(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    def to_dict(self):
        return {
            "healthy": not self.failed,
            "checks": [
                {"name": c.name, "status": c.status, "message": c.message, "fix": c.fix}
                for c in self.checks
            ],
        }


def _looks_like_admin(client) -> bool | None:
    """Best-effort: does this token belong to a superuser/admin? Returns True /
    False, or None if we can't tell (endpoint absent on this Paperless version).

    Uses /api/ui_settings/ which embeds the current user's flags on modern
    Paperless. We treat is_superuser / is_staff as the admin signal (connectivity
    §4)."""
    try:
        r = client.request("GET", f"{client.base}/api/ui_settings/")
        data = r.json()
    except Exception:
        return None
    user = data.get("user") if isinstance(data, dict) else None
    if isinstance(user, dict):
        if user.get("is_superuser") or user.get("is_staff"):
            return True
        # If the flags are explicitly present and false, it's a scoped user.
        if "is_superuser" in user or "is_staff" in user:
            return False
    return None


def run_doctor(settings, client, *, check_providers=True) -> DoctorResult:
    """Run all checks against a resolved `settings` + a PaperlessClient."""
    result = DoctorResult()

    # --- 1. connectivity + token validity --------------------------------
    # Probe a concrete authenticated endpoint, NOT the API root `/api/`: the
    # root 302-redirects and (once followed) yields 406 Not Acceptable on real
    # Paperless-NGX, so it is useless as a health probe. /api/ui_settings/
    # returns 200 with a valid token and 401/403 with a bad one.
    reachable = False
    try:
        client.request("GET", f"{client.base}/api/ui_settings/")
        reachable = True
        result.add(
            "connectivity", OK,
            f"Paperless reachable at {client.base}",
        )
    except requests.HTTPError as e:
        msg = str(e)
        if "401" in msg or "403" in msg:
            result.add(
                "connectivity", FAIL,
                f"Paperless rejected the token (auth error) at {client.base}.",
                fix="Check PAPERLESS_TOKEN is a valid API token for this instance.",
            )
        else:
            result.add(
                "connectivity", FAIL,
                f"Paperless returned an error at {client.base}: {msg}",
                fix="Verify PAPERLESS_URL points at the Paperless webserver.",
            )
    except Exception as e:
        result.add(
            "connectivity", FAIL,
            f"Cannot reach Paperless at {client.base}: {e}",
            fix="Verify PAPERLESS_URL and that the paperless service is up on the "
                "compose network (e.g. http://webserver:8000).",
        )

    if not reachable:
        # No point probing further; report what we can (config) and bail.
        _add_config_summary(result, settings)
        return result

    # --- 2. token scope (admin warning, connectivity §4) -----------------
    admin = _looks_like_admin(client)
    if admin is True:
        result.add(
            "token-scope", WARN,
            "The Paperless token appears to belong to an ADMIN/superuser account.",
            fix="Create a dedicated non-admin service user with an API token scoped "
                "to documents/custom_fields/tags/correspondents/document_types/tasks "
                "and use that token instead (least privilege, connectivity §4).",
        )
    elif admin is False:
        result.add("token-scope", OK, "Token belongs to a non-admin (scoped) user.")
    else:
        result.add(
            "token-scope", WARN,
            "Could not determine whether the token is admin-scoped on this "
            "Paperless version.",
            fix="Confirm the token belongs to a dedicated service user, not admin.",
        )

    # --- 3. required custom fields present + correct type ----------------
    try:
        fields = {f["name"]: f for f in client.get_all("custom_fields")}
    except Exception as e:
        fields = {}
        result.add("custom-fields", FAIL, f"Could not list custom fields: {e}",
                   fix="Ensure the token can read /api/custom_fields/.")
    # Prompt 011: check the CONFIGURED field/stage names (byte-identical default).
    required = required_fields(
        getattr(settings, "field_names", None),
        getattr(settings, "stage_names", None),
    )
    for name, spec in required.items():
        if name not in fields:
            result.add(
                f"field:{name}", FAIL,
                f"Required custom field '{name}' is missing.",
                fix="Run `pa setup` to provision the required fields and tags.",
            )
            continue
        have = fields[name].get("data_type")
        want = spec["data_type"]
        compatible = have == want
        if spec.get("role") == "score" and have in ("float", "integer", "monetary"):
            compatible = True
        if not compatible:
            result.add(
                f"field:{name}", FAIL,
                f"Custom field '{name}' has data_type='{have}', expected '{want}'.",
                fix="Rename/delete the conflicting field in the UI, then `pa setup`.",
            )
            continue
        if want == "select":
            opts = (fields[name].get("extra_data") or {}).get("select_options") or []
            have_labels = {o.get("label") for o in opts}
            missing = [o for o in spec["options"] if o not in have_labels]
            if missing:
                result.add(
                    f"field:{name}", FAIL,
                    f"Select field '{name}' is missing option(s) {missing}.",
                    fix="Add the missing options in the UI, then re-run `pa doctor`.",
                )
                continue
        result.add(f"field:{name}", OK, f"Custom field '{name}' present ({have}).")

    # --- 4. review-gate tags present -------------------------------------
    for tag in (settings.superseded_tag, settings.new_taxonomy_tag):
        try:
            data = client.request(
                "GET", f"{client.base}/api/tags/?name__iexact={tag}"
            ).json()
            if data.get("results"):
                result.add(f"tag:{tag}", OK, f"Review tag '{tag}' present.")
            else:
                result.add(
                    f"tag:{tag}", FAIL, f"Review tag '{tag}' is missing.",
                    fix="Run `pa setup` to create the review tags.",
                )
        except Exception as e:
            result.add(f"tag:{tag}", FAIL, f"Could not check tag '{tag}': {e}")

    # --- 5. provider reachability / credentials --------------------------
    if check_providers:
        _check_providers(result, settings)

    # --- 6. on-ingest webhook nudge config (Phase 4) ---------------------
    _check_webhook(result, settings)

    # --- 7. resolved effective config (incl. spend caps) -----------------
    _add_config_summary(result, settings)
    return result


def _check_webhook(result, settings):
    """Report on the on-ingest webhook receiver (plan §6.2). OFF is fine (the
    scheduled sweep is authoritative). Enabled-without-a-secret is a FAIL because
    the receiver would refuse to start; enabled-with-a-secret is OK and reminds the
    user to point a Paperless Workflow->Webhook at the in-network URL."""
    wh = settings.webhook
    if not wh.enabled:
        result.add("webhook", OK,
                   "on-ingest webhook nudge is OFF (scheduled sweep is "
                   "authoritative). Enable with PA_WEBHOOK_ENABLED=true + "
                   "PA_WEBHOOK_SECRET to get near-real-time processing.")
        return
    if not wh.secret:
        result.add(
            "webhook", FAIL,
            "webhook is ENABLED but PA_WEBHOOK_SECRET is not set — the receiver "
            "would refuse to start an unauthenticated listener.",
            fix="Set PA_WEBHOOK_SECRET in the environment (never the YAML).",
        )
        return
    result.add(
        "webhook", OK,
        f"webhook nudge receiver enabled on {wh.host}:{wh.port}{wh.path} "
        f"(in-network only, NO host port; shared secret configured). Point a "
        f"Paperless Workflow->Webhook (trigger: Consumption Finished/Updated, NOT "
        f"Added) at http://paperless-assistant:{wh.port}{wh.path}?token=<secret>, "
        f"body {{\"doc_url\": \"{{doc_url}}\"}}.",
    )


def _check_providers(result, settings):
    """Verify each SELECTED task provider has what it needs (credentials present /
    endpoint configured). Capability, not a live network call - a live probe would
    spend money / need a running Ollama."""
    tasks = [("metadata", settings.metadata_task)]
    if settings.reocr_enabled:
        tasks.append(("ocr", settings.ocr_task))

    seen = set()
    for task_name, task in tasks:
        prov = task.provider
        key = (prov, task_name)
        if key in seen:
            continue
        seen.add(key)
        if prov == "anthropic":
            if settings.anthropic_api_key:
                result.add(f"provider:{task_name}", OK,
                           f"{task_name}: anthropic credentials present.")
            else:
                result.add(
                    f"provider:{task_name}", FAIL,
                    f"{task_name} uses anthropic but ANTHROPIC_API_KEY is not set.",
                    fix="Set ANTHROPIC_API_KEY in the environment (never the YAML).",
                )
        elif prov == "openai":
            if not settings.openai_api_key:
                result.add(
                    f"provider:{task_name}", FAIL,
                    f"{task_name} uses openai but OPENAI_API_KEY is not set.",
                    fix="Set OPENAI_API_KEY in the environment.",
                )
            elif not _package_installed("openai"):
                result.add(
                    f"provider:{task_name}", FAIL,
                    f"{task_name} uses openai but the 'openai' package isn't installed.",
                    fix="Use the official Docker image (it bundles openai), or "
                        "`pip install paperless-assistant[openai]`.",
                )
            else:
                result.add(f"provider:{task_name}", OK,
                           f"{task_name}: openai credentials + package present.")
        elif prov == "ollama":
            if not settings.ollama_endpoint:
                result.add(
                    f"provider:{task_name}", FAIL,
                    f"{task_name} uses ollama but no endpoint is configured.",
                    fix="Set PA_OLLAMA_ENDPOINT (e.g. http://ollama:11434).",
                )
            elif not _package_installed("httpx"):
                result.add(
                    f"provider:{task_name}", FAIL,
                    f"{task_name} uses ollama but the 'httpx' package isn't installed.",
                    fix="Use the official Docker image (it bundles httpx), or "
                        "`pip install paperless-assistant[ollama]`.",
                )
            else:
                result.add(f"provider:{task_name}", OK,
                           f"{task_name}: ollama endpoint {settings.ollama_endpoint} "
                           f"(local, zero-egress).")
        else:
            result.add(
                f"provider:{task_name}", FAIL,
                f"{task_name} uses unknown provider '{prov}'.",
                fix="Use one of: anthropic, openai, ollama.",
            )


def _add_config_summary(result, settings):
    pub = settings.to_public_dict()
    msg = (
        f"mode={pub['mode']} stages={pub['stages_enabled']} "
        f"reocr={'on' if pub['reocr_enabled'] else 'OFF'} "
        f"spend per_run=${pub['spend']['per_run_cap']:.2f} "
        f"per_{pub['spend']['period']}=${pub['spend']['per_period_cap']:.2f} "
        f"workers={pub['workers']} dry_run={pub['dry_run']} data={pub['data_dir']}"
    )
    result.add("config", OK, msg)
