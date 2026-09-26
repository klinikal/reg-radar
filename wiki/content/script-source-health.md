---
title: Source Health
type: script
sources: [scripts/source_health.py, collector/detail.py]
last-verified: 2026-09-26
---

## Purpose

Shared source-health logic: fetches a source exactly as the production collector does,
checks its parser output against a fixture/live-URL contract plus a staleness check, and
files/updates/closes GitHub issues for failures. Used by both `scripts/capture_fixture.py`
and the nightly `source-health-nightly` workflow so the fetch path, parse path, and
assertions are defined exactly once.

## How it works

`fetch_raw()` looks up a source's registered adapter (`collector/adapters/registry.py`) and
calls its `fetch_raw()` with a `FetchContext`, returning raw bytes rather than parsed
`RawDocument`s. `parse_fixture()` runs the same adapter's `parse()` plus
`process_raw_documents()`, the same code path the collector uses; a `fetch_pdf=False` option
disables a scrape source's PDF text extraction so the offline fixture contract test never
makes a network call, while the live nightly check leaves it at the default `True`.

`assert_contract()` requires at least one parsed item to have a non-empty title, a non-empty
url, and a resolved date state (`published_at` set or `date_inferred` True); otherwise it
raises `AssertionError`.

`check_staleness()` separately judges whether a source's newest genuinely-dated item
(`date_inferred=False`) is too old, using per-source `warn_days`/`error_days` overrides read
from `source["staleness"]` (falling back to `STALENESS_WARN_DAYS`/`STALENESS_ERROR_DAYS`, env
defaults 90/365). It can only assess sources that supply real dates; a source whose items all
carry inferred (fetch-time) dates, or a `page_watch` source, is skipped rather than flagged.
This function answers "is this source still publishing", not "is this source still being
collected", since the module has no database access in the nightly CI context.

`check_source()` combines fetch, parse, contract assertion, and staleness into one call:
returns `None` on success or a failure string, and appends any non-fatal staleness warning to
a `warnings` list rather than treating it as a failure (so a quiet-but-alive regulator never
gets an issue filed for it).

`run_all_checks()` iterates every enabled source of every enabled regulator
(`iter_enabled_sources()`), runs `check_source()` for each, and, when `file_issues=True`
(the default), opens or updates a GitHub issue per failure (`open_or_update_issue()`, via the
`gh` CLI) and closes any previously-open issue for a source that has recovered
(`close_issue_if_open()`). Issues are found/deduplicated by title
(`source-health: <regulator>/<source> failing`) and the `source-health` label.

For a regulator with `detail_selector` configured (issue #17/#109), once its feed check
succeeds on an `rss` source, `run_all_checks()` also calls `check_detail_page()` once per
regulator - never per source - using that source's first parsed item's `url` as the article to
probe. `check_detail_page()` fetches it through `collector/detail.py`'s `fetch_detail_body()`,
the exact function the production collector calls at collection time for a brand-new RSS item
(`collector/main.py` `_fill_detail_body`), with the same "retry a block through FlareSolverr
even when the regulator isn't marked `cloudflare_protected`" divergence `fetch_raw()` already
makes for the feed check itself (issue #85/#86 - this runner's IP can get challenged when the
collector's wouldn't). One extra request per regulator with `detail_selector`, not per source.
Its result is tracked under its own `<regulator_id>/detail_page` key - filed, updated, or
closed as its own GitHub issue exactly like a source failure - so a persistent block or
selector miss on the detail page gets surfaced without being conflated with the feed source's
own health (`open_or_update_issue`/`close_issue_if_open` don't distinguish a `detail_page` key
from a real source label; it's just a string).

`main()` runs this as a standalone entrypoint: loads regulators and collector config, runs
all checks, logs a summary plus every failure and warning, and returns exit code 1 only if a
failure could not be reported to GitHub (`report_failures`). A source failure that *was*
successfully filed/updated as a GitHub issue does not fail the job - the issue is the
notification for that. This is deliberate: most source failures are transient site flakiness
that self-recovers within a night or two (CNMV's RSS/scrape endpoints have done this
repeatedly - issues #83/#84/#89/#90 - and each closed itself the next run), so failing the
whole workflow on every one of those just adds a red CI run and an email on top of the issue
that's already tracking it. The job (and its failure notification) is reserved for the case
where the automated tracking itself is broken and a human is the only backstop left.

## Inputs

- `config/regulators/` (via `load_regulators()`, `REGULATORS_PATH` env var) for source
  definitions and per-source `staleness`, `timeout`, `retries` overrides.
- Collector config (`collector.main.load_config()`): user agent, timeout, retries,
  FlareSolverr URL - a source's own `timeout`/`retries` (see `_template.yaml`) wins over
  these defaults when set.
- Env vars `STALENESS_WARN_DAYS` (default 90), `STALENESS_ERROR_DAYS` (default 365).
- `gh` CLI, authenticated, for issue read/create/comment/close.

## Outputs and side effects

Live HTTP/FlareSolverr requests to every enabled source, plus one extra request per regulator
with `detail_selector` configured (the detail-page probe, issue #109). When run with
`file_issues=True`: creates, comments on, or closes GitHub issues labelled `source-health`.
Writes nothing to the reg-radar database; this module has no DB connection.

## How to run or invoke

    python -m scripts.source_health

This is also imported as a library by `scripts/capture_fixture.py` and by the nightly
`source-health-nightly` GitHub Actions workflow, not only run directly.

## How to test

Run `python -m scripts.source_health` and check the exit code (0 = every failure was
successfully filed/updated as a GitHub issue, 1 = reporting itself failed for at least one
source) plus the logged per-source failures and warnings - a nonzero count of failures with a
zero exit code is the normal, expected outcome for a flaky-but-tracked source. There is no
dry-run flag; running it always performs live fetches, but it never writes to the database
and only files GitHub issues as a side effect of `run_all_checks(file_issues=True)`.

## When a source keeps failing

A source that fails once is usually the site having a bad night. A source that fails
repeatedly, or that turns out to have no working route at all, gets paused rather than
left to file the same issue nightly: set `enabled: false` on the source, write the reason
and the evidence in a comment directly above it, keep its captured fixture so re-enabling
is a one-line change, and add a row to
[Inactive and Degraded Sources](config-inactive-sources.md).

Pausing is for sources that cannot work, not for sources that are merely quiet. A real
but low-cadence source takes a `staleness:` override instead, sized against its own
observed gaps. The test is whether an operator would ever plausibly see an item from it
again.

## Boundaries

Must never write to the reg-radar database, it has no DB connection by design. Must never
raise a staleness *warning* as a failure or open an issue for it, only a hard staleness
*error* (over `error_days`) counts as a failure. Must never assess staleness on a source with
no genuinely-dated items, or on a `page_watch` source, silence is required there rather than
a false alarm; registry validation rejects a `staleness:` override on a `page_watch` source
outright.

## Related pages

[Source Health Nightly](job-source-health-nightly.md), [Collector Adapters](component-collector-adapters.md)
