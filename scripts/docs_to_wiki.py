#!/usr/bin/env python3
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
"""Transform the repo's docs/ Markdown into GitHub-Wiki pages.

The wiki is a SEPARATE git repo, so relative links that work inside docs/ break
there. This script renames each doc to its wiki page name and rewrites every
link: doc<->doc links become wiki links; links to any other repo file (LICENSE,
config.example.yml, plan/, src/, the internal phase*-acceptance runbooks) become
absolute blob URLs; external URLs and same-page anchors are left alone.

Deterministic and stdlib-only, so it runs identically in CI and locally:

    python scripts/docs_to_wiki.py --out wiki-build --repo bradsp/paperless-assistant
"""
from __future__ import annotations

import argparse
import os
import posixpath
import re
import sys

# docs/<stem>.md  ->  wiki page name (filename stem; hyphens render as spaces).
MAPPING = {
    "readme": "Home",
    "installation": "Installation",
    "quick-start": "Quick-Start",
    "usage": "Usage",
    "configuration": "Configuration",
    "ai-providers": "AI-Providers",
    "dashboard": "Dashboard",
    "webhook": "Webhook",
    "re-ocr": "Re-OCR",
    "advanced-modes": "Advanced-Modes",
    "troubleshooting": "Troubleshooting",
    "architecture": "Architecture",
}

# docs excluded from the wiki. integration-guide is a redirect stub (its links
# point at Installation); the phase*-acceptance runbooks stay in the repo only.
_PHASE_RE = re.compile(r"^phase\d+-acceptance$")


def _excluded(stem: str) -> bool:
    return stem == "integration-guide" or bool(_PHASE_RE.match(stem))


# Left column renders on every wiki page. Kept in sync with MAPPING by hand — if
# you add a page, add it here too (the build warns on an unmapped doc).
SIDEBAR = """\
### Getting started
- [Home](Home)
- [Installation](Installation)
- [Quick Start](Quick-Start)
- [Usage](Usage)

### Configuration
- [Configuration](Configuration)
- [AI Providers](AI-Providers)

### Optional features
- [Dashboard](Dashboard)
- [Webhook](Webhook)
- [Re-OCR](Re-OCR)

### Advanced
- [Advanced Modes](Advanced-Modes)
- [Architecture](Architecture)

### Help
- [Troubleshooting](Troubleshooting)
"""

_ABS = ("http://", "https://", "mailto:", "tel:", "//")


def _classify(core: str, root: str, blob: str) -> str | None:
    """Map one relative link target (path + optional #anchor) to its wiki form,
    or None if it can't be resolved to a repo file (caller leaves it as-is)."""
    frag = ""
    if "#" in core:
        core, f = core.split("#", 1)
        frag = "#" + f
    if core == "":  # pure same-page anchor
        return None
    if core.startswith("./"):
        core = core[2:]

    # Resolve against docs/ first (sibling doc), then the repo root (../LICENSE,
    # or an author's bare "config.example.yml"). Existence decides which it is.
    docs_path = posixpath.normpath(posixpath.join("docs", core))
    root_path = posixpath.normpath(core)
    if os.path.exists(os.path.join(root, docs_path)):
        resolved = docs_path
    elif os.path.exists(os.path.join(root, root_path)):
        resolved = root_path
    else:
        return None

    if resolved.startswith("docs/") and resolved.endswith(".md"):
        stem = posixpath.basename(resolved)[:-3].lower()
        if stem in MAPPING:
            return MAPPING[stem] + frag            # doc -> wiki page link
        if stem == "integration-guide":
            return "Installation" + frag           # redirect stub -> its successor
        return blob + resolved + frag              # excluded doc (phase*) -> blob URL
    return blob + resolved + frag                  # any other repo file -> blob URL


def _rewrite_target(raw: str, root: str, blob: str) -> str:
    """Rewrite a single link/image target, preserving any trailing title."""
    stripped = raw.strip()
    if stripped.lower().startswith(_ABS) or stripped.startswith("#"):
        return raw
    title = ""
    m = re.match(r"^(\S+)(\s+.+)$", stripped)          # split "url" from optional "title"
    core = m.group(1) if m else stripped
    if m:
        title = m.group(2)
    core = core.strip("<>")
    new = _classify(core, root, blob)
    return raw if new is None else new + title


def transform(text: str, root: str, blob: str) -> str:
    # Inline links and images: ](target)  and  ![alt](target)
    text = re.sub(r"\]\(([^)]+)\)",
                  lambda mo: "](" + _rewrite_target(mo.group(1), root, blob) + ")",
                  text)
    # Reference-style definitions:  [label]: target "title"
    text = re.sub(r"(?m)^(\s*\[[^\]]+\]:\s*)(\S+)(.*)$",
                  lambda mo: mo.group(1) + _rewrite_target(mo.group(2), root, blob) + mo.group(3),
                  text)
    return text


def build(docs_dir: str, out_dir: str, root: str, repo: str, branch: str) -> int:
    blob = f"https://github.com/{repo}/blob/{branch}/"
    os.makedirs(out_dir, exist_ok=True)
    written, warnings = [], []
    for name in sorted(os.listdir(docs_dir)):
        if not name.endswith(".md"):
            continue
        stem = name[:-3].lower()
        if _excluded(stem):
            continue
        if stem not in MAPPING:
            warnings.append(f"unmapped doc (skipped): docs/{name} — add it to MAPPING + SIDEBAR")
            continue
        src = os.path.join(docs_dir, name)
        with open(src, encoding="utf-8") as fh:
            content = fh.read()
        out_name = MAPPING[stem] + ".md"
        with open(os.path.join(out_dir, out_name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(transform(content, root, blob))
        written.append(out_name)
    with open(os.path.join(out_dir, "_Sidebar.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(SIDEBAR)
    written.append("_Sidebar.md")

    print(f"Wrote {len(written)} wiki pages to {out_dir}:")
    for w in written:
        print(f"  - {w}")
    for warn in warnings:
        print(f"WARNING: {warn}", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build GitHub-Wiki pages from docs/.")
    ap.add_argument("--docs", default="docs", help="source docs directory")
    ap.add_argument("--out", required=True, help="output directory for wiki pages")
    ap.add_argument("--root", default=".", help="repo root (for link existence checks)")
    ap.add_argument("--repo", default="bradsp/paperless-assistant", help="owner/repo for blob URLs")
    ap.add_argument("--branch", default="main", help="branch for blob URLs")
    args = ap.parse_args()
    return build(args.docs, args.out, args.root, args.repo, args.branch)


if __name__ == "__main__":
    raise SystemExit(main())
