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

"""Provider registry / factory: resolve (config, task) -> AIProvider instance.

"Which model" is config; "what task" is code (plan §5.3). The engine asks the
registry for the provider configured for a given task ("ocr" or "metadata") and
gets a ready adapter. Anthropic + the current models are the safe defaults, so a
config that specifies nothing reproduces Phase 1 behavior exactly.
"""
from __future__ import annotations

from .base import ProviderError

TASK_OCR = "ocr"
TASK_METADATA = "metadata"


def build_provider(task: str, cfg) -> "object":
    """Build the AIProvider for `task` from a resolved Config-like object.

    `cfg` must expose, per task, a provider name + model, plus credentials /
    endpoints. See config.ProviderSettings. Unknown providers raise a clear
    ProviderError (I6 spirit)."""
    settings = cfg.provider_for(task)
    name = settings.provider

    if name == "hosted":
        # Phase 6: hosted inference. The provider calls the control-plane inference
        # proxy over the agent's outbound transport (agent-credential auth). No AI
        # key is used agent-side; the vendor key stays server-side. The runtime
        # wiring is carried on cfg.hosted_inference (injected by the HostedAgent).
        from .hosted_provider import HostedProvider

        ctx = getattr(cfg, "hosted_inference", None)
        if ctx is None:
            raise ProviderError(
                "hosted inference selected but no inference context was wired. "
                "This is only valid inside a running hosted agent."
            )
        return HostedProvider(
            transport=ctx.transport,
            auth_headers=ctx.auth_headers,
            ocr_model=ctx.ocr_model,
            metadata_model=ctx.metadata_model,
            vision=ctx.vision,
        )

    if name == "anthropic":
        from .anthropic import AnthropicProvider

        return AnthropicProvider(
            api_key=cfg.anthropic_api_key,
            ocr_model=cfg.ocr_model,
            metadata_model=cfg.metadata_model,
            max_ocr_tokens=cfg.max_ocr_tokens,
            metadata_max_tokens=getattr(cfg, "metadata_max_tokens", 1024),
        )
    if name == "openai":
        from .openai import OpenAIProvider

        return OpenAIProvider(
            api_key=cfg.openai_api_key,
            ocr_model=cfg.ocr_model,
            metadata_model=cfg.metadata_model,
            max_ocr_tokens=cfg.max_ocr_tokens,
            base_url=cfg.openai_base_url or None,
            vision=settings.vision,
            metadata_max_tokens=getattr(cfg, "metadata_max_tokens", 1024),
            # max_retries left at the adapter default (6) — the SDK rides out
            # transient TPM 429s via its retry-after backoff instead of failing
            # the document.
        )
    if name == "ollama":
        from .ollama import OllamaProvider

        return OllamaProvider(
            ocr_model=cfg.ocr_model,
            metadata_model=cfg.metadata_model,
            endpoint=cfg.ollama_endpoint,
            vision=settings.vision,
        )
    raise ProviderError(
        f"unknown AI provider '{name}' for task '{task}'. "
        f"Supported: anthropic, openai, ollama."
    )
