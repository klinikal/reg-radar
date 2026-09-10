---
title: Backfill Scripts
type: script
sources: [scripts/backfill_cnmv_warnings.py, scripts/backfill_esma_library.py, scripts/backfill_fca_publications.py, scripts/backfill_mfsa_publications.py]
last-verified: 2026-09-10
---

## Purpose

One-off historic-archive pulls, one script per source. The daily collector only scrapes
the newest page of each listing, so these walk the full paginated backlog once to bring
historic documents into the database.

## How it works

Each script loads its source's URL and CSS selectors from `config/regulators/` rather than
duplicating them, and fetches every page through the same `fetch_source()` path the
collector itself uses (`collector/fetcher.py`), so parsing can never drift from the live
scrape config.

- `backfill_cnmv_warnings.py`: walks CNMV's unregistered-firm warnings listing
  (`es-cnmv`/`warnings_unregistered`, tipoNP=5), detecting total pages from a "Page X out of
  Y" string in the response HTML. Small archive (about 5 pages / 77 documents as of
  2026-07-02), safe to run directly.
- `backfill_esma_library.py`: walks the ESMA document library (`esma`/`library`, about 363
  pages / 7,260 documents), auto-detecting the last page from the pager's "Go to last page"
  link. Backs off with growing delay on consecutive page failures (5 in a row stops the
  run) rather than hammering a rate limit.
- `backfill_fca_publications.py`: walks FCA's `notices_and_decisions` and
  `policy_and_guidance` categories (about 5,980 and 3,743 documents respectively as of
  2026-07-02), detecting the total result count from the page's "of `<strong>N</strong>`
  search results" text. Deliberately not run automatically, the docstring calls running it
  "a real decision, not a side effect of adding the source."
- `backfill_mfsa_publications.py`: walks any of MFSA's roughly 65 publication categories
  (`mt-mfsa`), including categories not enabled for daily collection. MFSA is
  Cloudflare-protected and every request goes through FlareSolverr, so a full run is slow by
  design; the script fetches raw HTML itself (to check the "next page" link) and runs it
  through `ScrapeAdapter().parse()` plus `process_raw_documents()` rather than duplicating
  selector logic.

All four check `require_backfill_supported()` against the source's registry entry before
fetching or writing anything, and use `collector.storage.insert_item` plus
`record_run_start`/`record_run_complete` so a real run is recorded exactly like a normal
collector run.

## Inputs

- `config/regulators/` (source URLs, selectors, and the `backfill` support flag read by
  `require_backfill_supported()`).
- Environment variables shared with the collector: `REGULATORS_PATH`, `USER_AGENT`,
  `REQUEST_TIMEOUT_SECONDS`, `REQUEST_RETRIES`, `DB_PATH`; `backfill_mfsa_publications.py`
  additionally requires `FLARESOLVERR_URL` and aborts if it is unset.
- CLI flags: `--dry-run` on all four; `--delay` (seconds between page requests) on the
  cnmv/esma/fca scripts; `--start-page`/`--end-page` on the esma script; `--category`/`--all`
  and `--max-pages` on the fca and mfsa scripts.

## Outputs and side effects

Inserts new items via `insert_item()` and records a collector run row
(`record_run_start`/`record_run_complete`) with fetched/new/error counts, unless `--dry-run`
is given, in which case nothing is written. Logs per-page progress and a final summary; exits
1 if any page errored.

## How to run or invoke

Run inside the collector container:

    docker compose exec collector python -m scripts.backfill_cnmv_warnings --dry-run
    docker compose exec collector python -m scripts.backfill_esma_library --dry-run
    docker compose exec collector python -m scripts.backfill_fca_publications --category notices_and_decisions --dry-run --max-pages 2
    docker compose exec collector python -m scripts.backfill_mfsa_publications --category circulars_crypto_assets_circulars --dry-run --max-pages 2

Drop `--dry-run` to write. See each script's own docstring for the full flag set.

## How to test

`--dry-run` fetches and parses every page exactly as a real run would, without writing to
the database, so it is the way to sanity-check page counts and item counts before committing
to a full crawl.

## Boundaries

Must never write to the database when `--dry-run` is passed. Must refuse to run against a
source the registry does not mark as backfillable (`require_backfill_supported()`), before
any fetch or write happens. `backfill_fca_publications.py` must never be wired into
automatic/scheduled execution, per its own docstring. `backfill_mfsa_publications.py` must
never attempt a request without `FLARESOLVERR_URL` set.

## Related pages

[Collector Pipeline](component-collector-pipeline.md)
