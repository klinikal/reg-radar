---
title: Classification
type: component
sources: [regradar/classification.py]
last-verified: 2026-09-10
---

## Purpose

The classifications store and write path. `write_classification()` is the
single write path, every caller (collector source defaults, legacy
content_tags migration, `reclassify`, human corrections via the web API)
goes through it. Precedence between competing classifications is
implemented once, in `effective_labels()`.

## How it works

Each classification is a row: `item_id`, `axis`, `value`, `by` (provenance,
e.g. `human:<user>`, `llm:<model>`, `rule`, `source_default`, `import`),
`confidence`, `taxonomy_version`, `classifier_id`, `created_at`,
`superseded_at`.

`_by_rank(by)`: tie-break order for automated provenances on a
single-valued axis, lowest first: `import`=0, `source_default`=1, `rule`=2,
any `llm:*` prefix=3.

`winning_classification_rows(rows, taxonomy)` returns the live rows
(`superseded_at is None`) that determine the effective label per axis, one
row per single-valued axis, one row per qualifying value on a multi-valued
axis. Axes unknown to `taxonomy`, or values no longer in its vocabulary, are
ignored. Precedence: any live `human:*` row wins outright; on a
single-valued axis with more than one live human row, the most recent
`created_at` wins (falls back to the alphabetically last value if
`created_at` is missing or tied, for determinism). Otherwise, non-human rows
with `confidence >= taxonomy.min_confidence(axis)` are considered:
multi-valued axes take the union (highest confidence per value), 
single-valued axes take the highest confidence, ties broken by
`_by_rank`. If nothing qualifies, the axis is simply absent from the
result.

`effective_labels(rows, taxonomy)` builds a `{axis: value_or_list}` dict
from `winning_classification_rows()`.

`materialize_labels(engine, taxonomy, item_id)` recomputes
`effective_labels()` from an item's full classification history and writes
`items.labels` (JSON) and `items.updated_at`, and rebuilds the item's rows
in the `item_labels` side table (a query-speed index). `_materialize_labels_conn`
is the shared core that runs on an already-open connection without
committing, used both by `materialize_labels()` (own connection, commits)
and `classify_batch()` (one transaction for the whole batch).

`write_classification(engine, taxonomy, item_id, axis, value, by,
confidence, classifier_id=None)`: validates axis/value against the current
taxonomy (raises `ValueError` on unknown axis or value), appends a live row
(superseding a prior live row of the *same provenance*, i.e. both
`human:*`, or an exact `by` match, on single-valued axes only; multi-valued
axes never supersede on write), and re-materialises labels if anything
changed. Writing an identical live row again is a no-op. Raises `ValueError`
if `item_id` does not exist.

`remove_classification(engine, taxonomy, item_id, axis, value, by)`:
supersedes one live row by exact `(item_id, axis, value, by)` match, used by
the web UI's "remove instrument" control on a multi-valued axis. Returns
whether a row was found.

`import_classification(engine, item_id, axis, value, taxonomy_version,
confidence=0.5, classifier_id=None, by="import")`: inserts one row WITHOUT
taxonomy validation, used by `scripts/migrate_content_tags.py` (legacy tags
survive as history even if unmappable to a current taxonomy value) and
`regradar/dataset.py`'s `import-dataset` command (preserves the original
`by` from a published dataset's `label_provenance`). Does not supersede
anything and does not materialise labels, caller must call
`materialize_labels()` once per item after importing.

`supersede_axis(engine, item_id, axis, by=None)`: supersedes all live
non-human rows for `(item_id, axis)`, optionally restricted to a specific
`by`. Human rows are never touched by any automated path. Returns the count
superseded. Used by `regradar reclassify`.

`classify_batch(engine, taxonomy, classifications)`: batch write interface,
validates every entry (required fields, known axis/value, known item_id,
confidence in `[0.0, 1.0]`) before writing any of them, so one bad entry
never loses the rest. Raises `ValueError` if the batch exceeds
`MAX_BATCH_SIZE` (100). Every valid entry is written inside one
transaction (`engine.begin()`), if a DB-level write fails partway through
the whole transaction rolls back. Returns `{"written": [item_id, ...],
"errors": [{"index", "item_id", "error"}, ...]}`.

## Inputs

An open SQLAlchemy `engine`, a built `Taxonomy` (see
[Taxonomy](component-taxonomy.md)), and classification data: item_id, axis,
value, by, confidence, optional classifier_id.

## Outputs and side effects

Writes rows to the `classifications` table, and re-materialises
`items.labels`/`items.updated_at` plus the `item_labels` side table on any
change. `classify_batch()` writes/materialises transactionally for the
whole batch.

## How to run or invoke

Not run standalone. Called by `collector.main` (source defaults, after
`insert_item()`), `regradar reclassify`, `scripts/migrate_content_tags.py`,
`regradar/dataset.py`'s `import-dataset`, the web API (human corrections),
and the MCP classify tools (`classify_item_tool`, `classify_items_tool`,
which call into `classify_batch()`).

## How to test

Write a classification via `write_classification()`, read back
`items.labels` and confirm precedence: a `human:*` write must outrank a
prior `rule`/`llm:*`/`source_default` row on the same axis.

## Boundaries

`write_classification()` and `classify_batch()` must never write an
unvalidated axis/value, only `import_classification()` skips validation,
and only for legacy/re-import data. Automated writes (`supersede_axis`)
must never touch a `human:*` row. `classify_batch()` must never write a
partial batch on a DB-level failure, either all valid entries commit or
none do.

## Related pages

[Rules](component-rules.md)
[Taxonomy and Classification](concept-taxonomy-classification.md)
[reg-radar classify](cli-classify.md)
