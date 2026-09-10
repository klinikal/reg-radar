---
title: LLM Prices Config
type: config
sources: [config/llm_prices.yaml]
last-verified: 2026-09-10
---

## Purpose

Price table used by `reg-radar classify` (the batch LLM classifier runner) to log the cost of classification runs against API-metered providers.

## How it works

Nested by provider name (`anthropic`, `openai`), then by model id, with `input_per_million` and `output_per_million` figures in USD per 1,000,000 tokens. The file's own comment warns prices go stale and to check the provider's current pricing page before trusting these for real spend tracking; it carries a `Last updated: 2026-09-09` comment.

Currently defines `anthropic.claude-sonnet-4-5-20250929` ($3.00 input / $15.00 output per million) and `openai.gpt-5-mini` ($0.25 input / $2.00 output per million), each flagged in a comment as an ASSUMPTION to verify before relying on it.

Only used for API-metered providers. `ollama` and `command` providers always log "unmetered", never a fabricated cost. A missing model entry just logs "cost unknown"; it is not treated as an error.

## Inputs

Hand-edited YAML.

## Outputs and side effects

Cost figures logged by `reg-radar classify` runs; no effect on classification behaviour itself, only on the cost log.

## How to run or invoke

Not directly invoked; loaded via `LLM_PRICES_PATH` (see `.env.example`, default `/app/config/llm_prices.yaml`) when running `reg-radar classify`.

## How to test

No dedicated test command in the file's own comments.

## Boundaries

Must never be used to fabricate a cost for an unmetered provider (`ollama`, `command`); those always log "unmetered" regardless of this file's contents.

## Related pages

[reg-radar classify](cli-classify.md)
