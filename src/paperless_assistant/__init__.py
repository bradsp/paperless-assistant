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

"""paperless_assistant - core engine extracted from the three POC scripts.

Phase 1: behavior-preserving refactor of stage0_triage.py, stage1_reocr.py and
stage2_metadata.py into an installable package exposing a `pa` CLI with
triage/reocr/metadata subcommands. Anthropic remains the only AI provider,
called directly (behind a thin internal seam in ocr.py / metadata.py) - the
provider abstraction is Phase 2 and is intentionally NOT built here.
"""

__version__ = "0.1.0"

__all__ = [
    "client",
    "fields",
    "taxonomy",
    "safety",
    "spend",
    "stages",
    "ocr",
    "metadata",
    "prompts",
    "config",
    "report",
    # Phase 3 (Docker companion): onboarding, sweep, observability.
    "provision",
    "doctor",
    "sweep",
    "obs",
    "initcmd",
]
