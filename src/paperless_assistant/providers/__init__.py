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

"""AIProvider abstraction (plan §5): decouple *which model* from *what task*,
without weakening the structured-output guarantee.

Public surface:
  * AIProvider protocol, Transcription / StructuredResult result types
  * validate_against_schema / extract_structured_validated - engine-side
    validation (the core guarantee)
  * ProviderError / CapabilityError / SchemaValidationError
  * build_provider(task, cfg) - registry/factory resolving config -> provider

Concrete adapters (AnthropicProvider, OpenAIProvider, OllamaProvider) live in
their own modules and are import-guarded where their SDKs are optional. A future
HostedProvider (Phase 6) can slot in behind the same protocol without touching
the engine - it is intentionally NOT implemented here.
"""
from __future__ import annotations

from .base import (
    CAP_STRUCTURED,
    CAP_VISION,
    AIProvider,
    CapabilityError,
    ProviderError,
    SchemaValidationError,
    StructuredResult,
    Transcription,
    extract_structured_validated,
    validate_against_schema,
)
from .registry import TASK_METADATA, TASK_OCR, build_provider
from .catalog import model_catalog, is_vision_model

__all__ = [
    "model_catalog",
    "is_vision_model",
    "AIProvider",
    "Transcription",
    "StructuredResult",
    "CAP_VISION",
    "CAP_STRUCTURED",
    "ProviderError",
    "CapabilityError",
    "SchemaValidationError",
    "validate_against_schema",
    "extract_structured_validated",
    "build_provider",
    "TASK_OCR",
    "TASK_METADATA",
]
