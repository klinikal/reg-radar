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
   This starts three services: `collector` (fetches on a schedule — see [Configuration](#configuration)), `mcp` (the query server, exposed at `http://localhost:8000/mcp`), and `flaresolverr` (a Cloudflare-bypass helper for a handful of protected sources).

2. **Get some data in.** The collector runs on its own schedule (default 06:00 UTC daily), so on a fresh install the database is empty until the first run. To collect immediately instead of waiting:
   ```bash
   docker compose exec collector python -m collector.main --once
   ```

3. **Connect your agent** to the MCP server — see [Connecting an agent](#connecting-an-agent) below for setup across different clients.

4. **Give your agent the onboarding skill.** Point it at [`skills/casp-radar-onboarding/SKILL.md`](skills/casp-radar-onboarding/SKILL.md) and ask it to onboard you. It'll interview you about your entity — what you're licensed for, where, what you do and don't do — and generate a private `skills/casp-radar-operator/refs/context.md` (git-ignored, never leaves your machine) so the agent knows what's actually relevant to you instead of treating all ~33 regulators as equally important.

5. **You're set.** From here, just ask your agent regulatory questions — "any recent CASP warnings from Germany?", "what's ESMA published this week?" — and point it at [`skills/casp-radar-operator/SKILL.md`](skills/casp-radar-operator/SKILL.md) if it needs a refresher on how to navigate the data. See [Skills](#skills) for more on how these work.

## Coverage

~30 regulators across 29 jurisdictions (27 EU member states + Iceland + Norway), plus ESMA, EBA, and EIOPA. Full list and source status: `config/regulators.yaml`.

Sources with confirmed working RSS/Atom feeds: 25+. Scrape targets: ~8. Disabled entries (Cloudflare/SPA-protected): ~10 — documented in `regulators.yaml` so the gap is visible.

Optional: the `flaresolverr` service (included in Docker Compose by default) enables collection from Cloudflare-protected sources (e.g. Lithuania's lb.lt).

## MCP server

The MCP server exposes six tools over streamable HTTP:

| Tool | Description |
|---|---|
| `search_items_tool` | Full-text search across title and snippet |
| `get_recent_tool` | Most recent N items with optional filters |
| `list_regulators_tool` | Full registry with live fetch status |
| `get_item_tool` | Single item by ID, including raw content |
| `get_run_log_tool` | Recent collector run stats |
| `get_warnings_tool` | Items tagged `warnings`, optionally filtered by date range |

Server endpoint: **`http://localhost:8000/mcp`**.

**Port already in use?** Don't edit `docker-compose.yml` — add an untracked `docker-compose.override.yml` (already git-ignored) remapping the host port, e.g.:
```yaml
services:
  mcp:
    ports: !override
      - "127.0.0.1:8001:8000"
```
Use the `!override` merge tag — a plain list under `ports:` gets *appended* to the base port mapping by Compose, not replaced, and both would then try to bind and fail.

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
