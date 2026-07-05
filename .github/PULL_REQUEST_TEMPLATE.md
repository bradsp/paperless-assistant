<!--
Thanks for contributing! Please fill in the checklist below. See CONTRIBUTING.md
for the dev environment, the offline test suite, and the safety invariants.
-->

## What does this change?

A short description of the change and why it's needed. Link any related issue
(e.g. `Closes #123`).

## Checklist

- [ ] **Tests pass** — `pytest -q` is green (the suite runs fully offline; no live
      Paperless, real keys, or Ollama needed).
- [ ] **Tests added/updated** for any behavior change.
- [ ] **Docs updated** under `docs/` (and the README if user-facing) where behavior
      or configuration changed.
- [ ] **Safety invariants preserved** — idempotency (I1), snapshot-before-write (I2),
      spend caps (I3), human-review gates (I4), taxonomy-reuse-first (I5),
      surface-real-errors (I6), safe-by-default (I7). If any are affected, explain below.
- [ ] **No secrets** — no Paperless tokens, AI keys, agent credentials, real
      hostnames, or personal data in the diff. Secrets stay environment-only.
- [ ] **Provider SDK imports stay guarded** so the core runs without optional extras.

## Notes for reviewers

Anything reviewers should focus on, trade-offs made, or follow-ups.
