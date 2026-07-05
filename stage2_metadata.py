#!/usr/bin/env python3
"""
Stage 2 - AI metadata refresh (title / correspondent / document type / tags).

For every eligible document, sends the existing OCR `content` to Claude Sonnet
and writes back improved metadata DIRECTLY to the document. Choices baked in:

  * Direct write (snapshot to ./snapshots_stage2/ is the rollback).
  * Prefer EXISTING taxonomy; propose NEW entries only when nothing fits, and
    flag any doc that received a newly-created tag/correspondent/type with the
    'ai-new-taxonomy' tag so you can review what was created.
  * Process ALL docs EXCEPT the 35 garbage ones (ocr_quality >= GARBAGE_THRESH)
    and except docs already advanced past triage by Stage 1 OR already done here.
  * Model: claude-sonnet-4-6, via strict tool use for guaranteed structure.

State machine (ai_stage select): documents are advanced to 'metadata_done'.
Eligible = ai_stage is empty OR 'triaged', AND ocr_quality < GARBAGE_THRESH.
(Stage-1 outputs are 'reocr_done'; include them too once you've finished S1 by
 adding 'reocr_done' to ELIGIBLE_STAGES below.)

Usage:
  export PAPERLESS_URL="http://localhost:8000"
  export PAPERLESS_TOKEN="xxxx"
  export ANTHROPIC_API_KEY="sk-ant-..."

  python stage2_metadata.py --dry-run --limit 5     # show suggestions, write nothing
  python stage2_metadata.py --limit 5               # write metadata on 5 docs
  python stage2_metadata.py --max-spend 20.00       # full run, abort past $20
  python stage2_metadata.py                          # full run

  pip install --break-system-packages anthropic requests
"""

import argparse
import os
import sys
import time
import json
import pathlib
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
BASE = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not TOKEN:
    sys.exit("Set PAPERLESS_TOKEN (and PAPERLESS_URL) in the environment.")
if not ANTHROPIC_API_KEY:
    sys.exit("Set ANTHROPIC_API_KEY in the environment.")

MODEL = "claude-sonnet-4-6"
GARBAGE_THRESH = 0.55              # docs at/above this are the 35 we skip
ELIGIBLE_STAGES = {None, "", "triaged"}   # add "reocr_done" after Stage 1 is done
NEW_TAXONOMY_TAG = "ai-new-taxonomy"

FIELD_SCORE = "ocr_quality"
FIELD_STAGE = "ai_stage"
FIELD_NOTES = "ai_notes"
STAGE_DONE = "metadata_done"

# Conservative price guardrail for the spend cap (Sonnet-class, USD/token).
PRICE_IN = 3.0 / 1_000_000
PRICE_OUT = 15.0 / 1_000_000

# Cap how much document text we send (chars). Metadata doesn't need the whole
# doc; the head + tail usually carries title/sender/date/subject.
CONTENT_HEAD = 6000
CONTENT_TAIL = 1500

SNAP_DIR = pathlib.Path("./snapshots_stage2")
SNAP_DIR.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})

_spend_lock = threading.Lock()
_spend_total = 0.0
# Locks for lazy taxonomy creation so two threads don't create the same tag.
_tax_lock = threading.Lock()


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def _request(method, url, **kw):
    delay = 1.0
    for _ in range(6):
        r = SESSION.request(method, url, timeout=90, **kw)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay = min(delay * 2, 30)
            continue
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:400]
            raise requests.HTTPError(f"{r.status_code} {method} {url}\n  {detail}")
        return r
    raise requests.HTTPError(f"retries exhausted {method} {url}")


def get_all(endpoint, fields=None):
    out = []
    url = f"{BASE}/api/{endpoint}/?page_size=200" + (f"&fields={fields}" if fields else "")
    while url:
        data = _request("GET", url).json()
        out.extend(data["results"])
        url = data.get("next")
    return out


# ----------------------------------------------------------------------------
# Taxonomy (names <-> ids) with lazy creation
# ----------------------------------------------------------------------------
class Taxonomy:
    def __init__(self):
        self.tags = {t["name"]: t["id"] for t in get_all("tags", "id,name")}
        self.correspondents = {c["name"]: c["id"] for c in get_all("correspondents", "id,name")}
        self.doc_types = {d["name"]: d["id"] for d in get_all("document_types", "id,name")}
        # case-insensitive lookup helpers
        self._tags_ci = {k.lower(): v for k, v in self.tags.items()}
        self._corr_ci = {k.lower(): v for k, v in self.correspondents.items()}
        self._type_ci = {k.lower(): v for k, v in self.doc_types.items()}

    def existing_lists(self):
        return (sorted(self.tags), sorted(self.correspondents), sorted(self.doc_types))

    def _resolve_or_create(self, name, ci_map, name_map, endpoint, created_flag):
        if not name:
            return None, False
        key = name.strip().lower()
        if key in ci_map:
            return ci_map[key], False
        # create new
        with _tax_lock:
            if key in ci_map:                     # re-check inside lock
                return ci_map[key], False
            payload = {"name": name.strip()}
            if endpoint == "tags":
                payload["color"] = "#3b82f6"
            r = _request("POST", f"{BASE}/api/{endpoint}/", json=payload)
            new_id = r.json()["id"]
            name_map[name.strip()] = new_id
            ci_map[key] = new_id
            return new_id, True

    def tag_id(self, name):
        return self._resolve_or_create(name, self._tags_ci, self.tags, "tags", True)

    def correspondent_id(self, name):
        return self._resolve_or_create(name, self._corr_ci, self.correspondents, "correspondents", True)

    def doc_type_id(self, name):
        return self._resolve_or_create(name, self._type_ci, self.doc_types, "document_types", True)


# ----------------------------------------------------------------------------
# Custom fields
# ----------------------------------------------------------------------------
def get_field_map():
    fields = {}
    for f in get_all("custom_fields"):
        entry = {"id": f["id"], "data_type": f["data_type"]}
        if f["data_type"] == "select":
            opts = (f.get("extra_data") or {}).get("select_options") or []
            entry["options"] = {o["label"]: o["id"] for o in opts}
        fields[f["name"]] = entry
    for n in (FIELD_SCORE, FIELD_STAGE, FIELD_NOTES):
        if n not in fields:
            sys.exit(f"Missing custom field '{n}'. Run Stage 0 setup first.")
    return fields


def stage_option_id(fmap, label):
    f = fmap[FIELD_STAGE]
    if f["data_type"] == "select":
        if label not in f["options"]:
            sys.exit(f"ai_stage missing option '{label}'. Options: {list(f['options'])}")
        return f["options"][label]
    return label


def get_or_create_tag(name, color="#a0a0a0"):
    data = _request("GET", f"{BASE}/api/tags/?name__iexact={name}").json()
    if data["results"]:
        return data["results"][0]["id"]
    return _request("POST", f"{BASE}/api/tags/", json={"name": name, "color": color}).json()["id"]


# ----------------------------------------------------------------------------
# Eligibility / queue
# ----------------------------------------------------------------------------
def eligible(doc, fmap):
    cf = {c["field"]: c.get("value") for c in (doc.get("custom_fields") or [])}
    stage_val = cf.get(fmap[FIELD_STAGE]["id"])
    # map a select option id back to its label for comparison
    if fmap[FIELD_STAGE]["data_type"] == "select":
        id_to_label = {v: k for k, v in fmap[FIELD_STAGE]["options"].items()}
        stage_label = id_to_label.get(stage_val)
    else:
        stage_label = stage_val
    if stage_label not in ELIGIBLE_STAGES:
        return False
    score = cf.get(fmap[FIELD_SCORE]["id"])
    try:
        if score is not None and float(score) >= GARBAGE_THRESH:
            return False   # one of the 35 garbage docs - skip
    except (TypeError, ValueError):
        pass
    return True


def fetch_queue(fmap, limit):
    docs = []
    url = (f"{BASE}/api/documents/?fields=id,title,content,correspondent,"
           f"document_type,tags,custom_fields&page_size=100")
    while url:
        data = _request("GET", url).json()
        for d in data["results"]:
            if eligible(d, fmap):
                docs.append(d)
                if limit and len(docs) >= limit:
                    return docs
        url = data.get("next")
    return docs


def snapshot(doc):
    p = SNAP_DIR / f"{doc['id']}.json"
    if not p.exists():
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------------------
# Claude metadata extraction (strict tool use)
# ----------------------------------------------------------------------------
METADATA_TOOL = {
    "name": "document_metadata",
    "description": "Return structured metadata for a document.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "Concise, human-readable title (<= 12 words). No file extension."},
            "correspondent": {"type": "string",
                              "description": "The organization or person the document is from/to. Empty string if unclear."},
            "document_type": {"type": "string",
                              "description": "e.g. Invoice, Statement, Letter, Receipt, Contract, Report. Empty if unclear."},
            "tags": {"type": "array", "items": {"type": "string"},
                     "description": "1-5 topical tags."},
            "correspondent_is_new": {"type": "boolean",
                                     "description": "true if correspondent is NOT in the provided existing list."},
            "document_type_is_new": {"type": "boolean",
                                     "description": "true if document_type is NOT in the provided existing list."},
            "new_tags": {"type": "array", "items": {"type": "string"},
                        "description": "Subset of tags that are NOT in the provided existing list."},
        },
        "required": ["title", "correspondent", "document_type", "tags",
                     "correspondent_is_new", "document_type_is_new", "new_tags"],
    },
}


def build_prompt(doc, existing_tags, existing_corr, existing_types):
    content = doc.get("content") or ""
    if len(content) > CONTENT_HEAD + CONTENT_TAIL:
        content = content[:CONTENT_HEAD] + "\n...\n" + content[-CONTENT_TAIL:]
    return (
        "You are classifying a scanned/OCR'd document for a personal document "
        "management system. Generate metadata for it.\n\n"
        "STRONGLY PREFER reusing entries from these existing lists. Only invent a "
        "new value when none reasonably fits, and when you do, set the matching "
        "*_is_new flag / list so it can be reviewed.\n\n"
        f"EXISTING CORRESPONDENTS:\n{', '.join(existing_corr) or '(none)'}\n\n"
        f"EXISTING DOCUMENT TYPES:\n{', '.join(existing_types) or '(none)'}\n\n"
        f"EXISTING TAGS:\n{', '.join(existing_tags) or '(none)'}\n\n"
        f"CURRENT TITLE: {doc.get('title') or '(none)'}\n\n"
        f"DOCUMENT TEXT:\n{content}"
    )


def claude_metadata(doc, tax):
    global _spend_total
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    etags, ecorr, etypes = tax.existing_lists()
    prompt = build_prompt(doc, etags, ecorr, etypes)

    delay = 2.0
    for _ in range(6):
        try:
            msg = client.messages.create(
                model=MODEL,
                max_tokens=1024,
                tools=[METADATA_TOOL],
                tool_choice={"type": "tool", "name": "document_metadata"},
                messages=[{"role": "user", "content": prompt}],
            )
            result = next(b.input for b in msg.content if b.type == "tool_use")
            it, ot = msg.usage.input_tokens, msg.usage.output_tokens
            cost = it * PRICE_IN + ot * PRICE_OUT
            with _spend_lock:
                _spend_total += cost
            return result, cost
        except StopIteration:
            raise RuntimeError("model did not return tool_use block")
        except Exception as e:
            if "rate" in str(e).lower() or "overload" in str(e).lower() or "529" in str(e):
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
    raise RuntimeError("Claude metadata failed after retries")


# ----------------------------------------------------------------------------
# Write-back
# ----------------------------------------------------------------------------
def apply_metadata(doc, meta, tax, fmap, new_tax_tag_id):
    created_anything = False
    body = {}

    title = (meta.get("title") or "").strip()
    if title:
        body["title"] = title

    corr_name = (meta.get("correspondent") or "").strip()
    if corr_name:
        cid, created = tax.correspondent_id(corr_name)
        created_anything |= created
        if cid:
            body["correspondent"] = cid

    type_name = (meta.get("document_type") or "").strip()
    if type_name:
        tid, created = tax.doc_type_id(type_name)
        created_anything |= created
        if tid:
            body["document_type"] = tid

    # tags: merge AI tags with existing tags on the doc (don't wipe human tags)
    tag_ids = set(doc.get("tags") or [])
    for tname in (meta.get("tags") or []):
        gid, created = tax.tag_id(tname)
        created_anything |= created
        if gid:
            tag_ids.add(gid)
    if created_anything:
        tag_ids.add(new_tax_tag_id)
    body["tags"] = sorted(tag_ids)

    # advance stage + note, preserving other custom fields
    keep = [cf for cf in (doc.get("custom_fields") or [])
            if cf["field"] != fmap[FIELD_STAGE]["id"]]
    keep.append({"field": fmap[FIELD_STAGE]["id"],
                 "value": stage_option_id(fmap, STAGE_DONE)})
    body["custom_fields"] = keep

    _request("PATCH", f"{BASE}/api/documents/{doc['id']}/", json=body)
    return created_anything


# ----------------------------------------------------------------------------
# Per-doc
# ----------------------------------------------------------------------------
def process_one(doc, tax, fmap, new_tax_tag_id, args):
    if args.max_spend:
        with _spend_lock:
            if _spend_total >= args.max_spend:
                return ("spend_cap", doc["id"], "skipped (cap)", 0.0)

    snapshot(doc)
    meta, cost = claude_metadata(doc, tax)

    if args.dry_run:
        new_bits = []
        if meta.get("correspondent_is_new"):
            new_bits.append(f"NEW corr={meta['correspondent']}")
        if meta.get("document_type_is_new"):
            new_bits.append(f"NEW type={meta['document_type']}")
        if meta.get("new_tags"):
            new_bits.append(f"NEW tags={meta['new_tags']}")
        flag = ("  [" + "; ".join(new_bits) + "]") if new_bits else ""
        return ("dry", doc["id"],
                f"title='{meta.get('title')}' corr='{meta.get('correspondent')}' "
                f"type='{meta.get('document_type')}' tags={meta.get('tags')}{flag}",
                cost)

    created = apply_metadata(doc, meta, tax, fmap, new_tax_tag_id)
    return ("done" + ("_new_tax" if created else ""), doc["id"],
            f"title='{meta.get('title')}'", cost)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-spend", type=float, default=0.0)
    args = ap.parse_args()

    fmap = get_field_map()
    tax = Taxonomy()
    new_tax_tag_id = get_or_create_tag(NEW_TAXONOMY_TAG)
    queue = fetch_queue(fmap, args.limit)

    print(f"Eligible queue: {len(queue)} document(s)  "
          f"(excludes ocr_quality >= {GARBAGE_THRESH} and already-done)")
    print(f"Existing taxonomy: {len(tax.tags)} tags, "
          f"{len(tax.correspondents)} correspondents, {len(tax.doc_types)} types")
    print(f"Mode: {'DRY-RUN' if args.dry_run else 'WRITE'}  model={MODEL}  "
          f"workers={args.workers}  "
          f"spend_cap={'$'+format(args.max_spend,'.2f') if args.max_spend else 'none'}\n")
    if not queue:
        print("Nothing to do.")
        return

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, d, tax, fmap, new_tax_tag_id, args): d for d in queue}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                status, doc_id, msg, cost = fut.result()
            except Exception as e:
                status, doc_id, msg, cost = ("ERROR", d["id"], str(e), 0.0)
            results.append(status)
            print(f"[{status}] doc {doc_id}: {msg}  (${cost:.4f})")

    print("\n--- summary ---")
    for k, v in Counter(results).items():
        print(f"{k}: {v}")
    print(f"approx spend: ${_spend_total:.2f}")
    print(f"snapshots: {SNAP_DIR.resolve()}")
    if not args.dry_run:
        print(f"\nReview newly-created taxonomy: filter tag:{NEW_TAXONOMY_TAG} in the UI.")


if __name__ == "__main__":
    main()
