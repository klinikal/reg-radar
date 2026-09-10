---
title: reg-radar reclassify
type: cli
sources: [regradar/reclassify.py]
last-verified: 2026-09-10
---

## Purpose

Re-applies classification defaults to existing items on one taxonomy axis, either from `regulators.yaml` source defaults or by rescoring every item against `config/rules.yaml`.

## How it works

`run()` builds the taxonomy and validates `--axis` is one of its known axes (error and exit 1 otherwise). If `--by rule`, it delegates to `_run_rule_reclassify()`; otherwise it runs the source-default path inline.

`_run_rule_reclassify()` loads a `RulesEngine` from `--rules-path`, then iterates every item in bounded pages (`_iter_items_by_id`, 500 rows per page, keyset pagination on `id` so peak memory is bounded regardless of table size and no cursor is held open across writes). For each item it scores title/body with the rules engine and filters to the target axis's values. If no rule matches and a prior `by: rule` row is still live for that item, the row is superseded (not rewritten) so a rule that stopped matching doesn't leave a stale classification winning forever. If rules do match, it supersedes the axis then writes a fresh `by: "rule"` classification per matched value.

The source-default path (`--by source_default`, the default) reads each item's `(regulator_id, source_label)`, looks up `source_defaults()` (built from each regulator's `sources[].labels` block in `regulators.yaml`), and does the same supersede-then-rewrite dance: an item whose source no longer has a default for this axis gets its prior live row superseded (not rewritten); an item whose source does have a default gets `by: "source_default"` rows written, confidence from `taxonomy.source_default_confidence(args.axis)`. It also tracks and reports how many live rows on this axis carry a `taxonomy_version` older than the current one.

`--dry-run` runs the same counting logic but skips every actual write (`supersede_axis`/`write_classification`/`materialize_labels`), logging what would have changed instead.

Human-provenance rows (`by: "human:*"`) are never touched, `supersede_axis()` itself guards with a `NOT LIKE 'human:%'` filter.

## Inputs

- `--axis` (required): taxonomy axis to reclassify.
- `--by` (default `source_default`, choices `source_default` | `rule`): which provenance to supersede and rewrite. `llm:*` provenance is agent-driven via the classifier skill, not this command.
- `--dry-run`: report what would change without writing.
- `--db-path` (default from `DB_PATH` env var, else `/app/data/reg_radar.db`).
- `--regulators-path` (default from `REGULATORS_PATH` env var, else `config/regulators`).
- `--taxonomy-path` (default from `TAXONOMY_PATH` env var, else `config/taxonomy.yaml`).
- `--jurisdictions-path` (default from `JURISDICTIONS_PATH` env var, else `config/jurisdictions.yaml`).
- `--rules-path` (default from `RULES_PATH` env var, else `config/rules.yaml`), used only when `--by rule`.

## Outputs and side effects

Without `--dry-run`: writes and/or supersedes `classifications` rows for the target axis, and calls `materialize_labels()` for items whose live row was superseded with nothing to replace it. Logs a summary of items reclassified/superseded/stale-version via the module logger. With `--dry-run`: no database writes, only a logged summary of what would happen.

## How to run or invoke

```
reg-radar reclassify --axis <axis> --by source_default
reg-radar reclassify --axis <axis> --by rule --dry-run
```

Also runnable standalone: `python -m regradar.reclassify reclassify ...` (the module has its own `main()` that builds a minimal single-command parser).

## How to test

Run with `--dry-run` first and check the logged counts, then run for real and query `classifications` for the target axis to confirm the expected rows were written/superseded and human rows are untouched.

## Boundaries

Must never rewrite or supersede a `by: "human:*"` classification row. Must never write anything when `--dry-run` is set.

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [concept: Taxonomy and Classification](concept-taxonomy-classification.md)
