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

"""Container privilege-drop helper.

The published Docker image starts as **root** for one reason only: a bind-mounted
`/data` arrives owned by the host's root, which the non-root `pa` user cannot
write to (`mkdir /data/logs` -> EPERM). So `pa` makes `/data` writable and then
**immediately drops** to the `pa` user before doing any work.

Because this lives in the `pa` entry point, it fires for BOTH the long-running
container process (`pa serve`) AND any `docker exec ... pa ...`, so `/data` is
never left with root-owned files that would later break the running agent.

It is a strict NO-OP unless `PA_CONTAINER=1` (only this project's image sets it)
AND the process is genuinely root on POSIX -- so local or `sudo pa` usage is
never affected. On failure it warns and continues rather than crashing.
"""
from __future__ import annotations

import os
import sys


def _chown(path: str, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid, follow_symlinks=False)
    except OSError:
        pass


def drop_privileges_if_container_root() -> None:
    """If running as root inside this project's container, make PA_DATA_DIR
    writable by the pa user and drop to it. No-op otherwise."""
    if os.environ.get("PA_CONTAINER") != "1":
        return
    if os.name != "posix":
        return
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return

    uid = int(os.environ.get("PA_UID", "10001"))
    gid = int(os.environ.get("PA_GID", "10001"))
    data_dir = os.environ.get("PA_DATA_DIR", "/data")

    # Make /data writable by the target user. Fast path: skip the recursive chown
    # once /data is already target-owned (steady state after the first start).
    try:
        os.makedirs(data_dir, exist_ok=True)
        st = os.stat(data_dir)
        if st.st_uid != uid or st.st_gid != gid:
            _chown(data_dir, uid, gid)
            for root, dirs, files in os.walk(data_dir):
                for name in dirs + files:
                    _chown(os.path.join(root, name), uid, gid)
    except OSError as e:
        print(f"pa: warning: could not adjust ownership of {data_dir}: {e}",
              file=sys.stderr)

    # Drop supplementary groups, gid, then uid (order matters: setuid last).
    try:
        try:
            os.setgroups([gid])
        except OSError:
            pass
        os.setgid(gid)
        os.setuid(uid)
    except OSError as e:
        print(f"pa: warning: could not drop privileges to {uid}:{gid}: {e}",
              file=sys.stderr)
