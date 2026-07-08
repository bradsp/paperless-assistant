# AI providers — getting and configuring API access

Paperless Assistant is **bring-your-own-key**: you supply access to an AI provider,
and the assistant uses it. This page tells you, for each supported provider, **where
to go, how to get access, and which environment variable wires it in**.

Three providers are supported, chosen **per task** (metadata vs. re-OCR):

| Provider | Env credentials / endpoint | Vision (re-OCR) | Notes |
|----------|----------------------------|-----------------|-------|
| **`anthropic`** *(default)* | `ANTHROPIC_API_KEY` | Yes | Forced tool-use structured output. |
| **`openai`** | `OPENAI_API_KEY`, optional `OPENAI_BASE_URL` | Vision models only | Strict JSON-schema output. |
| **`ollama`** | `PA_OLLAMA_ENDPOINT` (default `http://localhost:11434`, the **root** — not a `/v1`/`/api` path) | llava-class only | **Local, zero-egress, no key.** Re-OCR needs the `[ollama]` extra (rasterizes PDFs). |

> **Secrets are environment-only.** Provider keys go in your `.env` (beside
> `docker-compose.yml`), never in a YAML config — the assistant refuses to load a
> config file containing a key. See [Configuration](configuration.md#secrets-are-environment-only).

---

## Anthropic (Claude) — the default

Claude is the default provider for **both** tasks, so a fresh install with an Anthropic
key works out of the box.

**How to get a key:**

1. Sign up / sign in at the **Anthropic Console**: <https://console.anthropic.com/>
2. Add billing and credits under **Settings → Billing** — a key with no credit can't
   make calls, and runs will stop with an "out of credits / over quota" error.
3. Create a key under **Settings → API Keys** → *Create Key*. Copy it (it looks like
   `sk-ant-...`; it is shown once).

**Wire it in** — add to your `.env`:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
```

**Default models:** metadata `claude-sonnet-4-6` (cheap/fast for classification),
re-OCR `claude-opus-4-8` (a strong vision model). Override with `PA_METADATA_MODEL` /
`PA_OCR_MODEL` if you prefer.

Claude models support both vision and structured output, so they work for either task
with no extra configuration.

---

## OpenAI

**How to get a key:**

1. Sign up / sign in at the **OpenAI Platform**: <https://platform.openai.com/>
2. Add billing and set usage limits under **Settings → Billing** (and the
   *Limits / Usage* page) — this is your safety net on OpenAI's side, in addition to
   the assistant's own [spend caps](#cost-awareness).
3. Create a key on the **API keys** page (**View API keys** → *Create new secret key*).
   Copy it (it looks like `sk-...`; shown once).

**Wire it in** — add to your `.env` and select the provider:

```dotenv
OPENAI_API_KEY=sk-...
```

```yaml
    environment:
      PA_METADATA_PROVIDER: openai
      PA_METADATA_MODEL: gpt-4o-mini
```

**Optional custom endpoint.** To use an OpenAI-compatible or proxy endpoint, set
`OPENAI_BASE_URL` (env only):

```dotenv
OPENAI_BASE_URL=https://api.example.com/v1
```

**Re-OCR needs a vision-capable model.** If you point the re-OCR task at OpenAI, pick a
vision model (e.g. `gpt-4o`, `gpt-4o-mini`, `gpt-4.1`, `gpt-4-turbo`). A model the
adapter doesn't recognize as vision-capable is **refused** for re-OCR at run time — it
never silently downgrades. Text-only models are fine for the metadata task.

---

## Ollama (local — no key, zero cloud egress)

Ollama runs models **on your own hardware**. No API key, no billing, and **nothing
leaves your machine** — this is the zero-egress privacy floor.

**How to set it up:**

1. Install Ollama from <https://ollama.com/> (or run the official `ollama/ollama`
   Docker image as a service in your compose stack).
2. Pull a model — it must be **pulled on the Ollama host** before use, or calls fail
   with `model "…" not found, try pulling it first`:

   ```bash
   ollama pull llama3.1                 # good for metadata (text classification)
   ollama pull llava                    # a VISION model, required for re-OCR
   ```

3. **Wire it in.** Ollama needs no key — just point the assistant at the endpoint
   **root**. In Docker, run an `ollama` service and target it by name:

   ```yaml
       environment:
         PA_METADATA_PROVIDER: ollama
         PA_METADATA_MODEL: llama3.1
         PA_OCR_PROVIDER: ollama
         PA_OCR_MODEL: llava              # vision model for re-OCR
         PA_OLLAMA_ENDPOINT: http://ollama:11434
   ```

   The default endpoint is `http://localhost:11434`; in Docker use the service name
   (e.g. `http://ollama:11434`).

> **Point `PA_OLLAMA_ENDPOINT` at the Ollama ROOT.** The assistant speaks Ollama's
> **native** API (`/api/generate`). Set the endpoint to the server root
> (`http://host:11434`), **not** an OpenAI-compatible `/v1` base or an `/api` path —
> those build a wrong URL and the server answers **405 Method Not Allowed** (the
> assistant now says exactly this). A trailing `/v1` or `/api` is auto-stripped, but
> the root form is the supported contract.

**Re-OCR rasterizes PDFs — install the `[ollama]` extra.** Ollama's vision endpoint
takes **raster images** (PNG/JPEG), not PDF containers, so for re-OCR the assistant
renders each PDF page to a PNG before the call. This uses `pypdfium2` (pure-pip
prebuilt wheels, **no system dependencies** — no poppler/ghostscript). The official
Docker image bundles it; for a local install add the extra:

```bash
pip install paperless-assistant[ollama]     # httpx + pypdfium2
```

Metadata (text-only) does **not** need the renderer.

**Re-OCR needs a llava-class vision model.** The adapter recognizes vision models by
name (`llava`, `bakllava`, `llama3.2-vision`, `minicpm-v`, `moondream`, or a name
containing `vision`). A non-vision model selected for re-OCR is refused at run time.

**Check it before a run.** `pa doctor` verifies the endpoint shape and that the
renderer is installed; add `pa doctor --probe-ollama` for a free, best-effort live
check that the endpoint is reachable and the configured model is actually pulled (no
inference, no spend).

For maximum privacy, use Ollama for **both** tasks — no document data ever leaves your
box.

---

## Per-task selection (metadata vs. re-OCR)

"What task" is code; **"which provider/model" is configuration**, chosen independently
per task. Resolution is **CLI flag > environment variable > default (Anthropic)**.

You can mix providers — for example, cheap/local metadata with Ollama, keeping a strong
cloud vision model for the (rarer) re-OCR task:

```yaml
    environment:
      PA_METADATA_PROVIDER: ollama
      PA_METADATA_MODEL: llama3.1
      PA_OLLAMA_ENDPOINT: http://ollama:11434
      PA_OCR_PROVIDER: anthropic
      PA_OCR_MODEL: claude-opus-4-8
```

Three ways to select, in increasing precedence:

- **YAML** — `metadata.provider` / `metadata.model` and `ocr.provider` / `ocr.model`
  in `/data/config.yml`.
- **Environment** — `PA_METADATA_PROVIDER` / `PA_METADATA_MODEL` /
  `PA_OCR_PROVIDER` / `PA_OCR_MODEL` (env beats YAML).
- **CLI flags** — `pa metadata --provider … --model …`, `pa reocr --provider … --model …`.
- **Dashboard** — the [web dashboard's](dashboard.md) *Settings → Models* panel lets
  you pick a provider + model per task with a pricing hint and a vision flag. Fields
  set by an environment variable show as **env-locked**.

---

## Cost awareness

The assistant is built so you can't be surprised by a bill:

- **Spend caps are the safety net.** Low, non-zero per-run (`PA_SPEND_PER_RUN`,
  default `$1.00`) and per-period (`PA_SPEND_PER_PERIOD`, default `$5.00`) USD caps
  **hard-abort** new work before it runs up a bill (invariant I3). Raise them
  deliberately.
- **Dry-run first.** The first run reports what it *would* spend without spending it.
- **Pricing hints.** The dashboard's model picker shows a per-model USD-per-1K-tokens
  hint from a built-in pricing table, so you can see the relative cost of a choice. A
  custom/uncatalogued model id runs without a hint — the spend cap still applies.
- **Ollama is free** at inference time (you pay only in local compute).

Providers don't expose a queryable account balance over their API, so if you run out
of credits the assistant surfaces the provider's own error text and stops the run (it
does not burn through the batch). See [troubleshooting](troubleshooting.md).

---

## The schema-revalidation guarantee

Whatever model you choose — even a weak local one — **the engine owns the metadata JSON
Schema and re-validates every structured result against it after every provider call.**
A malformed or invalid response is **retried**, and if it still doesn't validate it
**errors** — it is **never written** to Paperless. A custom prompt can change output
*quality* but can never corrupt your library.

---

## Vision requirement for re-OCR (no silent downgrade)

Re-OCR transcribes a scanned image, so it **requires a vision-capable model**. If you
select a non-vision model for the re-OCR task, the engine **refuses** it with a clear
error before any download or consume — it never silently downgrades to a text-only
path. The dashboard also warns inline when a selected/typed model isn't known to
support vision.

---

## Privacy tradeoff (honest)

- **BYO-key + local Ollama = zero cloud egress.** Nothing about your documents leaves
  your machine. This is the recommended privacy floor.
- **BYO-key + a cloud provider (Anthropic / OpenAI).** Document contents are sent to
  that provider for the model call, under **your own account** and their data policy —
  no third party is in the path.
- **Hosted inference** (an advanced [Mode B](advanced-modes.md) option) routes AI calls
  through a vendor proxy; contents transit the proxy for the call only and are not
  persisted server-side. It is **off by default** — BYO/local stays the floor.

---

## Getting-your-key checklist

- **Anthropic:** <https://console.anthropic.com/> → add billing/credits → *Settings →
  API Keys* → `ANTHROPIC_API_KEY=sk-ant-...`
- **OpenAI:** <https://platform.openai.com/> → add billing + usage limits → *API keys*
  → `OPENAI_API_KEY=sk-...` (optional `OPENAI_BASE_URL`); use a **vision** model for
  re-OCR.
- **Ollama:** install from <https://ollama.com/> → `ollama pull <model>` → point
  `PA_OLLAMA_ENDPOINT` at it (e.g. `http://ollama:11434`); use a llava-class model for
  re-OCR. No key.

**Common errors** (out of credits, rate-limit, missing key) and their fixes are in
[troubleshooting](troubleshooting.md).

---

**Next step:** [Re-OCR](re-ocr.md) if you want to enable vision re-OCR, or
[Configuration](configuration.md) for the full tunable surface.
