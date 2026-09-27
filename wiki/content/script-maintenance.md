---
title: Maintenance Scripts
type: script
sources: [scripts/rebuild_chunks.py, scripts/rebuild_fts_index.py, scripts/repair_undated_rss_items.py, scripts/reprocess_bodies.py]
last-verified: 2026-09-10
---

## Purpose

Rebuild, repair, and reprocess utilities for keeping derived database state (chunks, the FTS
index, item bodies) consistent with the source-of-truth `items` table, and for fixing a
specific data defect (undated FCA RSS items).

## How it works

- `rebuild_chunks.py`: rebuilds `item_chunks` from `items.raw_content`/`content_snippet` via
  `collector.enrichment.run_chunking()`, which is idempotent via a content hash, so unchanged
  items are skipped automatically. `--item-id` re-chunks a single item by first deleting its
  existing chunks (so it can't be skipped as unchanged), otherwise it runs a full-corpus pass.
- `rebuild_fts_index.py`: deletes and rebuilds `items_fts` from the `items` table. Used both
  as a backfill for existing rows and for disaster recovery if `items_fts` ever drifts from
  `items`.
- `repair_undated_rss_items.py`: fixes items stored with `published_at IS NULL` and
  `date_inferred = 1` because FCA's news/warnings feeds render dates in a human format
  feedparser can't parse (issue #67). Since `collector/adapters/rss.py`'s current fallback
  only helps items collected from now on, and the affected items' ids were computed without a
  date (so re-collecting would insert new rows rather than update), this script instead
  fetches each affected item's own page, reads the real date from its `<time datetime>`
  element, recomputes the item id via `make_item_id`, and rewrites the row plus every table
  with a foreign key to it (`classifications`, `item_artefacts`, `item_chunks`, and
  `items_fts`, which has no triggers and is maintained by hand). Skips (does not overwrite)
  any item whose target id already exists. Dry run by default; requires `--apply` to write.
- `reprocess_bodies.py`: re-runs Markdown body conversion (`regradar.document.convert_body`)
  for items whose stored `extractor` field is older than the currently installed
  `pypdf`/`markdownify` version, reading from each item's stored artefact
  (`item_artefacts`). Items with no stored artefact are left untouched in this mode. A
  separate `--from-raw-content` mode backfills `items.body_md` (once, persistently) for
  pre-v2 items that have `raw_content` but no `body_md` and no artefact, using the same
  derivation (`regradar.document.effective_body_md()`) that readers already apply on the fly
  at read time, so the persisted value matches what was already being served. A third,
  separate `--refetch` mode (issue #17) targets items with `body_md`, `raw_content` AND
  `content_snippet` all empty - e.g. an `is_partial` RSS source, which never had an article
  body fetched at all - plus, since issue #115, items whose `body_md` is only the "Full text not
  collected" placeholder (extractor `raw/1`) even though `body_md` itself isn't literally empty -
  e.g. a `scrape` source whose listing page had no `content_selector` match for that item. Both
  shapes are fetched the same way: each item's own `source_url`, extracting the body via the
  item's regulator's `detail_selector` config key (see Regulators Registry). A regulator with no
  `detail_selector` is skipped and reported, never guessed at. Since issue #109/#115, a regulator
  with `detail_selector` also gets this fetch for brand-new RSS items unconditionally, and for
  brand-new scrape items whose listing produced no real body, at collection time
  (`collector.main.run_collection`'s `_needs_detail_fetch`, via the shared `collector/detail.py`),
  so `--refetch` is now mainly a backfill for items collected before that landed or whose
  collection-time fetch failed on the day.

## Inputs

- All four read `DB_PATH` (default `/app/data/reg_radar.db`); `reprocess_bodies.py` also
  reads `ARTEFACTS_DIR`.
- `rebuild_chunks.py`: `--dry-run`, `--item-id`.
- `rebuild_fts_index.py`: `--dry-run`.
- `repair_undated_rss_items.py`: `--regulator` (default `uk-fca`), `--sources` (default
  `news,warnings`), `--apply`, `--limit`, `--db`.
- `reprocess_bodies.py`: `--dry-run`, `--limit`, `--from-raw-content`, `--refetch` (plus
  `--regulator` and `--delay`, meaningful only with `--refetch`). `--refetch` also reads
  `REGULATORS_PATH`, `USER_AGENT`, `REQUEST_TIMEOUT_SECONDS`, `REQUEST_RETRIES` and
  `FLARESOLVERR_URL` (only for a `cloudflare_protected` regulator), matching the collector's
  own fetch config.

## Outputs and side effects

- `rebuild_chunks.py`: writes/deletes `item_chunks` rows.
- `rebuild_fts_index.py`: replaces all `items_fts` rows.
- `repair_undated_rss_items.py`: rewrites `items.id`/`published_at`/`date_inferred` and every
  referencing table's foreign key, and makes one live HTTP request per affected item (with a
  1 second politeness delay between requests) unless in dry-run (the default).
- `reprocess_bodies.py`: updates `items.body_md` and `items.extractor` via
  `update_item_body()`. `--refetch` also writes an `item_artefacts` row for the fetched page
  (via `collector.storage.add_item_artefact()`, gated by `STORE_ARTEFACTS` like every other
  fetch) and prints a report: counts of `recovered`/`404`/`blocked`/`selector-miss`/
  `skipped-no-selector`, how many of the selected candidates were the placeholder-only case
  (issue #115), plus every item still empty afterwards with its reason.

## How to run or invoke

    docker compose exec collector python -m scripts.rebuild_chunks --dry-run
    docker compose exec collector python -m scripts.rebuild_chunks
    docker compose exec collector python -m scripts.rebuild_chunks --item-id <id>

    docker compose exec collector python -m scripts.rebuild_fts_index --dry-run
    docker compose exec collector python -m scripts.rebuild_fts_index

    python -m scripts.repair_undated_rss_items            # report only
    python -m scripts.repair_undated_rss_items --apply    # write

    docker compose exec collector python -m scripts.reprocess_bodies --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies
    docker compose exec collector python -m scripts.reprocess_bodies --from-raw-content --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies --refetch --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies --refetch --regulator hu-mnb --limit 50

## How to test

Every script's `--dry-run` (or, for `repair_undated_rss_items.py`, the no-`--apply` default)
reports the count of what would change without writing anything, so a run can be
sanity-checked first.

## Boundaries

`rebuild_fts_index.py` must never leave `items_fts` partially rebuilt; it deletes then
reinserts inside its `rebuild()` call. `repair_undated_rss_items.py` must never overwrite an
item id that already exists in `items` (it skips and reports a collision instead), and must
never write anything without `--apply`. `reprocess_bodies.py`'s `--from-raw-content` mode
must only touch items with no stored artefact and an empty `body_md`, never one already
handled by the default artefact-based mode. `--refetch`'s WHERE-guard (tasks/lessons.md) is
just as strict: it selects only rows where `body_md`, `raw_content` AND `content_snippet` are
all empty, so it can never overwrite a body a normal collection run already wrote.

## Related pages
