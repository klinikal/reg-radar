---
title: Rules Config
type: config
sources: [config/rules.yaml]
last-verified: 2026-09-10
---

## Purpose

Deterministic, weighted, regex-capable classification rules that score an (axis, value) pair from `config/taxonomy.yaml` against an item's title and/or body text. This replaced the older `alerts.yaml` keyword tagger.

## How it works

A top-level `min_confidence` (currently `0.5`) sets the threshold a summed score must reach to write a classification row with `by: rule`. Under `rules`, each entry names an `axis` and `value` (both must exist in `config/taxonomy.yaml`), a `match` list of `pattern`/`weight` pairs (with optional regex `flags`, e.g. `i` for case-insensitive), and `fields` naming which item fields (`title`, `body`) the patterns are checked against. An optional `title_multiplier` scales the weight when a match is in the title.

Example entries: `instrument: mica` matches `MiCA` (weight 0.7, case-insensitive) in title or body; `document_type: enforcement` matches several title-only patterns like `\bfine\b` and `decision notice` at weight 0.85; `document_type: consultation` matches `^(CP|Consultation Paper)\s*\d` at weight 0.95.

Alias lists on `config/taxonomy.yaml` instrument values are compiled into implicit rules at weight 0.7 automatically, so most instruments (those with aliases defined) need no explicit entry in this file at all; explicit entries here are for cases needing custom patterns or weights.

## Inputs

Hand-edited YAML, or managed via the web UI at `/rules`.

## Outputs and side effects

Writes classification rows tagged `by: rule` when a summed score for an (axis, value) pair reaches `min_confidence`.

## How to run or invoke

Not directly invoked; loaded via `RULES_PATH` (see `.env.example`, default `/app/config/rules.yaml`) and applied automatically during classification.

## How to test

No dedicated test command in the file's own comments; validated indirectly through the collector's classification pipeline.

## Boundaries

`axis` and `value` in each rule must exist in `config/taxonomy.yaml`. Rules are additive to (not a replacement for) the implicit alias-derived rules.

## Related pages

[Rules](component-rules.md)
