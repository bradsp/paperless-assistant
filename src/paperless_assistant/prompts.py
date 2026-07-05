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

"""Prompt customization: the single prompt-resolution helper + the default
instruction constants (prompt 010).

Model/prompt tuning is how a self-hoster makes the AI fit *their* library. This
module gives operators two per-task levers over the NATURAL-LANGUAGE instruction
the engine sends — WITHOUT ever touching the engine-owned JSON Schema / tool
contract (that stays fixed in `metadata.METADATA_TOOL`, and every structured
result is still re-validated against it, so a custom prompt can degrade quality
but can NEVER produce an invalid write):

  * `extra_instructions` — a safe block APPENDED to the built-in instruction (the
    casual "always prefer my tags / ISO dates" path). This is the default lever.
  * `prompt_override` — an advanced full replacement of the built-in instruction
    text (empty = use the built-in default). Reset = clearing either field.

The composition is defined ONCE here (`resolve_instruction`) and reused by the
metadata extractor, the OCR pipeline, and the dashboard's effective-prompt
preview — so what the UI previews is byte-for-byte what the engine sends.

DEFAULTS ARE BYTE-IDENTICAL. With no override and no extra, `resolve_instruction`
returns the default constant unchanged (proven by a characterization test). The
default constants below are the EXACT instruction text the engine sent before
this feature existed.
"""
from __future__ import annotations

from dataclasses import dataclass

# The default OCR / vision transcription instruction. Kept byte-identical to the
# pre-010 `ocr.OCR_PROMPT` (and the adapters' copy). Imported here so there is a
# single canonical default the resolver and the UI both reference.
from .ocr import OCR_PROMPT as OCR_INSTRUCTION_DEFAULT


# The default metadata classification instruction — the EXACT preamble the
# pre-010 `metadata.build_prompt` emitted before the EXISTING-* taxonomy lists.
# Byte-identical: a characterization test asserts the resolved default equals
# what `build_prompt` produced originally.
METADATA_INSTRUCTION_DEFAULT = (
    "You are classifying a scanned/OCR'd document for a personal document "
    "management system. Generate metadata for it.\n\n"
    "STRONGLY PREFER reusing entries from these existing lists. Only invent a "
    "new value when none reasonably fits, and when you do, set the matching "
    "*_is_new flag / list so it can be reviewed."
)


# The task keys the rest of the system uses.
TASK_METADATA = "metadata"
TASK_OCR = "ocr"

# Per-task default instruction, for lookups (UI, preview, resolution fallback).
DEFAULT_INSTRUCTIONS = {
    TASK_METADATA: METADATA_INSTRUCTION_DEFAULT,
    TASK_OCR: OCR_INSTRUCTION_DEFAULT,
}


def default_instruction(task: str) -> str:
    """Return the built-in default instruction for a task ("metadata"/"ocr")."""
    try:
        return DEFAULT_INSTRUCTIONS[task]
    except KeyError as e:
        raise ValueError(f"unknown prompt task '{task}'") from e


def resolve_instruction(
    default: str, *, prompt_override: str | None = None,
    extra_instructions: str | None = None,
) -> str:
    """Compose the EFFECTIVE instruction from the three inputs, in ONE place.

        effective = (prompt_override if non-empty else default)
                    + ("\n\n" + extra_instructions  if non-empty)

    Both custom fields are optional; an empty/whitespace-only value is treated as
    absent (so a blank field RESETS to the default). When neither is set, the
    return value is the `default` string UNCHANGED — the byte-identical guarantee.

    This only shapes the natural-language instruction. The JSON Schema / tool the
    engine validates against is never involved here.
    """
    base = default
    if prompt_override is not None and str(prompt_override).strip():
        base = str(prompt_override)
    extra = "" if extra_instructions is None else str(extra_instructions)
    if extra.strip():
        return base + "\n\n" + extra.strip()
    return base


@dataclass
class PromptConfig:
    """Per-task prompt customization (non-secret config). `prompt_override`
    replaces the built-in instruction; empty = use the default. `extra_instructions`
    is appended to whichever base is in effect. Both default to "" (no
    customization -> byte-identical behavior)."""

    extra_instructions: str = ""
    prompt_override: str = ""

    def effective(self, default: str) -> str:
        """The composed instruction for this task given its built-in default."""
        return resolve_instruction(
            default,
            prompt_override=self.prompt_override,
            extra_instructions=self.extra_instructions,
        )


def resolve_for_task(task: str, prompts: "PromptConfig | None") -> str:
    """Effective instruction for a task given its (optional) PromptConfig. With no
    PromptConfig, returns the byte-identical default."""
    default = default_instruction(task)
    if prompts is None:
        return default
    return prompts.effective(default)
