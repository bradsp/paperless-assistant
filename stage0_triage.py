#!/usr/bin/env python3
"""
Stage 0 - OCR quality triage for Paperless-ngx.

Walks every document, scores the quality of its EXISTING OCR text with a
heuristic, and writes the result back into Paperless custom fields so you can
filter/act on it natively in the UI:

    ocr_quality : float  (0.0 = clean .. 1.0 = garbage)
    ai_stage    : text   ("triaged")
    ai_notes    : text   (short diagnostic string)

Design goals:
  - Idempotent: re-running updates the same fields, never duplicates.
  - Resumable: skips docs already at the target stage unless --force.
  - Safe: snapshots each document's metadata to ./snapshots/ before any write.
  - Polite: bounded concurrency + retry/backoff so it never drains the DB pool.
  - Read-mostly: ONLY writes the three triage fields; never touches title/tags.

Usage:
  export PAPERLESS_URL="http://localhost:8000"     # or https://paperless.example.com
  export PAPERLESS_TOKEN="xxxxxxxx"
  python stage0_triage.py --dry-run        # score + report, write nothing
  python stage0_triage.py                  # score + write custom fields
  python stage0_triage.py --threshold 0.50 --limit 25   # test on 25 docs
  python stage0_triage.py --force          # re-triage even completed docs
"""

import argparse
import json
import os
import re
import sys
import time
import pathlib
import datetime as dt
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
BASE = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
if not TOKEN:
    sys.exit("Set PAPERLESS_TOKEN (and optionally PAPERLESS_URL) in the environment.")

SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Token {TOKEN}",
                        "Accept": "application/json"})

# The custom fields we read/write. Names must match what you created in the UI.
FIELD_SCORE = "ocr_quality"
FIELD_STAGE = "ai_stage"
FIELD_NOTES = "ai_notes"
STAGE_VALUE = "triaged"

SNAP_DIR = pathlib.Path("./snapshots")
SNAP_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------------
# HTTP helpers with retry/backoff (handles 429 + transient 5xx)
# ----------------------------------------------------------------------------
def _request(method, url, **kw):
    delay = 1.0
    for attempt in range(6):
        r = SESSION.request(method, url, timeout=60, **kw)
        if r.status_code in (429, 500, 502, 503, 504):
            wait = float(r.headers.get("Retry-After", delay))
            time.sleep(wait)
            delay = min(delay * 2, 30)
            continue
        if r.status_code >= 400:
            # Surface the server's actual validation message (e.g. which
            # custom field rejected which value) instead of a bare HTTPError.
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:500]
            raise requests.HTTPError(
                f"{r.status_code} on {method} {url}\n  server says: {detail}",
                response=r)
        return r
    r.raise_for_status()
    return r


def get_field_map():
    """Resolve custom-field names -> ids. For select fields, also capture the
    option label->id mapping, since select values must be written as option ids."""
    fields = {}
    url = f"{BASE}/api/custom_fields/?page_size=200"
    while url:
        data = _request("GET", url).json()
        for f in data["results"]:
            entry = {"id": f["id"], "data_type": f["data_type"]}
            # Select fields carry their options under extra_data.select_options,
            # each as {"id": "...", "label": "..."}. Build a label->id lookup.
            if f["data_type"] == "select":
                opts = (f.get("extra_data") or {}).get("select_options") or []
                entry["options"] = {o["label"]: o["id"] for o in opts}
            fields[f["name"]] = entry
        url = data.get("next")
    missing = [n for n in (FIELD_SCORE, FIELD_STAGE, FIELD_NOTES) if n not in fields]
    if missing:
        sys.exit(f"Missing custom fields in Paperless: {missing}. "
                 f"Create them under Settings > Custom Fields first.")
    return fields


def stage_value(fmap):
    """Return the correct value to write for ai_stage = STAGE_VALUE,
    translating to the select-option id when the field is a select."""
    f = fmap[FIELD_STAGE]
    if f["data_type"] == "select":
        opts = f.get("options") or {}
        if STAGE_VALUE not in opts:
            sys.exit(f"Select field '{FIELD_STAGE}' has no option labelled "
                     f"'{STAGE_VALUE}'. Options present: {list(opts)}")
        return opts[STAGE_VALUE]
    return STAGE_VALUE


def iter_documents(page_size=100):
    """Yield documents with the fields we need, paginated."""
    url = (f"{BASE}/api/documents/"
           f"?fields=id,title,content,custom_fields&page_size={page_size}")
    while url:
        data = _request("GET", url).json()
        for d in data["results"]:
            yield d
        url = data.get("next")


# ----------------------------------------------------------------------------
# The garbage scorer
# ----------------------------------------------------------------------------
def garbage_score(text: str):
    """Return (score, note). 0.0 = clean text, 1.0 = garbage/empty."""
    if not text or len(text.strip()) < 40:
        return 1.0, "empty_or_tiny"

    words = re.findall(r"[A-Za-z]{2,}", text)
    if not words:
        return 1.0, "no_alpha_words"

    nonspace = re.sub(r"\s", "", text)
    wordchars = sum(len(w) for w in words)
    word_ratio = wordchars / max(len(nonspace), 1)

    plausible = [w for w in words
                 if len(w) >= 3 and re.search(r"[aeiouAEIOU]", w)]
    plausible_ratio = len(plausible) / max(len(words), 1)

    # Average run length of alphabetic tokens (garbage tends to fragment)
    avg_word_len = wordchars / max(len(words), 1)
    frag_penalty = 1.0 if avg_word_len < 2.5 else 0.0

    score = 1.0 - (0.45 * word_ratio + 0.45 * plausible_ratio + 0.10 * (1 - frag_penalty))
    score = max(0.0, min(1.0, score))

    note = (f"wr={word_ratio:.2f} pr={plausible_ratio:.2f} "
            f"awl={avg_word_len:.1f} n={len(words)}")
    return round(score, 3), note


# ----------------------------------------------------------------------------
# Write-back
# ----------------------------------------------------------------------------
def snapshot(doc):
    path = SNAP_DIR / f"{doc['id']}.json"
    if not path.exists():  # only snapshot original state once
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2))


def _coerce_score(score, data_type):
    """Format the float score to whatever the ocr_quality field expects."""
    if data_type in ("float", "monetary"):
        return float(score)
    if data_type == "integer":
        # No float field available -> store as 0-100 integer percentage.
        return int(round(score * 100))
    if data_type in ("string", "text", "url"):
        return str(score)
    return float(score)


def merge_custom_fields(existing, fmap, score, note):
    """Build the custom_fields array preserving other fields, updating ours."""
    target_ids = {fmap[FIELD_SCORE]["id"], fmap[FIELD_STAGE]["id"],
                  fmap[FIELD_NOTES]["id"]}
    kept = [cf for cf in (existing or []) if cf["field"] not in target_ids]
    kept.extend([
        {"field": fmap[FIELD_SCORE]["id"],
         "value": _coerce_score(score, fmap[FIELD_SCORE]["data_type"])},
        {"field": fmap[FIELD_STAGE]["id"], "value": stage_value(fmap)},
        {"field": fmap[FIELD_NOTES]["id"], "value": note[:255]},
    ])
    return kept


def already_triaged(doc, fmap):
    stage_id = fmap[FIELD_STAGE]["id"]
    want = stage_value(fmap)   # option id for select fields, else the label
    for cf in doc.get("custom_fields") or []:
        if cf["field"] == stage_id and cf.get("value") == want:
            return True
    return False


def process_one(doc, fmap, args):
    if not args.force and already_triaged(doc, fmap):
        return ("skip", doc["id"], None, None)

    score, note = garbage_score(doc.get("content", ""))

    if args.dry_run:
        return ("dry", doc["id"], score, note)

    snapshot(doc)
    body = {"custom_fields": merge_custom_fields(
        doc.get("custom_fields"), fmap, score, note)}
    _request("PATCH", f"{BASE}/api/documents/{doc['id']}/", json=body)
    return ("wrote", doc["id"], score, note)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="score and report, write nothing")
    ap.add_argument("--force", action="store_true",
                    help="re-triage docs already marked triaged")
    ap.add_argument("--threshold", type=float, default=0.55,
                    help="score above which a doc is flagged for re-OCR")
    ap.add_argument("--limit", type=int, default=0,
                    help="process at most N documents (0 = all)")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent requests (keep modest; <= DB pool)")
    args = ap.parse_args()

    fmap = get_field_map()
    print(f"Resolved fields: "
          f"{FIELD_SCORE}=id{fmap[FIELD_SCORE]['id']}/{fmap[FIELD_SCORE]['data_type']}  "
          f"{FIELD_STAGE}=id{fmap[FIELD_STAGE]['id']}/{fmap[FIELD_STAGE]['data_type']}  "
          f"{FIELD_NOTES}=id{fmap[FIELD_NOTES]['id']}/{fmap[FIELD_NOTES]['data_type']}")
    if fmap[FIELD_SCORE]["data_type"] not in ("float", "integer", "monetary"):
        print(f"  WARNING: '{FIELD_SCORE}' is type "
              f"'{fmap[FIELD_SCORE]['data_type']}'. A Number (float) field is "
              f"recommended so you can filter with >= in the UI.")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}  "
          f"threshold={args.threshold}  workers={args.workers}")

    docs = []
    for d in iter_documents():
        docs.append(d)
        if args.limit and len(docs) >= args.limit:
            break
    print(f"Fetched {len(docs)} documents.\n")

    flagged, wrote, skipped = 0, 0, 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, d, fmap, args): d for d in docs}
        for fut in as_completed(futs):
            status, doc_id, score, note = fut.result()
            if status == "skip":
                skipped += 1
                continue
            if status in ("wrote", "dry"):
                if status == "wrote":
                    wrote += 1
                flag = score is not None and score >= args.threshold
                if flag:
                    flagged += 1
                marker = "  <-- FLAG re-OCR" if flag else ""
                print(f"[{status}] doc {doc_id:>5}  score={score}  {note}{marker}")

    dt_s = time.time() - t0
    print("\n--- summary ---")
    print(f"processed : {wrote if not args.dry_run else len(docs)-skipped}")
    print(f"skipped   : {skipped} (already triaged)")
    print(f"flagged   : {flagged} (>= {args.threshold}) for Stage 1 re-OCR")
    print(f"elapsed   : {dt_s:.1f}s")
    print(f"\nSnapshots saved to {SNAP_DIR.resolve()}")
    if not args.dry_run:
        print(f"\nIn the Paperless UI you can now filter: "
              f"{FIELD_SCORE} >= {args.threshold} to see the re-OCR queue.")


if __name__ == "__main__":
    main()
