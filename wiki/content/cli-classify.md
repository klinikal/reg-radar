---
title: reg-radar classify
type: cli
sources: [regradar/classify.py]
last-verified: 2026-09-10
---

## Purpose

Unattended batch classifier: pulls unclassified items per taxonomy axis
and runs them through a pluggable LLM provider, validating and writing
each response the same way the interactive `regradar-classifier` skill
does.

## How it works

For each axis (all axes, or one via `--axis`), `run()` fetches unclassified
items with `mcpserver.queries.get_unclassified()`, builds a per-item
prompt with `build_prompt()` (title, body truncated to 4000 chars, the
axis's valid values/aliases, and a cardinality note), calls the chosen
provider's `.classify()`, parses the response with `parse_response()`
(tolerant of a fenced or prose-wrapped JSON object), then validates it
against the taxonomy with `validate_response()`: axis must match the one
requested, must be a known axis, cardinality-one axes reject more than
one value, every value id must be valid, every confidence must be in
0.0-1.0.

A response that fails validation is logged with the reason and skipped,
never written as a guess. An empty `values` list is accepted as "the
model confidently found nothing" and leaves the label unset. A valid
non-empty response is written via `regradar.classification.write_classification()`
with `by=f"llm:{provider}/{model}"`, unless `--dry-run`, which prints
instead of writing.

`RateLimiter` enforces a requests-per-minute ceiling between provider
calls (`sleep_fn`/`clock` injectable for tests). `log_usage()` logs
tokens and an estimated cost from `config/llm_prices.yaml` for metered
providers (anthropic, openai); for unmetered providers (ollama, command)
it logs "unmetered" rather than fabricating a cost. A missing prices file
just means cost logs as "unknown", not an error.

## Inputs

- `--provider` (required): `anthropic`, `openai`, `ollama`, or `command`
  (shells out to any CLI that reads a prompt on stdin and writes text on
  stdout), from `regradar.providers.PROVIDER_DEFAULTS`.
- `--axis`: taxonomy axis to classify (default: every axis).
- `--limit` (default 50): max items to classify per axis this run.
- `--dry-run`: classify and print, write nothing.
- `--model`: override the provider's default model id.
- `--command`: shell command for `--provider command`, e.g. `"claude -p"`.
- `--rpm`: override the provider's default requests-per-minute limit.
- `--db-path` (env `DB_PATH`, default `/app/data/reg_radar.db`).
- `--taxonomy-path`, `--jurisdictions-path` (env-backed defaults).
- `--prices-path` (env `LLM_PRICES_PATH`, default
  `config/llm_prices.yaml`).

## Outputs and side effects

Writes classification rows via `write_classification()` for every
accepted response (skipped in `--dry-run`, which only prints). Logs a
summary line: items written and items skipped, per run. Logs per-call
token usage and estimated cost (or "unmetered").

## How to run or invoke

```
reg-radar classify --provider anthropic [--axis <axis>] [--limit 50] [--dry-run]
reg-radar classify --provider command --command "claude -p"
python -m regradar.classify classify --provider ollama
```

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_classify.py` and run it via pytest from `code/`.

## Boundaries

Must never write a low-confidence guess: a response that fails
`validate_response()` is logged and skipped, not quarantined or written
anyway. Must never fabricate a cost figure for an unmetered provider.
Must send at most one axis per model call (never bundle multiple axes
into one prompt).

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [Taxonomy and Classification](concept-taxonomy-classification.md)
