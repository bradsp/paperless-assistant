# Contributing to Paperless Assistant

Thanks for your interest in improving Paperless Assistant. This is a tool that holds
a Paperless token and can write to a user's document library, so contributions are
held to a high safety bar — but the dev loop is fast and **fully offline**.

## Development environment

Requires **Python 3.10+**.

```bash
python -m venv .venv
. .venv/Scripts/activate        # Windows; use .venv/bin/activate on POSIX
pip install -e ".[test]"        # core (Anthropic) path + test deps
```

Optional provider extras — only if you're working on those adapters:

```bash
pip install -e ".[openai]"      # OpenAI adapter (installs the `openai` SDK)
pip install -e ".[ollama]"      # Ollama adapter (installs `httpx`)
```

The OpenAI and Ollama adapters import-guard their clients, so the core still runs
and tests still pass without either extra installed.

## Running the tests

The suite is **fully offline**: it needs **no live Paperless, no real API keys, and
no Ollama server**. The Paperless HTTP surface is mocked and every provider client is
stubbed, so tests are deterministic and free.

```bash
pytest -q
```

On Windows you can run it through the venv directly:

```bash
.\.venv\Scripts\python.exe -m pytest -q
```

**Every change must keep the suite green.** If you change behavior, update or add the
tests that pin it.

## Preserve the safety invariants

The engine's correctness rests on seven invariants. A change that weakens any of them
will not be accepted without an extremely good reason and matching tests:

- **I1 — Idempotency / resumability.** Re-runs skip already-done docs unless `--force`.
- **I2 — Snapshot before write.** Original state is captured once before any mutation.
- **I3 — Spend cap as a hard abort.** A thread-safe USD ceiling gates new work.
- **I4 — Human review gate.** Nothing destructive without a gate (`superseded` /
  `ai-new-taxonomy`); originals are never deleted.
- **I5 — Taxonomy reuse-first.** Prefer existing tags/correspondents/types; flag
  anything new.
- **I6 — Surface the real error.** Show what Paperless actually rejected; don't swallow it.
- **I7 — Safe by default.** Dry-run is first-class; conservative concurrency; retry/backoff.

The characterization tests (`tests/test_characterization_*.py`) pin these behaviors
against the original POC scripts and re-assert them against the package — treat them
as the safety contract.

## Coding conventions

- **Match the surrounding style.** This codebase favors small, single-responsibility
  modules and explicit, readable code over cleverness. Follow what's already there.
- **Secrets come from the environment only.** Never read a Paperless token, AI key, or
  agent credential from a config file, and never commit one. `pa` refuses to load a
  config file containing a secret-looking key — keep it that way.
- **No new inbound network exposure** in the local agent (Modes A/B) without discussion.
- Keep provider SDK imports guarded so the core runs without optional extras installed.

## Pull request flow

1. Fork and branch from `main`.
2. Make your change with tests; run `pytest -q` and confirm it's green.
3. Update the relevant docs under `docs/` if behavior or config changed.
4. Open a PR and fill in the [pull request template](.github/PULL_REQUEST_TEMPLATE.md)
   — confirm tests pass, docs are updated, invariants are preserved, and no secrets
   are included.

## Reporting security issues

Please do **not** open a public issue for a vulnerability. See
[SECURITY.md](SECURITY.md) for private disclosure.

## License

By contributing, you agree that your contributions are licensed under the project's
**GPL-3.0-or-later** license (see [LICENSE](LICENSE)).
