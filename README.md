# casp-radar

Open-source regulatory intelligence tool for European (EU, UK coming soon) entities. Collects news, warnings, and publications from financial regulators across the EU on a daily schedule, stores them in SQLite, and exposes the database via an MCP server so AI agents can query it. There is also a lightweight webUX that can be run to see, query and search.

**Bring your own agent.** This tool provides clean structured data, a simple web UI, and an MCP server. AI classification and relevance scoring are out of scope — connect any MCP-compatible agent (Claude, ChatGPT, Gemini, Hermes, a custom agent, whatever you use).

## Prerequisites

- **[Docker](https://www.docker.com/products/docker-desktop/)** (or Docker Engine + the Compose plugin on Linux) — required to run the stack. This is third-party software, not something this tool installs for you. If you don't have this, it's suggested you download it directly https://www.docker.com/ or by using Homebrew (`brew install docker`).
- **Git**, to clone the repo (`brew install git`).

Beyond that, `docker compose up` pulls the `python:3.11-slim` base image and a `flaresolverr` image from public container registries, and installs pinned Python packages from PyPI per `collector/requirements.txt` / `mcpserver/requirements.txt` — standard supply chain for a Dockerised Python project, versions are pinned in those files if you want to audit them before building.

## Getting started (zero to working)

1. **Run the stack**
   ```bash
   git clone https://github.com/klinikal/casp-radar
   cd casp-radar
   cp .env.example .env
   docker compose up -d
   ```
   This starts four services: `collector` (fetches on a schedule — see [Configuration](#configuration)), `mcp` (the query server, exposed at `http://localhost:8000/mcp`), `webui` (a browsable list/search view at `http://localhost:8080` — see [Web UI](#web-ui)), and `flaresolverr` (a Cloudflare-bypass helper for a handful of protected sources).

2. **Get some data in.** On a fresh install the database is empty until the first run. To collect immediately:
   ```bash
   docker compose exec collector python -m collector.main --once
   ```

3. **Browse it** at `http://localhost:8080` to confirm data's actually there, or **connect your agent** to the MCP server — see [Connecting an agent](#connecting-an-agent) below for setup across different clients.

4. **Give your agent the onboarding skill.** Point it at [`skills/casp-radar-onboarding/SKILL.md`](skills/casp-radar-onboarding/SKILL.md) and ask it to onboard you. It'll interview you about your entity — what you're licensed for, where, what you do and don't do — and generate a private `skills/casp-radar-operator/refs/context.md` (git-ignored, never leaves your machine) so the agent knows what's actually relevant to you instead of treating every regulator in the registry as equally important.

5. **You're set.** From here, just ask your agent regulatory questions — "any recent CASP warnings from Germany?", "what's ESMA published this week?" — and point it at [`skills/casp-radar-operator/SKILL.md`](skills/casp-radar-operator/SKILL.md) if it needs a refresher on how to navigate the data. See [Skills](#skills) for more on how these work.

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

## MCP server

The MCP server exposes six tools over streamable HTTP:

| Tool | Description |
|---|---|
| `search_items_tool` | Full-text search (SQLite FTS5 over title, snippet, and full scraped text; falls back to LIKE if FTS fails) |
| `get_recent_tool` | Most recent N items with optional filters |
| `list_regulators_tool` | Full registry with live fetch status |
| `get_item_tool` | Single item by ID, including raw content |
| `get_run_log_tool` | Recent collector run stats |
| `get_warnings_tool` | Items tagged `warnings`, optionally filtered by date range — see note below |

**On `get_warnings_tool`:** some regulators only have one general news feed, so it's tagged `warnings` alongside `news`/`publications` rather than leaving genuine warnings uncollected — this means that feed's entire output shows up here, not just actual warning notices. Sources with a dedicated warnings-only feed are precise; broad `all_news`-style feeds are not. Treat results from a broad feed as "may contain warnings," not a pre-filtered list.

Server endpoint: **`http://localhost:8000/mcp`**.

**Port already in use?** Don't edit `docker-compose.yml` — add an untracked `docker-compose.override.yml` (already git-ignored) remapping the host port, e.g.:
```yaml
services:
  mcp:
    ports: !override
      - "127.0.0.1:8001:8000"
```
Use the `!override` merge tag — a plain list under `ports:` gets *appended* to the base port mapping by Compose, not replaced, and both would then try to bind and fail.

## Web UI

A minimal, functional list/search/reader view — no build step, server-rendered HTML with a small amount of vanilla JS for filter UX — at **`http://localhost:8080`**:

- **List view** shows collected items, most recent first, 50 per page with pagination. Filters: keyword search (auto-submits as you type), an exact-match toggle, a multi-select regulator panel (searchable by country, abbreviation, or full name, with per-regulator item counts, last-fetch date, and consecutive-failure warnings), and a date range filter. A **By month** sidebar shows publication-month facets for the current query/filter set.
- **Search syntax:** unquoted words are AND-ed (`bafin fines` matches items containing both, in either order); `+` behaves the same as a space (`bafin+fines`); wrap a phrase in quotes for adjacency (`"administrative fines"` only matches that exact phrase, not the two words scattered separately); check "exact match" to treat the entire query box as one literal phrase. Search uses the same FTS5 index as the MCP server (title, snippet, and full scraped text).
- **Reader view** (click any title) strips the item down to title, regulator, date, tags, a link to the original source, and cleaned body text — full scraped text where available, otherwise the RSS snippet with a note saying so and a link out to the source.
- **JSON API** at `/api/items`, `/api/items/{id}`, `/api/regulators`, and `/api/months` — same query logic as the HTML views, same filters, exists so a future frontend (or a script) doesn't have to scrape the HTML or duplicate query logic. The webui service reads the SQLite database directly (via the same query module the MCP server uses), so it works independently of whether the `mcp` container happens to be up.

This is intentionally plain — a functional first pass, not a finished design. The query/data layer (`mcpserver/queries.py`) is decoupled from presentation, so upgrading the look (or swapping in a JS frontend against the JSON API) later doesn't require touching how data is fetched or filtered.

## Connecting an agent

casp-radar speaks standard MCP over streamable HTTP — any MCP-compatible client works, not just one vendor's tool. Setup varies by client:

**Claude Code:**
```bash
claude mcp add --transport http casp-radar http://localhost:8000/mcp
```

**Clients using the common `mcpServers` JSON config** (Claude Desktop, and many other MCP-compatible apps use a similar shape — check your client's docs for the exact config file location):
```json
{
  "mcpServers": {
    "casp-radar": {
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

**ChatGPT, Gemini, Hermes, or anything else:** most modern agent tools have a settings area for adding a remote/custom MCP server — look for "MCP", "Connectors", or "Tools" in your client's settings and supply the URL above. Exact steps vary by vendor and change over time, so check your specific client's current documentation rather than relying on this README to have the latest UI paths.

## Skills

`skills/` contains two plain-markdown instruction documents — not a Claude-specific format, just structured guidance any agent (or human) can read and follow:

- **[`casp-radar-operator`](skills/casp-radar-operator/SKILL.md)** — how to run, query, and navigate casp-radar: checking whether the stack is up, the MCP tool reference, and how to weight relevance using the operator's context (see below).
- **[`casp-radar-onboarding`](skills/casp-radar-onboarding/SKILL.md)** — a guided interview that generates your private operator context (see next section).

If your agent harness has a native skill/tool-loading mechanism (e.g. Claude Code's `Skill` system), point it there. Otherwise, paste the file contents into your system prompt or custom instructions, or just tell your agent to read the file and follow it.

### Operator context (`refs/`)

`skills/casp-radar-operator/refs/` holds two files:

- **`example.context.md`** (tracked in git) — the template. Explains what each section means.
- **`context.md`** (git-ignored, generated by the onboarding skill or filled in by hand) — your actual entity facts: what you're licensed for, where, what you do and don't do, which affiliates matter, what kind of regulatory content to prioritise. Never committed — this is exactly the kind of detail that shouldn't end up in a public repo, since anyone running casp-radar has their own regulatory profile.

Run the onboarding skill any time to generate or rebuild this file.

## Configuration

Copy `.env.example` to `.env` and adjust:

| Variable | Default | Description |
|---|---|---|
| `DB_PATH` | `/app/data/casp_radar.db` | SQLite database path |
| `SCHEDULE` | `0 6 * * *` | Cron schedule (UTC) |
| `LOG_LEVEL` | `INFO` | Logging level |
| `USER_AGENT` | `casp-radar/1.0 (+https://github.com/klinikal/casp-radar)` | HTTP User-Agent header |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `REQUEST_RETRIES` | `3` | Retry attempts per source |
| `REGULATORS_PATH` | `/app/config/regulators.yaml` | Regulator registry file |
| `ALERTS_PATH` | `/app/config/alerts.yaml` | Keyword-to-tag rules file (optional — see below) |
| `FLARESOLVERR_URL` | `http://flaresolverr:8191` | FlareSolverr service URL; leave blank to disable (Cloudflare-protected sources are then skipped) |
| `FLARESOLVERR_MIN_INTERVAL_SECONDS` | `8` | Minimum seconds between FlareSolverr requests |
| `FLARESOLVERR_MAX_RETRIES` | `3` | Retry attempts per FlareSolverr fetch |
| `FLARESOLVERR_RETRY_BACKOFF_SECONDS` | `45` | Base backoff when a FlareSolverr fetch looks blocked (doubles per retry) |

### Keyword alerts

`config/alerts.yaml` holds optional keyword-to-tag rules — a lightweight, self-hosted "Google Alerts" layer. On each collection run, newly fetched items are scanned (case-insensitive substring match against title + content); matching items get the configured tag added to `content_tags` — a separate bucket from `source_tags` (the notice-type tags set per feed in `regulators.yaml`, e.g. `warnings`, `press_releases`). MCP tools (`search_items_tool`, `get_recent_tool`) filter by `tags` (both buckets combined) or `source_tags`/`content_tags` individually — the web UI and JSON API don't yet expose tag filtering.

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

casp-radar bypasses Cloudflare-protected sources using [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) (MIT licence), run as a standalone Docker service — see [Coverage](#coverage) above.

## Copyright

(c) Klinikal Limited, 2026. 
