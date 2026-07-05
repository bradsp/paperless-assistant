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

"""Config - constants + env resolution + the layered settings system (plan §7).

Extracted from: the argparse blocks and module-level constants across all three
scripts. Phase 1 kept config minimal (env vars + CLI flags). Phase 3 GROWS this
into the layered resolver (plan §7.1):

    built-in safe defaults  <  mounted YAML file  <  env vars  <  per-run overrides

The Phase 1 `Config` dataclass + `Config.from_env` are preserved verbatim for
backward compatibility (existing callers/tests keep working). The layered system
is `Settings` + `load_settings()`; `Settings.to_config()` produces a `Config`, so
the two coexist and the new onboarding/sweep code builds on `Settings`.

Secrets come ONLY from the environment / secret files (PAPERLESS_TOKEN,
ANTHROPIC_API_KEY, OPENAI_API_KEY, PA_AGENT_TOKEN, ...), NEVER from the YAML
config file - enforced in `load_settings` (plan §7.1).
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field

# --- Custom-field names (must match what the user created in the Paperless UI).
FIELD_SCORE = "ocr_quality"
FIELD_STAGE = "ai_stage"
FIELD_NOTES = "ai_notes"

# --- ai_stage state-machine labels.
STAGE_TRIAGED = "triaged"
STAGE_REOCR_DONE = "reocr_done"
STAGE_METADATA_DONE = "metadata_done"

# The ai_stage state machine advances triaged -> reocr_done -> metadata_done.
# "Already handled" (I1) means a doc has reached `triaged` OR any LATER stage;
# it must never be re-triaged back to `triaged` (which would reset its stage and
# make metadata re-process + re-bill it, an I1/I3 violation). This ordered tuple
# is the single source of truth for that predicate (see
# StageOrchestrator.already_triaged).
STAGE_ORDER = (STAGE_TRIAGED, STAGE_REOCR_DONE, STAGE_METADATA_DONE)

# --- Review-gate tag names (I4).
SUPERSEDED_TAG = "superseded"
NEW_TAXONOMY_TAG = "ai-new-taxonomy"

# --- Stage 2 eligibility (stage2_metadata.py).
GARBAGE_THRESH = 0.55
ELIGIBLE_STAGES = {None, "", "triaged"}  # add "reocr_done" after Stage 1 is done

# --- Triage default threshold (stage0_triage.py argparse default).
DEFAULT_TRIAGE_THRESHOLD = 0.55

# --- Models (unchanged from the scripts).
OCR_MODEL = "claude-opus-4-8"          # stage1
METADATA_MODEL = "claude-sonnet-4-6"   # stage2
MAX_OCR_TOKENS = 8000

# --- Provider selection (Phase 2). "Which model" is config; "what task" is code.
# Anthropic is the safe default for BOTH tasks, so a fresh config reproduces
# Phase 1 behavior exactly. BYO-key credentials/endpoints come from env only.
DEFAULT_PROVIDER = "anthropic"

# --- Pricing guardrails (USD/token). The cap is a SAFETY ABORT, not accounting.
# DEPRECATED (Phase 2): the authoritative per-provider/per-model price numbers
# now live in `providers/pricing.py`. These module-level constants are kept only
# as legacy aliases (some external callers may still read them) and are NOT used
# by the engine's spend path anymore. Do not add new prices here.
# stage1 (opus-class):
PRICE_IN_PER_TOK = 15.0 / 1_000_000
PRICE_OUT_PER_TOK = 75.0 / 1_000_000
# stage2 (sonnet-class):
PRICE_IN = 3.0 / 1_000_000
PRICE_OUT = 15.0 / 1_000_000

# --- Metadata content window (chars) sent to the model (stage2).
CONTENT_HEAD = 6000
CONTENT_TAIL = 1500

# --- Metadata structured-output token cap (stage2). Byte-identical to the POC
# tool call (`max_tokens=1024`); exposed as a normal tunable (metadata.max_tokens).
METADATA_MAX_TOKENS = 1024

# --- Paperless REST client defaults (client.py). Exposed as normal tunables
# (HTTP timeouts / pagination) + Advanced retry/backoff. All byte-identical to the
# hardcoded values they replace.
DEFAULT_REQUEST_TIMEOUT = 90        # client.request(timeout=90)
DEFAULT_DOWNLOAD_TIMEOUT = 120      # download_original(timeout=120)
DEFAULT_POST_DOCUMENT_TIMEOUT = 180 # post_document(timeout=180)
DEFAULT_TASK_POLL_TIMEOUT = 180     # find_new_doc_by_task(timeout=180)
DEFAULT_TASK_POLL_INTERVAL = 3.0    # find_new_doc_by_task sleep(3)
DEFAULT_PAGE_SIZE = 100             # iter_documents page_size=100 (get_all uses 200)
# Advanced retry/backoff (client.request): 6 tries, 1.0s initial backoff, 30s cap.
DEFAULT_HTTP_RETRIES = 6
DEFAULT_HTTP_BACKOFF_INITIAL = 1.0
DEFAULT_HTTP_BACKOFF_CAP = 30.0

# --- garbage_score heuristic coefficients + gates (ocr.py). Advanced tuning. The
# DEFAULTS below reproduce today's EXACT scores byte-for-byte.
GARBAGE_MIN_LENGTH = 40          # < this many stripped chars -> empty_or_tiny (1.0)
GARBAGE_WORD_RATIO_WEIGHT = 0.45
GARBAGE_PLAUSIBLE_WEIGHT = 0.45
GARBAGE_FRAGMENT_WEIGHT = 0.10
GARBAGE_FRAGMENT_THRESHOLD = 2.5  # avg_word_len < this -> fragment penalty
GARBAGE_PLAUSIBLE_MIN_LEN = 3     # plausible word: len >= this AND has a vowel

# --- Default worker counts (per-script argparse defaults).
DEFAULT_TRIAGE_WORKERS = 4
DEFAULT_REOCR_WORKERS = 3
DEFAULT_METADATA_WORKERS = 3


@dataclass
class HostedInferenceContext:
    """Runtime wiring for the agent-side HostedProvider (Phase 6).

    Carries the OUTBOUND transport and the agent-credential auth-headers callable
    the HostedProvider uses to reach the control-plane inference proxy. Injected by
    the running HostedAgent, so the provider reuses the SAME outbound-only path and
    the SAME (only) agent secret. It holds NO vendor model key — that lives
    server-side. `ocr_model` / `metadata_model` are optional non-secret hints.
    """

    transport: object
    auth_headers: object  # zero-arg callable -> dict of headers
    ocr_model: str = ""
    metadata_model: str = ""
    vision: bool | None = None


@dataclass
class ProviderSettings:
    """Per-task provider selection. `vision` may pin whether the configured
    model has the vision capability (None = let the adapter infer from the
    model name)."""

    provider: str = DEFAULT_PROVIDER
    vision: bool | None = None


@dataclass
class Config:
    """Resolved runtime config: connection + secrets from env, everything else
    from CLI flags. Phase 2 adds per-task provider/model/endpoint selection with
    Anthropic-default safe defaults that reproduce Phase 1 behavior."""

    base_url: str
    paperless_token: str
    anthropic_api_key: str = ""

    # --- Per-task provider selection (Phase 2). ---------------------------
    ocr_provider: str = DEFAULT_PROVIDER
    metadata_provider: str = DEFAULT_PROVIDER
    ocr_model: str = OCR_MODEL
    metadata_model: str = METADATA_MODEL
    max_ocr_tokens: int = MAX_OCR_TOKENS
    # Prompt 011: metadata structured-output token cap (byte-identical default).
    metadata_max_tokens: int = METADATA_MAX_TOKENS

    # --- Non-Anthropic credentials / endpoints (env only). ----------------
    openai_api_key: str = ""
    openai_base_url: str = ""
    ollama_endpoint: str = "http://localhost:11434"

    # Optional explicit vision override per task (None = infer from model name).
    ocr_vision: bool | None = None

    # --- Phase 6 hosted-inference wiring (agent-side, no AI key). ----------
    # When `hosted_inference` is set, the registry resolves BOTH tasks to the
    # HostedProvider, which calls the control-plane inference proxy over the
    # agent's outbound transport. This object carries the (transport, auth_headers)
    # the running HostedAgent injects — never a vendor model key (that stays
    # server-side). None => not in hosted-inference mode (BYO/local).
    hosted_inference: object = None

    def provider_for(self, task: str) -> ProviderSettings:
        """Return the ProviderSettings for a task ("ocr" or "metadata")."""
        if self.hosted_inference is not None:
            # Hosted inference selected: both tasks route to the HostedProvider.
            return ProviderSettings(provider="hosted", vision=None)
        if task == "ocr":
            return ProviderSettings(provider=self.ocr_provider, vision=self.ocr_vision)
        if task == "metadata":
            return ProviderSettings(provider=self.metadata_provider, vision=None)
        raise ValueError(f"unknown AI task '{task}'")

    @classmethod
    def from_env(
        cls,
        *,
        require_anthropic: bool = False,
        ocr_provider: str | None = None,
        metadata_provider: str | None = None,
        ocr_model: str | None = None,
        metadata_model: str | None = None,
    ) -> "Config":
        base = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
        token = os.environ.get("PAPERLESS_TOKEN", "")
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not token:
            raise SystemExit(
                "Set PAPERLESS_TOKEN (and optionally PAPERLESS_URL) in the environment."
            )

        # Provider precedence: explicit arg (CLI flag) > env > default.
        ocr_prov = ocr_provider or os.environ.get("PA_OCR_PROVIDER") or DEFAULT_PROVIDER
        meta_prov = (
            metadata_provider
            or os.environ.get("PA_METADATA_PROVIDER")
            or DEFAULT_PROVIDER
        )
        ocr_mdl = ocr_model or os.environ.get("PA_OCR_MODEL") or OCR_MODEL
        meta_mdl = metadata_model or os.environ.get("PA_METADATA_MODEL") or METADATA_MODEL

        # Only require an Anthropic key when a task actually uses Anthropic.
        uses_anthropic = "anthropic" in (ocr_prov, meta_prov)
        if require_anthropic and uses_anthropic and not anthropic_key:
            raise SystemExit("Set ANTHROPIC_API_KEY in the environment.")

        return cls(
            base_url=base,
            paperless_token=token,
            anthropic_api_key=anthropic_key,
            ocr_provider=ocr_prov,
            metadata_provider=meta_prov,
            ocr_model=ocr_mdl,
            metadata_model=meta_mdl,
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_base_url=os.environ.get("OPENAI_BASE_URL", ""),
            ollama_endpoint=os.environ.get(
                "PA_OLLAMA_ENDPOINT", "http://localhost:11434"
            ),
        )


# ===========================================================================
# Layered configuration system (plan §7). New in Phase 3.
# ===========================================================================

# The single mounted volume root (plan §8.1). Everything durable lives here so
# state survives restarts/upgrades. Overridable via PA_DATA_DIR for local dev
# (the Windows dev host is not "/data").
DEFAULT_DATA_DIR = "/data"

# Secret keys that must NEVER appear in the YAML config file (plan §7.1). If any
# of these is present in the YAML we refuse to load, loudly (I6/I7).
SECRET_YAML_KEYS = frozenset(
    {
        "paperless_token",
        "token",
        "anthropic_api_key",
        "openai_api_key",
        "agent_token",
        "pa_agent_token",
        "api_key",
        "secret",
        "enrollment_token",
        "agent_credential",
    }
)


class ConfigError(RuntimeError):
    """Raised when the layered config cannot be resolved (bad YAML, a secret in
    the YAML, an unsupported value). Message says exactly what's wrong (I6)."""


@dataclass
class SpendCaps:
    """Spend governance (I3). Both caps default LOW and non-zero (plan §7.2) so a
    fresh install can't run up a surprise bill."""

    per_run: float = 1.00
    per_period: float = 5.00
    period: str = "monthly"  # accounting bucket label for the period cap


@dataclass
class TaskProvider:
    """Per-task provider/model/endpoint selection (plan §7.3)."""

    provider: str = DEFAULT_PROVIDER
    model: str = ""  # "" -> fall back to the task's default model


@dataclass
class PromptCustomization:
    """Per-task prompt customization (prompt 010), NON-secret config.

    Two levers over the natural-language INSTRUCTION the engine sends, never the
    engine-owned JSON schema:
      * `extra_instructions` — appended to whichever base instruction is in effect.
      * `prompt_override`     — replaces the built-in default instruction; "" (or
                                whitespace) = use the default (i.e. clearing it
                                RESETS to default).
    Both default to "" so an unconfigured install is byte-identical to pre-010.
    """

    extra_instructions: str = ""
    prompt_override: str = ""


@dataclass
class FieldNames:
    """Configurable custom-field NAMES (non-secret). Default to the constants so an
    unconfigured install is byte-identical. These are the names the assistant
    provisions (`pa setup`), checks (`pa doctor`), and resolves at run time — they
    must match what exists in the user's Paperless. The RESOLVER threads these by
    ROLE so every consumer resolves the configured name, not the module constant."""

    score: str = FIELD_SCORE   # ocr_quality
    stage: str = FIELD_STAGE   # ai_stage
    notes: str = FIELD_NOTES   # ai_notes


@dataclass
class StageNames:
    """Configurable ai_stage select-option LABELS (non-secret). Default to the
    constants (byte-identical). The state machine still advances
    triaged -> reocr_done -> metadata_done by ROLE; only the visible labels change."""

    triaged: str = STAGE_TRIAGED
    reocr_done: str = STAGE_REOCR_DONE
    metadata_done: str = STAGE_METADATA_DONE

    def order(self) -> tuple:
        """Role-ordered labels (the STAGE_ORDER equivalent for configured names)."""
        return (self.triaged, self.reocr_done, self.metadata_done)


@dataclass
class HttpSettings:
    """Paperless REST client tunables (non-secret). Normal timeouts + pagination;
    Advanced retry/backoff. All default byte-identically to the hardcoded values."""

    # -- normal (installation-specific: slow servers / large PDFs / big libraries) --
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    download_timeout: float = DEFAULT_DOWNLOAD_TIMEOUT
    post_document_timeout: float = DEFAULT_POST_DOCUMENT_TIMEOUT
    task_poll_timeout: float = DEFAULT_TASK_POLL_TIMEOUT
    task_poll_interval: float = DEFAULT_TASK_POLL_INTERVAL
    page_size: int = DEFAULT_PAGE_SIZE
    # -- advanced (retry/backoff internals) --
    retries: int = DEFAULT_HTTP_RETRIES
    backoff_initial: float = DEFAULT_HTTP_BACKOFF_INITIAL
    backoff_cap: float = DEFAULT_HTTP_BACKOFF_CAP


@dataclass
class MetadataWindow:
    """Metadata content window + output token cap (non-secret, normal tunables).
    Defaults byte-identical to CONTENT_HEAD/CONTENT_TAIL/METADATA_MAX_TOKENS."""

    content_head: int = CONTENT_HEAD
    content_tail: int = CONTENT_TAIL
    max_tokens: int = METADATA_MAX_TOKENS


@dataclass
class GarbageHeuristic:
    """ADVANCED: garbage_score coefficients + gates. Defaults reproduce today's
    EXACT scores. Misconfiguring changes what gets flagged for re-OCR (spend +
    rewrites) — surfaced behind the collapsed Advanced UI with per-field reset."""

    min_length: int = GARBAGE_MIN_LENGTH
    word_ratio_weight: float = GARBAGE_WORD_RATIO_WEIGHT
    plausible_weight: float = GARBAGE_PLAUSIBLE_WEIGHT
    fragment_weight: float = GARBAGE_FRAGMENT_WEIGHT
    fragment_threshold: float = GARBAGE_FRAGMENT_THRESHOLD
    plausible_min_len: int = GARBAGE_PLAUSIBLE_MIN_LEN


@dataclass
class WebhookSettings:
    """Phase 4 on-ingest webhook NUDGE receiver (plan §6.2).

    A small stdlib HTTP listener that runs alongside the scheduled sweep in
    `pa serve`. Paperless' Workflow -> Webhook action POSTs a THIN payload
    (`{doc_url}` placeholder) to the agent's IN-NETWORK address (reached by compose
    service name, e.g. http://paperless-assistant:8765). The nudge carries only a
    document id; the agent PULLS the doc via REST and runs it through the same
    idempotent pipeline as the sweep.

    Trust boundary (plan §8.1): this binds INSIDE the compose network only and
    publishes NO host port. It is NOT an external exposure of the agent.

    `secret` is read from the environment ONLY (PA_WEBHOOK_SECRET), never the YAML
    (like every other secret, §7.1). `enabled` is OFF by default: the scheduled
    sweep is authoritative and the webhook is an opt-in latency optimisation.
    """

    enabled: bool = False
    # Bind host: 0.0.0.0 so Paperless can reach it by service name over the
    # compose network. Still no PUBLISHED host port (no compose `ports:` mapping),
    # so this is not reachable from outside the user's private network.
    host: str = "0.0.0.0"
    port: int = 8765
    # URL path of the single nudge endpoint.
    path: str = "/hooks/paperless"
    # Shared secret (env only). If empty while enabled, the receiver refuses to
    # start rather than accept unauthenticated nudges.
    secret: str = ""
    # Collapse rapid duplicate nudges for the same doc id within this window
    # (seconds). Idempotency already makes duplicates safe; this avoids redundant
    # pulls/work for the common "Paperless fires several events" case.
    debounce_seconds: float = 30.0


@dataclass
class UiSettings:
    """Phase 8 local WEB DASHBOARD (product-architecture §8.4, prompt 009).

    A small stdlib HTTP server + a single self-contained HTML page that lets a
    self-hoster observe + operate the (Mode A / BYO-key) agent from a browser:
    view status/stats/runs/errors, start manual sweeps, and edit the layered-config
    tunables. Unlike the outbound-only agent and the in-network webhook, the UI is
    an EXPLICITLY-PUBLISHED host port (the user maps it) — so it is protected by a
    built-in token.

    `token` is read from the environment ONLY (PA_UI_TOKEN), never the YAML (like
    every other secret, §7.1). `enabled` is OFF by default. If the UI is enabled
    with NO token, the server refuses to start (fail closed) — mirror the webhook.
    """

    enabled: bool = False
    # Bind host: 0.0.0.0 so the PUBLISHED host port reaches the browser. Auth (the
    # token) protects it — this is the one deliberately-published port.
    host: str = "0.0.0.0"
    port: int = 8770
    # Shared secret (env ONLY, never YAML, never logged). Empty while enabled ->
    # the server refuses to start rather than serve an unauthenticated dashboard.
    token: str = ""


@dataclass
class HostedSettings:
    """Phase 5 HOSTED mode (Mode B, plan/connectivity-design.md §2.3, §3).

    The agent dials OUT to a control plane and PULLS work (long-poll); the control
    plane never dials in. Binds NO inbound listener, publishes NO host port.

    Secrets (`enrollment_token` -> PA_ENROLLMENT_TOKEN, and the long-lived agent
    credential) come from env / `/data` ONLY, never the YAML (§4.1, §7.1). The
    enrollment token is one-time; the agent credential is persisted under /data by
    the pull-loop, not carried here.
    """

    # `control_plane_url` is the stable public endpoint the agent dials OUT to
    # (https:// in production). Non-secret, so it MAY come from YAML/env.
    control_plane_url: str = ""
    # One-time enrollment token, exchanged on first start for the agent credential.
    # Env ONLY (PA_ENROLLMENT_TOKEN); never YAML, never logged.
    enrollment_token: str = ""
    # Long-poll + heartbeat cadence, and reconnect backoff bounds (seconds).
    heartbeat_interval_seconds: int = 60
    reconnect_backoff_min: float = 1.0
    reconnect_backoff_max: float = 60.0
    # Phase 6: hosted INFERENCE. When True (and no local AI key is set), the agent
    # routes AI tasks through the control-plane inference proxy (HostedProvider)
    # instead of a local provider — the subscriber's no-local-key path. Non-secret
    # toggle (PA_HOSTED_INFERENCE); the vendor's model key stays SERVER-SIDE and is
    # never present agent-side. Default OFF so BYO/local stays the zero-egress floor.
    inference_enabled: bool = False
    # Optional model hints the proxy may honor (non-secret). The agent holds no key.
    inference_ocr_model: str = ""
    inference_metadata_model: str = ""


@dataclass
class Settings:
    """The resolved, layered runtime settings (plan §7).

    Non-secret tunables carry their §7.2 safe defaults. Secrets are separate
    fields populated ONLY from env/secret files (never YAML). `to_config()`
    projects these onto the Phase 1 `Config` the engine already consumes.
    """

    # --- connection + secrets (env/secret files only) --------------------
    base_url: str = "http://localhost:8000"
    # The base URL the agent uses for API calls is in-stack/LAN-internal (e.g.
    # http://webserver:8000). `paperless_public_url` is the EXTERNAL URL a browser
    # uses (e.g. https://paperless.example) and is used ONLY to build user-facing
    # links in the UI (the Activity doc links). Empty -> fall back to base_url.
    paperless_public_url: str = ""
    paperless_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    ollama_endpoint: str = "http://localhost:11434"
    agent_token: str = ""  # hosted mode (Phase 5); read but unused here

    # --- global posture (plan §7.2) --------------------------------------
    mode: str = "conservative"  # conservative / review-first is the default

    # --- which stages are enabled (plan §7.2) ----------------------------
    # triage + metadata ENABLED; auto re-OCR DISABLED by default (opt-in per
    # library; re-OCR spends money and rewrites documents).
    triage_enabled: bool = True
    metadata_enabled: bool = True
    reocr_enabled: bool = False

    # --- thresholds (plan §7.2/§7.3) -------------------------------------
    triage_threshold: float = DEFAULT_TRIAGE_THRESHOLD  # 0.55
    garbage_threshold: float = GARBAGE_THRESH  # 0.55 metadata skip

    # --- concurrency (plan §7.2: 3-4, <= DB pool) ------------------------
    workers: int = 3

    # --- spend caps (plan §7.2: low, non-zero) ---------------------------
    spend: SpendCaps = field(default_factory=SpendCaps)

    # --- per-task providers/models (plan §7.2: metadata cheap/fast,
    # vision-OCR strong; both Anthropic by default) ----------------------
    metadata_task: TaskProvider = field(
        default_factory=lambda: TaskProvider(provider=DEFAULT_PROVIDER, model=METADATA_MODEL)
    )
    ocr_task: TaskProvider = field(
        default_factory=lambda: TaskProvider(provider=DEFAULT_PROVIDER, model=OCR_MODEL)
    )
    max_ocr_tokens: int = MAX_OCR_TOKENS

    # --- per-task prompt customization (prompt 010, non-secret) -----------
    # Empty => byte-identical default behavior. `prompt_override` replaces the
    # built-in instruction; `extra_instructions` is appended.
    metadata_prompts: PromptCustomization = field(default_factory=PromptCustomization)
    ocr_prompts: PromptCustomization = field(default_factory=PromptCustomization)

    # --- taxonomy policy (plan §7.2: reuse-first; I5) --------------------
    taxonomy_policy: str = "reuse-first"

    # --- review-gate tag names (plan §7.3) -------------------------------
    superseded_tag: str = SUPERSEDED_TAG
    new_taxonomy_tag: str = NEW_TAXONOMY_TAG

    # --- configurable custom-field / stage-option NAMES (prompt 011) ------
    # Default to the module constants so an unconfigured install is byte-identical.
    field_names: FieldNames = field(default_factory=FieldNames)
    stage_names: StageNames = field(default_factory=StageNames)

    # --- created-taxonomy tag colors (prompt 011, minor) -----------------
    superseded_tag_color: str = "#a0a0a0"
    new_taxonomy_tag_color: str = "#f59e0b"

    # --- HTTP client tunables (timeouts / pagination / Advanced retry) ---
    http: HttpSettings = field(default_factory=HttpSettings)

    # --- metadata content window + output token cap ----------------------
    metadata_window: MetadataWindow = field(default_factory=MetadataWindow)

    # --- which ai_stage ROLES are metadata-eligible (prompt 011) ---------
    # Roles (not labels): "" == "no stage yet"; default reproduces
    # ELIGIBLE_STAGES = {None, "", "triaged"}. Add "reocr_done" to also re-run
    # metadata on re-OCR'd docs.
    metadata_eligible_roles: list = field(default_factory=lambda: ["", "triaged"])

    # --- ADVANCED: garbage_score heuristic coefficients + gates ----------
    garbage_heuristic: GarbageHeuristic = field(default_factory=GarbageHeuristic)

    # --- on-ingest webhook nudge (Phase 4, plan §6.2) --------------------
    webhook: WebhookSettings = field(default_factory=WebhookSettings)

    # --- local web dashboard (Phase 8, prompt 009) -----------------------
    ui: UiSettings = field(default_factory=UiSettings)

    # --- hosted mode (Phase 5, plan/connectivity-design.md §3) -----------
    hosted: HostedSettings = field(default_factory=HostedSettings)

    # --- schedule + run behaviour (plan §7.3) ----------------------------
    schedule_interval_seconds: int = 3600  # `pa serve` sweep interval
    dry_run: bool | None = None  # None -> first-run auto dry-run (I7); see sweep
    force: bool = False
    limit: int = 0  # 0 = all eligible

    # --- storage (plan §8.1 /data layout) --------------------------------
    data_dir: str = DEFAULT_DATA_DIR
    snapshot_retention_days: int = 0  # 0 = keep forever (delete never automated)

    # --- per-document activity/audit log (prompt 013, non-secret) --------
    # The searchable field-level before->after log under /data/activity.db. ON by
    # default (observational, best-effort; a store failure never fails a doc/run).
    # `activity_retention_days` auto-purges rows older than the cutoff; 0 = keep
    # forever (the byte-identical-to-snapshot-retention default is 90 here).
    activity_enabled: bool = True
    activity_retention_days: int = 90  # 0 = keep forever

    # --- delete originals: NEVER automated (plan §7.2, I4) ---------------
    delete_originals: bool = False  # read-only sentinel; setting it is refused

    # -- projections -------------------------------------------------------
    def to_config(self) -> "Config":
        """Project onto the Phase 1 `Config` the engine consumes today."""
        return Config(
            base_url=self.base_url.rstrip("/"),
            paperless_token=self.paperless_token,
            anthropic_api_key=self.anthropic_api_key,
            ocr_provider=self.ocr_task.provider,
            metadata_provider=self.metadata_task.provider,
            ocr_model=self.ocr_task.model or OCR_MODEL,
            metadata_model=self.metadata_task.model or METADATA_MODEL,
            max_ocr_tokens=self.max_ocr_tokens,
            metadata_max_tokens=self.metadata_window.max_tokens,
            openai_api_key=self.openai_api_key,
            openai_base_url=self.openai_base_url,
            ollama_endpoint=self.ollama_endpoint,
        )

    def prompt_customization(self, task: str) -> "PromptCustomization":
        """Return the PromptCustomization for a task ("metadata"/"ocr")."""
        if task == "metadata":
            return self.metadata_prompts
        if task == "ocr":
            return self.ocr_prompts
        raise ValueError(f"unknown prompt task '{task}'")

    def resolved_instruction(self, task: str) -> str:
        """The EFFECTIVE instruction for a task (default -> override -> +extra),
        composed by the single `prompts.resolve_instruction` helper. Returns the
        built-in default byte-identically when nothing is customized."""
        from . import prompts as _prompts

        pc = self.prompt_customization(task)
        return _prompts.resolve_instruction(
            _prompts.default_instruction(task),
            prompt_override=pc.prompt_override,
            extra_instructions=pc.extra_instructions,
        )

    def resolved_instruction_or_none(self, task: str) -> str | None:
        """Like `resolved_instruction`, but returns None when NOTHING is customized
        (neither override nor extra). Callers pass None to the engine so the
        adapter/pipeline uses its own built-in default constant — preserving the
        byte-identical path and avoiding threading a redundant string."""
        pc = self.prompt_customization(task)
        if not (str(pc.prompt_override).strip() or str(pc.extra_instructions).strip()):
            return None
        return self.resolved_instruction(task)

    def data_path(self, *parts) -> pathlib.Path:
        """A path under the /data layout (Linux-correct in the container; a local
        dir on the dev host)."""
        return pathlib.Path(self.data_dir, *parts)

    def uses_anthropic(self) -> bool:
        return "anthropic" in (self.ocr_task.provider, self.metadata_task.provider)

    def hosted_mode(self) -> bool:
        """True when the agent should run the outbound hosted pull-loop (Mode B).
        Triggered by PA_MODE=hosted."""
        return str(self.mode).strip().lower() == "hosted"

    def has_local_ai_key(self) -> bool:
        """True if the agent holds ANY local AI provider key/endpoint that would let
        it do inference itself. Ollama's local endpoint is treated as a BYO/local
        path (zero egress) — its presence keeps hosted inference off by default.

        Used to gate hosted inference: hosted inference is the NO-LOCAL-KEY path, so
        if the subscriber configured a real key we keep BYO to preserve the
        zero-egress floor unless they explicitly opted into hosted inference."""
        return bool(self.anthropic_api_key or self.openai_api_key)

    def hosted_inference_active(self) -> bool:
        """True when AI tasks should route through the control-plane inference proxy
        (Phase 6). Requires: hosted mode, the inference toggle ON, and NO local AI
        key (the subscriber no-key path). BYO-key/local remains the default
        zero-egress floor whenever a local key is present or the toggle is off."""
        return (
            self.hosted_mode()
            and self.hosted.inference_enabled
            and not self.has_local_ai_key()
        )

    def enabled_stages(self) -> list[str]:
        out = []
        if self.triage_enabled:
            out.append("triage")
        if self.reocr_enabled:
            out.append("reocr")
        if self.metadata_enabled:
            out.append("metadata")
        return out

    # -- role/name resolution helpers (prompt 011) -------------------------
    def stage_label_for_role(self, role: str) -> str:
        """Configured ai_stage LABEL for a state-machine ROLE
        (triaged/reocr_done/metadata_done)."""
        return {
            STAGE_TRIAGED: self.stage_names.triaged,
            STAGE_REOCR_DONE: self.stage_names.reocr_done,
            STAGE_METADATA_DONE: self.stage_names.metadata_done,
        }[role]

    def eligible_stage_labels(self) -> set:
        """The set of ai_stage LABELS that are metadata-eligible, translated from
        the configured `metadata_eligible_roles` (roles -> configured labels). ""
        stays "" (no stage yet); role labels map through stage_names."""
        role_to_label = {
            "": "",
            STAGE_TRIAGED: self.stage_names.triaged,
            STAGE_REOCR_DONE: self.stage_names.reocr_done,
            STAGE_METADATA_DONE: self.stage_names.metadata_done,
        }
        out = set()
        for role in self.metadata_eligible_roles:
            out.add(role_to_label.get(role, role))
        # Preserve the historical {None, "", ...} shape: None and "" are equivalent
        # "no stage yet"; include None so a doc with a null stage value matches.
        if "" in out:
            out.add(None)
        return out

    def public_base_url(self) -> str:
        """The base URL for USER-FACING links (browser). Falls back to the
        in-stack `base_url` when no external URL is configured."""
        return (self.paperless_public_url or self.base_url).rstrip("/")

    def to_public_dict(self) -> dict:
        """Resolved config WITHOUT secrets, for `pa doctor` / run reports."""
        return {
            "base_url": self.base_url,
            "paperless_public_url": self.paperless_public_url,
            "mode": self.mode,
            "stages_enabled": self.enabled_stages(),
            "reocr_enabled": self.reocr_enabled,
            "triage_threshold": self.triage_threshold,
            "garbage_threshold": self.garbage_threshold,
            "workers": self.workers,
            "limit": self.limit,
            "spend": {
                "per_run_cap": self.spend.per_run,
                "per_period_cap": self.spend.per_period,
                "period": self.spend.period,
            },
            "metadata": {
                "provider": self.metadata_task.provider,
                "model": self.metadata_task.model or METADATA_MODEL,
                "extra_instructions": self.metadata_prompts.extra_instructions,
                "prompt_override": self.metadata_prompts.prompt_override,
            },
            "ocr": {
                "provider": self.ocr_task.provider,
                "model": self.ocr_task.model or OCR_MODEL,
                "extra_instructions": self.ocr_prompts.extra_instructions,
                "prompt_override": self.ocr_prompts.prompt_override,
            },
            "taxonomy_policy": self.taxonomy_policy,
            "review_tags": {
                "superseded": self.superseded_tag,
                "new_taxonomy": self.new_taxonomy_tag,
                "superseded_color": self.superseded_tag_color,
                "new_taxonomy_color": self.new_taxonomy_tag_color,
            },
            # --- configurable field/stage names (prompt 011) -------------
            "field_names": {
                "score": self.field_names.score,
                "stage": self.field_names.stage,
                "notes": self.field_names.notes,
            },
            "stage_names": {
                "triaged": self.stage_names.triaged,
                "reocr_done": self.stage_names.reocr_done,
                "metadata_done": self.stage_names.metadata_done,
            },
            # --- HTTP timeouts / pagination (normal) + retry (advanced) --
            "http": {
                "request_timeout": self.http.request_timeout,
                "download_timeout": self.http.download_timeout,
                "post_document_timeout": self.http.post_document_timeout,
                "task_poll_timeout": self.http.task_poll_timeout,
                "task_poll_interval": self.http.task_poll_interval,
                "page_size": self.http.page_size,
                "retries": self.http.retries,
                "backoff_initial": self.http.backoff_initial,
                "backoff_cap": self.http.backoff_cap,
            },
            # --- metadata content window + token cap (normal) ------------
            "metadata_window": {
                "content_head": self.metadata_window.content_head,
                "content_tail": self.metadata_window.content_tail,
                "max_tokens": self.metadata_window.max_tokens,
            },
            "max_ocr_tokens": self.max_ocr_tokens,
            "metadata_eligible_roles": list(self.metadata_eligible_roles),
            # --- ADVANCED: garbage_score heuristic -----------------------
            "garbage_heuristic": {
                "min_length": self.garbage_heuristic.min_length,
                "word_ratio_weight": self.garbage_heuristic.word_ratio_weight,
                "plausible_weight": self.garbage_heuristic.plausible_weight,
                "fragment_weight": self.garbage_heuristic.fragment_weight,
                "fragment_threshold": self.garbage_heuristic.fragment_threshold,
                "plausible_min_len": self.garbage_heuristic.plausible_min_len,
            },
            "schedule_interval_seconds": self.schedule_interval_seconds,
            "dry_run": self.dry_run,
            "data_dir": self.data_dir,
            "snapshot_retention_days": self.snapshot_retention_days,
            "activity_enabled": self.activity_enabled,
            "activity_retention_days": self.activity_retention_days,
            "delete_originals": self.delete_originals,
            "webhook": {
                # NB: never expose the secret; only whether one is configured.
                "enabled": self.webhook.enabled,
                "host": self.webhook.host,
                "port": self.webhook.port,
                "path": self.webhook.path,
                "secret_configured": bool(self.webhook.secret),
                "debounce_seconds": self.webhook.debounce_seconds,
            },
            "ui": {
                # NB: never expose the UI token; only whether one is configured.
                "enabled": self.ui.enabled,
                "host": self.ui.host,
                "port": self.ui.port,
                "token_configured": bool(self.ui.token),
            },
            "hosted": {
                # NB: never expose the enrollment token / agent credential; only
                # whether hosted mode is on and where the agent dials OUT to.
                "enabled": self.hosted_mode(),
                "control_plane_url": self.hosted.control_plane_url,
                "enrollment_token_configured": bool(self.hosted.enrollment_token),
                "heartbeat_interval_seconds": self.hosted.heartbeat_interval_seconds,
                # Phase 6: whether AI tasks route through the vendor inference proxy
                # (the no-local-key subscriber path). Active only in hosted mode with
                # the toggle on and no local AI key (BYO/local otherwise).
                "inference_enabled": self.hosted.inference_enabled,
                "inference_active": self.hosted_inference_active(),
            },
        }


# Map each EDITABLE non-secret tunable (the config-endpoint surface) to the env
# var that overrides it. When that env var is set, env beats YAML (§7.1), so the
# UI must show the field as read-only/locked ("I saved it but it didn't change").
# Keys here are the flat field names the web-config endpoint uses.
ENV_OVERRIDE_KEYS = {
    "paperless_public_url": "PAPERLESS_PUBLIC_URL",
    "mode": "PA_MODE",
    "triage_enabled": "PA_TRIAGE_ENABLED",
    "metadata_enabled": "PA_METADATA_ENABLED",
    "reocr_enabled": "PA_REOCR_ENABLED",
    "triage_threshold": "PA_TRIAGE_THRESHOLD",
    "workers": "PA_WORKERS",
    "schedule_interval_seconds": "PA_SCHEDULE_INTERVAL",
    "limit": "PA_LIMIT",
    "dry_run": "PA_DRY_RUN",
    "data_dir": "PA_DATA_DIR",
    "activity_enabled": "PA_ACTIVITY_ENABLED",
    "activity_retention_days": "PA_ACTIVITY_RETENTION_DAYS",
    "spend_per_run": "PA_SPEND_PER_RUN",
    "spend_per_period": "PA_SPEND_PER_PERIOD",
    "ocr_provider": "PA_OCR_PROVIDER",
    "ocr_model": "PA_OCR_MODEL",
    "metadata_provider": "PA_METADATA_PROVIDER",
    "metadata_model": "PA_METADATA_MODEL",
    # Prompt customization (prompt 010): env locks the corresponding UI field.
    "metadata_extra_instructions": "PA_METADATA_EXTRA_INSTRUCTIONS",
    "metadata_prompt_override": "PA_METADATA_PROMPT_OVERRIDE",
    "ocr_extra_instructions": "PA_OCR_EXTRA_INSTRUCTIONS",
    "ocr_prompt_override": "PA_OCR_PROMPT_OVERRIDE",
    "webhook_enabled": "PA_WEBHOOK_ENABLED",
    "ui_enabled": "PA_UI_ENABLED",
    "ui_host": "PA_UI_HOST",
    "ui_port": "PA_UI_PORT",
    # --- prompt 011: configurable names / HTTP / metadata window ----------
    "field_score": "PA_FIELD_SCORE",
    "field_stage": "PA_FIELD_STAGE",
    "field_notes": "PA_FIELD_NOTES",
    "stage_triaged": "PA_STAGE_TRIAGED",
    "stage_reocr_done": "PA_STAGE_REOCR_DONE",
    "stage_metadata_done": "PA_STAGE_METADATA_DONE",
    "http_request_timeout": "PA_HTTP_REQUEST_TIMEOUT",
    "http_download_timeout": "PA_HTTP_DOWNLOAD_TIMEOUT",
    "http_post_document_timeout": "PA_HTTP_POST_TIMEOUT",
    "http_task_poll_timeout": "PA_HTTP_TASK_POLL_TIMEOUT",
    "http_task_poll_interval": "PA_HTTP_TASK_POLL_INTERVAL",
    "http_page_size": "PA_HTTP_PAGE_SIZE",
    "http_retries": "PA_HTTP_RETRIES",
    "http_backoff_initial": "PA_HTTP_BACKOFF_INITIAL",
    "http_backoff_cap": "PA_HTTP_BACKOFF_CAP",
    "metadata_content_head": "PA_METADATA_CONTENT_HEAD",
    "metadata_content_tail": "PA_METADATA_CONTENT_TAIL",
    "metadata_max_tokens": "PA_METADATA_MAX_TOKENS",
}


def env_overridden_fields(environ=None) -> dict:
    """Return {field_name: True} for each editable tunable whose controlling env
    var is currently set (so env beats any YAML write — the UI locks these). Read
    ONLY the env-var NAMES here; the VALUES (some secret-adjacent) are never
    surfaced. Purely a presence check."""
    env = os.environ if environ is None else environ
    return {
        field: (env.get(var) not in (None, ""))
        for field, var in ENV_OVERRIDE_KEYS.items()
    }


def _load_yaml_file(path: pathlib.Path) -> dict:
    """Read a YAML config file into a dict; refuse any secret keys (plan §7.1)."""
    import yaml

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config file {path} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"config file {path} must be a YAML mapping at the top level")
    _reject_secrets(raw, path)
    return raw


def _reject_secrets(node, path, _trail=""):
    """Recursively refuse any secret-looking key anywhere in the YAML (§7.1)."""
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() in SECRET_YAML_KEYS:
                where = f"{_trail}{k}" if _trail else k
                raise ConfigError(
                    f"secret key '{where}' found in config file {path}. "
                    f"Secrets (Paperless token, AI keys, agent token) must come "
                    f"from environment variables / secret files, NEVER the YAML "
                    f"config (plan §7.1). Remove it and set it via the environment."
                )
            _reject_secrets(v, path, f"{_trail}{k}.")
    elif isinstance(node, list):
        for item in node:
            _reject_secrets(item, path, _trail)


def _apply_yaml(settings: "Settings", raw: dict) -> None:
    """Apply a validated (secret-free) YAML mapping onto `settings` in place.

    Only known, non-secret tunables are honored; unknown keys are ignored with a
    tolerant posture (forward-compat). Delete-originals can never be turned on
    from config (plan §7.2, I4)."""
    def _f(key, cast=None):
        if key in raw and raw[key] is not None:
            return cast(raw[key]) if cast else raw[key]
        return _MISSING

    simple = {
        "paperless_public_url": str,
        "mode": str, "triage_enabled": bool, "metadata_enabled": bool,
        "reocr_enabled": bool, "triage_threshold": float, "garbage_threshold": float,
        "workers": int, "taxonomy_policy": str, "superseded_tag": str,
        "new_taxonomy_tag": str, "schedule_interval_seconds": int, "force": bool,
        "limit": int, "data_dir": str, "snapshot_retention_days": int,
        "activity_enabled": bool, "activity_retention_days": int,
        "max_ocr_tokens": int,
        # prompt 011 scalars
        "superseded_tag_color": str, "new_taxonomy_tag_color": str,
    }
    for key, cast in simple.items():
        val = _f(key, cast)
        if val is not _MISSING:
            setattr(settings, key, val)

    if "dry_run" in raw and raw["dry_run"] is not None:
        settings.dry_run = bool(raw["dry_run"])

    sp = raw.get("spend") or {}
    if isinstance(sp, dict):
        if sp.get("per_run") is not None:
            settings.spend.per_run = float(sp["per_run"])
        if sp.get("per_period") is not None:
            settings.spend.per_period = float(sp["per_period"])
        if sp.get("period") is not None:
            settings.spend.period = str(sp["period"])

    for task_key, task, prompts in (
        ("metadata", settings.metadata_task, settings.metadata_prompts),
        ("ocr", settings.ocr_task, settings.ocr_prompts),
    ):
        block = raw.get(task_key) or {}
        if isinstance(block, dict):
            if block.get("provider") is not None:
                task.provider = str(block["provider"])
            if block.get("model") is not None:
                task.model = str(block["model"])
            # Prompt customization (non-secret): multi-line strings via YAML block
            # scalars. Empty/omitted = keep the default (byte-identical).
            if block.get("extra_instructions") is not None:
                prompts.extra_instructions = str(block["extra_instructions"])
            if block.get("prompt_override") is not None:
                prompts.prompt_override = str(block["prompt_override"])

    # webhook nudge (non-secret tunables only; the secret comes from env, §7.1).
    wh = raw.get("webhook") or {}
    if isinstance(wh, dict):
        if wh.get("enabled") is not None:
            settings.webhook.enabled = bool(wh["enabled"])
        if wh.get("host") is not None:
            settings.webhook.host = str(wh["host"])
        if wh.get("port") is not None:
            settings.webhook.port = int(wh["port"])
        if wh.get("path") is not None:
            settings.webhook.path = str(wh["path"])
        if wh.get("debounce_seconds") is not None:
            settings.webhook.debounce_seconds = float(wh["debounce_seconds"])

    # web dashboard (Phase 8): only NON-secret tunables from YAML. The UI token is
    # a SECRET (env only, §7.1) — never read here.
    ui = raw.get("ui") or {}
    if isinstance(ui, dict):
        if ui.get("enabled") is not None:
            settings.ui.enabled = bool(ui["enabled"])
        if ui.get("host") is not None:
            settings.ui.host = str(ui["host"])
        if ui.get("port") is not None:
            settings.ui.port = int(ui["port"])

    # hosted mode (Phase 5): only NON-secret tunables from YAML. The enrollment
    # token / agent credential are secrets (env / /data only, §4.1) — never here.
    hosted = raw.get("hosted") or {}
    if isinstance(hosted, dict):
        if hosted.get("control_plane_url") is not None:
            settings.hosted.control_plane_url = str(hosted["control_plane_url"]).rstrip("/")
        if hosted.get("heartbeat_interval_seconds") is not None:
            settings.hosted.heartbeat_interval_seconds = int(hosted["heartbeat_interval_seconds"])
        # Phase 6 hosted-inference toggle + non-secret model hints (never a key).
        if hosted.get("inference_enabled") is not None:
            settings.hosted.inference_enabled = bool(hosted["inference_enabled"])
        if hosted.get("inference_ocr_model") is not None:
            settings.hosted.inference_ocr_model = str(hosted["inference_ocr_model"])
        if hosted.get("inference_metadata_model") is not None:
            settings.hosted.inference_metadata_model = str(hosted["inference_metadata_model"])

    # --- prompt 011: configurable field/stage NAMES (non-secret) ----------
    fn = raw.get("field_names") or {}
    if isinstance(fn, dict):
        for k in ("score", "stage", "notes"):
            if fn.get(k) is not None:
                setattr(settings.field_names, k, str(fn[k]))
    sn = raw.get("stage_names") or {}
    if isinstance(sn, dict):
        for k in ("triaged", "reocr_done", "metadata_done"):
            if sn.get(k) is not None:
                setattr(settings.stage_names, k, str(sn[k]))

    # --- prompt 011: HTTP timeouts / pagination + Advanced retry ----------
    http = raw.get("http") or {}
    if isinstance(http, dict):
        for k, cast in (
            ("request_timeout", float), ("download_timeout", float),
            ("post_document_timeout", float), ("task_poll_timeout", float),
            ("task_poll_interval", float), ("page_size", int),
            ("retries", int), ("backoff_initial", float), ("backoff_cap", float),
        ):
            if http.get(k) is not None:
                setattr(settings.http, k, cast(http[k]))

    # --- prompt 011: metadata content window + token cap ------------------
    mw = raw.get("metadata_window") or {}
    if isinstance(mw, dict):
        for k in ("content_head", "content_tail", "max_tokens"):
            if mw.get(k) is not None:
                setattr(settings.metadata_window, k, int(mw[k]))

    # --- prompt 011: which stage ROLES are metadata-eligible --------------
    if raw.get("metadata_eligible_roles") is not None:
        roles = raw["metadata_eligible_roles"]
        if isinstance(roles, (list, tuple)):
            settings.metadata_eligible_roles = ["" if r is None else str(r) for r in roles]

    # --- prompt 011 ADVANCED: garbage_score heuristic coefficients --------
    gh = raw.get("garbage_heuristic") or {}
    if isinstance(gh, dict):
        for k, cast in (
            ("min_length", int), ("word_ratio_weight", float),
            ("plausible_weight", float), ("fragment_weight", float),
            ("fragment_threshold", float), ("plausible_min_len", int),
        ):
            if gh.get(k) is not None:
                setattr(settings.garbage_heuristic, k, cast(gh[k]))

    # delete_originals is a hard NO from config (plan §7.2, I4).
    if raw.get("delete_originals"):
        raise ConfigError(
            "delete_originals cannot be enabled via config - deletion of "
            "originals is NEVER automated (plan §7.2, I4). The 'superseded' "
            "review set is provided instead; delete manually in the Paperless UI."
        )


_MISSING = object()


def _apply_env(settings: "Settings") -> None:
    """Overlay environment variables (higher precedence than YAML). Secrets are
    read here; non-secret env overrides mirror the tunable surface."""
    e = os.environ.get

    # Secrets + connection (env/secret files ONLY).
    if e("PAPERLESS_URL"):
        settings.base_url = e("PAPERLESS_URL").rstrip("/")
    if e("PAPERLESS_PUBLIC_URL"):
        settings.paperless_public_url = e("PAPERLESS_PUBLIC_URL").rstrip("/")
    if e("PAPERLESS_TOKEN"):
        settings.paperless_token = e("PAPERLESS_TOKEN")
    if e("ANTHROPIC_API_KEY"):
        settings.anthropic_api_key = e("ANTHROPIC_API_KEY")
    if e("OPENAI_API_KEY"):
        settings.openai_api_key = e("OPENAI_API_KEY")
    if e("OPENAI_BASE_URL"):
        settings.openai_base_url = e("OPENAI_BASE_URL")
    if e("PA_OLLAMA_ENDPOINT"):
        settings.ollama_endpoint = e("PA_OLLAMA_ENDPOINT")
    if e("PA_AGENT_TOKEN"):
        settings.agent_token = e("PA_AGENT_TOKEN")
    # Webhook shared secret: env ONLY (never YAML), like every other secret.
    if e("PA_WEBHOOK_SECRET"):
        settings.webhook.secret = e("PA_WEBHOOK_SECRET")
    # Web-dashboard token: env ONLY (never YAML), like every other secret.
    if e("PA_UI_TOKEN"):
        settings.ui.token = e("PA_UI_TOKEN")
    # Hosted-mode enrollment token: env ONLY (never YAML), one-time (Phase 5, §4.1).
    if e("PA_ENROLLMENT_TOKEN"):
        settings.hosted.enrollment_token = e("PA_ENROLLMENT_TOKEN")

    # Non-secret env overrides (plan §7.1 env > YAML).
    _env_bool(settings, "PA_TRIAGE_ENABLED", "triage_enabled")
    _env_bool(settings, "PA_METADATA_ENABLED", "metadata_enabled")
    _env_bool(settings, "PA_REOCR_ENABLED", "reocr_enabled")
    _env_num(settings, "PA_TRIAGE_THRESHOLD", "triage_threshold", float)
    _env_num(settings, "PA_WORKERS", "workers", int)
    _env_num(settings, "PA_SCHEDULE_INTERVAL", "schedule_interval_seconds", int)
    _env_num(settings, "PA_LIMIT", "limit", int)
    if e("PA_MODE"):
        settings.mode = e("PA_MODE")
    if e("PA_DATA_DIR"):
        settings.data_dir = e("PA_DATA_DIR")
    if e("PA_DRY_RUN") is not None:
        settings.dry_run = _truthy(e("PA_DRY_RUN"))
    _env_bool(settings, "PA_ACTIVITY_ENABLED", "activity_enabled")
    _env_num(settings, "PA_ACTIVITY_RETENTION_DAYS", "activity_retention_days", int)
    if e("PA_SPEND_PER_RUN"):
        settings.spend.per_run = float(e("PA_SPEND_PER_RUN"))
    if e("PA_SPEND_PER_PERIOD"):
        settings.spend.per_period = float(e("PA_SPEND_PER_PERIOD"))

    # Webhook non-secret tunables (env > YAML).
    if e("PA_WEBHOOK_ENABLED") not in (None, ""):
        settings.webhook.enabled = _truthy(e("PA_WEBHOOK_ENABLED"))
    if e("PA_WEBHOOK_HOST"):
        settings.webhook.host = e("PA_WEBHOOK_HOST")
    if e("PA_WEBHOOK_PORT"):
        settings.webhook.port = int(e("PA_WEBHOOK_PORT"))
    if e("PA_WEBHOOK_PATH"):
        settings.webhook.path = e("PA_WEBHOOK_PATH")
    if e("PA_WEBHOOK_DEBOUNCE"):
        settings.webhook.debounce_seconds = float(e("PA_WEBHOOK_DEBOUNCE"))

    # Web-dashboard non-secret tunables (env > YAML). The token is a SECRET
    # handled above (PA_UI_TOKEN).
    if e("PA_UI_ENABLED") not in (None, ""):
        settings.ui.enabled = _truthy(e("PA_UI_ENABLED"))
    if e("PA_UI_HOST"):
        settings.ui.host = e("PA_UI_HOST")
    if e("PA_UI_PORT"):
        settings.ui.port = int(e("PA_UI_PORT"))

    # Hosted mode (Phase 5) non-secret tunables (env > YAML). The enrollment
    # token is a SECRET handled above (PA_ENROLLMENT_TOKEN); the control-plane URL
    # is non-secret.
    if e("PA_CONTROL_PLANE_URL"):
        settings.hosted.control_plane_url = e("PA_CONTROL_PLANE_URL").rstrip("/")
    if e("PA_HEARTBEAT_INTERVAL"):
        settings.hosted.heartbeat_interval_seconds = int(e("PA_HEARTBEAT_INTERVAL"))
    # Phase 6 hosted-inference toggle + non-secret model hints (env > YAML). The
    # vendor model key is NEVER read agent-side — it lives on the control plane.
    if e("PA_HOSTED_INFERENCE") not in (None, ""):
        settings.hosted.inference_enabled = _truthy(e("PA_HOSTED_INFERENCE"))
    if e("PA_HOSTED_OCR_MODEL"):
        settings.hosted.inference_ocr_model = e("PA_HOSTED_OCR_MODEL")
    if e("PA_HOSTED_METADATA_MODEL"):
        settings.hosted.inference_metadata_model = e("PA_HOSTED_METADATA_MODEL")

    # Per-task provider/model (mirror the Phase 2 env names).
    if e("PA_OCR_PROVIDER"):
        settings.ocr_task.provider = e("PA_OCR_PROVIDER")
    if e("PA_METADATA_PROVIDER"):
        settings.metadata_task.provider = e("PA_METADATA_PROVIDER")
    if e("PA_OCR_MODEL"):
        settings.ocr_task.model = e("PA_OCR_MODEL")
    if e("PA_METADATA_MODEL"):
        settings.metadata_task.model = e("PA_METADATA_MODEL")

    # --- prompt 011: configurable field/stage NAMES (non-secret, env > YAML) --
    if e("PA_FIELD_SCORE"):
        settings.field_names.score = e("PA_FIELD_SCORE")
    if e("PA_FIELD_STAGE"):
        settings.field_names.stage = e("PA_FIELD_STAGE")
    if e("PA_FIELD_NOTES"):
        settings.field_names.notes = e("PA_FIELD_NOTES")
    if e("PA_STAGE_TRIAGED"):
        settings.stage_names.triaged = e("PA_STAGE_TRIAGED")
    if e("PA_STAGE_REOCR_DONE"):
        settings.stage_names.reocr_done = e("PA_STAGE_REOCR_DONE")
    if e("PA_STAGE_METADATA_DONE"):
        settings.stage_names.metadata_done = e("PA_STAGE_METADATA_DONE")

    # --- prompt 011: HTTP timeouts / pagination (env > YAML) --------------
    _env_num(settings.http, "PA_HTTP_REQUEST_TIMEOUT", "request_timeout", float)
    _env_num(settings.http, "PA_HTTP_DOWNLOAD_TIMEOUT", "download_timeout", float)
    _env_num(settings.http, "PA_HTTP_POST_TIMEOUT", "post_document_timeout", float)
    _env_num(settings.http, "PA_HTTP_TASK_POLL_TIMEOUT", "task_poll_timeout", float)
    _env_num(settings.http, "PA_HTTP_TASK_POLL_INTERVAL", "task_poll_interval", float)
    _env_num(settings.http, "PA_HTTP_PAGE_SIZE", "page_size", int)
    _env_num(settings.http, "PA_HTTP_RETRIES", "retries", int)
    _env_num(settings.http, "PA_HTTP_BACKOFF_INITIAL", "backoff_initial", float)
    _env_num(settings.http, "PA_HTTP_BACKOFF_CAP", "backoff_cap", float)

    # --- prompt 011: metadata content window + token cap (env > YAML) -----
    _env_num(settings.metadata_window, "PA_METADATA_CONTENT_HEAD", "content_head", int)
    _env_num(settings.metadata_window, "PA_METADATA_CONTENT_TAIL", "content_tail", int)
    _env_num(settings.metadata_window, "PA_METADATA_MAX_TOKENS", "max_tokens", int)

    # Per-task prompt customization (prompt 010; non-secret, env > YAML). Multi-line
    # values are supported (env vars may embed newlines).
    if e("PA_METADATA_EXTRA_INSTRUCTIONS") is not None:
        settings.metadata_prompts.extra_instructions = e("PA_METADATA_EXTRA_INSTRUCTIONS")
    if e("PA_METADATA_PROMPT_OVERRIDE") is not None:
        settings.metadata_prompts.prompt_override = e("PA_METADATA_PROMPT_OVERRIDE")
    if e("PA_OCR_EXTRA_INSTRUCTIONS") is not None:
        settings.ocr_prompts.extra_instructions = e("PA_OCR_EXTRA_INSTRUCTIONS")
    if e("PA_OCR_PROMPT_OVERRIDE") is not None:
        settings.ocr_prompts.prompt_override = e("PA_OCR_PROMPT_OVERRIDE")


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _env_bool(settings, env_key, attr):
    v = os.environ.get(env_key)
    if v is not None and v != "":
        setattr(settings, attr, _truthy(v))


def _env_num(settings, env_key, attr, cast):
    v = os.environ.get(env_key)
    if v:
        setattr(settings, attr, cast(v))


def load_settings(
    *,
    config_file: str | pathlib.Path | None = None,
    overrides: dict | None = None,
    require_token: bool = True,
) -> "Settings":
    """Resolve the layered config (plan §7.1), lowest -> highest precedence:

        built-in safe defaults  <  YAML file  <  env vars  <  per-run overrides

    `config_file` defaults to `<data_dir>/config.yml` if present. `overrides` are
    the highest-precedence per-run values (CLI flags / sweep job options), and are
    applied last. Secrets never come from the YAML (enforced in `_load_yaml_file`).
    """
    settings = Settings()  # layer 1: safe defaults (§7.2)

    # Resolve data_dir early (env may relocate it) so the default config path is
    # correct before we look for the YAML file.
    if os.environ.get("PA_DATA_DIR"):
        settings.data_dir = os.environ["PA_DATA_DIR"]

    # layer 2: YAML file (mounted). Default location under /data.
    path = pathlib.Path(config_file) if config_file else settings.data_path("config.yml")
    if path.exists():
        _apply_yaml(settings, _load_yaml_file(path))

    # layer 3: environment variables.
    _apply_env(settings)

    # layer 4: per-run overrides (CLI flags / sweep job options).
    if overrides:
        _apply_overrides(settings, overrides)

    if require_token and not settings.paperless_token:
        raise ConfigError(
            "PAPERLESS_TOKEN is not set. Set it (and optionally PAPERLESS_URL) in "
            "the environment / a secret file - it must NEVER go in the YAML config."
        )
    return settings


def _apply_overrides(settings: "Settings", overrides: dict) -> None:
    """Apply per-run overrides (highest precedence). Only known attributes are
    honored; None values are ignored so callers can pass argparse defaults."""
    for key, val in overrides.items():
        if val is None:
            continue
        if key == "per_run_cap":
            settings.spend.per_run = float(val)
            continue
        if key == "per_period_cap":
            settings.spend.per_period = float(val)
            continue
        if key in ("ocr_provider", "ocr_model", "metadata_provider", "metadata_model"):
            task = settings.ocr_task if key.startswith("ocr") else settings.metadata_task
            if key.endswith("provider"):
                task.provider = val
            else:
                task.model = val
            continue
        if key == "delete_originals" and val:
            raise ConfigError(
                "delete_originals cannot be enabled - deletion is never automated (I4)."
            )
        if hasattr(settings, key):
            setattr(settings, key, val)
