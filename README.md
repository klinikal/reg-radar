# regradar

> Renamed from casp-radar. If you have an existing install, see [Upgrading from casp-radar](#upgrading-from-casp-radar).

Open-source regulatory intelligence tool for European (EU, UK coming soon) entities. Collects news, warnings, and publications from financial regulators across the EU on a daily schedule, stores them in SQLite, and exposes the database via an MCP server so AI agents can query it. There is also a lightweight webUX that can be run to see, query and search.

**Bring your own agent.** This tool provides clean structured data, a simple web UI, and an MCP server. AI classification and relevance scoring are out of scope — connect any MCP-compatible agent (Claude, ChatGPT, Gemini, Hermes, a custom agent, whatever you use).

## Prerequisites

- **[Docker](https://www.docker.com/products/docker-desktop/)** (or Docker Engine + the Compose plugin on Linux) — required to run the stack. This is third-party software, not something this tool installs for you. If you don't have this, it's suggested you download it directly https://www.docker.com/ or by using Homebrew (`brew install docker`).
- **Git**, to clone the repo (`brew install git`).

Beyond that, `docker compose up` pulls the `python:3.11-slim` base image and a `flaresolverr` image from public container registries, and installs pinned Python packages from PyPI per `collector/requirements.txt` / `mcpserver/requirements.txt` — standard supply chain for a Dockerised Python project, versions are pinned in those files if you want to audit them before building.

## Getting started (zero to working)

1. **Run the stack**
   ```bash
   git clone https://github.com/klinikal/regradar
   cd regradar
   cp .env.example .env
   docker compose up -d
   ```
   This starts four services: `collector` (fetches on a schedule — see [Configuration](#configuration)), `mcp` (the query server, exposed at `http://localhost:8000/mcp`), `webui` (a browsable list/search view at `http://localhost:8080` — see [Web UI](#web-ui)), and `flaresolverr` (a Cloudflare-bypass helper for a handful of protected sources).

2. **Get some data in.** On a fresh install the database is empty until the first run. To collect immediately:
   ```bash
   docker compose exec collector python -m collector.main --once
   ```

3. **Browse it** at `http://localhost:8080` to confirm data's actually there, or **connect your agent** to the MCP server — see [Connecting an agent](#connecting-an-agent) below for setup across different clients.

4. **Give your agent the onboarding skill.** Point it at [`skills/regradar-onboarding/SKILL.md`](skills/regradar-onboarding/SKILL.md) and ask it to onboard you. It'll interview you about your entity — what you're licensed for, where, what you do and don't do — and generate a private `skills/regradar-operator/refs/context.md` (git-ignored, never leaves your machine) so the agent knows what's actually relevant to you instead of treating every regulator in the registry as equally important.

5. **You're set.** From here, just ask your agent regulatory questions — "any recent CASP warnings from Germany?", "what's ESMA published this week?" — and point it at [`skills/regradar-operator/SKILL.md`](skills/regradar-operator/SKILL.md) if it needs a refresher on how to navigate the data. See [Skills](#skills) for more on how these work.

## Coverage

44 entries in the registry: 4 EU supranational bodies (ESMA, AMLA, EBA, EIOPA), national/EEA competent authorities (some jurisdictions have more than one — e.g. Belgium's FSMA and NBB, Italy's CONSOB/Banca d'Italia/OAM), plus the UK's FCA as a deliberate non-EEA exception. Full list, per-source status, and notes on disabled entries: `config/regulators.yaml`.

Most active sources are RSS/Atom feeds or scrape targets; a smaller set is disabled (Cloudflare/SPA-protected, no working feed found, or architecturally unscrapeable) — documented in `regulators.yaml` so gaps stay visible rather than silently missing.

Note: some sources are better than others. This is beta software. Contributions are welcome to fill gaps, enhance coverage of regulators or sources *you* care about.

Optional: the `flaresolverr` service (included in Docker Compose by default) enables collection from Cloudflare-protected sources (e.g. Lithuania's lb.lt).

### Non-EEA additions

The UK's FCA is included as a deliberate exception to the EEA scope above — see the "Non-EEA Additions" section at the bottom of `config/regulators.yaml` for why and what's collected (news, warnings, notices and decisions, policy and guidance). It's kept structurally separate so the EEA coverage numbers above stay accurate.

### Cloudflare coverage

Some regulators' websites e.g. MFSA in Malta are Cloudflare-protected — confirmed to block plain HTTP regardless of User-Agent, so every request goes through `flaresolverr` (already in the Docker stack). `config/regulators.yaml` defines the taxonomy. Some are `enabled: true` by default, others are defined but disabled. Flip `enabled: true` on any of them, or target them directly via the backfill script below, without needing to redefine URLs or selectors.

Because every request needs FlareSolverr (a real browser, ~10-15s each), `collector/fetcher.py` throttles and backs off automatically — a minimum interval between requests (`FLARESOLVERR_MIN_INTERVAL_SECONDS`, default 8s) and longer exponential backoff specifically when the target's response looks like a ban/block rather than a normal failure (`FLARESOLVERR_RETRY_BACKOFF_SECONDS`, default 45s, doubling per retry, `FLARESOLVERR_MAX_RETRIES` attempts). This is global — any current or future `cloudflare_protected` source benefits. It also skips wasting plain-HTTP retry attempts against a known-Cloudflare-protected source, since those still count toward the target's own rate limit for no chance of success.

### Historic backfills

The regular collector only scrapes the first page of each scrape-type source on every run (newest-first) — enough to catch new publications going forward, but not the historic archive behind a source that only recently got added here. One-off backfill scripts under `scripts/` read URLs/selectors straight from `config/regulators.yaml` (no duplicated config to drift out of sync), all support `--dry-run`, and none run automatically — running one is a deliberate decision, not a side effect of adding a source:

- `backfill_esma_library.py` — ESMA's document library, ~7,260 documents across ~363 pages. Has been run; that data is in the default dataset once you clone and run the collector's normal `--once`/scheduled pass — no need to re-run this unless you're rebuilding from an empty database.
- `backfill_fca_publications.py` — FCA's notices/decisions (~5,980 documents) and policy/guidance (~3,743 documents). **Has not been run** — built and tested (`--dry-run --max-pages N`) but the full historic crawl is left to you to trigger deliberately, since FCA is a non-EEA addition and a larger crawl than ESMA's.
- `backfill_mfsa_publications.py` — any MFSA category, enabled or not (`--category <label>`), or all ~65 at once (`--all`). **Has not been run at scale** — verified against several categories including a currently-disabled one, but a full `--all` run is, by design, hours-long given FlareSolverr's per-request cost and the built-in throttling — best scheduled overnight or off-hours, not run casually.
- `backfill_cnmv_warnings.py` — CNMV's unregistered-firm warnings archive (~5 pages / ~77 documents as of mid-2026). Small enough to run directly when you want the full history; the daily collector only picks up page 0 going forward.

*More to come. Feel free to use the same format to contribute additional backfills, or more regulators.*

Always `--dry-run` first — backfill scripts fetch and parse every page exactly as a real run would, just without writing to the database, so you can sanity-check counts before committing to a multi-hundred-request crawl of someone else's site.

### Maintenance scripts

Two other one-off scripts under `scripts/` are for disaster recovery or after bulk imports — not part of the daily collector:

- `rebuild_fts_index.py` — rebuilds the SQLite FTS5 search index (`items_fts`) from the `items` table if it ever drifts or you're restoring from backup.
- `rebuild_chunks.py` — rebuilds the `item_chunks` table (paragraph-aware text chunks derived from each item's full content). Chunks are prep infrastructure for future semantic search; the collector does not populate them automatically yet — run this after a large backfill if you want chunks in place.
- `reprocess_bodies.py` — re-runs Markdown body conversion from stored artefacts for items whose `extractor` predates the currently installed library version (see [Canonical documents & artefacts](#canonical-documents--artefacts)).

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

- **`source_tags`** — structural, config-curated per feed in `regulators.yaml` (e.g. `warnings`, `press_releases`, `news`, `publications`, `page_watch`). Set once when a source is configured, same for every item that feed produces.
- **`content_tags`** — what the item is actually about, computed per item. Today this comes from the [keyword alerts](#keyword-alerts) config; nothing else populates it yet.

Regulation-name labels (`casp`, `mica`, `aml`, `art`, `emt`, `dora`) are deliberately excluded from `source_tags` — they used to be stamped on nearly every item from a MiCA-relevant regulator regardless of content, which made them useless for filtering. If you want one of those as a real tag, add it as a keyword rule instead (see below) so it only lands on items that actually mention it.

**Deprecated for one release:** `source_tags`/`content_tags` and the `tags`/`source_tags`/`content_tags` filter parameters keep working against the old columns, but every read path now logs a deprecation warning. Prefer the [classification store](#classification) below — it records *why* a label is there and who/what set it, not just a flat string.

## Classification

Items are classified along **axes** defined in `config/taxonomy.yaml` (currently `document_type`, single-valued, and `instrument`, multi-valued — the specific EU/UK regulation an item concerns). The vocabulary is config, not code: adding a value, or a whole new axis, is a config change, not a code change.

Every classification is written with **provenance** — who or what said it, and how confident it was — to a `classifications` table (`by`: `source_default`, `rule`, `llm:<provider>/<model>`, `human:<name>`, or `import`; a confidence 0.0–1.0; the `taxonomy_version` at write time). Nothing is ever overwritten in place: a reclassification supersedes the old row and adds a new one, so history survives. A human correction (`by: human:*`) always wins over anything automated, permanently, for that axis — an automated reclassification pass can never silently undo one.

`items.labels` is a materialised JSON summary of the current *effective* label per axis (computed by the same precedence logic on every write), kept in sync with a queryable `item_labels(item_id, axis, value)` side table used for filtering — see `search_items_tool`'s `labels` parameter below.

**Jurisdictions** (`config/jurisdictions.yaml`) form a hierarchy — a regulator's jurisdiction has zero or more parents (e.g. every EU member state's parent is `eu`; EEA-EFTA states Norway and Iceland also list `eu` directly). A jurisdiction filter is expanded through its own ancestors before matching: requesting `fr` expands to `{fr, eu}`, so it returns both AMF (`fr`) and ESMA (`eu`) items — a country-level filter also surfaces the supranational regulator that covers it. Requesting `gb` (no parents) matches FCA only.

Re-applying source defaults to existing items after a config change:

```bash
docker compose exec collector python -m regradar.reclassify reclassify --axis instrument --dry-run
docker compose exec collector python -m regradar.reclassify reclassify --axis instrument
```

`--dry-run` reports what would change, including how many live rows on that axis carry an older `taxonomy_version` than the current config, without writing anything. Human corrections are never touched by this or any other automated path. (The rules engine and LLM classifier skill that also write through this same command are a follow-on.)

**Migrating from v1:** `scripts/migrate_regulator_labels.py` renames `sources[].tags` to `sources[].labels` in `regulators.yaml` (preserving comments via `ruamel.yaml`) and lower-cases `jurisdiction` (`UK` → `gb`); `scripts/migrate_content_tags.py` imports existing per-item `content_tags` into the classifications store as `by: import`, confidence 0.5 rows, so they survive but rank below anything better. Both support `--dry-run`.

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

Every MCP query tool that filters items (`search_items_tool`, `get_recent_tool`, `get_warnings_tool`, `count_items_tool`, `get_month_facets_tool`, `get_items_since_tool`) takes an optional `profile` parameter — a shipped template name or a path — that combines (AND) with every other filter. The same `profile` parameter works on `/api/items` and `/api/months`, and the web UI has a profile picker in the search filters that round-trips through page/pagination links like the other filters.

Profiles are parsed and expanded once per process and cached — editing a profile file requires a restart to take effect, same as `taxonomy.yaml`.

## MCP server

The MCP server exposes twelve tools over streamable HTTP, returning structured content (not JSON-encoded strings) so clients get real typed data:

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
| `get_items_since_tool` | Items collected (`fetched_at`) strictly after a given timestamp, oldest first — the polling primitive for "everything new since my last check"; also takes `profile` |
| `get_health_tool` | Most recent collector run's status, freshness, database integrity (see [Backups & recovery](#backups--recovery)), and `stalled_sources` (see [Source health](#source-health)) — check this before trusting search results after a long gap |

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
- **JSON API** at `/api/items`, `/api/items/{id}`, `/api/regulators`, `/api/months` (both `/api/items` and `/api/months` accept `profile`), and `POST /api/items/{id}/classifications` (body: `{"axis", "value", "by", "confidence"?, "classifier_id"?, "remove"?}` — the same write path the reader view's form uses; `by` should be `human:<name>` for a genuine human correction) — same query logic as the HTML views, same filters, exists so a future frontend (or a script) doesn't have to scrape the HTML or duplicate query logic. The webui service reads the SQLite database directly (via the same query module the MCP server uses), so it works independently of whether the `mcp` container happens to be up.

This is intentionally plain — a functional first pass, not a finished design. The query/data layer (`mcpserver/queries.py`) is decoupled from presentation, so upgrading the look (or swapping in a JS frontend against the JSON API) later doesn't require touching how data is fetched or filtered.

## Connecting an agent

regradar speaks standard MCP over streamable HTTP — any MCP-compatible client works, not just one vendor's tool. Setup varies by client:

**Claude Code:**
```bash
claude mcp add --transport http regradar http://localhost:8000/mcp
```

**Clients using the common `mcpServers` JSON config** (Claude Desktop, and many other MCP-compatible apps use a similar shape — check your client's docs for the exact config file location):
```json
{
  "mcpServers": {
    "regradar": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

**ChatGPT, Gemini, Hermes, or anything else:** most modern agent tools have a settings area for adding a remote/custom MCP server — look for "MCP", "Connectors", or "Tools" in your client's settings and supply the URL above. Exact steps vary by vendor and change over time, so check your specific client's current documentation rather than relying on this README to have the latest UI paths.

## Skills

`skills/` contains two plain-markdown instruction documents — not a Claude-specific format, just structured guidance any agent (or human) can read and follow:

- **[`regradar-operator`](skills/regradar-operator/SKILL.md)** — how to run, query, and navigate regradar: checking whether the stack is up, the MCP tool reference, and how to weight relevance using the operator's context (see below).
- **[`regradar-onboarding`](skills/regradar-onboarding/SKILL.md)** — a guided interview that generates your private operator context (see next section).

If your agent harness has a native skill/tool-loading mechanism (e.g. Claude Code's `Skill` system), point it there. Otherwise, paste the file contents into your system prompt or custom instructions, or just tell your agent to read the file and follow it.

### Operator context (`refs/`)

`skills/regradar-operator/refs/` holds two files:

- **`example.context.md`** (tracked in git) — the template. Explains what each section means.
- **`context.md`** (git-ignored, generated by the onboarding skill or filled in by hand) — your actual entity facts: what you're licensed for, where, what you do and don't do, which affiliates matter, what kind of regulatory content to prioritise. Never committed — this is exactly the kind of detail that shouldn't end up in a public repo, since anyone running regradar has their own regulatory profile.

Run the onboarding skill any time to generate or rebuild this file.

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/app/data/regradar.db` | SQLite database path |
| `SCHEDULE` | `0 6 * * *` | Cron schedule (UTC) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `USER_AGENT` | `regradar/2.0 (+https://github.com/klinikal/regradar)` | HTTP User-Agent header |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `REQUEST_RETRIES` | `3` | Retry attempts per source |
| `REGULATORS_PATH` | `/app/config/regulators.yaml` | Regulator registry file |
| `TAXONOMY_PATH` | `/app/config/taxonomy.yaml` | Classification axis/value vocabulary — see [Classification](#classification) |
| `JURISDICTIONS_PATH` | `/app/config/jurisdictions.yaml` | Jurisdiction hierarchy |
| `ACTIVITIES_PATH` | `/app/config/activities.yaml` | Activity-to-instrument mapping — see [Profiles](#profiles) |
| `PROFILES_DIR` | `/app/config/profiles` | Directory of shipped profile templates |
| `ALERTS_PATH` | `/app/config/alerts.yaml` | Keyword-to-tag rules file (optional — see below) |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | FlareSolverr service URL; leave blank to disable (Cloudflare-protected sources are then skipped) |
| `FLARESOLVERR_MIN_INTERVAL_SECONDS` | `8` | Minimum seconds between FlareSolverr requests |
| `FLARESOLVERR_MAX_RETRIES` | `3` | Retry attempts per FlareSolverr fetch |
| `FLARESOLVERR_RETRY_BACKOFF_SECONDS` | `45` | Base backoff when a FlareSolverr fetch looks blocked (doubles per retry) |
| `STALL_THRESHOLD_RUNS` | `7` | Consecutive successful-but-empty runs before a source is flagged `stalled` (see [Source health](#source-health)) |
| `MCP_EXTRA_ALLOWED_HOSTS` | *(unset)* | Comma-separated `host:port` values to add to the MCP server's Host-header allowlist — required if you remap the published port (see above) |
| `ARTEFACTS_DIR` | `/app/data/originals` | Where fetched originals (HTML/PDF) are stored — see [Canonical documents & artefacts](#canonical-documents--artefacts) |
| `MAX_ARTEFACT_BYTES` | `26214400` (25 MB) | Artefacts larger than this are not stored (the item itself is still collected) |
| `STORE_ARTEFACTS` | `true` | Set to `false` to skip writing artefacts entirely (a constrained install) |

## Canonical documents & artefacts

Every item is also available as a canonical Markdown document with YAML frontmatter (`regulator`, `jurisdiction`, `labels`, `content_hash`, etc.) — the format any downstream tool (Obsidian, a vector store, another script) ingests, described in [`docs/design/regradar-architecture.md`](docs/design/regradar-architecture.md#3-canonical-document-format) and validated against [`docs/schema/document-v1.json`](docs/schema/document-v1.json). `get_item_tool` and `/api/items/{id}` include it as `document`.

The collector also keeps the original fetched artefact (HTML or PDF) for every scrape item that has one, under `ARTEFACTS_DIR` (content-addressed by item id) and referenced from the `item_artefacts` table.

`regradar render <item_id> --out <dir>` writes one item's document to disk; `regradar render --all --out <dir>` writes every item, skipping files whose content hasn't changed. Run it via `docker compose exec collector regradar render --all --out /app/data/export` (or any container that has the database mounted).

If a body-conversion library is upgraded, `scripts/reprocess_bodies.py --dry-run` reports how many stored artefacts would be reconverted; drop `--dry-run` to actually rewrite `body_md` for those items.

## Backups & recovery

The live database lives in a Docker-managed named volume (`regradar_data`), not a bind-mounted host folder — SQLite's WAL mode needs precise file-locking guarantees that Docker Desktop's macOS bind-mount bridge doesn't fully honour, which caused a real corruption incident. A `litestream` service continuously replicates the database to `./backups/regradar` on the host disk, so a corruption or data-loss event is a restore, not a salvage job.

The collector also runs a full `PRAGMA integrity_check` at the start of every collection cycle, so corruption is caught immediately and surfaced loudly (`get_health_tool`'s `db_integrity` field: `"ok"`, `"corrupted"`, or `"unknown"` if no run has completed yet) — not discovered later as a failed search.

**`originals/` is not covered by Litestream** — Litestream only replicates the SQLite file. The original artefacts (fetched HTML/PDF, see [Canonical documents & artefacts](#canonical-documents--artefacts)) live on the same data volume under `originals/` and need their own host-level backup (a periodic `docker cp`/volume snapshot, or whatever backup tool you already run against the host).

Full restore procedure and details: see [Backups & recovery in the operator skill](skills/regradar-operator/SKILL.md#backups--recovery).

## Upgrading from casp-radar

This project was renamed from casp-radar to regradar (same code, same data, new name and new default volume/database names). If you're on a fresh install, ignore this section.

**Option A: keep running on the old volume for now.** Add this to `.env` and restart — nothing else changes:
```bash
echo "DATA_VOLUME_NAME=casp_radar_data" >> .env
docker compose up -d
```
Remove that line once you've migrated with Option B so future upgrades use the new default.

**Option B: migrate to the new volume name.** This copies your existing data into a volume named `regradar_data` and moves the Litestream backup directory to match:
```bash
docker compose down
docker volume create regradar_data
docker run --rm -v casp_radar_data:/from -v regradar_data:/to alpine sh -c "cp -a /from/. /to/"
docker volume rm casp_radar_data
mv ./backups/casp_radar ./backups/regradar
docker compose up -d
```
Confirm the migration worked: `count_items_tool` should return the same number of items before and after.

### Keyword alerts

`config/alerts.yaml` holds optional keyword-to-tag rules — a lightweight, self-hosted "Google Alerts" layer. On each collection run, newly fetched items are scanned (case-insensitive substring match against title + content); matches get added to that item's `content_tags` (see [Tags](#tags) above).

Manage rules from the browser at **`http://localhost:8080/alerts`** — view, add, and delete rules without editing the file directly. Or edit `config/alerts.yaml` by hand; either way, changes take effect on the next collection run (no restart needed).

The file is optional — delete it, point `ALERTS_PATH` elsewhere, or leave it empty and keyword tagging is simply disabled; collection is unaffected. Example rules ship by default (`MiCA` → `mica`, `travel rule` → `travel-rule`, etc.). Edit freely for your entity's watchlist.

Push notifications (webhook, email, Telegram, etc.) are **not** implemented yet — tagging only, for now.

## Contributing

The primary contribution surface is `config/regulators.yaml`. Community PRs to:
- Add missing regulators (Liechtenstein PFSA is a known gap)
- Fix broken feed URLs
- Add or improve scrape selectors for no-RSS regulators

## Licence

MIT

## Credits

regradar bypasses Cloudflare-protected sources using [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (MIT licence), run as a standalone Docker service — see [Coverage](#coverage) above.

## Copyright

(c) Klinikal Limited, 2026. 
