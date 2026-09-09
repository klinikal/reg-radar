# Contributing

The primary contribution surface is the regulator registry under
`config/regulators/`. See the [README's Contributing section](README.md#contributing)
for the kinds of PRs that are welcome (new regulators, fixed feed URLs,
better selectors). This document covers the mechanics of adding or fixing a
source.

## Adding or fixing a regulator source

Three things, one PR:

1. **One YAML entry** under `config/regulators/<jurisdiction>.yaml` - one
   file per ISO alpha-2 jurisdiction id from `config/jurisdictions.yaml`
   (lower-case; `eu` for supranational bodies like ESMA, AMLA, EBA, EIOPA).
   If the jurisdiction's file doesn't exist yet, create it - it's just a
   YAML list of regulator entries, same shape as every other file in that
   directory. `config/regulators/_template.yaml` is fully commented for all
   six adapter types (`rss`/`atom`, `scrape`, `page_watch`, `sitemap`,
   `json_api`, `browser`) - copy the block for your source's type.

2. **One fixture** under `tests/fixtures/sources/<regulator_id>/<label>.<ext>`
   (`.xml` for `rss`/`atom`/`sitemap`, `.json` for `json_api`, `.html`
   otherwise) - a real, captured response for your source, used offline by
   the contract test below. Capture one with:

   ```
   .venv/bin/python -m scripts.capture_fixture --regulator <id> --source <label>
   ```

   This fetches through the exact production path (same adapter, same
   FlareSolverr wiring/throttling if the regulator is `cloudflare_protected`)
   and writes the raw response unmodified. Review the diff before
   committing - a fixture over ~300KB is flagged as worth a second look.

3. **No new test file needed.** `tests/test_source_fixtures.py` is
   parametrised over every enabled source in the registry and picks your new
   entry up automatically - it runs your source's adapter against the
   fixture and asserts the result has at least one item with a non-empty
   title, non-empty url, and a resolved date (either a real `published_at`
   or `date_inferred: true`). Run it to confirm:

   ```
   .venv/bin/python -m pytest tests/test_source_fixtures.py -k "<regulator_id>"
   ```

## Registry validation

`collector.main.load_regulators()` loads every `*.yaml` file in
`config/regulators/` (except ones starting with `_`, like the template) and
fails loudly - not silently - on:

- a duplicate regulator `id` across files
- a duplicate source `label` within one regulator
- a regulator `jurisdiction` that isn't a known id in `config/jurisdictions.yaml`

Run `.venv/bin/python -m pytest tests/test_main.py -k load_regulators` after
adding or editing an entry to catch these before opening a PR.

## Source adapter framework

Each `sources[].type` maps to an adapter under `collector/adapters/` -
`rss.py`, `scrape.py`, `page_watch.py`, `sitemap.py`, `json_api.py`,
`browser.py` - registered by `type_id` in `collector/adapters/registry.py`.
Adding a genuinely new fetch strategy (not just a new source using an
existing one) means adding a new adapter file, not touching the others -
see `docs/design/regradar-architecture.md` section 7 and
`docs/issues/08-source-adapter-framework-registry-split.md` for the
`SourceAdapter`/`RawDocument`/`FetchContext` contract.

The `browser` adapter (Playwright, for JS-rendered listings) needs no
browser binary to test against - `BrowserAdapter().parse()` runs against
already-rendered HTML, same as every other adapter's fixture test. The
browser binary is only needed for a real `fetch()` call, and is installed in
the collector's Docker image only (`Dockerfile.collector`).
