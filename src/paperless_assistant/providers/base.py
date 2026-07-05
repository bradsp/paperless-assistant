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

"""AIProvider abstraction - the interface boundary between *which model* and
*what task* (plan §5.2).

The three AI-shaped tasks (plan §5.1) are:
  * transcribe(doc_bytes)            -> free text  (requires the "vision" capability)
  * extract_structured(prompt, schema) -> dict GUARANTEED to validate, or raises

The core guarantee (plan §5.3): the ENGINE owns the JSON Schema and re-validates
every structured result against it after every provider call. Adapters only
translate the schema into each provider's native structured-output mechanism;
the validation authority never leaves the engine. A validation failure is
retry-then-error, NEVER a silent bad write.

`garbage_score` is deliberately NOT a provider task - it stays a local, free
heuristic in ocr.py (plan §5.1 note).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Capability tokens a provider may advertise in `capabilities`.
CAP_VISION = "vision"
CAP_STRUCTURED = "structured_output"


@dataclass
class Transcription:
    """Result of a vision transcription. Carries token usage + cost so the
    SpendGovernor (I3) can gate the billable call exactly as before."""

    text: str
    in_tokens: int
    out_tokens: int
    cost: float


@dataclass
class StructuredResult:
    """Result of a structured extraction. `data` is the raw dict the provider
    produced; the engine validates it against its own schema before use."""

    data: dict
    in_tokens: int
    out_tokens: int
    cost: float


class ProviderError(RuntimeError):
    """Base class for provider-layer failures."""


class CapabilityError(ProviderError):
    """Raised when a provider is asked to do something it cannot do (e.g. a
    vision-less provider selected for re-OCR). Actionable, not a silent
    downgrade (plan §5.3, I6 spirit)."""


class SchemaValidationError(ProviderError):
    """Raised by the engine when a structured result fails schema validation
    after all retries are exhausted. Guarantees NO bad write reaches Paperless."""


@runtime_checkable
class AIProvider(Protocol):
    """The stable provider interface. Concrete adapters (Anthropic, OpenAI,
    Ollama - and later a HostedProvider) implement this.

    `capabilities` advertises what the *configured model* can do. `name` and
    `model` are used for reporting and pricing selection.
    """

    name: str
    capabilities: set

    def transcribe(self, doc: bytes, *, opts: dict | None = None) -> Transcription:
        """Vision transcription: PDF/image bytes -> free text. Must raise
        CapabilityError if this provider/model lacks the vision capability."""
        ...

    def extract_structured(
        self, prompt: str, schema: dict, *, opts: dict | None = None
    ) -> StructuredResult:
        """Return a StructuredResult whose `.data` the provider intends to
        satisfy `schema`. The provider translates the schema into its native
        mechanism; the ENGINE re-validates `.data` against `schema`."""
        ...


# ---------------------------------------------------------------------------
# Engine-side validation - the core guarantee (plan §5.3).
# ---------------------------------------------------------------------------
def validate_against_schema(data: Any, schema: dict) -> None:
    """Validate `data` against a JSON Schema using a real validator
    (`jsonschema`). Raises SchemaValidationError on any violation.

    This is the single chokepoint the engine calls after every structured
    provider response, regardless of which provider produced it.
    """
    import jsonschema

    try:
        jsonschema.validate(instance=data, schema=schema)
    except jsonschema.ValidationError as e:
        raise SchemaValidationError(
            f"provider output failed engine schema validation: {e.message}"
        ) from e
    except jsonschema.SchemaError as e:  # pragma: no cover - our schema is fixed
        raise SchemaValidationError(f"invalid schema: {e.message}") from e


def extract_structured_validated(
    provider: AIProvider,
    prompt: str,
    schema: dict,
    *,
    opts: dict | None = None,
    max_attempts: int = 3,
) -> StructuredResult:
    """Call `provider.extract_structured` and re-validate the returned dict
    against the engine-owned `schema`. On a validation failure, RETRY up to
    `max_attempts`; if every attempt is off-schema, raise SchemaValidationError
    so the caller NEVER writes a bad structured result to Paperless.

    Cost from every attempt (including the rejected ones) is returned on the
    successful result via accumulation so the SpendGovernor still sees every
    billable call. On total failure the accumulated cost is attached to the
    raised error as `.spent`.
    """
    spent = 0.0
    in_tok = 0
    out_tok = 0
    last_err: Exception | None = None
    for _ in range(max_attempts):
        result = provider.extract_structured(prompt, schema, opts=opts)
        spent += result.cost
        in_tok += result.in_tokens
        out_tok += result.out_tokens
        try:
            validate_against_schema(result.data, schema)
        except SchemaValidationError as e:
            last_err = e
            continue
        # success: return with the accumulated cost across attempts
        return StructuredResult(
            data=result.data, in_tokens=in_tok, out_tokens=out_tok, cost=spent
        )
    err = SchemaValidationError(
        f"structured output never validated after {max_attempts} attempts: {last_err}"
    )
    err.spent = spent  # type: ignore[attr-defined]
    raise err
