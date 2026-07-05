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

"""Curated model catalog for the config UI (prompt 010), DERIVED from the pricing
table so pricing stays single-source.

`model_catalog()` returns, per provider, the list of KNOWN models with:
  * `id`     — the model id to store in config (e.g. "claude-opus-4-8")
  * `label`  — a short human label for the dropdown
  * `in_price_per_1k` / `out_price_per_1k` — pricing hints (USD per 1K tokens),
    computed FROM `pricing.PRICING` (None for zero/local cost)
  * `vision` — whether the model is vision-capable (drives the re-OCR warning)
  * `recommended` — whether it's a sensible default for its class

The catalog is a UI convenience only: it is NOT authoritative. A configured model
that is NOT in the catalog is always allowed (custom/"other…" ids) — nothing here
rejects an unknown id, and the dashboard offers a free-text field. Extending the
catalog when a new model ships is a one-line addition to `_KNOWN` (plus a pricing
entry if it is a paid model).
"""
from __future__ import annotations

from . import pricing
from .openai import _VISION_MODELS as _OPENAI_VISION
from .ollama import _looks_vision as _ollama_looks_vision


# Per-provider curated presentation metadata (labels / vision / recommended).
# Pricing is NOT duplicated here — it is read from `pricing.PRICING` at build time.
# `id`s for anthropic/openai come from the pricing table; extra display-only hints
# (label, vision, recommended) are keyed by id. Ollama has no fixed price table
# (local, zero cost), so a few representative local models are listed for
# convenience; any other local id is fine via the free-text option.
_META: dict[str, dict[str, dict]] = {
    "anthropic": {
        # All Claude 4+ models advertise vision; opus-class is the strong re-OCR
        # default, sonnet-class the cheap metadata default (defaults unchanged).
        "claude-fable-5": {"label": "Claude Fable 5 (vision, most capable)"},
        "claude-opus-4-8": {
            "label": "Claude Opus 4.8 (vision, strong)", "recommended_ocr": True,
        },
        "claude-opus-4-7": {"label": "Claude Opus 4.7 (vision)"},
        "claude-opus-4-6": {"label": "Claude Opus 4.6 (vision)"},
        "claude-sonnet-5": {"label": "Claude Sonnet 5 (vision, balanced)"},
        "claude-sonnet-4-6": {
            "label": "Claude Sonnet 4.6 (cheap/fast classification)",
            "recommended_metadata": True,
        },
        "claude-haiku-4-5": {"label": "Claude Haiku 4.5 (vision, fastest)"},
    },
    "openai": {
        # vision is derived from openai._VISION_MODELS (same predicate the adapter
        # uses at run time), so entries need no explicit `vision` hint.
        "gpt-5.4": {"label": "GPT-5.4 (vision, production)"},
        "gpt-4.1": {"label": "GPT-4.1 (vision, 1M context)"},
        "gpt-4.1-mini": {"label": "GPT-4.1 mini (cheap vision)"},
        "gpt-4.1-nano": {"label": "GPT-4.1 nano (cheapest vision)"},
        "gpt-4o": {"label": "GPT-4o (vision)", "recommended_ocr": True},
        "gpt-4o-mini": {"label": "GPT-4o mini (cheap vision)", "recommended_metadata": True},
        "o4-mini": {"label": "o4-mini (vision, reasoning)"},
    },
    "ollama": {
        "llava": {"label": "LLaVA (local vision)", "recommended_ocr": True},
        "llama3.2-vision": {"label": "Llama 3.2 Vision (local vision)"},
        "llama3.1": {"label": "Llama 3.1 (local, text-only)"},
    },
}


def _price_per_1k(provider: str, model: str) -> tuple[float | None, float | None]:
    """Pricing hints in USD per 1K tokens, derived from the per-token table.
    Returns (None, None) for zero / unpriced (local) models."""
    pin, pout = pricing.price_for(provider, model)
    if pin == 0.0 and pout == 0.0:
        return None, None
    return round(pin * 1000, 6), round(pout * 1000, 6)


def _vision_for(provider: str, model: str, hint: dict) -> bool:
    """Whether a model is vision-capable — reuse the SAME predicates the adapters
    use so the UI warning matches run-time capability negotiation."""
    if "vision" in hint:
        return bool(hint["vision"])
    if provider == "anthropic":
        return True  # opus/sonnet-class advertise vision
    if provider == "openai":
        return model in _OPENAI_VISION
    if provider == "ollama":
        return _ollama_looks_vision(model)
    return False


def model_catalog() -> dict[str, list[dict]]:
    """Return {provider: [ {id, label, in_price_per_1k, out_price_per_1k, vision,
    recommended}, ... ]}. Derived from pricing.py; extend `_META` (and the pricing
    table for paid models) to add entries."""
    out: dict[str, list[dict]] = {}
    # Union of ids: those in the pricing table AND those with display metadata, so a
    # newly-priced model shows up even before it gets a label, and vice-versa.
    for provider in sorted(set(pricing.PRICING) | set(_META)):
        ids = sorted(set(pricing.PRICING.get(provider, {})) | set(_META.get(provider, {})))
        entries = []
        for model in ids:
            hint = _META.get(provider, {}).get(model, {})
            in1k, out1k = _price_per_1k(provider, model)
            recommended = bool(hint.get("recommended_ocr") or hint.get("recommended_metadata"))
            entries.append({
                "id": model,
                "label": hint.get("label", model),
                "in_price_per_1k": in1k,
                "out_price_per_1k": out1k,
                "vision": _vision_for(provider, model, hint),
                "recommended": recommended,
                "recommended_ocr": bool(hint.get("recommended_ocr")),
                "recommended_metadata": bool(hint.get("recommended_metadata")),
            })
        out[provider] = entries
    return out


def is_vision_model(provider: str, model: str) -> bool:
    """Best-effort vision check for an arbitrary (possibly uncatalogued) model,
    reusing the adapters' predicates. Used by the UI to warn early on a non-vision
    re-OCR model. Unknown provider/model -> False (warn, don't assume vision)."""
    hint = _META.get(provider, {}).get(model, {})
    return _vision_for(provider, model, hint)
