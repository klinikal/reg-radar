# casp-radar

Open-source regulatory intelligence tool for crypto-asset service providers (CASPs). Collects news, warnings, and publications from financial regulators across the EEA on a daily schedule, stores them in SQLite, and exposes the database via an MCP server so AI agents can query it.

**Bring your own agent.** This tool provides clean structured data. AI classification and relevance scoring are out of scope — connect any MCP-compatible agent (Claude, ChatGPT, Gemini, Hermes, a custom agent, whatever you use).

## Prerequisites

- **[Docker](https://www.docker.com/products/docker-desktop/)** (or Docker Engine + the Compose plugin on Linux) — required to run the stack. This is third-party software, not something casp-radar installs for you. Download it directly from the official Docker website — don't run install scripts from anywhere else, and don't let an AI agent install it on your behalf.
- **Git**, to clone the repo.

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

2. **Get some data in.** The collector runs on its own schedule (default 06:00 UTC daily), so on a fresh install the database is empty until the first run. To collect immediately instead of waiting:
   ```bash
   docker compose exec collector python -m collector.main --once
   ```

3. **Browse it** at `http://localhost:8080` to confirm data's actually there, or **connect your agent** to the MCP server — see [Connecting an agent](#connecting-an-agent) below for setup across different clients.

4. **Give your agent the onboarding skill.** Point it at [`skills/casp-radar-onboarding/SKILL.md`](skills/casp-radar-onboarding/SKILL.md) and ask it to onboard you. It'll interview you about your entity — what you're licensed for, where, what you do and don't do — and generate a private `skills/casp-radar-operator/refs/context.md` (git-ignored, never leaves your machine) so the agent knows what's actually relevant to you instead of treating all ~33 regulators as equally important.

5. **You're set.** From here, just ask your agent regulatory questions — "any recent CASP warnings from Germany?", "what's ESMA published this week?" — and point it at [`skills/casp-radar-operator/SKILL.md`](skills/casp-radar-operator/SKILL.md) if it needs a refresher on how to navigate the data. See [Skills](#skills) for more on how these work.

## Coverage

~30 regulators across 29 jurisdictions (27 EU member states + Iceland + Norway), plus ESMA, EBA, and EIOPA. Full list and source status: `config/regulators.yaml`.

Sources with confirmed working RSS/Atom feeds: 25+. Scrape targets: ~8. Disabled entries (Cloudflare/SPA-protected): ~10 — documented in `regulators.yaml` so the gap is visible.

Optional: the `flaresolverr` service (included in Docker Compose by default) enables collection from Cloudflare-protected sources (e.g. Lithuania's lb.lt).

### Non-EEA additions

The UK's FCA is included as a deliberate exception to the EEA/MiCA scope above — see the "Non-EEA Additions" section at the bottom of `config/regulators.yaml` for why and what's collected (news, warnings, notices and decisions, policy and guidance). It's kept structurally separate so the EEA coverage numbers above stay accurate.

### MFSA (Malta) coverage

MFSA's site is Cloudflare-protected — confirmed to block plain HTTP regardless of User-Agent, so every MFSA request goes through `flaresolverr` (already in the Docker stack). `config/regulators.yaml` defines MFSA's full publication taxonomy (~65 categories, manually crawled from the site nav — its own category sitemap points to broken/mismatched URLs and can't be trusted), but only ~10 CASP-relevant categories (crypto-assets circulars, notices and decisions, dear CEO letters, fintech, authorisations, AML, conduct of business, ICT/cybersecurity, consultations, feedback and statements) are `enabled: true` by default. The other ~55 (banking supervision, insurance/pensions, UCITS, short-selling, etc.) are defined but disabled — real MFSA output, just not applicable to a bitcoin-only CASP's licensed activities. Flip `enabled: true` on any of them, or target them directly via the backfill script below, without needing to redefine URLs or selectors.

Because every request needs FlareSolverr (a real browser, ~10-15s each), `collector/fetcher.py` throttles and backs off automatically — a minimum interval between requests (`FLARESOLVERR_MIN_INTERVAL_SECONDS`, default 8s) and longer exponential backoff specifically when the target's response looks like a ban/block rather than a normal failure (`FLARESOLVERR_RETRY_BACKOFF_SECONDS`, default 45s, doubling per retry, `FLARESOLVERR_MAX_RETRIES` attempts). This is global, not MFSA-specific — any current or future `cloudflare_protected` source benefits. It also skips wasting plain-HTTP retry attempts against a known-Cloudflare-protected source, since those still count toward the target's own rate limit for no chance of success.

### Historic backfills

The regular collector only scrapes the first page of each scrape-type source on every run (newest-first) — enough to catch new publications going forward, but not the historic archive behind a source that only recently got added here. Three one-off backfill scripts exist for that, all under `scripts/`, all read their URLs/selectors straight from `config/regulators.yaml` (no duplicated config to drift out of sync), all support `--dry-run`, and none run automatically — running one is a deliberate decision, not a side effect of adding a source:

- `backfill_esma_library.py` — ESMA's document library, ~7,260 documents across ~363 pages. Has been run; that data is in the default dataset once you clone and run the collector's normal `--once`/scheduled pass — no need to re-run this unless you're rebuilding from an empty database.
- `backfill_fca_publications.py` — FCA's notices/decisions (~5,980 documents) and policy/guidance (~3,743 documents). **Has not been run** — built and tested (`--dry-run --max-pages N`) but the full historic crawl is left to you to trigger deliberately, since FCA is a non-EEA addition and a larger crawl than ESMA's.
- `backfill_mfsa_publications.py` — any MFSA category, enabled or not (`--category <label>`), or all ~65 at once (`--all`). **Has not been run at scale** — verified against several categories including a currently-disabled one, but a full `--all` run is, by design, hours-long given FlareSolverr's per-request cost and the built-in throttling — best scheduled overnight or off-hours, not run casually.

Always `--dry-run` first — all three scripts fetch and parse every page exactly as a real run would, just without writing to the database, so you can sanity-check counts before committing to a multi-hundred-request crawl of someone else's site.

## MCP server

The MCP server exposes six tools over streamable HTTP:

| Tool | Description |
|---|---|
| `search_items_tool` | Full-text search across title and snippet |
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

A minimal, functional list/search/reader view — no build step, no JS framework, just server-rendered HTML — at **`http://localhost:8080`**:

- **List view** shows every collected item, most recent first, with keyword search, an exact match toggle, a regulator filter, and a date range filter.
- **Search syntax:** unquoted words are AND-ed (`bafin fines` matches items containing both, in either order); `+` behaves the same as a space (`bafin+fines`); wrap a phrase in quotes for adjacency (`"administrative fines"` only matches that exact phrase, not the two words scattered separately); check "exact match" to treat the entire query box as one literal phrase.
- **Reader view** (click any title) strips the item down to title, regulator, date, tags, a link to the original source, and cleaned body text — full scraped text where available, otherwise the RSS snippet with a note saying so and a link out to the source.
- **JSON API** at `/api/items`, `/api/items/{id}`, and `/api/regulators` — same query logic as the HTML views, same filters, exists so a future frontend (or a script) doesn't have to scrape the HTML or duplicate query logic. The webui service reads the SQLite database directly (via the same query module the MCP server uses), so it works independently of whether the `mcp` container happens to be up.

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
| `USER_AGENT` | `casp-radar/1.0` | HTTP User-Agent header |
| `REQUEST_TIMEOUT_SECONDS` | `30` | Per-request timeout |
| `REQUEST_RETRIES` | `3` | Retry attempts per source |
| `FLARESOLVERR_URL` | *(blank)* | Leave blank to disable FlareSolverr |

## Contributing

The primary contribution surface is `config/regulators.yaml`. Community PRs to:
- Add missing regulators (Liechtenstein PFSA is a known gap)
- Fix broken feed URLs
- Add or improve scrape selectors for no-RSS regulators

## Licence

MIT
