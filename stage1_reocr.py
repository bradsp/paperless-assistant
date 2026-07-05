#!/usr/bin/env python3
"""
Stage 1 - Claude vision re-OCR for genuinely-garbage scans (Option B).

Reads the queue produced by Stage 0 (documents where ai_stage == 'triaged'
AND ocr_quality >= threshold), and for each document:

  1. Downloads the ORIGINAL file from Paperless.
  2. Snapshots ALL of its metadata (title, tags, correspondent, type, dates,
     ASN, custom fields) to ./snapshots_stage1/ for safe transfer + rollback.
  3. Sends it to Claude vision (claude-opus-4-8) for a faithful transcription,
     with bounded concurrency, retry/backoff, and a HARD spend ceiling.
  4. Builds a new PDF: the original visual pages with Claude's clean text
     added as an INVISIBLE text overlay (pure-python: pypdf + reportlab;
     no OCRmyPDF, no system deps). For image-only scans this makes Claude's
     text the only extractable text, which is what Paperless indexes.
  5. Re-consumes that PDF via POST /api/documents/post_document/, carrying the
     original title/correspondent/type/tags/ASN/created so the new document
     lands fully classified.
  6. After the new doc is created, re-applies custom fields (post_document
     can't always set them) and copies the ocr_quality/notes forward, sets
     ai_stage = 'reocr_done' on the NEW document.
  7. Tags the OLD document with 'superseded' (a safety hold) and ALSO sets its
     ai_stage = 'reocr_done' so it drops out of the queue. The old doc is NOT
     deleted - you review the 'superseded' set in the UI and bulk-delete when
     satisfied.

Nothing is deleted by this script. Deletion is left entirely to you.

Usage:
  export PAPERLESS_URL="http://localhost:8000"     # or your https URL
  export PAPERLESS_TOKEN="xxxx"
  export ANTHROPIC_API_KEY="sk-ant-..."

  python stage1_reocr.py --dry-run --limit 2     # OCR + build PDFs, DO NOT consume
  python stage1_reocr.py --limit 2               # full flow on 2 docs (test!)
  python stage1_reocr.py --max-spend 5.00        # process queue, abort if > $5
  python stage1_reocr.py                          # full run on all flagged docs

Prerequisites:
  - Custom fields from Stage 0 exist: ocr_quality, ai_stage(select), ai_notes
  - The ai_stage select has options: triaged, reocr_done, metadata_done
  - A tag named 'superseded' exists (script creates it if missing)
  - pip install --break-system-packages anthropic pypdf reportlab pillow requests
  - Consume folder reachable; this script uploads via the API (post_document),
    so it does NOT need direct filesystem access to the consume dir.
"""

import argparse
import base64
import io
import os
import sys
import time
import json
import pathlib
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ----------------------------------------------------------------------------
# Config / constants
# ----------------------------------------------------------------------------
BASE = os.environ.get("PAPERLESS_URL", "http://localhost:8000").rstrip("/")
TOKEN = os.environ.get("PAPERLESS_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not TOKEN:
    sys.exit("Set PAPERLESS_TOKEN (and PAPERLESS_URL) in the environment.")
if not ANTHROPIC_API_KEY:
    sys.exit("Set ANTHROPIC_API_KEY in the environment.")

OCR_MODEL = "claude-opus-4-8"   # best accuracy for the worst scans
MAX_OCR_TOKENS = 8000

# Rough price guardrail (USD per token) for the spend cap. These are
# deliberately conservative placeholders; verify against current pricing.
# The cap is a SAFETY ABORT, not an accounting tool.
PRICE_IN_PER_TOK = 15.0 / 1_000_000     # ~$ per input token (opus-class)
PRICE_OUT_PER_TOK = 75.0 / 1_000_000    # ~$ per output token (opus-class)

SUPERSEDED_TAG = "superseded"
FIELD_SCORE = "ocr_quality"
FIELD_STAGE = "ai_stage"
FIELD_NOTES = "ai_notes"
STAGE_TRIAGED = "triaged"
STAGE_REOCR_DONE = "reocr_done"

SNAP_DIR = pathlib.Path("./snapshots_stage1")
SNAP_DIR.mkdir(exist_ok=True)
BUILT_DIR = pathlib.Path("./built_pdfs")
BUILT_DIR.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({"Authorization": f"Token {TOKEN}", "Accept": "application/json"})

# Thread-safe spend accumulator
_spend_lock = threading.Lock()
_spend_total = 0.0


# ----------------------------------------------------------------------------
# HTTP helpers with retry/backoff
# ----------------------------------------------------------------------------
def _request(method, url, **kw):
    delay = 1.0
    for _ in range(6):
        r = SESSION.request(method, url, timeout=120, **kw)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(float(r.headers.get("Retry-After", delay)))
            delay = min(delay * 2, 30)
            continue
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text[:500]
            raise requests.HTTPError(f"{r.status_code} on {method} {url}\n  server says: {detail}")
        return r
    raise requests.HTTPError(f"exhausted retries on {method} {url}")


# ----------------------------------------------------------------------------
# Field / tag resolution
# ----------------------------------------------------------------------------
def get_field_map():
    fields = {}
    url = f"{BASE}/api/custom_fields/?page_size=200"
    while url:
        data = _request("GET", url).json()
        for f in data["results"]:
            entry = {"id": f["id"], "data_type": f["data_type"]}
            if f["data_type"] == "select":
                opts = (f.get("extra_data") or {}).get("select_options") or []
                entry["options"] = {o["label"]: o["id"] for o in opts}
            fields[f["name"]] = entry
        url = data.get("next")
    for n in (FIELD_SCORE, FIELD_STAGE, FIELD_NOTES):
        if n not in fields:
            sys.exit(f"Missing custom field '{n}'. Run Stage 0 setup first.")
    return fields


def stage_option_id(fmap, label):
    f = fmap[FIELD_STAGE]
    if f["data_type"] == "select":
        if label not in f["options"]:
            sys.exit(f"ai_stage has no option '{label}'. Options: {list(f['options'])}")
        return f["options"][label]
    return label


def get_or_create_superseded_tag():
    url = f"{BASE}/api/tags/?name__iexact={SUPERSEDED_TAG}"
    data = _request("GET", url).json()
    if data["results"]:
        return data["results"][0]["id"]
    # create it
    r = _request("POST", f"{BASE}/api/tags/",
                 json={"name": SUPERSEDED_TAG, "color": "#a0a0a0"})
    return r.json()["id"]


# ----------------------------------------------------------------------------
# Queue
# ----------------------------------------------------------------------------
def fetch_queue(fmap, threshold, limit):
    """Documents with ai_stage == triaged AND ocr_quality >= threshold.

    Filters client-side rather than via custom_field_query: the server-side
    query grammar for combining conditions is version-sensitive, and at this
    library size pulling all docs and filtering in Python is reliable and fast.
    """
    triaged_id = stage_option_id(fmap, STAGE_TRIAGED)
    score_fid = fmap[FIELD_SCORE]["id"]
    stage_fid = fmap[FIELD_STAGE]["id"]

    def matches(doc):
        cf = {c["field"]: c.get("value") for c in (doc.get("custom_fields") or [])}
        if cf.get(stage_fid) != triaged_id:
            return False
        score = cf.get(score_fid)
        try:
            return score is not None and float(score) >= threshold
        except (TypeError, ValueError):
            return False

    docs = []
    url = (f"{BASE}/api/documents/?fields=id,title,correspondent,document_type,"
           f"tags,created,archive_serial_number,custom_fields&page_size=100")
    while url:
        data = _request("GET", url).json()
        for d in data["results"]:
            if matches(d):
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
# Download original
# ----------------------------------------------------------------------------
def download_original(doc_id):
    # original=true ensures we get the source file, not the archived render
    r = _request("GET", f"{BASE}/api/documents/{doc_id}/download/?original=true")
    return r.content


# ----------------------------------------------------------------------------
# Claude vision OCR
# ----------------------------------------------------------------------------
OCR_PROMPT = (
    "You are an OCR engine. Transcribe ALL text from this document exactly as "
    "it appears, preserving reading order, line breaks, numbers, dates, and "
    "punctuation. Do not summarize, interpret, translate, or add commentary. "
    "If a region is illegible, write [illegible]. Output only the transcribed text."
)


def claude_ocr(pdf_bytes):
    """Send the PDF to Claude vision; return (text, in_tokens, out_tokens)."""
    global _spend_total
    from anthropic import Anthropic
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(pdf_bytes).decode("utf-8")

    delay = 2.0
    for _ in range(6):
        try:
            msg = client.messages.create(
                model=OCR_MODEL,
                max_tokens=MAX_OCR_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "document",
                         "source": {"type": "base64",
                                    "media_type": "application/pdf",
                                    "data": b64}},
                        {"type": "text", "text": OCR_PROMPT},
                    ],
                }],
            )
            text = "".join(b.text for b in msg.content if b.type == "text")
            it, ot = msg.usage.input_tokens, msg.usage.output_tokens
            cost = it * PRICE_IN_PER_TOK + ot * PRICE_OUT_PER_TOK
            with _spend_lock:
                _spend_total += cost
            return text, it, ot, cost
        except Exception as e:
            # crude rate-limit / transient handling
            if "rate" in str(e).lower() or "overload" in str(e).lower():
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
    raise RuntimeError("Claude OCR failed after retries")


# ----------------------------------------------------------------------------
# Build corrected PDF (invisible text overlay)
# ----------------------------------------------------------------------------
def build_overlay_pdf(original_bytes, text):
    """Return new PDF bytes = original visual pages + invisible text overlay.
    Text is placed on page 1 (Paperless indexes the whole doc's extracted text,
    so single-page placement is sufficient for searchability)."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    reader = PdfReader(io.BytesIO(original_bytes))
    writer = PdfWriter()

    # page size of first page (fallback to letter)
    try:
        box = reader.pages[0].mediabox
        pw, ph = float(box.width), float(box.height)
    except Exception:
        pw, ph = letter

    # Build invisible-text overlay sized to page 1
    ov_buf = io.BytesIO()
    co = canvas.Canvas(ov_buf, pagesize=(pw, ph))
    to = co.beginText(20, ph - 20)
    to.setTextRenderMode(3)              # invisible
    to.setFont("Helvetica", 6)
    wrap_width = max(40, int(pw / 4))    # rough chars per line for the page width
    for raw_line in text.splitlines() or [text]:
        for line in (textwrap.wrap(raw_line, wrap_width) or [""]):
            to.textLine(line)
            # if we run off the bottom, start a new column-ish reset near top
            if to.getY() < 20:
                co.drawText(to)
                co.showPage()
                to = co.beginText(20, ph - 20)
                to.setTextRenderMode(3)
                to.setFont("Helvetica", 6)
    co.drawText(to)
    co.save()
    ov_buf.seek(0)

    ov_reader = PdfReader(ov_buf)
    # Merge overlay page(s) onto original pages; extra overlay pages (if the
    # text was long) get appended as blank-visual text-only pages.
    n_orig = len(reader.pages)
    n_ov = len(ov_reader.pages)
    for i in range(n_orig):
        page = reader.pages[i]
        if i < n_ov:
            page.merge_page(ov_reader.pages[i])
        writer.add_page(page)
    for j in range(n_orig, n_ov):
        writer.add_page(ov_reader.pages[j])

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# ----------------------------------------------------------------------------
# Re-consume + metadata
# ----------------------------------------------------------------------------
def post_document(pdf_bytes, doc, filename):
    """Upload corrected PDF, carrying core metadata. Returns the consume task UUID."""
    data = {}
    if doc.get("title"):
        data["title"] = doc["title"]
    if doc.get("correspondent"):
        data["correspondent"] = str(doc["correspondent"])
    if doc.get("document_type"):
        data["document_type"] = str(doc["document_type"])
    if doc.get("created"):
        data["created"] = doc["created"]
    if doc.get("archive_serial_number"):
        # ASN must be unique; only carry it if you intend to free it from the
        # old doc first. Safer to leave ASN off and re-apply after deletion.
        pass
    files = {"document": (filename, pdf_bytes, "application/pdf")}
    # tags: post_document accepts repeated 'tags' fields by ID
    tag_fields = [("tags", str(t)) for t in (doc.get("tags") or [])]
    # requests needs tuples for repeated keys -> use a list in data via files trick
    r = SESSION.post(f"{BASE}/api/documents/post_document/",
                     files=files,
                     data=list(data.items()) + tag_fields,
                     timeout=180)
    if r.status_code >= 400:
        raise requests.HTTPError(f"post_document {r.status_code}: {r.text[:500]}")
    return r.json()  # task UUID (string)


def find_new_doc_by_task(task_uuid, timeout=180):
    """Poll the tasks endpoint until the consume task finishes; return new doc id."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = _request("GET", f"{BASE}/api/tasks/?task_id={task_uuid}")
        results = r.json()
        if results:
            task = results[0] if isinstance(results, list) else results
            status = task.get("status")
            if status == "SUCCESS":
                # related_document holds the new doc id in recent versions
                doc_id = task.get("related_document") or task.get("result")
                return doc_id
            if status in ("FAILURE", "REVOKED"):
                raise RuntimeError(f"consume task failed: {task}")
        time.sleep(3)
    raise TimeoutError(f"consume task {task_uuid} did not finish in {timeout}s")


def apply_post_consume_metadata(new_doc_id, old_doc, fmap):
    """Re-apply custom fields to the NEW doc: carry ocr_quality + notes,
    set ai_stage=reocr_done. (Tags/title/type/corr already set at consume.)"""
    # carry forward the old doc's ocr_quality + notes values if present
    old_cf = {cf["field"]: cf.get("value") for cf in (old_doc.get("custom_fields") or [])}
    score_val = old_cf.get(fmap[FIELD_SCORE]["id"])
    notes_val = old_cf.get(fmap[FIELD_NOTES]["id"]) or ""
    cf_payload = [
        {"field": fmap[FIELD_STAGE]["id"], "value": stage_option_id(fmap, STAGE_REOCR_DONE)},
        {"field": fmap[FIELD_NOTES]["id"], "value": (f"reocr from doc {old_doc['id']}; " + str(notes_val))[:255]},
    ]
    if score_val is not None:
        cf_payload.append({"field": fmap[FIELD_SCORE]["id"], "value": score_val})
    _request("PATCH", f"{BASE}/api/documents/{new_doc_id}/",
             json={"custom_fields": cf_payload})


def mark_old_superseded(old_doc, fmap, superseded_tag_id):
    """Add 'superseded' tag to the old doc and advance its ai_stage so it
    leaves the queue. Does NOT delete - that's left to you."""
    existing_tags = list(old_doc.get("tags") or [])
    if superseded_tag_id not in existing_tags:
        existing_tags.append(superseded_tag_id)
    # preserve other custom fields, set ai_stage=reocr_done
    keep = [cf for cf in (old_doc.get("custom_fields") or [])
            if cf["field"] != fmap[FIELD_STAGE]["id"]]
    keep.append({"field": fmap[FIELD_STAGE]["id"],
                 "value": stage_option_id(fmap, STAGE_REOCR_DONE)})
    _request("PATCH", f"{BASE}/api/documents/{old_doc['id']}/",
             json={"tags": existing_tags, "custom_fields": keep})


# ----------------------------------------------------------------------------
# Per-document pipeline
# ----------------------------------------------------------------------------
def process_one(doc, fmap, superseded_tag_id, args):
    doc_id = doc["id"]
    snapshot(doc)

    # spend guard BEFORE the API call
    if args.max_spend:
        with _spend_lock:
            if _spend_total >= args.max_spend:
                return ("spend_cap", doc_id, "skipped: spend cap reached", 0.0)

    original = download_original(doc_id)
    text, it, ot, cost = claude_ocr(original)

    if not text.strip():
        return ("empty_ocr", doc_id, "Claude returned no text", cost)

    corrected = build_overlay_pdf(original, text)
    built_path = BUILT_DIR / f"{doc_id}_corrected.pdf"
    built_path.write_bytes(corrected)
    (BUILT_DIR / f"{doc_id}_text.txt").write_text(text)

    if args.dry_run:
        return ("dry", doc_id,
                f"OCR ok ({len(text)} chars, in={it} out={ot}); PDF built, NOT consumed",
                cost)

    # consume the corrected PDF
    filename = f"{(doc.get('title') or ('doc_'+str(doc_id)))}.pdf".replace("/", "-")
    task_uuid = post_document(corrected, doc, filename)
    new_doc_id = find_new_doc_by_task(task_uuid)
    if not new_doc_id:
        return ("no_new_id", doc_id, f"consumed (task {task_uuid}) but new id unknown", cost)

    apply_post_consume_metadata(new_doc_id, doc, fmap)
    mark_old_superseded(doc, fmap, superseded_tag_id)

    return ("done", doc_id, f"new doc {new_doc_id}; old tagged superseded", cost)


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="OCR + build corrected PDFs, but DO NOT consume or modify Paperless")
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3,
                    help="concurrent docs (keep low: <= DB pool and gentle on the API)")
    ap.add_argument("--max-spend", type=float, default=0.0,
                    help="USD ceiling; abort starting new docs once exceeded (0 = no cap)")
    args = ap.parse_args()

    fmap = get_field_map()
    superseded_tag_id = get_or_create_superseded_tag()
    queue = fetch_queue(fmap, args.threshold, args.limit)
    print(f"Queue: {len(queue)} document(s) with ai_stage=triaged and "
          f"{FIELD_SCORE} >= {args.threshold}")
    print(f"Mode: {'DRY-RUN (no consume)' if args.dry_run else 'FULL'}  "
          f"workers={args.workers}  model={OCR_MODEL}  "
          f"spend_cap={'$'+format(args.max_spend,'.2f') if args.max_spend else 'none'}\n")
    if not queue:
        print("Nothing to do.")
        return

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, d, fmap, superseded_tag_id, args): d for d in queue}
        for fut in as_completed(futs):
            d = futs[fut]
            try:
                status, doc_id, msg, cost = fut.result()
            except Exception as e:
                status, doc_id, msg, cost = ("ERROR", d["id"], str(e), 0.0)
            results.append(status)
            print(f"[{status}] doc {doc_id}: {msg}  (${cost:.3f})")

    print("\n--- summary ---")
    from collections import Counter
    for k, v in Counter(results).items():
        print(f"{k}: {v}")
    print(f"approx spend: ${_spend_total:.2f}")
    print(f"\nCorrected PDFs + text in: {BUILT_DIR.resolve()}")
    print(f"Metadata snapshots in:    {SNAP_DIR.resolve()}")
    if not args.dry_run:
        print(f"\nReview the re-OCR'd documents, then in the Paperless UI filter "
              f"tag:{SUPERSEDED_TAG} and bulk-delete the old originals once you're satisfied.")


if __name__ == "__main__":
    main()
