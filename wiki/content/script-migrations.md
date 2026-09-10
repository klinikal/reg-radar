---
title: Migration Scripts
type: script
sources: [scripts/migrate_alerts_to_rules.py, scripts/migrate_content_tags.py, scripts/migrate_regulator_labels.py, scripts/migrate_source_content_tags.py]
last-verified: 2026-09-10
---

## Purpose

One-off v1-to-v2 data migrations, already run against production. Kept in the repo for
reference and for anyone upgrading an older installation, not for repeated use.

## How it works

- `migrate_alerts_to_rules.py`: converts `config/alerts.yaml`'s flat `keyword`/`tag` pairs
  into `config/rules.yaml` entries. Maps each alert to the nearest taxonomy (axis, value)
  pair, in priority order: exact tag-to-value-id match, then keyword-to-alias match, then a
  case-insensitive prefix match between tag and value id. Every migrated rule gets weight
  0.7, matches title and body case-insensitively, using `re.escape()` of the original
  keyword. A rule matching nothing under any of these rules is left out of `rules.yaml` and
  printed for manual handling, not silently dropped.
- `migrate_content_tags.py`: imports every item's existing `content_tags` JSON list into the
  `classifications` table as `by: import`, confidence 0.5 rows, on the `instrument` axis
  (documented as an assumption: every content_tags value seen in this codebase's data is
  instrument-shaped; verify before running against a production database with different
  tags). Uses `import_classification()`, which bypasses taxonomy validation so tags that no
  longer map to a current value are preserved as history. Re-running is safe: pairs already
  imported (same item/axis/value, `by='import'`) are skipped.
- `migrate_regulator_labels.py`: renames each source's old flat `tags` list to the new
  `labels` dict, and lower-cases `jurisdiction` on both the regulators YAML and the
  `regulators` table (`UK` becomes `gb`, everything else lower-cased as-is). Rewrites the
  given `--regulators-path` file in place using `ruamel.yaml`'s round-trip loader to
  preserve comments and formatting. Maps specific old tags
  (`warnings`/`page_watch`/`press_releases`/`news`) to a `document_type` default in that
  priority order, and maps instrument-shaped tags
  (`mica`/`casp`/`aml`/`dora`/`art`/`emt`/`crypto`) to an `instrument` default only for an
  explicit small allowlist of confirmed single-purpose sources; everywhere else those tags
  are dropped as having been stamped blanket-wide. Idempotent: a source that already has
  `labels` and no `tags` is left untouched. Takes one regulators YAML file at a time (predates
  the per-jurisdiction config split), and separately rewrites the `regulators` table's
  jurisdiction column, which runs unconditionally even if the YAML rewrite is a no-op.
- `migrate_source_content_tags.py`: splits every item's old blended `tags` column into
  `source_tags` (old tags minus the blanket regulation-name denylist,
  `collector/fetcher.py`'s `_BLANKET_LABEL_DENYLIST`, via `filter_source_tags()`) and
  `content_tags`, which is not carried over but freshly recomputed by re-running the legacy
  keyword matcher (inlined from the removed `collector.keyword_alerts` behaviour) against
  each item's title/snippet/raw_content. No-ops if the database has no legacy `tags` column
  (i.e. was created after the schema split).

## Inputs

- `migrate_alerts_to_rules.py`: `config/alerts.yaml`, `config/rules.yaml`,
  `config/taxonomy.yaml`, `config/jurisdictions.yaml` (all overridable by flag or env var).
- `migrate_content_tags.py`: `DB_PATH`, `config/taxonomy.yaml`, `config/jurisdictions.yaml`.
- `migrate_regulator_labels.py`: `--regulators-path` (required, one file), `--db-path`.
- `migrate_source_content_tags.py`: `DB_PATH`, `ALERTS_PATH`.
- All four support `--dry-run`.

## Outputs and side effects

Writes to `config/rules.yaml`, the `classifications` table, a regulators YAML file plus the
`regulators` table, or the `items` table's `source_tags`/`content_tags` columns,
respectively, unless `--dry-run` is passed, in which case each script only logs what it
would do.

## How to run or invoke

Run from the repository root (the mfsa/esma-style backfills run inside the collector
container, but these four are documented as repo-root invocations):

    .venv/bin/python -m scripts.migrate_alerts_to_rules --dry-run
    .venv/bin/python -m scripts.migrate_content_tags --dry-run
    .venv/bin/python -m scripts.migrate_regulator_labels --regulators-path config/regulators/mt.yaml --dry-run
    docker compose exec collector python -m scripts.migrate_source_content_tags --dry-run

## How to test

`--dry-run` on every script computes and logs what would change without writing. For
`migrate_alerts_to_rules.py`, the dry-run output also lists every unmapped rule so they can
be reviewed before a real run.

## Boundaries

Must never write when `--dry-run` is passed. Must never overwrite an already-migrated
source's `labels` from a stale re-derivation once its `tags` key is gone
(`migrate_regulator_labels.py`'s idempotency guarantee). Must never duplicate an
already-imported `(item_id, axis, value)` row (`migrate_content_tags.py`). These are
historical, already-run migrations, not scripts meant for routine or repeated use.

## Related pages

[Rules Config](config-rules.md), [Taxonomy and Classification](concept-taxonomy-classification.md)
