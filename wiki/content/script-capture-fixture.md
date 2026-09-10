---
title: Capture Fixture
type: script
sources: [scripts/capture_fixture.py]
last-verified: 2026-09-10
---

## Purpose

Manual tool to (re)capture a regression fixture used by `tests/test_source_fixtures.py`,
fetching a source through the exact production fetch path and writing the raw response
unmodified.

## How it works

For a single `--regulator`/`--source` pair, or every enabled source with `--all`, the script
looks up the source in `config/regulators/` (via `scripts.source_health.load_regulators()`
and `iter_enabled_sources()`), then calls `scripts.source_health.capture()`, which fetches the
source's raw bytes through the same adapter fetch path (`fetch_source()` uses internally,
same FlareSolverr wiring and per-domain throttling) and writes them unmodified to
`tests/fixtures/sources/<regulator_id>/<label>.<ext>` (extension chosen by source type: xml
for rss/atom/sitemap, json for json_api, html otherwise). A response over
`MAX_FIXTURE_BYTES` (2MB) is reported as not captured rather than trimmed, since trimming
would defeat the point of an unmodified regression fixture; one over `FIXTURE_WARN_BYTES`
(300KB) is still written but flagged as worth a second look before committing.

`--all` reuses `fetch_source()`'s existing per-domain FlareSolverr throttle rather than
adding a second one. This is a manual local tool only, never invoked by CI or the nightly
workflow; last-known-good fixtures only change on a deliberate run of this script.

## Inputs

- `config/regulators/` or a directory given via `--config` (default `config/regulators`).
- Collector config (`collector.main.load_config()`): user agent, timeout, retries,
  FlareSolverr URL.
- CLI flags: `--regulator` and `--source` (together, for one source), `--all` (every enabled
  source), `--dry-run` (fetch and validate but do not write fixture files), `--config`.

## Outputs and side effects

Writes raw fixture bytes to `tests/fixtures/sources/<regulator_id>/<label>.<ext>` unless
`--dry-run` is given. Makes a live HTTP (or FlareSolverr) request per targeted source. Logs
per-source capture results and a final summary of captured/failed counts.

## How to run or invoke

    python -m scripts.capture_fixture --regulator <id> --source <label> [--dry-run]
    python -m scripts.capture_fixture --all [--dry-run]

## How to test

Run with `--dry-run` first: it fetches and validates every targeted source exactly as a real
capture would, without writing fixture files, so failures and oversized responses can be
reviewed before committing new fixture content.

## Boundaries

Must never write a fixture file over `MAX_FIXTURE_BYTES`; that case is reported as a failure,
not silently truncated. Must never be invoked by CI or the nightly source-health workflow,
per its own docstring, fixture updates only happen on a deliberate manual run.

## Related pages
