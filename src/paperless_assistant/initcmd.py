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

"""`pa init` - emit the docker-compose service block (plan §8.1, §8.2 step 1).

Prints the exact add-on snippet a self-hoster pastes into their existing
Paperless-NGX docker-compose.yml. Optionally writes it to a file. The block is
generated in code (not read from a bundled file) so it works inside the shipped
container too.

Design constraints echoed in the block itself (plan §8.1):
  * reaches Paperless by in-stack service name over the LAN;
  * secrets from env, never YAML;
  * NO `ports:` - the agent exposes nothing to the host (outbound-only).
"""
from __future__ import annotations

import pathlib

COMPOSE_BLOCK = """\
# --- Paperless Assistant (add to your existing docker-compose.yml) ---------
  paperless-assistant:
    image: ghcr.io/bradsp/paperless-assistant:latest
    depends_on: [webserver]              # the paperless-ngx service
    environment:
      PAPERLESS_URL: http://webserver:8000       # in-stack service name, LAN-internal
      PAPERLESS_TOKEN: ${PAPERLESS_ASSISTANT_TOKEN}   # scoped service-user token (NOT admin)
      PA_MODE: byo-key
      # AI provider + model default to Anthropic and are chosen in the dashboard
      # (Settings -> Models). Set PA_METADATA_PROVIDER / PA_OCR_PROVIDER (and the
      # matching _MODEL vars) here only if you want to PIN them -- doing so
      # env-locks the field so the dashboard shows it read-only.
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}     # byo-key only; NEVER put secrets in the YAML config
      # --- Web dashboard (opt-in): view status/runs/stats, run sweeps, edit config
      # It is the ONLY thing published to the host; a token protects it (env only).
      # PA_UI_ENABLED: "true"
      # PA_UI_TOKEN: ${PA_UI_TOKEN}               # env only; NEVER in the YAML config
      # PA_UI_PORT: "8770"                         # container port the dashboard binds
    volumes:
      - ./pa-data:/data                           # snapshots, config, run reports, cursor
    restart: unless-stopped
    command: ["serve"]                            # scheduled sweeps (first run = dry-run report)
    # NOTE: no `ports:` - the agent exposes nothing to the host by default. The ONLY
    # opt-in exception is the token-protected web dashboard; publish it by
    # uncommenting this mapping WHEN PA_UI_ENABLED + PA_UI_TOKEN are set:
    # ports:
    #   - "8770:8770"                             # host:container — the dashboard
# --------------------------------------------------------------------------
"""

NEXT_STEPS = """\
Next steps:
  1. Paste the block above into your existing Paperless-NGX docker-compose.yml
     (alongside the `webserver` service), and set PAPERLESS_ASSISTANT_TOKEN and
     ANTHROPIC_API_KEY in your .env (never in the YAML config file).
  2. `docker compose up -d paperless-assistant`
  3. `docker compose exec paperless-assistant pa setup`   # provision fields + tags
  4. `docker compose exec paperless-assistant pa doctor`  # expect all green
  The first scheduled sweep runs as a DRY-RUN and writes a report to ./pa-data.
"""


def render(write_path: str | None = None) -> str:
    """Return the compose block (+ guidance). If `write_path` is given, also write
    just the YAML block to that file."""
    if write_path:
        p = pathlib.Path(write_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(COMPOSE_BLOCK, encoding="utf-8")
    return COMPOSE_BLOCK + "\n" + NEXT_STEPS
