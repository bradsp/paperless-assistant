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

"""SpendGovernor - thread-safe USD accumulator with a hard-abort cap (I3).

Extracted from: `_spend_lock`, `_spend_total`, the `--max-spend` pre-call
checks and `PRICE_*` constants (stage1 + stage2).

Semantics preserved exactly:
  * `should_abort()` returns True once total >= cap (checked BEFORE starting new
    billable work). A cap of 0 (falsy) means "no cap".
  * `add(cost)` accumulates under a lock so concurrent workers never lose an
    update.
"""
from __future__ import annotations

import threading


class SpendGovernor:
    def __init__(self, max_spend=0.0):
        self.max_spend = max_spend
        self._lock = threading.Lock()
        self._total = 0.0

    @property
    def total(self):
        with self._lock:
            return self._total

    def add(self, cost):
        """Accumulate spend thread-safely. Returns the new running total."""
        with self._lock:
            self._total += cost
            return self._total

    def should_abort(self):
        """Pre-call gate: refuse to start new work once the cap is reached.

        Mirrors the scripts: `if args.max_spend: with lock: _spend_total >=
        args.max_spend`. A falsy cap disables the gate.
        """
        if not self.max_spend:
            return False
        with self._lock:
            return self._total >= self.max_spend


# ---------------------------------------------------------------------------
# Pricing moved into the providers (plan §5.3): the per-provider/per-model price
# numbers now live in `providers/pricing.py`, and each adapter computes and
# exposes cost so the SpendGovernor above still gates every billable call. The
# SpendGovernor keeps ALL accumulator/cap logic; only the numbers moved out.
#
# These two helpers remain as thin back-compat shims that delegate to the
# Anthropic default pricing (the Phase 1 opus/sonnet split), so any external
# caller of `spend.ocr_cost` / `spend.metadata_cost` still works unchanged.
# ---------------------------------------------------------------------------
def ocr_cost(in_tokens, out_tokens):
    """USD cost for an OCR (opus-class) Anthropic call. Delegates to the
    provider pricing table. The cap is a safety abort, not accounting."""
    from . import config
    from .providers.pricing import cost_of

    return cost_of("anthropic", config.OCR_MODEL, in_tokens, out_tokens)


def metadata_cost(in_tokens, out_tokens):
    """USD cost for a metadata (sonnet-class) Anthropic call. Delegates to the
    provider pricing table."""
    from . import config
    from .providers.pricing import cost_of

    return cost_of("anthropic", config.METADATA_MODEL, in_tokens, out_tokens)
