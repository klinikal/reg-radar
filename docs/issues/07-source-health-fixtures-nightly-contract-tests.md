# Source health: fixtures, offline contract tests, nightly live checks and stall alerts

**Size:** M  **Depends on:** #3 rename to RegRadar  **Design doc:** docs/design/regradar-architecture.md §7 (second paragraph), principle 5

## Why

Selector drift is invisible today. `run_collection()` in `collector/main.py` treats a `scrape` source returning zero items as an error, but that check is scrape-only, resets the moment one item comes back, and has no memory beyond `SourceErrorRow.consecutive_failures`. A regulator redesign can keep returning HTTP 200 with a parser matching nothing, or matching the wrong elements, for weeks before anyone notices. Selectors are verified by hand today (see the notes fields throughout `config/regulators.yaml`) and only re-verified when someone happens to look. This issue gives every enabled source a regression fixture, a nightly live check that files GitHub issues instead of letting sources decay silently, and a stall detector for the "200 OK, wrong content" case the zero-items check misses.

## Scope

- One HTML/XML fixture per enabled source, captured from the live page/feed.
- A parametrised offline contract test that runs every source's existing parser against its fixture.
- `scripts/capture_fixture.py` to (re)capture a fixture from the live URL.
- A nightly GitHub Actions workflow that runs the same parsers against live URLs and files/updates one issue per failing source.
- A stalled-source detector: N consecutive runs (default 7, configurable) with zero items despite a successful fetch.
- Stall state surfaced in `get_health_tool`, `list_regulators_tool`, and the web UI regulator panel.

## Non-goals

- Changing any parser's selectors or behaviour (that's coverage/adapter work, issues #10 and #11).
- Auto-fixing broken selectors. The nightly workflow reports; a human or agent fixes.
- Fixtures for disabled sources (`enabled: false` at source or regulator level).
- Any change to the taxonomy/classification work in §3-5; this is fetch-layer health only.

## Design notes

**Fixture layout.** `tests/fixtures/sources/<regulator_id>/<source_label>.<html|xml>`, using `.xml` for `rss`/`atom` sources and `.html` for `scrape`/`page_watch`. `regulator_id` plus `source_label` is already `SourceErrorRow`'s key; reuse it.

**Contract test.** New `tests/test_source_fixtures.py`. Load `config/regulators.yaml` (check `config/regulators/` first if issue #10 has landed), enumerate every enabled regulator and source (`enabled: true` default), and per source: assert the fixture exists (fail with the missing path), call the existing parser directly (`_parse_rss`/`_parse_scrape`/`_parse_page_watch`, already imported directly by `tests/test_fetcher.py`) against the fixture content, and assert at least one `Item` with non-empty `title`, non-empty `url`, and a resolved date state (`published_at` set, or `date_inferred is True`).

**`scripts/capture_fixture.py`.** `python -m scripts.capture_fixture --regulator <id> --source <label> [--all] [--dry-run]`. Fetches exactly as `fetch_source` does, using the same `_fetch_html`/`_fetch_bytes`, the same FlareSolverr wiring and throttling when `cloudflare_protected: true`, and writes the raw response to the fixture path unmodified. `--all` iterates every enabled source, reusing the existing per-domain throttle rather than adding a second one.

**Nightly workflow.** `.github/workflows/source-health-nightly.yml`, cron-scheduled, with a FlareSolverr service container (same image `docker-compose.yml` uses) so `cloudflare_protected` sources aren't skipped. Per source: fetch live, sharing the fetch-and-parse function with `capture_fixture.py`, then apply the offline test's three-part assertion. On failure, open or update one issue per source (stable title, `source-health` label) with the error and a short diff against the last-known-good fixture; close it on recovery.

**Stall detector.** Add `consecutive_empty_runs` to `SourceErrorRow` (`collector/storage.py`, via the existing `_ensure_column` pattern). In `run_collection()`, a successful fetch yielding zero items increments it for every source type (not just scrape, as today) via a new `record_source_empty_run()`; any non-empty result resets it, alongside `reset_source_errors`. `STALL_THRESHOLD_RUNS` (default 7) is env-configurable, matching the `FLARESOLVERR_*` convention. Keep today's scrape-zero-items error recording too: a single bad run is a transient error, a week of silence is a stall.

**Exposure.** `get_health`/`list_regulators` in `mcpserver/queries.py` gain a `stalled: bool` per source, sourced from the threshold check; document it in `get_health_tool`/`list_regulators_tool`'s docstrings. The web UI regulator panel gets an equivalent stall warning alongside its existing consecutive-failure warning.

## Acceptance criteria

- [ ] Every enabled source has a fixture at `tests/fixtures/sources/<regulator_id>/<source_label>.<html|xml>`.
- [ ] `tests/test_source_fixtures.py` is parametrised over all enabled sources, fails loudly on a missing fixture, and asserts >=1 parsed item with title, url, and resolved date state.
- [ ] `scripts/capture_fixture.py` fetches a named source or all sources through the production fetch path, respecting FlareSolverr throttling.
- [ ] A scheduled workflow runs the live equivalent nightly, opens one issue per newly-failing source, updates it on repeat failure, closes it on recovery.
- [ ] `SourceErrorRow.consecutive_empty_runs` is added and maintained for all source types, not just scrape.
- [ ] A configurable stall threshold (default 7) is used consistently by the detector and the query layer.
- [ ] `get_health_tool` and `list_regulators_tool` surface stall state, documented in their docstrings.
- [ ] The web UI regulator panel shows a stall warning distinct from the failure warning.
- [ ] `tests/test_fetcher.py`, `tests/test_main.py`, `tests/test_storage.py` continue to pass.

## Open questions

- Should `capture_fixture.py --all` run unattended in CI to refresh fixtures, or stay a manual local tool?
- Should the "last known good" fixture used for nightly diffs auto-refresh on every green run, or only on a deliberate manual capture?
