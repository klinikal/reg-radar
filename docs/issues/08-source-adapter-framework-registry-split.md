# Source adapter framework and per-jurisdiction registry split

**Size:** M  **Depends on:** #9 source health, #5 taxonomy and classification store (for `config/jurisdictions.yaml`)  **Design doc:** docs/design/regradar-architecture.md §7 (first and third paragraphs)

## Why

`collector/fetcher.py` hard-codes three source types (`rss`/`atom`, `scrape`, `page_watch`) as branches inside one `fetch_source()`, with fetch logic, parsing, and shared post-fetch mechanics tangled together. Adding a new fetch strategy, such as a sitemap, a JSON API, or a JS-rendered listing like AMLA's document library (currently a gap in `config/regulators.yaml` because it's "not reliably scrapeable with current tooling"), means editing shared code rather than adding a file. `config/regulators.yaml` is also a single 1,800+ line list mixing EU supranational bodies with 30+ national regulators (MFSA Malta alone contributes around 60 source entries), making review noisy and ownership unclear. This issue turns `type` into a proper adapter registry and splits the registry so adding a regulator is genuinely one YAML entry, one fixture, one test.

## Scope

- `SourceAdapter` base class and a registry keyed on `type`.
- `RawDocument` dataclass as the adapter output contract.
- `rss`, `scrape`, `page_watch` migrated to adapters with unchanged behaviour.
- New adapters: `sitemap`, `json_api`, `browser` (Playwright).
- The shared post-fetch pipeline (extract, hash, classify, store, artefact write) extracted out of `fetcher.py`, adapter-agnostic.
- `config/regulators.yaml` split into `config/regulators/<jurisdiction>.yaml`, loaded as a directory, with validation that fails loudly on duplicate ids or unknown jurisdictions.
- A CONTRIBUTING.md section and template file for adding a regulator.

## Non-goals

- Changing any existing source's selectors or fetched output.
- Building AMLA's real `browser` config (a coverage-expansion child issue under #11); this issue only proves `browser` works against a real JS-rendered target's structure.
- The taxonomy/classification work in §3-5; the pipeline's classify step is a passthrough for issue #6 to fill in.
- Rewriting every `scripts/backfill_*.py` script; at least one must be updated to prove the adapter pattern.

## Design notes

**Base and registry.** `collector/adapters/base.py`:

```python
@dataclass
class RawDocument:
    url: str
    title: str
    published_at: datetime | None
    body: bytes
    mime: str
    labels: dict = field(default_factory=dict)
    date_inferred: bool = False

class SourceAdapter:
    type_id: str
    def fetch(self, source_cfg: dict, ctx: "FetchContext") -> list[RawDocument]: ...
```

`FetchContext` carries `user_agent`, `timeout`, `retries`, `flaresolverr_url`, `regulator_id`. `collector/adapters/registry.py` holds a `{type_id: SourceAdapter}` dict.

**Migration.** `collector/adapters/rss.py`, `scrape.py`, `page_watch.py` wrap today's `_parse_rss`/`_parse_scrape`/`_parse_page_watch`. HTTP plumbing already adapter-agnostic (`_fetch_html`, `_fetch_via_flaresolverr`, `_fetch_bytes`, `_extract_pdf_text`, the SSRF guards, FlareSolverr throttle/backoff) moves to `collector/adapters/http.py`, shared rather than duplicated. Adapters return `RawDocument`, not `Item`; `Item` construction is the shared pipeline's job.

**Shared pipeline.** `collector/pipeline.py`: `process_raw_documents(raw_docs, source, regulator_id, config) -> list[Item]`, doing what's currently duplicated across the three parsers (language resolution, snippet truncation, `_filter_source_tags`, item id generation), plus a call-out point for classification/storage that later issues fill in. `run_collection()` calls `registry.get(type).fetch(...)` then `process_raw_documents(...)`.

**New adapters.**
- `sitemap`: parses sitemap.xml `<loc>`/`<lastmod>`; config block `sitemap: {url_pattern, title_from}`.
- `json_api`: fetches JSON, maps fields via dotted paths; config block `json_api: {items_path, title_field, url_field, published_field}`. A minimal dotted-path resolver is enough; no full JSONPath library is needed.
- `browser`: Playwright-driven for JS-rendered listings, reusing `scrape`'s selector shape plus a `wait_selector`. Targets AMLA's document library and DFSA Denmark's Blazor news list (both currently disabled or gapped for this reason), and lets sources needing only JS rendering, not a Cloudflare challenge, drop the FlareSolverr dependency. Requires `playwright` in `collector/requirements.txt` and a browser install step in the Dockerfile. SSRF checks must apply to this adapter's navigation too.

**Registry split.** `config/regulators/<jurisdiction>.yaml`, one file per ISO alpha-2 (e.g. `mt.yaml`, `eu.yaml` for supranational bodies), same list-of-dicts shape as today. `load_regulators()` becomes directory-aware: a directory loads and concatenates every `*.yaml` in it; a file path still works for migration compatibility. Add validation that fails loudly (matching the existing `sys.exit(1)` convention) on a duplicate regulator id across files, a duplicate source label within a regulator, and an unknown jurisdiction value, validated against `config/jurisdictions.yaml` from #5.

**CONTRIBUTING.md.** Document: one YAML entry under `config/regulators/<jurisdiction>.yaml` (point at a fully commented `config/regulators/_template.yaml` covering all adapter types), one fixture under `tests/fixtures/sources/<regulator_id>/<label>.<ext>` (issue #9), and issue #9's parametrised test picks it up with no extra test file needed. Cross-reference the README's Contributing section rather than duplicating it.

**Backfill scripts.** `scripts/backfill_*.py` currently read selectors straight from `regulators.yaml`. Update at least `backfill_esma_library.py` or `backfill_mfsa_publications.py` to call the relevant adapter's `fetch()` instead, and note the same expectation in the others' docstrings.

## Acceptance criteria

- [ ] `SourceAdapter`, `RawDocument`, `FetchContext`, and a `type_id`-keyed registry exist under `collector/adapters/`.
- [ ] `rss`, `scrape`, `page_watch` are adapters; every test in `tests/test_fetcher.py` passes against the new code paths.
- [ ] `sitemap`, `json_api`, `browser` adapters exist with fixture-based unit tests each.
- [ ] `browser` is proven against a fixture reproducing a real JS-rendered target (e.g. AMLA or DFSA's DOM shape).
- [ ] Post-fetch logic (id generation, tag filtering, language resolution, snippet truncation) exists once, shared across adapters.
- [ ] `config/regulators.yaml` is replaced by `config/regulators/<jurisdiction>.yaml` files with equivalent content.
- [ ] `load_regulators()` loads a directory, fails loudly on duplicate regulator ids, duplicate source labels, and unknown jurisdictions.
- [ ] `REGULATORS_PATH` defaults to the new directory; a single-file path still works.
- [ ] `CONTRIBUTING.md` documents the one-entry/one-fixture/one-test flow with a working template file.
- [ ] At least one backfill script fetches via its adapter instead of duplicating selector logic.
- [ ] `playwright` and its browser binaries install cleanly in the collector's Docker image.

## Open questions

- Should `browser` adapter runs share the FlareSolverr per-domain throttle even when FlareSolverr itself isn't involved, given Playwright launches carry a similar per-request cost?
- Is `json_api`'s dotted-path mapping sufficient, or will an early coverage-expansion issue need real JSONPath (array filters, wildcards)?
