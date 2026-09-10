---
title: Collector Pipeline
type: component
sources: [collector/main.py, collector/pipeline.py, collector/fetcher.py, collector/storage.py, collector/enrichment.py, collector/chunking.py, collector/language.py, collector/schema.py]
last-verified: 2026-09-10
---

## Purpose

Runs the scheduled collection loop that fetches every enabled regulator source, turns each raw fetch into a canonical `Item`, and writes it to SQLite. Also owns original-artefact storage, text chunking, and language detection for collected items.

## How it works

`collector/main.py` is the entry point. `main()` initialises the database (`collector.storage.init_db`), loads the regulator registry (`load_regulators`, directory or single-file YAML, optionally filtered by `config/modules.yaml` via issue #48's module selection), resolves `on_enable` transitions (`apply_on_enable`), loads the taxonomy and `regradar.rules.RulesEngine`, then either runs one pass (`--once`) or starts an `apscheduler` `BlockingScheduler` grouped by distinct cron cadence (`_schedule_collection`), plus one job per `config/digests.yaml` entry (`_schedule_digests`).

`run_collection` (and its lock-guarded wrapper `run_collection_locked`, which uses a file lock via `fcntl` so two processes never write the DB concurrently) iterates regulators and their enabled sources, calling `fetch_source` per source. For a `cloudflare_protected` regulator with no `FLARESOLVERR_URL` configured, every source is recorded as blocked rather than silently skipped. Per-source outcomes are tracked: `BlockedFetchError` is recorded separately from a generic exception; a `scrape` source returning 0 items is treated as an error (possible broken selector); any source returning 0 items increments an empty-run counter used for stall detection. A DB integrity check (`PRAGMA integrity_check`) runs once per run before fetching.

`collector/fetcher.py`'s `fetch_source` looks up the adapter for the source's `type` in the adapter registry, calls `adapter.fetch()`, and applies a decoy-detection heuristic: a large response (`>= BLOCKED_LARGE_BODY_BYTES`, default 256KB) that parses to zero items raises `BlockedFetchError` rather than looking like a quiet day. It then hands the adapter's `RawDocument` list to `collector.pipeline.process_raw_documents`.

`collector/pipeline.py` is the shared post-fetch stage, used by every adapter type. It extracts `raw_content`/snippet from a `RawDocument` (priority: `content_html` > `pdf_text` > `content_text`, or the feed summary for `is_partial` RSS/Atom entries), builds `body_md` via `regradar.document.convert_body` (or `rss_body_markdown` for partial entries), resolves language (adapter override > source's `preferred_language` > `collector.language.detect_language`), strips blanket source-tag labels (`BLANKET_LABEL_DENYLIST`, e.g. "casp", "mica", "aml"), generates the item id (`make_item_id` or `make_page_watch_item_id` for `page_watch` sources), and runs the rules engine against title/content if one was passed (`classify()`, scoring only, writes `item.rule_labels`, does not touch the DB).

Back in `main.run_collection`, each returned `Item` passes through `_collectable` (issue #51's per-module watermark filter based on `module_state.collect_from`, plus `_page_watch_changes` for `page_watch` sources comparing against the stored baseline) before `insert_item`. On a successful insert, source-default labels (`sources[].labels` in the regulator config) and rule-scored labels are written as classifications via `regradar.classification.write_classification`.

`collector/storage.py` (SQLAlchemy ORM over SQLite) defines the schema: `RegulatorRow`, `ItemRow` (plus an FTS5 virtual table `items_fts`), `RunRow`, `SourceErrorRow` (three independent counters: `consecutive_failures`, `consecutive_empty_runs`, `consecutive_blocked_runs`), `ItemArtefactRow`, `ClassificationRow`/`ItemLabelRow` (provenance and materialised labels), `ItemChunkRow`, `ModuleStateRow`/`PageWatchBaselineRow` (issue #51 module enable state), and webhook/digest/sync-state bookkeeping tables. `init_db` runs `PRAGMA journal_mode=WAL`, creates the FTS5 table, and adds any missing columns via `_ensure_column` for forward migration. `insert_item` writes the `ItemRow`, the FTS row, and, if `item.raw_bytes` is set, an artefact via `_write_artefact` (writes to `ARTEFACTS_DIR/<id[:2]>/<id>.<ext>`, capped by `MAX_ARTEFACT_BYTES`, skipped if `STORE_ARTEFACTS=false`).

`collector/enrichment.py`'s `run_chunking` walks every item's `raw_content`/`content_snippet`, skips items whose content is empty (deleting any stale chunks), skips items whose content hash is unchanged since the last chunking pass, and otherwise re-chunks via `collector.chunking.chunk_text` and writes `ItemChunkRow`s.

`collector/chunking.py`'s `chunk_text` splits on paragraph boundaries (blank-line-separated), packing paragraphs into word-count windows (`target_tokens`, default `(500, 900)`, "tokens" meaning whitespace-split words, not a real tokenizer) with overlap (`overlap_tokens`, default `(75, 150)`) carried into the next chunk; an oversized single paragraph is hard-split via `_window_split`. `content_hash` is a SHA-256 of the text.

`collector/language.py`'s `detect_language` wraps `langdetect.detect`, returning `"unknown"` for text under 10 characters or on a `LangDetectException`.

`collector/schema.py` defines the `Item` dataclass (the canonical in-memory document) and `make_item_id`/`make_page_watch_item_id`, both SHA-256 hashes: the former over `regulator_id|url|published_at` (or just `regulator_id|url` if undated), the latter over `regulator_id|url|content_hash`.

## Inputs

Regulator registry YAML (`config/regulators/` or a single file), `config/modules.yaml` (module/source selection), `config/taxonomy.yaml` and `config/jurisdictions.yaml`, `config/rules.yaml` (classification rules), `config/webhooks.yaml` and `config/digests.yaml` (both optional), environment variables (`DB_PATH`, `SCHEDULE`, `USER_AGENT`, `REQUEST_TIMEOUT_SECONDS`, `REQUEST_RETRIES`, `FLARESOLVERR_URL`, `ARTEFACTS_DIR`, `MAX_ARTEFACT_BYTES`, `STORE_ARTEFACTS`, `BLOCKED_LARGE_BODY_BYTES`, and the various `*_PATH` config overrides), and whatever each adapter fetches over the network.

## Outputs and side effects

Writes rows to the SQLite database at `DB_PATH`: `items` (plus `items_fts`), `runs`, `source_errors`, `item_artefacts`, `classifications`/`item_labels`, `item_chunks`, `module_state`, `page_watch_baselines`. Writes original-artefact files under `ARTEFACTS_DIR` when enabled. May write `config/modules.yaml` on first run after an upgrade (`_migrate_modules_file`). Dispatches outbound webhooks after a run completes if `config/webhooks.yaml` exists.

## How to run or invoke

`docker compose up -d collector` runs it on its configured schedule. For a one-off pass: `docker compose exec collector python -m collector.main --once`.

## How to test

`pytest tests/` from `code/` (project-wide test suite covers pipeline, fetcher, storage, and enrichment behaviour).

## Boundaries

Must never write to the database concurrently from two processes; `run_collection_locked`'s file lock exists specifically to prevent this. Must never let a challenge/block page reach an adapter's parser (block detection runs before parsing in `collector/adapters/http.py`). A source marked `enabled: false` in the registry is never collected, regardless of module selection. A module's watermark is set once at enable and never advanced by later runs, so `on_enable` is a one-time transition rule, not a per-run filter.

## Related pages

[Collector Adapters](component-collector-adapters.md)
[Source Adapter Framework](concept-source-adapter-framework.md)
[Canonical Document Format](concept-canonical-document-format.md)
