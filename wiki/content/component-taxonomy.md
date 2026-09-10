---
title: Taxonomy
type: component
sources: [regradar/taxonomy.py]
last-verified: 2026-09-10
---

## Purpose

Loads and validates `config/taxonomy.yaml` and `config/jurisdictions.yaml`
into a `Taxonomy` object. No code elsewhere may name a taxonomy axis except
"jurisdiction", axes and their vocabularies come entirely from
`taxonomy.yaml`, jurisdiction hierarchy comes entirely from
`jurisdictions.yaml`.

## How it works

`build_taxonomy(taxonomy_path, jurisdictions_path)` loads and validates both
files, cross-checking that every instrument value's optional `jurisdiction`
field (informational only, never rejected) references a known jurisdiction
id. Raises `TaxonomyError` naming the offending file on any structural
problem.

`taxonomy.yaml` must be a mapping with a top-level integer `version` and a
non-empty `axes` list. Each axis entry needs an `id` (unique), a
`cardinality` of `"one"` or `"many"`, and a non-empty `values` list. Each
value needs an `id` (unique within the axis), an optional `name` (defaults
to the id), optional `aliases` list, and an optional `jurisdiction`. Axes
also carry `min_confidence` (default 0.5) and `source_default_confidence`
(default 0.6, the confidence a `by: source_default` write gets on that
axis).

`jurisdictions.yaml` must be a non-empty list of entries, each with an `id`
(unique), optional `name` (defaults to id), optional `type`, optional
`parents` list (must reference known ids), and optional `note`. Parent
references are checked for cycles via a three-colour DFS
(`_build_jurisdictions`), a cycle raises `TaxonomyError` naming the cycle
path.

The `Taxonomy` class exposes: `version()`, `axes()` (list of axis ids),
`cardinality(axis)`, `min_confidence(axis)`, `source_default_confidence(axis)`,
`is_valid(axis, value)`, `axis_values(axis)`, `jurisdiction_ids()`,
`jurisdiction_name(id)`, `ancestors(jurisdiction_id)` (transitive closure of
`parents`, insertion-ordered, no duplicates, unknown ids simply have no
ancestors), `descendants(jurisdiction_id)` (reverse of `ancestors()`, built
by walking every known jurisdiction's ancestors rather than maintaining a
second graph, used by profile expansion's `<id>/*` wildcard), and
`to_dict()` (the plain-dict rendering handed to both the `get_taxonomy_tool`
MCP tool and the batch LLM classifier, a single rendering so the two paths
can't drift on what they tell a model is valid).

`validate_regulator_jurisdictions(regulators, taxonomy, regulators_path)`
raises `TaxonomyError` if any regulator's `jurisdiction` is not a known
jurisdiction id.

`load_and_exit_on_error(taxonomy_path, jurisdictions_path, regulators=None,
regulators_path="")` is the collector startup entry point: builds the
taxonomy (and validates regulators, if given), or logs the error and calls
`sys.exit(1)`, matching `collector/main.py`'s fail-loudly-on-bad-config
convention.

## Inputs

`config/taxonomy.yaml`, `config/jurisdictions.yaml`. Optionally a list of
regulator dicts and their source path, for cross-validation.

## Outputs and side effects

A `Taxonomy` object (in-memory, immutable after construction). No
filesystem or database writes. `load_and_exit_on_error()` exits the process
on invalid config.

## How to run or invoke

```
python3 -m regradar.taxonomy
```
Uses `TAXONOMY_PATH` (default `config/taxonomy.yaml`) and
`JURISDICTIONS_PATH` (default `config/jurisdictions.yaml`) environment
variables; builds the taxonomy and raises on any structural error.

## How to test

`python3 -m regradar.taxonomy` against the real config files exits non-zero
(uncaught `TaxonomyError`) if either file is invalid.

## Boundaries

Must never let code elsewhere hardcode an axis name other than
"jurisdiction". Must never accept a jurisdiction parent cycle, an unknown
axis/value's jurisdiction reference, or a regulator with an unknown
jurisdiction, these all raise `TaxonomyError` rather than being silently
dropped.

## Related pages

[Taxonomy Config](config-taxonomy.md)
[Taxonomy and Classification](concept-taxonomy-classification.md)
