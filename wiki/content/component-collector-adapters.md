---
title: Collector Adapters
type: component
sources: [collector/adapters/]
last-verified: 2026-09-10
---

## Purpose

Per-source-type fetch logic behind a common contract, registered in a type-keyed registry so `collector.fetcher.fetch_source` can look up the right adapter for a source's configured `type` without branching on it itself.

## How it works

`collector/adapters/base.py` defines the contract every adapter implements: a `SourceAdapter` subclass sets `type_id` and implements `fetch(source_cfg, ctx) -> list[RawDocument]`, optionally splitting out `fetch_raw()` (the network half, for fixture capture and `scripts/source_health.py`) from `parse()` (pure, fixture-testable). `RawDocument` is the adapter-agnostic output shape: `url`, `title`, `published_at`, `body`/`mime` (the artefact to store), `content_html`/`content_text`/`pdf_text` (content for the shared pipeline, normally exactly one set), `is_partial` (true for a feed summary, never a full document), `content_hash` (page_watch only), and `language` (a pre-resolved override). `parse_text_date` is a shared date parser applying `dayfirst=True` for non-ISO free text (EEA/UK sources render DD/MM/YYYY), exempting ISO-shaped strings.

`collector/adapters/registry.py` holds `REGISTRY: dict[str, SourceAdapter]`, populated by module-level `register()` calls for each adapter, with `"atom"` aliased to the same instance as `"rss"`. Importing the registry must not require optional heavy dependencies (playwright); each adapter guards its own heavy import.

`collector/adapters/http.py` is shared plumbing used by every HTTP-based adapter (not itself a `SourceAdapter`): SSRF guards (`validate_url_not_private`, DNS-rebinding-aware via `_host_resolves_to_private`), FlareSolverr integration (`fetch_via_flaresolverr`, per-domain throttled at `FLARESOLVERR_MIN_INTERVAL_SECONDS`, retried with jittered backoff), block/challenge detection (`detect_block`, matched against response headers and, for a 200, a set of known interstitial body markers, checked before any parse), `fetch_html`/`fetch_bytes` (retrying HTTP fetches with byte caps for `fetch_bytes`), and bounded PDF text extraction (`extract_pdf_text`, capped at `MAX_PDF_PAGES`).

The eight registered types:

- **http**: not a distinct adapter type; `http.py` is the shared HTTP layer used by the others (rss, json_api, sitemap, scrape, page_watch all call into it).
- **rss** (`rss.py`, also serves `atom`): fetches via `fetch_bytes`, parses with `feedparser`. Falls back to `parse_text_date` on `published`/`updated`/`created` text fields when `feedparser`'s own `published_parsed` is unset (needed for feeds like the FCA's that render dates for humans). Always sets `is_partial=True` since a feed entry is only ever a summary.
- **scrape** (`scrape.py`): fetches HTML via `fetch_html`, then applies CSS selectors from a `scrape:` config block (`item_selector`, `title_selector`, `link_selector`, `date_selector`/`date_attribute`, `content_selector`, `extract_pdf_text`, `date_fuzzy`). Optionally downloads and extracts text from a linked PDF per item (`extract_pdf_text: true`), with a blocked attachment fetch (`raise_on_block=False`) not failing the whole source.
- **page_watch** (`page_watch.py`): fetches one page, hashes the text of a `watch_selector` CSS fragment (`content_hash`). Returns nothing if the fragment is missing or empty; the diff/baseline comparison itself lives in `collector.main._page_watch_changes`, not here. Verifies the fragment is actually present (`has_expected_content`) since a redirect or maintenance page could otherwise hash the wrong content as a legitimate "change".
- **sitemap** (`sitemap.py`): fetches and parses a sitemap XML document's `<url><loc>`/`<lastmod>` entries, optionally filtered by `url_pattern` substring match. Title is derived from the URL's last path segment (`title_from: last_segment`, the only supported value).
- **json_api** (`json_api.py`): fetches JSON, resolves a dotted path (`items_path`) to a list, then maps each item's fields via further dotted paths (`title_field`, `url_field`, `published_field`) using a minimal resolver (`resolve_path`, attribute/key/list-index lookup only, no JSONPath). `url_field: null` opts into a shared `landing_url` for APIs whose items carry no per-item URL.
- **browser** (`browser.py`): Playwright-driven, for JS-rendered listings that don't need a Cloudflare challenge solved. Navigates via Chromium (`--no-sandbox`, since the collector container runs non-root), waits for a `wait_selector`, and reuses `ScrapeAdapter.parse()` against the rendered HTML by reshaping `browser:` config into `scrape:` config. SSRF-checks every request the rendered page makes (redirects, XHR, assets), not just the initial navigation.

## Inputs

A source config dict (`sources[]` entry from the regulator registry YAML: `type`, `url`, `label`, plus a type-specific config block like `scrape:`/`json_api:`/`sitemap:`/`browser:`) and a `FetchContext` (`user_agent`, `timeout`, `retries`, `flaresolverr_url`, `regulator_id`).

## Outputs and side effects

A `list[RawDocument]` per `fetch()` call, handed to `collector.pipeline.process_raw_documents`. Network requests to the source URL (and, for `scrape` with `extract_pdf_text`, to each linked PDF). `browser` additionally launches a Chromium subprocess per fetch.

## How to run or invoke

Not invoked directly; called by `collector.fetcher.fetch_source` during a collection run (`docker compose exec collector python -m collector.main --once`).

## How to test

`pytest tests/` from `code/`. Each adapter's `parse()` (and, where split out, `fetch_raw()`) is designed to be fixture-testable without a live network call.

## Boundaries

Must never let a challenge/block page reach a `parse()` call; block detection happens in `collector/adapters/http.py` before any parsing. Must never fetch a private/reserved-address URL (SSRF guard applies to every adapter, including redirects and, for `browser`, every request the rendered page makes). `json_api`'s path resolver must never raise on a malformed/missing field for one item; it skips that field instead.

## Related pages

[Collector Pipeline](component-collector-pipeline.md)
[Source Adapter Framework](concept-source-adapter-framework.md)
[Regulators Registry](config-regulators.md)
