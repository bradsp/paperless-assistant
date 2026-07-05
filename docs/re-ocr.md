# AI re-OCR of garbage scans

Re-OCR re-transcribes genuinely bad scans with a **vision model**, builds a corrected
PDF, re-files it carrying the original metadata, and tags the old original
`superseded`. It is **off by default** — enable it deliberately.

## Why it's off by default

- **It costs money.** Re-OCR uses a vision model on the document image, which is more
  expensive than the free local triage heuristic and the (cheap) metadata step.
- **It rewrites documents.** It replaces the OCR text and re-consumes the document, so
  it is a genuine change to your library (though the original is snapshotted and only
  tagged, never deleted).

Because of both, re-OCR only runs when you explicitly turn it on.

## See what would be re-OCR'd first

Triage scores every document's existing OCR into the `ocr_quality` custom field
(`0.0` = clean … `1.0` = garbage). In the Paperless UI, filter `ocr_quality >= 0.55` to
see the scans that would benefit. Those are your re-OCR candidates.

## Try it on a couple of docs (no changes)

Run a bounded dry-run — it transcribes and builds corrected PDFs locally but **does not
consume or modify Paperless**, and caps spend:

```bash
docker compose exec paperless-assistant pa reocr --dry-run --limit 2 --max-spend 5.00
```

Inspect the built PDFs and the report before committing.

## Turn it on

Enable re-OCR in scheduled sweeps by setting the environment variable (or
`reocr_enabled: true` in `/data/config.yml`):

```dotenv
PA_REOCR_ENABLED=true
```

Or enable it for a single run: `pa run --reocr` / `pa serve --reocr`.

When enabled, re-OCR:

1. **Snapshots** the original document's metadata (I2).
2. Builds a corrected PDF (an invisible-text overlay carrying the new transcription).
3. Re-consumes it, carrying the original metadata forward, advancing `ai_stage` to
   `reocr_done`.
4. Tags the **old** original `superseded` — **never deletes it**.

## The superseded / never-auto-delete workflow

Originals that re-OCR replaced are tagged `superseded` and left in place (invariant I4).
Review them, then bulk-delete when satisfied:

- In the Paperless UI, filter `tag:superseded`.
- Confirm the re-OCR'd replacements look right.
- Bulk-delete the old originals **manually**. The assistant will never do this for you.

If you want to undo a re-OCR, the pre-write metadata snapshot is under
`pa-data/snapshots/`.

## Choosing a vision model

Re-OCR **requires a vision-capable model** — the engine refuses a text-only model with
a clear error before any download or consume (no silent downgrade). Defaults and
options:

- **Anthropic** (default): `claude-opus-4-8` — vision-capable out of the box.
- **OpenAI**: a vision model such as `gpt-4o` / `gpt-4o-mini` / `gpt-4.1` / `gpt-4-turbo`.
- **Ollama** (local, zero egress): a **llava-class** model (`llava`, `bakllava`,
  `llama3.2-vision`, `minicpm-v`, `moondream`). Pull it first with `ollama pull llava`.

Set the model per task with `PA_OCR_PROVIDER` / `PA_OCR_MODEL`, or pick it in the
[dashboard](dashboard.md). Full provider setup is in [AI providers](ai-providers.md).

---

**Next step:** [AI providers](ai-providers.md) to configure a vision model, or
[Troubleshooting](troubleshooting.md) if a run stops on spend or credits.
