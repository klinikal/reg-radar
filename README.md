# reg-radar

> Renamed from casp-radar (interim name `regradar`) to reg-radar. If you have an existing install, see [Upgrading from casp-radar](#upgrading-from-casp-radar).

Open-source regulatory intelligence tool for European (EU and UK) entities. Collects news, warnings, and publications from financial regulators across the EU on a daily schedule, stores them in SQLite, and exposes the database via an MCP server so AI agents can query it. There is also a lightweight webUX that can be run to see, query and search.

**Bring your own agent.** This tool provides clean structured data, a simple web UI, and an MCP server. Classification is built in and deterministic — source defaults and a weighted regex rules engine write into a provenance-tracked label store (see [Classification](#classification) and [Classification rules](#classification-rules)) — and any MCP-compatible agent can classify the remaining backlog through the write tools, or just read the data. No model is called for you.

## Prerequisites

- **[Docker](https://www.docker.com/products/docker-desktop/)** (or Docker Engine + the Compose plugin on Linux) — required to run the stack. This is third-party software, not something this tool installs for you. If you don't have this, it's suggested you download it directly https://www.docker.com/ or by using Homebrew (`brew install docker`).
- **Git**, to clone the repo (`brew install git`).

Beyond that, `docker compose up` pulls the `python:3.11-slim` base image and a `flaresolverr` image from public container registries, and installs pinned Python packages from PyPI per `collector/requirements.txt` / `mcpserver/requirements.txt` — standard supply chain for a Dockerised Python project, versions are pinned in those files if you want to audit them before building.

## Getting started (zero to working)

1. **Run the stack**
   ```bash
   git clone https://github.com/klinikal/reg-radar
   cd reg-radar
   cp .env.example .env
   docker compose up -d
   ```
   This starts four services: `collector` (fetches on a schedule — see [Configuration](#configuration)), `mcp` (the query server, exposed at `http://localhost:8000/mcp`), `webui` (a browsable list/search view at `http://localhost:8080` — see [Web UI](#web-ui)), and `flaresolverr` (a Cloudflare-bypass helper for a handful of protected sources).

2. **Choose what to collect.** `config/regulators/` is a complete catalogue of 62 regulators; a fresh install collects nothing until you select from it.
   ```bash
   docker compose exec collector reg-radar setup
   ```
   `setup` walks you through jurisdictions → regulators → sources, tells you roughly how many items the first run will collect **before** it writes anything, and writes `config/modules.yaml` for you. Prefer to hand-edit? `cp config/modules.example.yaml config/modules.yaml` and see [Modules](#modules) for the schema. Without this file (and without `REGRADAR_COLLECT_ALL=true`) the collector starts, says so in one line, and exits cleanly.

3. **Get some data in.** On a fresh install the database is empty until the first run. To collect immediately:
   ```bash
   docker compose exec collector python -m collector.main --once
   ```

4. **Browse it** at `http://localhost:8080` to confirm data's actually there, or **connect your agent** to the MCP server — see [Connecting an agent](#connecting-an-agent) below for setup across different clients.

5. **Give your agent the onboarding skill.** Point it at [`skills/reg-radar-onboarding/SKILL.md`](skills/reg-radar-onboarding/SKILL.md) and ask it to onboard you. It'll interview you about your entity — what you're licensed for, where, what you do and don't do — and generate a private `skills/reg-radar-operator/refs/context.md` (git-ignored, never leaves your machine) so the agent knows what's actually relevant to you instead of treating every regulator in the registry as equally important.

6. **You're set.** From here, just ask your agent regulatory questions — "any recent CASP warnings from Germany?", "what's ESMA published this week?" — and point it at [`skills/reg-radar-operator/SKILL.md`](skills/reg-radar-operator/SKILL.md) if it needs a refresher on how to navigate the data. See [Skills](#skills) for more on how these work.

## Modules

A **module is a regulator** — any `id` in `config/regulators/*.yaml`. `config/modules.yaml` (yours, gitignored, never touched by `git pull`) says which of them this install actually collects. `config/modules.example.yaml` is the committed template.

```yaml
version: 1

defaults:
  schedule: "0 6 * * *"          # inherited by any module without its own
  on_enable: new_only            # new_only | backfill_from | backfill_all

modules:
  - id: eu/*                     # jurisdiction wildcard - every EU/EEA regulator
    schedule: "0 6 * * *"

  - id: uk-fca
    sources: [news, warnings]    # omit `sources:` for all of them
    schedule: "0 6 * * *"
```

A **jurisdiction wildcard** (`eu/*`, `gb/*`) is a convenience selector, not a separate concept: it expands to that jurisdiction and every descendant of it in `config/jurisdictions.yaml`, so `eu/*` covers ESMA *and* every member state's authority.

**Resolution order**, narrower always winning: `defaults` → jurisdiction wildcard (`eu/*`) → explicit module id (`uk-fca`) → `sources:` within a module. Between two wildcards, the nearer one wins — `fr/*` beats `eu/*` for the AMF. An explicit module id that omits `sources:` means *all* of them; it does not inherit a wildcard's list.

**The registry wins on `false`.** Anything `config/regulators/` marks `enabled: false` is never collected, however explicitly you select it here — that flag is a maintainer statement that a source is known broken or blocked, not a statement about whether you want it. When it overrides something you asked for by name, the collector logs a `WARNING` naming the module and source, so it is never silent.

Three loader states:

| State | Behaviour |
|---|---|
| `config/modules.yaml` present | Collect exactly what it selects |
| Absent, `REGRADAR_COLLECT_ALL=true` | Collect the whole registry |
| Absent, no env var | Collect nothing; one line pointing at `reg-radar setup`, clean exit 0 |

An invalid `modules.yaml` — bad cron, unknown module id, unknown source label — is refused at startup with a message naming the file and the offending entry, never a traceback and never a half-configured run.

**`schedule:`** is a per-module cron cadence, resolved through the same order as everything else. The collector adds one scheduled job per *distinct* resolved expression — not per module — so the job count tracks the cadences you actually use, not the size of the registry. A module with no `schedule:` anywhere falls back to `SCHEDULE` in `.env`, so an install with no cadence opinions behaves exactly as before.

Suggested cadences:

| Cadence | Sources | Why |
|---|---|---|
| Every 4h (`0 */4 * * *`) | OCC (10-item retention) | Loses documents otherwise |
| Daily (`0 6 * * *`) | Most RSS and scrape sources | The default |
| Weekly (`0 6 * * 1`) | FATF grey list, statute `page_watch` sources | Change ~3x/year |

The cadences never overlap each other: every run still takes the same single file lock, so a job that fires while another is still collecting is skipped with a `WARNING` naming its cron expression and the regulators it was carrying. That means a slow cadence can starve a fast one — the log line is there so you can see it happening. An unusable expression on a module logs and skips that group only; the scheduler still starts and the other cadences run. `--once` ignores cadence entirely and runs every enabled module in one pass.

**`on_enable:`** decides what the *first* run after switching a module on collects — and only that run. It is consulted at the transition into enabled and never again, so editing it later never re-cuts history you already have.

| Value | Behaviour |
|---|---|
| `new_only` (default) | Records the moment you enabled the module and collects only what is published after it. Cheap, no history |
| `backfill_from` | Same, with a date you give: `on_enable: backfill_from` plus `backfill_from: 2026-01-01` |
| `backfill_all` | No date bound at all |

The watermark is recorded *before* the first fetch and is the enable moment, never "the newest item the last run happened to see" — so a first run that fails leaves nothing half-set, and the retry still collects everything published since.

Both backfill values are a **hard error** against a source `config/regulators/` declares un-backfillable, before a run is recorded or an item written (see [Declared backfill capability](#declared-backfill-capability-and-volume)). Where they are allowed, be clear on what they do: they stop the collector *filtering* older items out — the collector itself only ever reads the current page of a feed. Walking an archive is a `scripts/backfill_*.py` run, and the enable log names the script for each source.

**Switching a module off is non-destructive.** Every row already collected stays in the database — items, artefacts, classifications — and so does each `page_watch` source's stored baseline. That baseline is why re-enabling is safe: a `page_watch` source's entire state is a hash of the fragment it watches, so a lost baseline would report a change to a page that never changed. Re-enabling compares against the surviving baseline and reports a change only if the page genuinely changed while the module was off. The first-ever enable of a `page_watch` source records the baseline and reports nothing, logging *"baseline established"* — distinct from *"change detected"*, which is what the same source logs when the fragment actually moves. Pruning rows for a disabled module is a separate decision that has not been taken.

### Choosing modules from the CLI

`reg-radar setup` is the interactive route and `reg-radar modules` the scripted one; both write the same `config/modules.yaml`, and both validate the result against the registry **before** anything is written, so a selection the collector would refuse at startup is refused here with the file untouched.

- **`reg-radar setup`** — asks for jurisdictions (grouped: EU/EEA, UK, US, Asia-Pacific, Middle East, international), then all-or-a-subset within each, then per-regulator source labels if you want to narrow further, then `on_enable`. Before writing it prints the selection, the [declared first-run volume](#declared-backfill-capability-and-volume) — *"approximately 9,733 items from 3 sources that declare a volume estimate, plus 8 sources of unknown volume"*, never a total that quietly treats undeclared sources as zero — and the cadence it is about to write. It writes the [suggested cadences](#modules) per module rather than a flat `defaults.schedule`, so you never have to know that the OCC needs four-hourly polling. Re-running it over an existing file shows what you collect today, names anything the new selection would switch **off** (with the sources that can never be backfilled to fill that gap), asks before overwriting, and leaves the old file as `config/modules.yaml.bak`.
- **`reg-radar modules list [--all]`** — the current selection with each module's resolved cadence, `on_enable`, and the entry it resolved through. Anything you selected that the registry force-disables is called out with the registry's own reason, because that is a state your file cannot fix. `--all` adds the registry regulators you have not selected.
- **`reg-radar modules enable <id|jurisdiction/*> ... [--on-enable=new_only|backfill_all|backfill_from=<YYYY-MM-DD>]`** — adds one or more modules, or a whole jurisdiction (quote the wildcard: `'us/*'`). Several ids at once, with or without commas, so the list the [web UI](#web-ui) prints when a jurisdiction is missing pastes straight in. A backfill mode against a source the registry declares un-backfillable is the same hard error the collector raises, before the file changes.
- **`reg-radar modules disable <id>`** — drops a module, after warning that nothing published while it is off will be collected and naming each source that declares no way to backfill, with the registry's reason (*"the feed retains only the 10 most recent items"*). A module currently selected through a wildcard is dropped by expanding that wildcard into explicit entries, which the command tells you it did.

Two caveats. `modules enable`/`disable` rewrite the file with PyYAML, which has no round-trip mode, so **comments in a hand-edited `config/modules.yaml` do not survive them** — every rewrite keeps the previous file as `config/modules.yaml.bak`. And `setup` is interactive by design: for Docker and scripted installs use `modules enable`, which needs no terminal.

### The trade-off: collection-time filtering is not free to reverse

Modules gate the **fetch**, at country/regulator granularity. [Profiles](#profiles) gate the **view**, at topic granularity. The question that decides which layer a filter belongs to is *would this operator ever plausibly want this data?* Never → a module. Maybe later → a profile.

That split matters because the two are not equally reversible. A profile change re-filters rows you already have, instantly. A module that was off for six months leaves a six-month hole, and filling it later only works if the source supports backfill — many do not, and some feeds only ever expose the last handful of items. Turn a module off because you will never want it, not because you are unsure.

### Upgrading an existing install

Nothing to do. On its first run after upgrading, an install that already has collected items writes `config/modules.yaml` for you, containing `eu/*` and `gb/*` — which resolves to exactly the regulator/source set it collected before. You'll see one `INFO` line saying the file was written and where. It is yours from that point: reg-radar never rewrites it once it exists.

If your `config/` mount is read-only the write fails with a `WARNING` naming the problem; create the file by hand, or set `REGRADAR_COLLECT_ALL=true`.

## Coverage

62 entries in the registry: 4 EU supranational bodies (ESMA, AMLA, EBA, EIOPA), national/EEA competent authorities (some jurisdictions have more than one — e.g. Belgium's FSMA and NBB, Italy's CONSOB/Banca d'Italia/OAM), plus the UK's FCA, eight US federal entries (the Federal Register, SEC, CFTC, Federal Reserve, OCC, CFPB and Treasury, plus FINRA as a self-regulatory organisation), three US state-level entries (New York's DFS, California's DFPI, and CSBS's NMLS as a national state-licensing aggregator), Singapore (MAS and Singapore Statutes Online) and the UAE (VARA in Dubai, the CBUAE rulebook, and the DFSA and ADGM FSRA free zones) as deliberate non-EEA exceptions. Full list, per-source status, and notes on disabled entries: `config/regulators/` (one file per jurisdiction).

Most active sources are RSS/Atom feeds or scrape targets; a smaller set is disabled (Cloudflare/SPA-protected, no working feed found, or architecturally unscrapeable) — documented in `config/regulators/` so gaps stay visible rather than silently missing.

Each source's `type` selects one of six adapters from a small registry (`collector/adapters/`): `rss`, `scrape` (CSS selectors over static HTML), `page_watch` (a page with no per-item URLs, diffed instead of parsed), `sitemap`, `json_api`, and `browser` (Playwright, for JS-rendered pages — an optional dependency, only imported when a source actually needs it). Adding a new source type means adding one adapter module and registering it, not touching the collector's fetch loop.

Note: some sources are better than others. This is beta software. Contributions are welcome to fill gaps, enhance coverage of regulators or sources *you* care about.

Optional: the `flaresolverr` service (included in Docker Compose by default) enables collection from Cloudflare-protected sources (e.g. Lithuania's lb.lt).

### Non-EEA additions

The UK's FCA is included as a deliberate exception to the EEA scope above — see `config/regulators/gb.yaml` for why and what's collected (news, warnings, notices and decisions, policy and guidance).

US federal coverage is a second exception, in `config/regulators/us.yaml`: the Federal Register (one API call covering rulemaking and notices for seven agencies, plus FINRA rule filings), and news, enforcement and speeches feeds for the SEC, CFTC, Federal Reserve, OCC and CFPB. Treasury and FINRA publish no usable feed at all — Treasury has no RSS anywhere, and `finra.org/rss.xml` is a dead Drupal default whose newest entry is from July 2020 — so both are scraped: Treasury's press releases, and FINRA's news releases plus its monthly disciplinary-action reports. FINRA is the registry's only `jurisdiction_type: sro`, a private self-regulatory organisation rather than a government agency. SEC litigation releases are not yet covered: they exist only as HTML, and `www.sec.gov`'s HTML path currently answers a 403 rate-limit page.

US state coverage is deliberately three bodies, not fifty, in `config/regulators/us-ny.yaml`, `us-ca.yaml` and the NMLS entry in `us.yaml`. NYDFS is the state regulator whose crypto rules firms elsewhere actually read — BitLicense, virtual-currency listing guidance, consent orders — and contributes its newsroom, industry letters and enforcement tables plus a `page_watch` on the virtual-currency licensee hub. California's DFPI is the only other state with a machine-readable feed and an active crypto docket. CSBS's NMLS `State Licensing` feed is a national aggregator of state licensing developments maintained by state-regulator staff — one anonymous JSON endpoint covering all fifty states — so it sits under `us` rather than any single state, and building 49 state scrapers to rediscover it is work not worth doing. Texas DOB (RSS with zero items), Florida OFR (~10 items a year) and Wyoming (no feed) were checked and rejected; NASAA is hard geo-blocked outside the US and is unverified.

Singapore is a third exception, in `config/regulators/sg.yaml`: MAS publishes no RSS anywhere, so all eight MAS sources — media releases, speeches, consultations, enforcement actions, notices, circulars, guidelines and legislation — come from its open Solr search API, plus two scoped `page_watch` sources on Singapore Statutes Online tracking amendments to the Payment Services Act 2019 and the Financial Services and Markets Act 2022.

FATF is a fourth exception, in `config/regulators/intl.yaml`, under a standalone `intl` jurisdiction for international standard-setting bodies. It covers the black-and-grey lists (a `page_watch` — the watched fragment carries the actual country lists, so the diff is the substance) and the home-page news carousel. `intl` deliberately has no parent/child edges in `config/jurisdictions.yaml`: FATF Recommendations bind nobody directly, they are transposed into national and supranational law, so modelling FATF as an ancestor of any jurisdiction would assert an applicability that does not exist. Profiles opt in by naming `intl` explicitly. FATF is Cloudflare-protected and collects only with `FLARESOLVERR_URL` set.

The UAE financial free zones are a fifth exception, in `config/regulators/ae.yaml`: the DIFC's DFSA and Abu Dhabi Global Market's FSRA, modelled as `ae-difc` and `ae-adgm`, children of an onshore `ae` they are constitutionally carved out of — only the financial-services acquis is disapplied, federal AML/CFT law still bites, so over-inclusion upward is the safe direction. Both regulators' rulebook updates come from Thomson Reuters-hosted mirrors: DFSA's own site returns a deterministic Cloudflare 403 on `/alerts`, `/rulebook` and `/public-register` while serving `/news` and `/rss` at 200, so the mirror is not a convenience but the only route in. ADGM's announcements source ships disabled — `adgm.com`'s sitemap `lastmod` is a last-edit timestamp rather than a publication date, so its items would carry confidently wrong dates. Dubai's VARA sits alongside them under its own `ae-dubai` jurisdiction — an emirate-level regime, a flat sibling of `ae-difc`/`ae-adgm` rather than their parent, because the free zones are carved out of emirate law too and an `ae-dubai → ae-difc` edge would push VARA rulebook changes at DFSA-regulated firms. VARA's news and regulatory notices are scraped from `vara.ae`, its rulebook revisions from `rulebooks.vara.ae`, and its public register keeps a `page_watch` because it has no feed of any kind. CBUAE is covered through `rulebook.centralbank.ae` only — that host is unprotected and carries the Payment Token Services Regulation, the UAE Virtual Assets Travel Rule and the SVF Regulation, while `www.centralbank.ae` sits behind a Cloudflare managed challenge site-wide and is deliberately left unconfigured rather than guessed at.

These are kept structurally separate so the EEA coverage numbers above stay accurate — and neither is collected unless you select it, since `config/modules.yaml` gates collection per regulator.

### Cloudflare coverage

Some regulators' websites e.g. MFSA in Malta are Cloudflare-protected — confirmed to block plain HTTP regardless of User-Agent, so every request goes through `flaresolverr` (already in the Docker stack). `config/regulators/` defines the taxonomy. Some are `enabled: true` by default, others are defined but disabled. Flip `enabled: true` on any of them, or target them directly via the backfill script below, without needing to redefine URLs or selectors.

Because every request needs FlareSolverr (a real browser, ~10-15s each), `collector/fetcher.py` throttles and backs off automatically — a minimum interval between requests (`FLARESOLVERR_MIN_INTERVAL_SECONDS`, default 8s) and longer exponential backoff specifically when the target's response looks like a ban/block rather than a normal failure (`FLARESOLVERR_RETRY_BACKOFF_SECONDS`, default 45s, doubling per retry, `FLARESOLVERR_MAX_RETRIES` attempts). This is global — any current or future `cloudflare_protected` source benefits. It also skips wasting plain-HTTP retry attempts against a known-Cloudflare-protected source, since those still count toward the target's own rate limit for no chance of success.

### Historic backfills

The regular collector only scrapes the first page of each scrape-type source on every run (newest-first) — enough to catch new publications going forward, but not the historic archive behind a source that only recently got added here. One-off backfill scripts under `scripts/` read URLs/selectors straight from `config/regulators/` (no duplicated config to drift out of sync), all support `--dry-run`, and none run automatically — running one is a deliberate decision, not a side effect of adding a source:

- `backfill_esma_library.py` — ESMA's document library, ~7,260 documents across ~363 pages. Has been run; that data is in the default dataset once you clone and run the collector's normal `--once`/scheduled pass — no need to re-run this unless you're rebuilding from an empty database.
- `backfill_fca_publications.py` — FCA's notices/decisions (~5,980 documents) and policy/guidance (~3,743 documents). **Has not been run** — built and tested (`--dry-run --max-pages N`) but the full historic crawl is left to you to trigger deliberately, since FCA is a non-EEA addition and a larger crawl than ESMA's.
- `backfill_mfsa_publications.py` — any MFSA category, enabled or not (`--category <label>`), or all ~65 at once (`--all`). **Has not been run at scale** — verified against several categories including a currently-disabled one, but a full `--all` run is, by design, hours-long given FlareSolverr's per-request cost and the built-in throttling — best scheduled overnight or off-hours, not run casually.
- `backfill_cnmv_warnings.py` — CNMV's unregistered-firm warnings archive (~5 pages / ~77 documents as of mid-2026). Small enough to run directly when you want the full history; the daily collector only picks up page 0 going forward.

*More to come. Feel free to use the same format to contribute additional backfills, or more regulators.*

Always `--dry-run` first — backfill scripts fetch and parse every page exactly as a real run would, just without writing to the database, so you can sanity-check counts before committing to a multi-hundred-request crawl of someone else's site.

#### Declared backfill capability and volume

**Backfill capability is a property of the source, not a choice you get to make.** The OCC's RSS feeds retain the ten most recent items: a source switched off for a week has permanently lost that week, and no script or setting recovers it. Two optional per-source fields in `config/regulators/<jurisdiction>.yaml` say so out loud, so the tool can refuse an impossible request instead of quietly returning ten items and calling it a success:

```yaml
backfill:
  supported: true
  method: script            # script | paged_api | none
  script: scripts/backfill_esma_library.py
  note: "~7,260 docs across ~363 pages"

volume_estimate:
  first_run: 7260           # items a full backfill would produce
  per_week: 40              # steady-state new items per week
  as_of: 2026-09-10         # required whenever a figure is given
```

- `method: paged_api` — the fetcher itself can walk back through pages (MAS's Solr index, the Federal Register API). `method: script` — a dedicated script under `scripts/` does it, and `script:` must name a file that exists. `method: none` — someone checked and there is no path.
- **A source with no `backfill:` block is `supported: false`.** That is the safe default: asserting backfill nobody verified is precisely the failure this field exists to close. A source with no `volume_estimate:` is reported as *unknown*, never assumed to be small.
- Asking for `on_enable: backfill_all` or `backfill_from` (see [Modules](#modules)) against a source declared unsupported is a **hard error at startup**, before a run is recorded or an item written — naming the source, the reason, and what you can have instead:

  > `us-occ/news does not support backfill: the feed retains only the 10 most recent items, so there is no history to fetch. Use on_enable: new_only — you will get everything published from now on.`

  The backfill scripts under `scripts/` refuse on the same rule, at the same point: before the first fetch.
- Figures are declared only where they were actually counted — OCC (10), NMLS (50), ADGM (1,113), FCA (~5,980 + ~3,743), ESMA's library (~7,260), each MAS content type (`numFound`) — and left undeclared everywhere else rather than guessed. `as_of` is what makes a stale estimate visible instead of invisible.

### Maintenance scripts

Two other one-off scripts under `scripts/` are for disaster recovery or after bulk imports — not part of the daily collector:

- `rebuild_fts_index.py` — rebuilds the SQLite FTS5 search index (`items_fts`) from the `items` table if it ever drifts or you're restoring from backup.
- `rebuild_chunks.py` — rebuilds the `item_chunks` table (paragraph-aware text chunks derived from each item's full content). Chunks are prep infrastructure for future semantic search; the collector does not populate them automatically yet — run this after a large backfill if you want chunks in place.
- `reprocess_bodies.py` — re-runs Markdown body conversion from stored artefacts for items whose `extractor` predates the currently installed library version; `--from-raw-content` instead backfills `body_md` from `raw_content` for pre-v2 items that never had an artefact (see [Canonical documents & artefacts](#canonical-documents--artefacts)).

### Source health

Selector drift (a regulator redesign keeps returning HTTP 200 with a parser matching nothing) is invisible to a single-run check, so it's caught two ways:

- **Regression fixtures.** Every enabled source has a captured fixture under `tests/fixtures/sources/<regulator_id>/<source_label>.{html,xml}`. `tests/test_source_fixtures.py` runs each source's existing parser against its fixture offline and asserts at least one parsed item with a title, url, and resolved date. Recapture one deliberately (never automatic) with:
  ```
  python -m scripts.capture_fixture --regulator <id> --source <label>
  python -m scripts.capture_fixture --all       # every enabled source
  ```
  Fetches go through the exact same code path (and FlareSolverr wiring/throttling) as the real collector. A source that can't be captured here — a Cloudflare challenge with no FlareSolverr running, a bot-blocking WAF — is recorded with the reason and its contract test marked as an expected failure (`xfail`) naming that reason, not silently skipped.
- **Nightly live check.** `.github/workflows/source-health-nightly.yml` runs the same fetch-and-parse contract against every source's live URL (with a FlareSolverr service container, so Cloudflare-protected sources are checked too) and opens or updates one GitHub issue per newly-failing source, closing it automatically on recovery.
- **Stall detector.** Independent of a hard fetch error: a source that fetches successfully but returns zero items for `STALL_THRESHOLD_RUNS` consecutive runs (default 7) is flagged `stalled` in `get_health_tool`/`list_regulators_tool` and shown in the web UI's regulator panel, distinct from the existing consecutive-failure warning.

## Tags

Every item carries two separate tag lists, kept apart deliberately:

- **`source_tags`** — structural, config-curated per feed in `config/regulators/` (e.g. `warnings`, `press_releases`, `news`, `publications`, `page_watch`). Set once when a source is configured, same for every item that feed produces.
- **`content_tags`** — a legacy, keyword-derived column. Nothing populates it going forward; use the [classification store](#classification) below (`labels`, populated by the [rules engine](#classification-rules)) instead.

Regulation-name labels (`casp`, `mica`, `aml`, `art`, `emt`, `dora`) are deliberately excluded from `source_tags` — they used to be stamped on nearly every item from a MiCA-relevant regulator regardless of content, which made them useless for filtering. If you want one of those as a real classification, add a [classification rule](#classification-rules) instead so it only lands on items that actually mention it.

**Deprecated for one release:** `source_tags`/`content_tags` and the `tags`/`source_tags`/`content_tags` filter parameters keep working against the old columns, but every read path now logs a deprecation warning. Prefer the [classification store](#classification) below — it records *why* a label is there and who/what set it, not just a flat string.

## Classification

Items are classified along **axes** defined in `config/taxonomy.yaml` (currently `document_type`, single-valued, and `instrument`, multi-valued — the specific EU/UK regulation an item concerns). The vocabulary is config, not code: adding a value, or a whole new axis, is a config change, not a code change.

Every classification is written with **provenance** — who or what said it, and how confident it was — to a `classifications` table (`by`: `source_default`, `rule`, `llm:<provider>/<model>`, `human:<name>`, or `import`; a confidence 0.0–1.0; the `taxonomy_version` at write time). Nothing is ever overwritten in place: a reclassification supersedes the old row and adds a new one, so history survives. A human correction (`by: human:*`) always wins over anything automated, permanently, for that axis — an automated reclassification pass can never silently undo one.

`items.labels` is a materialised JSON summary of the current *effective* label per axis (computed by the same precedence logic on every write), kept in sync with a queryable `item_labels(item_id, axis, value)` side table used for filtering — see `search_items_tool`'s `labels` parameter below.

**Jurisdictions** (`config/jurisdictions.yaml`) form a hierarchy — a regulator's jurisdiction has zero or more parents (e.g. every EU member state's parent is `eu`; EEA-EFTA states Norway and Iceland also list `eu` directly). A jurisdiction filter is expanded through its own ancestors before matching: requesting `fr` expands to `{fr, eu}`, so it returns both AMF (`fr`) and ESMA (`eu`) items — a country-level filter also surfaces the supranational regulator that covers it. Requesting `gb` (no parents) matches FCA only.

Re-applying source defaults to existing items after a config change:

```bash
docker compose exec collector reg-radar reclassify --axis instrument --dry-run
docker compose exec collector reg-radar reclassify --axis instrument
```

`--dry-run` reports what would change, including how many live rows on that axis carry an older `taxonomy_version` than the current config, without writing anything. Human corrections are never touched by this or any other automated path. `--by rule` rescores every item against `config/rules.yaml` instead of source defaults (see [Classification rules](#classification-rules)); `--by source_default` (the default) re-applies `regulators.yaml` source defaults as above.

**Migrating from v1:** `scripts/migrate_regulator_labels.py` renames `sources[].tags` to `sources[].labels` (preserving comments via `ruamel.yaml`) and lower-cases `jurisdiction` (`UK` → `gb`) - it predates the registry split and targets a single regulators file, so point it at one of the files under `config/regulators/` at a time; `scripts/migrate_content_tags.py` imports existing per-item `content_tags` into the classifications store as `by: import`, confidence 0.5 rows, so they survive but rank below anything better; `scripts/migrate_alerts_to_rules.py` converts the old `alerts.yaml` keyword rules into `rules.yaml` entries, printing any it can't map onto a taxonomy value for manual handling. All three support `--dry-run`.

Run the one-off upgrade steps in this order (also linked from [Migrating v1 data](#migrating-v1-data), where the volume rename leaves off):

1. Run the collector once (it refreshes the `regulators` table's `jurisdiction` column on every upsert), or run `scripts/migrate_regulator_labels.py --db-path <path>` directly — its DB-rewrite step runs independently of whether the YAML still has `tags` to migrate.
2. `reg-radar reclassify --axis document_type`
3. `reg-radar reclassify --axis instrument --by rule`
4. `reg-radar reclassify --axis document_type --by rule`
5. `scripts/migrate_content_tags.py`
6. `scripts/reprocess_bodies.py --from-raw-content` (re-extracts body Markdown from raw content saved before the extractor upgrade)

## Classification rules

`config/rules.yaml` is a deterministic, weighted, regex-capable rules engine that runs on every newly-collected item (and via `reg-radar reclassify --by rule`), replacing v1's keyword-to-tag alerts. Each entry scores one (axis, value) pair:

```yaml
min_confidence: 0.5
rules:
  - axis: instrument
    value: mica
    match:
      - pattern: "\\bMiCA(R)?\\b"
        weight: 0.9
      - pattern: "crypto-asset service provider"
        weight: 0.6
        flags: i
    fields: [title, body]
    title_multiplier: 1.5
```

For each field in `fields`, each pattern contributes its `weight` once if it matches at all in that field (not multiplied by match count), times `title_multiplier` on the title field; contributions sum across all of a rule's patterns and fields, capped at 1.0. A rule writes a `by: rule` classification (`classifier_id` = sha256 of `rules.yaml` + the taxonomy version) when the score reaches the file's top-level `min_confidence` (default 0.5 - separate from each axis's `min_confidence` in `taxonomy.yaml`, which controls what counts as an *effective* label, not what gets written to the store at all).

**Alias lists in `config/taxonomy.yaml` compile into implicit rules automatically**, at weight 0.7, word-boundary matched, case-insensitive, over both title and body - most instruments need no entry in `rules.yaml` at all; add one only for wording an alias doesn't catch.

Manage rules from the browser at **`http://localhost:8080/rules`** (`/alerts` redirects here) — list, add, and delete rules, validated against `config/taxonomy.yaml` on save. Or edit `config/rules.yaml` by hand; either way, changes take effect on the next collection run (no restart needed) or immediately via `reg-radar reclassify --axis <a> --by rule`.

`scripts/eval_classifier.py` compares live `by: rule` (and, when present, `by: llm:*`) rows against a hand-labelled sample at `tests/fixtures/classification/gold.csv`, printing precision/recall per axis and per value — confidence from a rule or an LLM is not proof of accuracy, this is how drift gets caught.

## Classifying via an agent

`skills/reg-radar-classifier/SKILL.md` instructs any MCP-connected agent to pull unclassified items via `get_unclassified_tool`, read `get_taxonomy_tool`, and write labels back via `classify_items_tool` with `by: "llm:<provider>/<model>"` and a self-reported confidence — no API keys or provider lock-in in this repo; the agent reading the skill *is* the classifier. `get_taxonomy_tool()` returns every axis/value/alias so an agent never needs filesystem access to `config/taxonomy.yaml`. See the skill file for the full worked examples and judgment calls (consultation vs. feedback statement, when an instrument mention counts as "substantive," etc.).

## Batch LLM classifier runner

`skills/reg-radar-classifier/SKILL.md` (above) needs an agent sat in a chat session. `reg-radar classify` does the same job unattended, against a provider of your choosing, so classification can run on a schedule alongside the collector:

```bash
docker compose exec collector python -m regradar.classify classify \
  --provider anthropic --axis instrument --limit 200 --dry-run
```

Drop `--dry-run` to actually write. Omit `--axis` to work every axis in `config/taxonomy.yaml`; `--limit` caps items classified *per axis* this run. Every write goes through `write_classification()` (the same function the skill's `classify_item_tool` calls) with `by: "llm:<provider>/<model>"` and `classifier_id: "reg-radar-classify/<provider>"` — never a bespoke insert. One taxonomy axis is requested per model call; a response with an unknown axis, an unknown value id, the wrong cardinality (more than one value for a single-valued axis), or an out-of-range confidence is rejected, logged, and written nowhere — never quarantined for later review, just a log line.

**Four built-in providers** (`regradar/providers/`), one file each:

| Provider | Requires | Notes |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY`, `pip install reg-radar[llm]` | Structured output via tool-use, built from the taxonomy for the axis in play |
| `openai` | `OPENAI_API_KEY`, `pip install reg-radar[llm]` | Structured output via a JSON-schema response format |
| `ollama` | A local Ollama server (`OLLAMA_BASE_URL`, default `http://localhost:11434`) | Strict prompt instructions + parsing, no key needed |
| `command` | `--command "<shell command>"` | Shells out to any CLI that reads a prompt on stdin and writes text on stdout — see below |

`anthropic`/`openai` import their SDK lazily, only when that provider is actually selected, so the rest of the CLI (and every other provider) works without either package installed. `pip install reg-radar[llm]` (or the `anthropic`/`openai` packages directly) pulls them in.

**The `command` provider** runs a configured shell command, feeding the prompt on stdin and reading the raw response from stdout — no provider-specific code, so a subscription-backed CLI tool (rather than a metered API key) can drive unattended classification, e.g. `--provider command --command "claude -p"`. **Before pointing this at any vendor's subscription CLI for scheduled, unattended use, check that vendor's terms of service actually permit automated use under a subscription plan** — this README makes no recommendation either way; that's an operator decision, not a code one.

**Rate limiting:** `--rpm <n>` caps requests per minute between provider calls (sensible per-provider defaults if omitted — see `regradar/providers/PROVIDER_DEFAULTS`); the wait is real time in production, injectable in tests so the test suite never sleeps.

**Cost logging:** `anthropic`/`openai` calls log input/output token counts and an estimated cost per call, priced from `config/llm_prices.yaml` (`LLM_PRICES_PATH`, editable — it records the date it was last updated). `ollama`/`command` always log as **unmetered** — there's no generic way to price a local model or an arbitrary shelled-out command, so no cost figure is ever fabricated for them.

## Profiles

The taxonomy above says what a document *is*. A **profile** says who should care: a named, versionable filter turning "everything the taxonomy knows" into "what a UK equities dealer, or an EU CASP, needs to read." A profile file (`config/profiles/<name>.yaml`) has:

```yaml
name: uk-equities-dealer
jurisdictions: [gb]             # "eu/*" selects the EU and every jurisdiction beneath it
activities: [equities_dealing]
instruments: []                  # additional explicit instruments
exclude_instruments: [mica]      # hard exclude, applied last
document_types: []               # empty means all
regulators: []                   # empty means all in matched jurisdictions
languages: [en]
include_unclassified: true       # default true - unclassified items are visible by default
```

`activities` (`config/activities.yaml`) is the interpretive layer mapping a line of business to the instruments it needs: `equities_dealing`, `casp` (crypto-asset service provider), `emi` (e-money/payment institution), `bank` (credit institution).

**Expansion:** the jurisdiction set is each selected jurisdiction plus its ancestors; a `<id>/*` entry also adds every descendant (`eu/*` matches ESMA-level output plus every member-state regulator; plain `eu` matches ESMA-level output only). The instrument set is the union of instruments from selected `activities` plus explicit `instruments`, minus `exclude_instruments`. An item matches if its jurisdiction is in the jurisdiction set, and (its instrument labels intersect the instrument set, or it has no instrument label and `include_unclassified` is true), and its `document_type`/`regulators`/`languages` are in the respective lists whenever those aren't empty.

**Four shipped templates** live in `config/profiles/`: `uk-equities-dealer`, `eu-casp`, `eu-emi`, `eu-bank`. Pass one by its name, or write your own YAML file anywhere and pass it by path — `config/profiles/<name>.yaml` is checked first, then the argument is treated as a literal path. Loading a profile that references an unknown jurisdiction, activity or instrument id fails loudly rather than silently matching nothing.

Every MCP query tool that filters items (`search_items_tool`, `get_recent_tool`, `get_warnings_tool`, `count_items_tool`, `get_month_facets_tool`, `get_items_since_tool`, `get_changes_tool`) takes an optional `profile` parameter — a shipped template name or a path — that combines (AND) with every other filter. The same `profile` parameter works on `/api/items`, `/api/months`, `/api/changes`, and `/feeds/<profile>.atom` (where it's part of the path, not a query param), and the web UI has a profile picker in the search filters that round-trips through page/pagination links like the other filters.

Profiles are parsed and expanded once per process and cached — editing a profile file requires a restart to take effect, same as `taxonomy.yaml`.

## MCP server

The MCP server exposes seventeen tools over streamable HTTP, returning structured content (not JSON-encoded strings) so clients get real typed data:

| Tool | Description |
|---|---|
| `search_items_tool` | Full-text search (SQLite FTS5 over title, snippet, and full scraped text; falls back to LIKE if FTS fails), with AND semantics, quoted-phrase support, and `limit`/`offset` pagination. Filter by `jurisdictions` (expanded through ancestors), `labels` (per-axis, e.g. `{"document_type": ["warning"]}`) — see [Classification](#classification) — or `profile` (a shipped template name or a path — see [Profiles](#profiles)). The deprecated `tags`/`source_tags`/`content_tags` filters still work against the old columns for one release |
| `get_recent_tool` | Most recent N items with optional filters, same `tags`/`source_tags`/`content_tags` options as above, plus `profile` |
| `list_regulators_tool` | Full registry with live fetch status, including `stalled` (a source has hit the consecutive zero-item-run threshold — see [Source health](#source-health)) |
| `get_item_tool` | Single item by ID, including raw content, `body_md` (the same text as Markdown), and a `document` object with the canonical document frontmatter fields; optional `max_content_chars` to truncate large text and protect context windows |
| `get_run_log_tool` | Recent collector run stats |
| `get_warnings_tool` | Items whose `source_tags` include `warnings`, optionally filtered by date range and `profile` — see note below. Deprecated for one release: kept as an alias, prefer `search_items_tool(labels={"document_type": ["warning"]})` |
| `count_items_tool` | Total count matching a search, without fetching rows — cheaper than `search_items_tool` when only the number matters; also takes `profile` |
| `get_month_facets_tool` | Item counts grouped by publish month for a search, for a month-picker style view; also takes `profile` |
| `list_tags_tool` | Every distinct `source_tags`/`content_tags` value in use, with counts. Deprecated for one release: prefer `list_labels_tool` |
| `list_labels_tool` | Every taxonomy axis (config-driven) with its distinct classified values and item counts — discover valid `labels` values for `search_items_tool` before guessing |
| `get_items_since_tool` | Items collected (`fetched_at`) strictly after a given timestamp, oldest first — also takes `profile`. Deprecated: only sees newly collected items, not a reclassification or human correction — prefer `get_changes_tool` |
| `get_health_tool` | Most recent collector run's status, freshness, database integrity (see [Backups & recovery](#backups--recovery)), and `stalled_sources` (see [Source health](#source-health)) — check this before trusting search results after a long gap |
| `get_taxonomy_tool` | Every taxonomy axis/value/alias/version — the source of truth for valid `labels`/classification values, no filesystem access needed |
| `get_unclassified_tool` | Items with no live classification on a given axis, oldest first, with `title`/`body_md`/`regulator`/`jurisdiction`/other-axis `labels` — the classification backlog, paged; `include_low_confidence` also surfaces items whose only row on that axis never reached the effective-label threshold |
| `classify_item_tool` | Writes one classification (any `by` value) and returns the item's new effective labels — see [Classification](#classification) |
| `classify_items_tool` | Writes up to 100 classifications in one call; validates every entry before writing any, so one bad entry doesn't cost the rest of the batch — returns written item ids plus per-entry errors |
| `get_changes_tool` | Everything that changed since an opaque cursor — the sync primitive (see [Export and sync](#export-and-sync)); replaces `get_items_since_tool` |

**On `get_warnings_tool`:** some regulators only have one general news feed, so it's source-tagged `warnings` alongside `news`/`publications` rather than leaving genuine warnings uncollected — this means that feed's entire output shows up here, not just actual warning notices. Sources with a dedicated warnings-only feed are precise; broad `all_news`-style feeds are not. Treat results from a broad feed as "may contain warnings," not a pre-filtered list. `labels={"document_type": ["warning"]}` reads the classification store instead — a superset that also catches warnings a rule or a human classified on a broad feed, not just items from a feed tagged `warnings` wholesale.

Server endpoint: **`http://localhost:8000/mcp`**.

**Port already in use?** Don't edit `docker-compose.yml` — add an untracked `docker-compose.override.yml` (already git-ignored) remapping the host port, e.g.:
```yaml
services:
  mcp:
    ports: !override
      - "127.0.0.1:8001:8000"
```
Use the `!override` merge tag — a plain list under `ports:` gets *appended* to the base port mapping by Compose, not replaced, and both would then try to bind and fail.

If you remap the port, also set `MCP_EXTRA_ALLOWED_HOSTS` in `.env` (e.g. `127.0.0.1:8001,localhost:8001`) — the server validates the `Host` header against the *external* port a client actually connects to, and rejects anything not on the list with a 421.

## Web UI

A minimal, functional list/search/reader view — no build step, server-rendered HTML with a small amount of vanilla JS for filter UX — at **`http://localhost:8080`**:

- **List view** shows collected items, most recent first, 50 per page with pagination. Filters: keyword search (auto-submits as you type), an exact-match toggle, a multi-select regulator panel (searchable by country, abbreviation, or full name, with per-regulator item counts, last-fetch date, and consecutive-failure warnings), a jurisdiction panel (expanded through ancestors, same semantics as `search_items_tool`'s `jurisdictions` — see [Classification](#classification)), a date range filter, and a **profile picker** (shipped templates from `config/profiles/*.yaml` — see [Profiles](#profiles)). A **By month** sidebar shows publication-month facets for the current query/filter set.
- **Search syntax:** unquoted words are AND-ed (`bafin fines` matches items containing both, in either order); `+` behaves the same as a space (`bafin+fines`); wrap a phrase in quotes for adjacency (`"administrative fines"` only matches that exact phrase, not the two words scattered separately); check "exact match" to treat the entire query box as one literal phrase. Search uses the same FTS5 index as the MCP server (title, snippet, and full scraped text).
- **Reader view** (click any title) strips the item down to title, regulator, date, source and content tags, a link to the original source, and cleaned body text — full scraped text where available, otherwise the RSS snippet with a note saying so and a link out to the source. A minimal classification form lets you set `document_type` and add/remove `instrument` values by hand — it writes through the same `write_classification()` path as everything else, as a `human:*` correction that always wins over anything automated for that axis.
- **JSON API** at `/api/items`, `/api/items/{id}`, `/api/regulators`, `/api/months` (both `/api/items` and `/api/months` accept `profile`), `/api/unclassified?axis=<a>` (mirrors `get_unclassified_tool`), `/api/changes` (see [Export and sync](#export-and-sync)), and `POST /api/items/{id}/classifications` (body: `{"axis", "value", "by", "confidence"?, "classifier_id"?, "remove"?}` — the same write path the reader view's form uses; `by` should be `human:<name>` for a genuine human correction) — same query logic as the HTML views, same filters, exists so a future frontend (or a script) doesn't have to scrape the HTML or duplicate query logic. The webui service reads the SQLite database directly (via the same query module the MCP server uses), so it works independently of whether the `mcp` container happens to be up.

This is intentionally plain — a functional first pass, not a finished design. The query/data layer (`mcpserver/queries.py`) is decoupled from presentation, so upgrading the look (or swapping in a JS frontend against the JSON API) later doesn't require touching how data is fetched or filtered.

## Connecting an agent

reg-radar speaks standard MCP over streamable HTTP — any MCP-compatible client works, not just one vendor's tool. Setup varies by client:

**Claude Code:**
```bash
claude mcp add --transport http reg-radar http://localhost:8000/mcp
```

**Clients using the common `mcpServers` JSON config** (Claude Desktop, and many other MCP-compatible apps use a similar shape — check your client's docs for the exact config file location):
```json
{
  "mcpServers": {
    "reg-radar": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

**ChatGPT, Gemini, Hermes, or anything else:** most modern agent tools have a settings area for adding a remote/custom MCP server — look for "MCP", "Connectors", or "Tools" in your client's settings and supply the URL above. Exact steps vary by vendor and change over time, so check your specific client's current documentation rather than relying on this README to have the latest UI paths.

## Skills

`skills/` contains plain-markdown instruction documents — not a Claude-specific format, just structured guidance any agent (or human) can read and follow:

- **[`reg-radar-operator`](skills/reg-radar-operator/SKILL.md)** — how to run, query, and navigate reg-radar: checking whether the stack is up, the MCP tool reference, and how to weight relevance using the operator's context (see below).
- **[`reg-radar-onboarding`](skills/reg-radar-onboarding/SKILL.md)** — a guided interview that generates your private operator context (see next section).
- **[`reg-radar-classifier`](skills/reg-radar-classifier/SKILL.md)** — works the classification backlog via `get_unclassified_tool`/`classify_items_tool`, with worked examples for `document_type` and `instrument` (see [Classifying via an agent](#classifying-via-an-agent)).

If your agent harness has a native skill/tool-loading mechanism (e.g. Claude Code's `Skill` system), point it there. Otherwise, paste the file contents into your system prompt or custom instructions, or just tell your agent to read the file and follow it.

### Operator context (`refs/`)

`skills/reg-radar-operator/refs/` holds two files:

- **`example.context.md`** (tracked in git) — the template. Explains what each section means.
- **`context.md`** (git-ignored, generated by the onboarding skill or filled in by hand) — your actual entity facts: what you're licensed for, where, what you do and don't do, which affiliates matter, what kind of regulatory content to prioritise. Never committed — this is exactly the kind of detail that shouldn't end up in a public repo, since anyone running reg-radar has their own regulatory profile.

Run the onboarding skill any time to generate or rebuild this file.

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/app/data/reg_radar.db` | SQLite database path |
| `SCHEDULE` | `0 6 * * *` | Cron schedule (UTC) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `USER_AGENT` | `Mozilla/5.0 (compatible; reg-radar/2.0; +https://github.com/klinikal/reg-radar)` | HTTP User-Agent header |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `REQUEST_RETRIES` | `3` | Retry attempts per source |
| `REGULATORS_PATH` | `/app/config/regulators` | Regulator registry directory (a single file path also still works) |
| `MODULES_PATH` | `/app/config/modules.yaml` | Module selection — what this install collects; see [Modules](#modules) |
| `REGRADAR_COLLECT_ALL` | *(unset)* | Set to `true` to collect the whole registry when there is no `modules.yaml` |
| `TAXONOMY_PATH` | `/app/config/taxonomy.yaml` | Classification axis/value vocabulary — see [Classification](#classification) |
| `JURISDICTIONS_PATH` | `/app/config/jurisdictions.yaml` | Jurisdiction hierarchy |
| `ACTIVITIES_PATH` | `/app/config/activities.yaml` | Activity-to-instrument mapping — see [Profiles](#profiles) |
| `PROFILES_DIR` | `/app/config/profiles` | Directory of shipped profile templates |
| `RULES_PATH` | `/app/config/rules.yaml` | Classification rules engine config — see [Classification rules](#classification-rules) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | *(unset)* | API keys for `reg-radar classify --provider anthropic\|openai` — see [Batch LLM classifier runner](#batch-llm-classifier-runner); only needed for the provider you use |
| `LLM_PRICES_PATH` | `/app/config/llm_prices.yaml` | Price table for `reg-radar classify` cost logging — see [Batch LLM classifier runner](#batch-llm-classifier-runner) |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | FlareSolverr service URL; leave blank to disable (Cloudflare-protected sources are then skipped) |
| `FLARESOLVERR_MIN_INTERVAL_SECONDS` | `8` | Minimum seconds between FlareSolverr requests |
| `FLARESOLVERR_MAX_RETRIES` | `3` | Retry attempts per FlareSolverr fetch |
| `FLARESOLVERR_RETRY_BACKOFF_SECONDS` | `45` | Base backoff when a FlareSolverr fetch looks blocked (doubles per retry) |
| `STALL_THRESHOLD_RUNS` | `7` | Consecutive successful-but-empty runs before a source is flagged `stalled` (see [Source health](#source-health)) |
| `MCP_EXTRA_ALLOWED_HOSTS` | *(unset)* | Comma-separated `host:port` values to add to the MCP server's Host-header allowlist — required if you remap the published port (see above) |
| `ARTEFACTS_DIR` | `/app/data/originals` | Where fetched originals (HTML/PDF) are stored — see [Canonical documents & artefacts](#canonical-documents--artefacts) |
| `MAX_ARTEFACT_BYTES` | `26214400` (25 MB) | Artefacts larger than this are not stored (the item itself is still collected) |
| `STORE_ARTEFACTS` | `true` | Set to `false` to skip writing artefacts entirely (a constrained install) |
| `FEED_MAX_ITEMS` | `50` | Entries returned by `/feeds/<profile>.atom` — see [Export and sync](#export-and-sync) |
| `WEBHOOKS_PATH` | `/app/config/webhooks.yaml` | Webhook subscriber config — see [Export and sync](#export-and-sync); a missing file disables webhook delivery entirely |
| `DIGESTS_PATH`, `SMTP_*`, `TELEGRAM_BOT_TOKEN` | see [Digests](#digests) | Scheduled email/Telegram/webhook digest config; a missing `digests.yaml` disables digests entirely |

## Canonical documents & artefacts

Every item is also available as a canonical Markdown document with YAML frontmatter (`regulator`, `jurisdiction`, `labels`, `content_hash`, etc.) — the format any downstream tool (Obsidian, a vector store, another script) ingests, described in [`docs/design/regradar-architecture.md`](docs/design/regradar-architecture.md#3-canonical-document-format) and validated against [`docs/schema/document-v1.json`](docs/schema/document-v1.json). `get_item_tool` and `/api/items/{id}` include it as `document`.

The collector also keeps the original fetched artefact (HTML or PDF) for every scrape item that has one, under `ARTEFACTS_DIR` (content-addressed by item id) and referenced from the `item_artefacts` table.

`reg-radar render <item_id> --out <dir>` writes one item's document to disk; `reg-radar render --all --out <dir>` writes every item, skipping files whose content hasn't changed. Run it via `docker compose exec collector reg-radar render --all --out /app/data/export` (or any container that has the database mounted).

If a body-conversion library is upgraded, `scripts/reprocess_bodies.py --dry-run` reports how many stored artefacts would be reconverted; drop `--dry-run` to actually rewrite `body_md` for those items.

Every reader (`get_item_tool`, `get_changes_tool`, `reg-radar render`/`export`, ...) already falls back to deriving a body from `raw_content` on the fly for pre-v2 rows where `body_md` is empty, so upgrading from a database that predates `body_md` doesn't lose any content at read time. Run `scripts/reprocess_bodies.py --from-raw-content` once after upgrading such a database to persist that derived body into `body_md` itself (`--dry-run` first, as above) — this is what lets cursor-based consumers (`get_changes_tool`/webhooks/digests) see those items as a change, rather than only benefiting readers that re-fetch the item directly.

## Export and sync

The primitive every downstream adapter builds on is "everything that changed since cursor X" (architecture doc [§6](docs/design/regradar-architecture.md#6-export-and-sync-contract)). A cursor is an opaque string (`<updated_at>|<item_id>`, don't parse it) returned by every call below — pass it back in as `since` to resume exactly where you left off. `updated_at` bumps on any content or label change, not just a new fetch, so this catches a reclassification or human correction that `get_items_since_tool`/`get_recent_tool` (which key off `fetched_at`) would miss.

- **`get_changes_tool`** (MCP) and **`GET /api/changes?since=&profile=&limit=&include_body=`** (HTTP) both return `{"items": [...], "next_cursor"}`, items oldest-first, frontmatter-shaped like `get_item_tool`'s `document`. `include_body=true` also includes `body_md`; omit it to keep responses small when only checking what's new.
- **`reg-radar export --format markdown|jsonl --out <dir> [--profile <p>] [--since <cursor>] [--with-originals] [--exclude-regulator-file <yaml>]`** writes the section 3.1 layout (`markdown`) or a snapshot to `<out>/export.jsonl` (`jsonl`). `--format` is repeatable — `--format markdown --format jsonl` writes both from the same pass over the change set; it defaults to `markdown` alone if omitted. Both are idempotent: an unchanged markdown file is never rewritten, and `export.jsonl` is only rewritten when its content actually changes. `export.jsonl` is a **full current-state snapshot**, not an append log — exactly one line per item id, latest wins. Each run rebuilds it by folding whatever changed since the last export into the existing snapshot (so an update to an already-exported item replaces its one line rather than adding a second), then writes the result back only if the bytes differ from what's already on disk — a no-op run leaves the file's mtime untouched. This is what the [published dataset](#published-dataset) reads: "the whole corpus right now," not a history of every change. Every export also computes `label_provenance` for each item from its live classification rows, same shape as `get_item_tool`'s document. The command persists its own cursor at `<out>/.reg-radar-cursor`, so a scheduled export without `--since` always picks up only what's new since the last run — **all formats written into the same `--out` share that one cursor**, so run `--format markdown --format jsonl` together rather than as two separate commands into the same directory: the second command would read the first's already-advanced cursor and see nothing left to export. (If a persisted cursor is found with no prior output for a format being requested — no `export.jsonl`, or no `.md` files under `export/` — the export falls back to a full export for safety instead of trusting it.) A missing or unparseable persisted cursor falls back to a full export too (logged, not an error) rather than risk corrupting the target directory. `--since` on the command line always overrides the persisted cursor for that one run. `--with-originals` also copies each item's artefact into `<out>/originals/`, mirroring the artefact's own path (see [Canonical documents & artefacts](#canonical-documents--artefacts)); omit it and the export is Markdown/JSONL only. `--exclude-regulator-file <yaml>` drops items from any regulator id listed in that file (a flat YAML list, e.g. [`config/dataset_exclusions.yaml`](config/dataset_exclusions.yaml)) out of the export entirely — used by the published dataset workflow, has no effect on the live collector or on an export without the flag. Run it the same way as `reg-radar render`: `docker compose exec collector reg-radar export --format markdown --format jsonl --out /app/data/export`.
- **`GET /feeds/<profile>.atom`** — a standard Atom feed for any RSS/Atom reader, newest first, capped at `FEED_MAX_ITEMS` entries (default 50), summary only (never the full body) with a link back to the item.
- **Webhooks**: `config/webhooks.yaml` (a list, unset by default — a missing file means webhook delivery is entirely skipped) defines subscribers pulling from the same change stream after every collection run:

  ```yaml
  - profile: uk-equities-dealer
    url: https://example.org/hooks/reg-radar
    secret_env: WEBHOOK_SECRET_UK_EQUITIES   # the secret itself lives in the env, never in this file
    batch_size: 25       # default 25
    max_retries: 5        # default 5
    backoff_seconds: 30   # default 30, doubles per retry
  ```

  Each subscriber gets its own cursor (stored in SQLite, not a file) advanced only after a successful (2xx) delivery, so a failed batch is retried — with doubling backoff — on the *same* items next time rather than skipped. The POST body is `{"cursor", "next_cursor", "items"}` (same item shape as `/api/changes`), signed with `X-Reg-Radar-Signature: sha256=<hex hmac-sha256 of the raw body>` using the subscriber's own secret — verify it before trusting a delivery. One subscriber's failure (bad URL, receiver down, wrong secret) never blocks another's delivery. Delivery outcomes are logged to a `webhook_deliveries` table for operator visibility.

## Digests

`config/digests.yaml` (a list, unset by default — a missing file disables all digests) turns "everything a profile hasn't seen yet" into a scheduled email, Telegram or webhook notification, built on the same [change stream](#export-and-sync) as everything above:

```yaml
- name: uk-equities-daily
  profile: uk-equities-dealer      # config/profiles/<name>.yaml
  schedule: "0 7 * * *"            # cron, UTC
  send_empty: false                # default false - skip rather than send "nothing new"
  channels:
    - type: email
      to: ops@example.org
    - type: telegram
      chat_id: "-1001234567890"    # numeric chat id only
    - type: webhook
      url: https://example.org/hooks/reg-radar-digest
      secret_env: WEBHOOK_SECRET_DIGEST_UK   # same secret-in-env convention as webhooks.yaml
```

Each digest is grouped by jurisdiction then regulator, one entry per item (title, `document_type`, `instrument` labels, a one-line snippet, and a link), rendered as both Markdown and HTML (used as the plain-text and HTML parts of an email). Its own cursor is tracked in the same SQLite mechanism as webhook subscribers (a `digest_state` table, keyed by the digest's `name`) and is only advanced once **every** configured channel for that digest succeeds — a partial failure (e.g. email delivered, Telegram down) leaves the cursor untouched, so the whole digest, not just the failed channel, is retried in full next time, and a missed scheduled run catches up exactly rather than skipping items.

- **`reg-radar digest --preview --profile <name> [--since <cursor>]`** renders and prints the digest to stdout; it never delivers anything or touches stored state — use it to see what a digest would contain before wiring up channels.
- **`reg-radar digest --send [--name <digest-name>]`** renders and delivers. With `--name`, only that `config/digests.yaml` entry runs; without it, every configured digest runs once. Run it manually, from the collector's own scheduler (below), or from an external cron/systemd timer — all three call the same code path.
- The collector process reads `config/digests.yaml` at startup and adds one cron job per digest to its existing scheduler (independent of the collector's own `SCHEDULE`) — no second scheduling mechanism. A missing file is a no-op; an invalid one is logged and skipped, never crashing the collector.
- **Email** uses `smtplib` with the SMTP settings below, sending `multipart/alternative` (plain text + HTML parts).
- **Telegram** uses the Bot API's `sendMessage`, chunked at its 4096-character message limit on paragraph boundaries.
- **Webhook** reuses the signed-POST mechanism above exactly (same HMAC signing, same retry/backoff) — no second implementation.

| Variable | Default | Description |
|---|---|---|
| `DIGESTS_PATH` | `/app/config/digests.yaml` | Digest config; a missing file disables digests entirely |
| `SMTP_HOST` | *(unset)* | SMTP server hostname — required for any `email` channel |
| `SMTP_PORT` | `587` | SMTP server port |
| `SMTP_USER` | *(unset)* | SMTP auth username (omit for an unauthenticated relay) |
| `SMTP_PASSWORD_ENV` | *(unset)* | Name of the env var holding the SMTP password — the password itself is never in config, matching `secret_env` above |
| `SMTP_FROM` | *(unset)* | `From` address — required for any `email` channel |
| `SMTP_STARTTLS` | `true` | Set to `false` to skip STARTTLS |
| `TELEGRAM_BOT_TOKEN` | *(unset)* | Bot token — required for any `telegram` channel; never put it in `digests.yaml` |
## Published dataset

A new install starts with an empty database and would otherwise have to run every historic backfill (see [Historic backfills](#historic-backfills)) — slow, FlareSolverr-throttled, and measured in hours — before it has anything worth querying. Instead, a nightly [GitHub Actions workflow](.github/workflows/publish-dataset.yml) publishes the full corpus as a downloadable release: load one tarball and you have the whole history immediately.

**What importing does and doesn't change.** It saves you the *backfill scripts*, not the routine collection run. A collection run reads whatever is currently on each source's feed/listing pages either way — nothing filters fetches by date, and re-collecting an item you already imported is a no-op, because item ids are content-derived. The stored sync cursor is a **bookmark, not a filter**: it records where the snapshot ends so you can ask "what has arrived since?" — see the cursor bullet below.

- **Cadence**: nightly, at 02:00 UTC. Each release is a full snapshot (not an incremental diff) of everything `reg-radar export` would produce, in both `markdown` and `jsonl` formats, minus any regulator listed in [`config/dataset_exclusions.yaml`](config/dataset_exclusions.yaml).
- **Download URL pattern**: `https://github.com/klinikal/reg-radar/releases/download/<tag>/reg-radar-dataset-<date>.tar.gz` plus a `manifest.json` asset on the same release. `latest` is a rolling tag moved forward every run; `dataset-YYYY-MM-DD` is never moved, so pin to one for a fixed corpus.
- **`manifest.json`** (schema version 1): `regradar_schema`, `taxonomy_version`, `generated_at`, `item_count`, `per_regulator_counts`, `excluded_regulators`, `sha256` (of the tarball), `tarball` (its filename).
- **`reg-radar import-dataset <url|path> [--force] [--db-path <path>] [--timeout <s>] [--max-bytes <n>]`**: downloads (or reads a local path to) the tarball, verifies its sha256 against `manifest.json` (checked beside the tarball first, then inside it), and refuses to run against a database that already has items unless `--force` is passed. Loads every item and its classifications (with original provenance — `by`, `confidence`, `taxonomy_version` — preserved) directly from `export.jsonl`, no re-fetching or re-parsing Markdown. Prints a summary: items loaded, any excluded regulators (which need a manual backfill if you want their content too), and the sync cursor with ready-to-paste commands for using it. A URL download is bounded by `--timeout` (default 60s per read) and `--max-bytes` (default 2 GiB, GitHub's asset limit); exceeding either deletes the partial file and fails loudly. Run it the same way as `reg-radar export`: `docker compose exec collector reg-radar import-dataset https://github.com/klinikal/reg-radar/releases/latest/download/reg-radar-dataset-2026-09-09.tar.gz`.
- **The sync cursor**, stored under `dataset_import_cursor` in the `sync_state` table, is set to the manifest's `generated_at` in the [change stream's](#export-and-sync) `<updated_at>|<id>` shape. Three ways to use it, none of them automatic except the third:
  - `get_health_tool()` (and `get_health`) reports it as `dataset_import` = `{cursor, generated_at, imported_at}`, or `null` if this database was never seeded from a dataset.
  - Pass it as `since` to any [change-stream](#export-and-sync) consumer that should see only post-import changes: `get_changes_tool(since=…)`, `GET /api/changes?since=…`, or a webhook subscriber's starting cursor. `import-dataset` prints the ready-to-paste lines for you.
  - `reg-radar export --since <cursor>` does the same for a one-off export of just the newer items. Note that this is the **explicit** path only: `reg-radar export` never applies the cursor on its own, so an export into a fresh `--out` always writes the whole corpus — which is the point of having imported one.
- **[`NOTICE`](NOTICE)** records what's known about each regulator's reuse terms, one row per currently enabled regulator — a starting inventory, not a legal sign-off. Most rows are marked "unclear" (no reuse statement found or checked yet) and are still included in the published dataset; `config/dataset_exclusions.yaml` is where a regulator moves once its terms are confirmed to forbid redistribution.

## Backups & recovery

The live database lives in a Docker-managed named volume (`reg_radar_data`), not a bind-mounted host folder — SQLite's WAL mode needs precise file-locking guarantees that Docker Desktop's macOS bind-mount bridge doesn't fully honour, which caused a real corruption incident. A `litestream` service continuously replicates the database to `./backups/reg_radar` on the host disk, so a corruption or data-loss event is a restore, not a salvage job.

The collector also runs a full `PRAGMA integrity_check` at the start of every collection cycle, so corruption is caught immediately and surfaced loudly (`get_health_tool`'s `db_integrity` field: `"ok"`, `"corrupted"`, or `"unknown"` if no run has completed yet) — not discovered later as a failed search.

**`originals/` is not covered by Litestream** — Litestream only replicates the SQLite file. The original artefacts (fetched HTML/PDF, see [Canonical documents & artefacts](#canonical-documents--artefacts)) live on the same data volume under `originals/` and need their own host-level backup (a periodic `docker cp`/volume snapshot, or whatever backup tool you already run against the host).

Full restore procedure and details: see [Backups & recovery in the operator skill](skills/reg-radar-operator/SKILL.md#backups--recovery).

## Upgrading from casp-radar

This project was renamed from casp-radar to reg-radar (same code, same data, new name and new default volume/database names; the codebase briefly used the interim name `regradar` during v2 development). If you're on a fresh install, ignore this section.

**Option A: keep running on the old volume for now.** Add this to `.env` and restart — nothing else changes:
```bash
echo "DATA_VOLUME_NAME=casp_radar_data" >> .env
docker compose up -d
```
Remove that line once you've migrated with Option B so future upgrades use the new default.

**Option B: migrate to the new volume name.** This copies your existing data into a volume named `reg_radar_data` and moves the Litestream backup directory to match. Do the steps in this order — the old volume is only deleted at the end, after you have confirmed the new one works.

1. If you took Option A earlier, remove the override first, or step 4 will still resolve to the old volume name and start the stack against a brand-new empty one:
   ```bash
   docker compose down
   sed -i.bak '/^DATA_VOLUME_NAME=/d' .env
   ```
   (On macOS `sed -i.bak` leaves a `.env.bak` you can delete once you have checked `.env`.) If you never took Option A, just run `docker compose down`.
2. Copy the data into the new volume:
   ```bash
   docker volume create reg_radar_data
   docker run --rm -v casp_radar_data:/from -v reg_radar_data:/to alpine sh -c "cp -a /from/. /to/"
   ```
3. Move the Litestream backup directory to match:
   ```bash
   mv ./backups/casp_radar ./backups/reg_radar
   ```
4. Start the stack and confirm the migration worked — `count_items_tool` must return the same number of items as before:
   ```bash
   docker compose up -d
   ```
5. **Only once step 4 checks out**, delete the old volume:
   ```bash
   docker volume rm casp_radar_data
   ```
   Deleting it before the check leaves you with nothing to fall back to: Litestream would already have replicated the new (possibly empty) database over `./backups/reg_radar`.

## Migrating v1 data

The volume rename above moves the database file. These are the one-off steps that bring the *contents* of a v1 database up to the v2 schema — labels, classifications and document bodies. Run them once, after the stack is up on the new volume; full detail and `--dry-run` notes are in [Classification](#classification).

1. Run the collector once (it refreshes the `regulators` table's `jurisdiction` column on every upsert), or run `scripts/migrate_regulator_labels.py --db-path <path>` directly.
2. `reg-radar reclassify --axis document_type`
3. `reg-radar reclassify --axis instrument --by rule`
4. `reg-radar reclassify --axis document_type --by rule`
5. `scripts/migrate_content_tags.py`
6. `scripts/reprocess_bodies.py --from-raw-content`

See [Classification rules](#classification-rules) above for the rules engine that replaced the old keyword-alerts feature; classified labels feed into every filter above, including `config/webhooks.yaml` subscribers — see [Export and sync](#export-and-sync) for webhook delivery and [Digests](#digests) for scheduled email, Telegram and webhook digests.

## Contributing

The primary contribution surface is `config/regulators/` - see [CONTRIBUTING.md](CONTRIBUTING.md) for the one-entry/one-fixture/one-test flow. Community PRs to:
- Add missing regulators (Liechtenstein PFSA is a known gap)
- Fix broken feed URLs
- Add or improve scrape selectors for no-RSS regulators

## Naming

The product is **reg-radar** (hyphenated). The Python package stays `regradar` (hyphens aren't valid in a Python import name), so `python -m regradar.cli`, `from regradar import ...`, and the `regradar/` directory are unchanged — only the console script (`reg-radar`), Docker/compose names, the MCP server name, and other user-facing strings use the hyphenated form.

## Licence

MIT

## Credits

reg-radar bypasses Cloudflare-protected sources using [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (MIT licence), run as a standalone Docker service — see [Coverage](#coverage) above.

## Copyright

(c) Klinikal Limited, 2026. 
