---
title: Taxonomy and Classification
type: concept
sources: [regradar/taxonomy.py, regradar/classification.py, regradar/rules.py, config/taxonomy.yaml, config/jurisdictions.yaml]
last-verified: 2026-09-10
---

## Purpose

The objective, config-driven vocabulary for labelling items (`taxonomy.yaml`,
`jurisdictions.yaml`, `rules.yaml`) and the classification store, write
interface and LLM classifier that assign and record those labels with
provenance.

## How it works

`config/taxonomy.yaml` declares axes and their vocabularies. The code knows
nothing about specific axes except `jurisdiction`, which has hierarchy
semantics. Each axis has an id, a `cardinality` (`one` or `many`), and a
list of values, each with an id, name, and optionally `aliases` and an
informational `jurisdiction`. The file has a top-level `version`, bumped on
any change to values and recorded on every label. Instrument aliases feed
the rules engine and the LLM prompt. A `topic` axis is deliberately absent
from v2; adding one later is a config change only.

`config/jurisdictions.yaml` lists jurisdictions with `id`, `name`, optional
`type` (e.g. `supranational`) and `parents` (a list, so the graph can be a
DAG). Ancestors are the transitive closure. A profile selecting `fr`
matches items whose jurisdiction is `fr` or any ancestor. Regulators'
`jurisdiction` field must be an id in this file, the collector fails loudly
at startup if it is not. Migration lower-cases the existing
`regulators.yaml` ISO alpha-2 codes.

`config/rules.yaml` replaces `alerts.yaml`: deterministic, weighted,
regex-capable rules scoped to an axis and value, each with `match` patterns
carrying a `weight`, applied to `fields` (e.g. title, body), with an
optional `title_multiplier`. Scores sum per (axis, value), capped at 1.0. A
rule label is written when the score reaches `min_confidence` (default
0.5). Alias lists in `taxonomy.yaml` compile into implicit rules at weight
0.7. Source defaults live in `regulators.yaml` under `labels` (renamed from
`tags`), carrying `by: source_default`, confidence 1.0 for `document_type`
on single-purpose feeds and 0.6 for instruments.

Classification storage: a `classifications` table with `item_id`, `axis`,
`value`, `by` (`source_default`, `rule`, `llm:<provider>/<model>`,
`human:<name>`, or `import`), `confidence`, `taxonomy_version`,
`classifier_id`, `created_at`, `superseded_at`. Unknown values are rejected
on write.

Precedence, computed on every write and on `reclassify`, materialised into
`items.labels` (JSON) and `items.updated_at`:

1. Any live `human:*` row wins for that axis outright. For multi-valued
   axes, human rows define the full set. On a single-valued axis with
   competing human rows, the most recently written wins, falling back to
   the alphabetically last value if `created_at` ties or is missing.
2. Otherwise, all live non-human rows with `confidence >= threshold`
   (default 0.5, per-axis override in `taxonomy.yaml`) are considered.
   Single-valued axes take the highest confidence, ties broken
   `llm > rule > source_default`. Multi-valued axes take the union.
3. Otherwise the axis is unset. Unset is a real state, profiles include
   unclassified items by default.

`reclassify --axis instrument --by rule` marks matching non-human rows
superseded and rewrites them from current config, `--dry-run` reports what
would change. Human rows are never touched by any automated path.

Write interface: one function, `write_classification(item_id, axis, value,
by, confidence, classifier_id)`, exposed as a Python module, an HTTP
endpoint `POST /api/items/{id}/classifications`, and an MCP tool
`classify_item_tool`. All validate against the current taxonomy. A
companion `get_unclassified_tool(axis, limit, offset)` returns items with
no live row on that axis, oldest first.

LLM classifier: shipped as a skill (`skills/regradar-classifier/SKILL.md`),
not a service. It instructs any MCP-connected agent to pull unclassified
items, read `taxonomy.yaml`, classify, and write back via
`classify_item_tool` with a confidence and `by: llm:<provider>/<model>`. A
batch runner (`regradar classify --provider anthropic|openai|ollama`) is a
follow-on issue, not v2 core, sharing the same write interface.

Migration from v1 tags: `source_tags` values map to `document_type`
defaults where unambiguous (`warnings` to `warning`, `press_releases` and
`news` to `press_release`, `page_watch` to `page_change`; `publications` is
dropped as too broad). `source_tags` crypto-related values
(`mica`, `casp`, `aml`, `dora`, `art`, `emt`, `crypto`) become `instrument`
defaults only on single-purpose sources, otherwise dropped. `content_tags`
from `alerts.yaml` become `rules.yaml` entries; existing per-item
`content_tags` import as `by: import`, confidence 0.5. `source_tags` and
`content_tags` columns are kept read-only for one release, then dropped.

## Inputs

`config/taxonomy.yaml`, `config/jurisdictions.yaml`, `config/rules.yaml`,
`regulators.yaml` source defaults, item title and body text, and human or
LLM classification calls via the write interface.

## Outputs and side effects

Rows in the `classifications` table, effective labels materialised into
`items.labels` and `items.updated_at`, and `label_provenance` entries
written to exported frontmatter (see
[Canonical Document Format](concept-canonical-document-format.md)).

## How to run or invoke

Not applicable at this level, this page describes the model. See
[Taxonomy](component-taxonomy.md), [Classification](component-classification.md),
[Rules](component-rules.md) and [classify](cli-classify.md) for the
runnable pieces.

## How to test

Not applicable at this level. See the component and CLI pages linked above
for the commands that prove taxonomy loading, rule scoring and
classification writes work.

## Boundaries

The classifier must only assign objective, factual labels, never
interpretive "who cares about this" judgments, those live in
`config/activities.yaml` (see
[Profiles and Activities](concept-profiles-activities.md)). Unknown axis
values are rejected on write. No automated path may touch a live
`human:*` row.

## Related pages

[Principles](concept-principles.md)
[Canonical Document Format](concept-canonical-document-format.md)
[Profiles and Activities](concept-profiles-activities.md)
[Taxonomy](component-taxonomy.md)
[Classification](component-classification.md)
[Rules](component-rules.md)
[classify](cli-classify.md)
