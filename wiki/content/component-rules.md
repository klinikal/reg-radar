---
title: Rules
type: component
sources: [regradar/rules.py]
last-verified: 2026-09-10
---

## Purpose

The rules engine: compiles `config/rules.yaml` plus implicit alias rules
derived from `config/taxonomy.yaml` into `CompiledRule`s once per run, and
scores an item's title/body against them. `RulesEngine.score()` computes
scores only, it never touches the database.

## How it works

`CompiledRule` is a dataclass: `axis`, `value`, `patterns` (list of
`(compiled_regex, weight)`), `fields` (default `["title", "body"]`),
`title_multiplier` (default 1.0).

`_compile_explicit_rules(path, raw_rules, taxonomy)`: for each entry in
`rules.yaml`'s `rules` list, validates it has `axis`/`value` (known to
`taxonomy`), optional `fields` (subset of `{"title", "body"}`), and a
`match` list of `{pattern, weight}` entries (optionally `flags: "i"` for
case-insensitive). Any structurally invalid entry, unknown axis/value, or
unusable regex is logged as a warning and skipped, never raises.

`_compile_alias_rules(taxonomy)`: one implicit `CompiledRule` per taxonomy
value that has `aliases`, word-boundary matched (`\balias\b`),
case-insensitive, at a fixed weight `ALIAS_RULE_WEIGHT = 0.7`. Most
instruments need no explicit rule at all, just aliases in `taxonomy.yaml`.

`_score_rule(rule, title, body)`: for each field the rule targets, a
pattern contributes its weight once if it matches anywhere in that field
(not multiplied by match count), multiplied by `title_multiplier` on the
title field. Contributions sum across all the rule's patterns and fields,
capped at 1.0.

`RulesEngine.load(rules_path, taxonomy)`: reads `rules.yaml` (missing file
treated as empty), parses `min_confidence` (default
`DEFAULT_MIN_CONFIDENCE = 0.5`, falls back to the default on a bad value),
compiles explicit rules then appends alias-derived rules. Invalid YAML or a
non-mapping top level logs an error and disables explicit rules for the run
(alias rules still compile). `classifier_id` is a sha256 of the raw rules
file bytes plus the taxonomy version, so a rules or taxonomy change changes
the classifier_id.

`RulesEngine.score(title, body)`: returns `{(axis, value): confidence}` for
every pair whose combined score reaches `min_confidence`. Scores sum per
`(axis, value)` across every rule targeting that pair (an explicit rule plus
its alias-derived counterpart stack), capped at 1.0.

`load_raw_rules(path)` / `write_raw_rules(path, min_confidence, rules)`:
read/write `rules.yaml` as plain dicts (not compiled patterns), the shape
the `/rules` web UI reads and edits. `write_raw_rules()` prepends a fixed
explanatory header comment to the file.

## Inputs

`config/rules.yaml` (optional, missing file is treated as empty), a built
`Taxonomy` (for validating explicit rules and sourcing alias rules), and per
item a `title` string and `body` string to score.

## Outputs and side effects

`RulesEngine.load()` returns an in-memory `RulesEngine`, no I/O beyond
reading the file. `RulesEngine.score()` is pure, returns a dict, no side
effects. `write_raw_rules()` overwrites `rules.yaml` on disk.

## How to run or invoke

Not run standalone. `collector.main` builds a `RulesEngine` via
`RulesEngine.load()` and calls `.score()` after `insert_item()`, writing
each qualifying `(axis, value)` as a `classifications` row with `by:
"rule"` via [Classification](component-classification.md)'s write path.
`regradar reclassify --by rule` does the same for existing items. The
`/rules` web UI reads/writes `rules.yaml` via `load_raw_rules()`/
`write_raw_rules()`.

## How to test

Load a `RulesEngine` against a known `rules.yaml` and taxonomy, call
`.score()` with a title/body known to match an explicit or alias rule, and
confirm the returned confidence for that `(axis, value)` matches the
expected summed weight (capped at 1.0) and clears `min_confidence`.

## Boundaries

Must never raise on malformed `rules.yaml`, invalid entries are logged and
skipped so a bad rule or bad YAML degrades to fewer rules rather than
crashing the collector. Must never write to the database, `RulesEngine`
only scores, the caller is responsible for writing
`classifications` rows.

## Related pages

[Rules Config](config-rules.md)
[Classification](component-classification.md)
