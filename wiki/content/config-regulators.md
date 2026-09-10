---
title: Regulators Registry
type: config
sources: [config/regulators/]
last-verified: 2026-09-10
---

## Purpose

The community-maintained catalogue of every regulator reg-radar knows how to collect from: one YAML file per jurisdiction, each holding a list of regulator entries and their sources.

## How it works

The directory holds 38 files: `_template.yaml` (documentation only, never loaded, any filename starting with `_` is skipped by `collector.main.load_regulators()`) plus 37 jurisdiction files named by lower-case ISO alpha-2 id matching `config/jurisdictions.yaml` (e.g. `gb.yaml`, `eu.yaml` for supranational bodies like ESMA/EBA/EIOPA/AMLA, `us-ny.yaml`, `ae.yaml`).

Each regulator entry has: `id` (unique across every file, `<jurisdiction>-<short-name>`), `name`, `full_name`, `jurisdiction` (must resolve in `config/jurisdictions.yaml`), `jurisdiction_type` (free text, unvalidated, display-only; values in use: `supranational`, `member_state`, `eea_non_eu`, `national`, `state`, `sro`, `international`), `website`, `mica_competent_authority` (bool), `enabled` (bool; `false` disables every source under it without deleting them, and overrides any module selection that names it, logging a WARNING), `cloudflare_protected` (bool; routes sources through FlareSolverr), `warnings_url` (an optional field seen in real files, e.g. `gb.yaml`'s FCA entry and `eu.yaml`'s ESMA/AMLA/EBA/EIOPA entries, not documented in `_template.yaml`'s comments), `notes` (free text explaining quirks), and `sources` (list).

Each source has a `type` (`rss`/`atom`, `scrape`, `page_watch`, `sitemap`, `json_api`, or `browser`), `url`, `label` (unique within the regulator), `preferred_language`, `labels` (source-default classification, `{}` if none), and optional `tags`. Type-specific blocks configure extraction: `scrape` and `browser` use CSS selectors (`item_selector`, `title_selector`, `link_selector`, `date_selector`, etc.); `page_watch` uses a single `watch_selector` whose text change is recorded as an item; `sitemap` optionally filters `<loc>` entries by `url_pattern`; `json_api` maps a dotted `items_path` plus field names.

Any source can optionally carry `staleness` (`warn_days`/`error_days`, checked by `scripts/source_health.py`, defaults warn at 90 / error at 365), `backfill` (`supported` bool; no block means `supported: false`; when `true`, `method` is `script`, `paged_api`, or `none`, plus a `note` that becomes the operator-facing reason when `supported: false`), and `volume_estimate` (`first_run`, optional `per_week`, and a required `as_of` date so a stale estimate is visible).

Spot-checked against `gb.yaml` (FCA: rss news/warnings feeds plus two scrape sources for notices/decisions and policy/guidance, both with `backfill` and `volume_estimate` blocks) and `eu.yaml` (ESMA, AMLA, EBA, EIOPA entries): both files match the template's documented shape, with the one addition noted above (`warnings_url`).

## Inputs

Hand-edited YAML, one entry per regulator, one fixture and one test per the CONTRIBUTING.md "one-entry/one-fixture/one-test" flow.

## Outputs and side effects

Loaded at collector startup by `collector.main.load_regulators()`; drives every scheduled fetch.

## How to run or invoke

Not directly invoked; loaded via `REGULATORS_PATH` (see `.env.example`, default `/app/config/regulators`). Adding a new regulator means copying `_template.yaml` to `config/regulators/<jurisdiction>.yaml` (or appending to an existing jurisdiction file) and filling in the fields.

## How to test

No dedicated test command in these files' own comments; see CONTRIBUTING.md for the fixture/test flow expected per new entry, and `scripts/source_health.py` for the nightly staleness check.

## Boundaries

`jurisdiction` on every entry must resolve to an id in `config/jurisdictions.yaml`; the collector fails loudly at startup otherwise. A `_`-prefixed filename is never loaded. `backfill.supported: true` must only be declared where someone actually walked the archive; `method: none` is the only method allowed alongside `supported: false`.

### Known gap: SEC litigation releases are not covered

`us-sec` carries the SEC's RSS feeds only. Its litigation releases and accounting and
auditing enforcement releases are not collected, and the Federal Register does not
carry them either (a full-text search since 2026-01-01 returned 2 hits for
`"Litigation Release"` and 0 for `"Accounting and Auditing Enforcement"`, against
hundreds of real releases). No RSS exists for them: the legacy `/rss/litigation/*.xml`
feeds are retired and return 404.

Scraping the listing is the only route, and it was attempted and abandoned rather
than shipped unverified. `www.sec.gov`'s HTML path answered a 1,925-byte
`Request Rate Threshold Exceeded` 403 from every network it was tried from, including
under a full browser user agent, while the RSS path returned 200 in the same request
sequence. Selectors could not be derived from a page that never loaded, and shipping
guessed ones would have produced a source that parses to zero items on a large body,
which source health now records as blocked, permanently.

Anyone picking this up needs egress that SEC does not rate-limit. Derive the selectors
from the live page, keep the 1 request per second throttle in
`collector/adapters/http.py`, and capture a fixture through
`scripts/capture_fixture.py` like any other source.

## Related pages

[Collector Adapters](component-collector-adapters.md), [Jurisdictions Config](config-jurisdictions.md)
