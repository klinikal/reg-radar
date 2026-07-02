# casp-radar

Open-source regulatory intelligence tool for crypto-asset service providers (CASPs). Collects news, warnings, and publications from financial regulators across the EEA on a daily schedule, stores them in SQLite, and exposes the database via an MCP server so AI agents can query it.

**Bring your own agent.** This tool provides clean structured data. AI classification and relevance scoring are out of scope - connect any MCP-compatible agent.

## Quick start

```bash
git clone https://github.com/klinikal/casp-radar
cd casp-radar
cp .env.example .env
docker compose up -d
```

The collector runs daily at 06:00 UTC by default. To run immediately:

```bash
docker compose run --rm collector python -m collector.main --once
```

## Coverage

~30 regulators across 29 jurisdictions (27 EU member states + Iceland + Norway), plus ESMA, EBA, and EIOPA. Full list and source status: `config/regulators.yaml`.

Sources with confirmed working RSS/Atom feeds: 25+. Scrape targets: ~8. Disabled entries (Cloudflare/SPA-protected): ~10 - documented in `regulators.yaml` so the gap is visible.

Optional: include the `flaresolverr` service in Docker Compose to enable collection from Cloudflare-protected sources (e.g. Lithuania lb.lt).

## MCP server

The MCP server exposes six tools:

| Tool | Description |
|---|---|
| `search_items` | Full-text search across title and snippet |
| `get_recent` | Most recent N items with optional filters |
| `list_regulators` | Full registry with live fetch status |
| `get_item` | Single item by ID, including raw content |
| `get_run_log` | Recent collector run stats |
| `get_warnings` | Items tagged `warnings`, optionally filtered by date range |

Connect your MCP client to `http://localhost:8000`.

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
