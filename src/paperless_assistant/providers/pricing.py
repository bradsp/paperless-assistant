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

"""Per-provider / per-model pricing tables (USD per token).

Per plan §5.3 the `PRICE_*` constants move OUT of spend.py and into the
providers. spend.py keeps the accumulator + cap logic (the SpendGovernor); the
numbers live here and each adapter exposes cost so the governor still gates
every billable call. "The cap is a safety abort, not accounting."

Prices are (input_per_token, output_per_token) and track each vendor's current
published per-million-token rates (the values below are written as `$/1e6`).
They are the single source of truth the model catalog (catalog.py) derives its
UI pricing hints from, so keep them current when a model ships or is repriced. A
price of (0.0, 0.0) means zero marginal cost (local Ollama).
"""
from __future__ import annotations

# (in_per_token, out_per_token)
_DEFAULT = (0.0, 0.0)

PRICING: dict[str, dict[str, tuple[float, float]]] = {
    # Anthropic — current per-Mtok rates (Opus/Sonnet/Haiku classes all advertise
    # vision). Opus-class is the strong re-OCR default; Sonnet-class the cheap
    # metadata default.
    "anthropic": {
        "claude-fable-5": (10.0 / 1_000_000, 50.0 / 1_000_000),
        "claude-opus-4-8": (5.0 / 1_000_000, 25.0 / 1_000_000),
        "claude-opus-4-7": (5.0 / 1_000_000, 25.0 / 1_000_000),
        "claude-opus-4-6": (5.0 / 1_000_000, 25.0 / 1_000_000),
        "claude-sonnet-5": (3.0 / 1_000_000, 15.0 / 1_000_000),
        "claude-sonnet-4-6": (3.0 / 1_000_000, 15.0 / 1_000_000),
        "claude-haiku-4-5": (1.0 / 1_000_000, 5.0 / 1_000_000),
    },
    # OpenAI — representative published rates; feed the SpendGovernor as a safety
    # cap. GPT-4o / GPT-4.1 / o4 / GPT-5 families are vision-capable (see
    # openai._VISION_MODELS, which the catalog reuses so UI and runtime agree).
    "openai": {
        "gpt-5.4": (2.5 / 1_000_000, 15.0 / 1_000_000),
        "gpt-4.1": (2.0 / 1_000_000, 8.0 / 1_000_000),
        "gpt-4.1-mini": (0.40 / 1_000_000, 1.60 / 1_000_000),
        "gpt-4.1-nano": (0.10 / 1_000_000, 0.40 / 1_000_000),
        "gpt-4o": (2.5 / 1_000_000, 10.0 / 1_000_000),
        "gpt-4o-mini": (0.15 / 1_000_000, 0.60 / 1_000_000),
        "o4-mini": (1.10 / 1_000_000, 4.40 / 1_000_000),
    },
    "ollama": {
        # Local inference: zero marginal cost. Any model name resolves to free.
    },
}


def price_for(provider: str, model: str) -> tuple[float, float]:
    """Return (in_per_token, out_per_token) for a provider/model, defaulting to
    zero marginal cost when the model is not in the table (e.g. any local model
    or an OpenAI model without a pinned price)."""
    return PRICING.get(provider, {}).get(model, _DEFAULT)


def cost_of(provider: str, model: str, in_tokens: int, out_tokens: int) -> float:
    """USD cost of a call. Zero when the model has no marginal cost."""
    pin, pout = price_for(provider, model)
    return in_tokens * pin + out_tokens * pout


def is_priced(provider: str, model: str) -> bool:
    """True iff (provider, model) has an EXPLICIT pricing entry.

    Distinct from `cost_of` returning 0.0: an unknown model also yields 0.0, but
    that is a *missing price*, not a free model. The hosted inference proxy uses
    this to fail closed rather than meter a real, paid vendor call at $0 — which
    would silently defeat the server-side per-tenant spend cap."""
    return model in PRICING.get(provider, {})
