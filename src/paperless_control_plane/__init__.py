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

"""Paperless Assistant — HOSTED CONTROL PLANE (vendor-side component).

    ┌──────────── TRUST BOUNDARY ────────────┐
    │  This package is the VENDOR CLOUD side. │
    │  It is DELIBERATELY a separate package  │
    │  from `paperless_assistant` (the agent) │
    │  so the boundary is visible in the repo │
    │  layout.  The agent NEVER imports this   │
    │  package at runtime; it only speaks the  │
    │  documented outbound HTTP protocol.      │
    └─────────────────────────────────────────┘

Phase 5 (plan/connectivity-design.md §3): a MINIMAL, thin protocol gateway that
exists only to EXERCISE and PROVE the outbound-only agent protocol. It implements:

  * enrollment          POST /agent/enroll     (one-time token -> agent credential)
  * pull work           GET  /agent/work       (parking long-poll; NEVER pushes)
  * push results        POST /agent/results     (outcome/report/usage)
  * heartbeat           POST /agent/heartbeat   (status, queue depth, spend-vs-cap)
  * enqueue a job       POST /admin/enqueue     (admin path / `pa-control-plane enqueue`)

CRITICAL INVARIANT (§7): the control plane is ALWAYS reached by the agent; it
NEVER dials into the user's network. Work is PULLED by the agent, never pushed.
Every endpoint here is a server the AGENT connects OUT to.

Explicitly NOT built here (later phases, per the Phase 5 scope fence):
  * NO inference proxy / HostedProvider   (Phase 6 — inference stays BYO/agent-side)
  * NO billing / metering / entitlement    (Phase 6)
  * NO dashboards / UI, NO multi-tenant depth, NO Mode C direct-connection (Phase 7)

Structured so tenancy/billing/inference-proxy can be ADDED later without reshaping
the protocol (jobs/results/agents are already keyed by tenant+agent), but none of
that is implemented now.
"""
from __future__ import annotations

from .store import ControlPlaneStore
from .app import ControlPlane

__all__ = ["ControlPlaneStore", "ControlPlane"]
