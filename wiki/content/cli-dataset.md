---
title: reg-radar import-dataset
type: cli
sources: [regradar/dataset.py]
last-verified: 2026-09-10
---

## Purpose

Loads a published reg-radar dataset (a tarball plus manifest) into a
database, saving a new install the historic backfill scripts by seeding
it from a snapshot instead.

## How it works

`import_dataset()` downloads (`_download()`, for a `http(s)://` source)
or reads a local tarball, locates its manifest with `_find_manifest()`
(a sibling `manifest.json` first, else a `manifest.json` member inside
the tarball), and verifies the tarball's sha256 against the manifest's
`sha256` field, raising `DatasetError` on mismatch. It refuses to import
into a non-empty `items` table unless `--force` is passed.

It extracts the tarball (`_extract()`, using `filter="data"` to block
absolute paths and `../` traversal), reads `export.jsonl` line by line,
upserts each item's regulator, inserts the `Item` (skipping ones already
present via `insert_item()`'s return value), replays any
`label_provenance` rows through `regradar.classification.import_classification()`
and `materialize_labels()`, and rewrites each row's `updated_at` with a
raw SQL `UPDATE` after insertion (needed because the ORM insert path
doesn't accept an arbitrary `updated_at`, and `get_changes()` string-
compares that column in the naive-UTC format `collector.storage.db_now`
writes).

On completion it stores the manifest's `generated_at` as a sync cursor
(`encode_cursor()` with a sentinel tie-break id `"z"`, chosen because
real item ids are lowercase hex and `"z"` sorts after all of them) via
`set_sync_state()`. This cursor does not make later collection
incremental, a collection run always fetches each source's current feed
pages regardless, but it is usable as `since` for `get_changes()`,
`/api/changes`, a webhook subscriber, or `reg-radar export --since`, and
is surfaced by `get_health()`'s `dataset_import` field.

The companion function `publish_dataset()` (used by
`.github/workflows/publish-dataset.yml`, not by this CLI command)
builds the tarball and manifest from an already-exported directory via
`create_tarball()` and `build_manifest()`; `build_manifest()` counts
`item_count`/`per_regulator_counts` directly from `export.jsonl` so the
manifest can't drift from what's actually published.

## Inputs

- `source` (positional, required): URL or local path to the dataset
  tarball (or a directory already containing one).
- `--force`: import even if the database already has items.
- `--timeout` (default 60s): per-read timeout when `source` is a URL.
- `--max-bytes` (default 2 GiB, GitHub's per-release-asset limit): aborts
  a URL download that exceeds it.
- `--db-path` (env `DB_PATH`, default `/app/data/reg_radar.db`).
- `--taxonomy-path`, `--jurisdictions-path` (env-backed defaults).

## Outputs and side effects

Inserts items, regulators, and classification provenance rows into the
database (via `init_db()`, which also creates the schema if the database
file doesn't yet exist). Writes a sync-state cursor row. Prints a summary
to stdout: items loaded, dataset's `generated_at`, any excluded
regulators (need a manual backfill), and, if a cursor was stored, the
cursor value plus copy-paste commands to fetch everything since it. A
download that exceeds `--timeout` or `--max-bytes`, or fails partway,
deletes the partial file before raising `DatasetError`.

## How to run or invoke

```
reg-radar import-dataset <url|path> [--force] [--timeout 60] [--max-bytes 2147483648]
```

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_dataset.py` and run it via pytest from `code/`.

## Boundaries

Must never import a tarball whose sha256 doesn't match its manifest
(refuses with `DatasetError`, "tampered or corrupted"). Must never import
into a non-empty `items` table without `--force`. Must never leave a
truncated tarball on disk after a failed or over-limit download. Must
never extract with a path-traversal-permissive filter, extraction always
uses `filter="data"`.

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [Taxonomy and Classification](concept-taxonomy-classification.md)
