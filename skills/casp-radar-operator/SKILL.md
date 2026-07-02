---
name: casp-radar-operator
description: Use when operating, querying, or debugging casp-radar — the regulatory intelligence tool for crypto-asset service providers (CASPs). Triggers on questions about EEA regulator news, MiCA developments, CASP warnings, or direct requests to run/query/debug the casp-radar MCP server or collector.
---

# casp-radar Operator

How to run, query, and navigate casp-radar — the regulatory intelligence tool for crypto-asset service providers (CASPs). It collects news, warnings, and publications from ~33 EEA financial regulators (30 member states + ESMA/EBA/EIOPA) on a schedule, stores them in SQLite, and exposes the data via an MCP server.

**Read your context first:** check `skills/casp-radar-operator/refs/context.md` (relative to repo root). If it exists, read it — it tells you what entity is operating this instance and what's relevant to them. That file is git-ignored on purpose (it's instance-specific and contains private company details) so it won't exist in a fresh clone. If it's missing, read `refs/example.context.md` instead — a template showing what that file should contain — and treat this as an unconfigured instance until the operator fills one in (see the `casp-radar-onboarding` skill, in the sibling `skills/casp-radar-onboarding/` folder, to generate it interactively).

---

## Is it running?

The MCP server only exists when the Docker stack is up — it is not a persistent background service. Check first, from the repo root:

```bash
docker compose ps
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/mcp
```

If nothing's running:

```bash
docker compose up -d
```

This starts three services: `collector` (scheduled fetcher, default 06:00 UTC daily — see `SCHEDULE` in `.env`), `mcp` (the query server), and `flaresolverr` (a Cloudflare-bypass helper for a handful of protected sources). To force an immediate collection pass instead of waiting for the schedule:

```bash
docker compose exec collector python -m collector.main --once
```

**Port conflicts:** the committed `docker-compose.yml` publishes `mcp` on host port 8000. If that port is already taken locally, don't edit the committed file — add an untracked `docker-compose.override.yml` (already git-ignored) remapping it, e.g.:

```yaml
services:
  mcp:
    ports: !override
      - "127.0.0.1:8001:8000"
```

Use the `!override` merge tag — a plain list under `ports:` gets *appended* to the base file's port mapping by Compose, not replaced, and both would then try to bind and fail.

---

## What's in the data

Registry with per-source status lives at `config/regulators.yaml`. Each item has `tags` (e.g. `casp`, `mica`, `warnings`, `dora`, `news`, `publications`) assigned at the *feed* level, not per-article — treat tags as "this feed generally covers X," not a precise per-item classifier. This tool deliberately does no AI classification or relevance scoring (by design — "bring your own agent"). When asked whether something is actually relevant, read the item and judge it; don't expect pre-filtered data.

## MCP tools

| Tool | Use for |
|---|---|
| `search_items_tool` | Full-text search across title + snippet. Filters: `regulator_id`, `language`, `tags`, `date_from`, `date_to`. |
| `get_recent_tool` | Most recent N items, optional `regulator_id`/`tags`/`language` filters. |
| `list_regulators_tool` | Full registry + live fetch stats (last success, error counts) — use to check source health. |
| `get_item_tool` | Single item by ID, includes `raw_content` (search/recent only return a snippet). |
| `get_run_log_tool` | Recent collector run stats — use to check if collection is working. |
| `get_warnings_tool` | Items tagged `warnings`, optionally filtered by `regulator_id` and a day window. |

## Typical queries

- "Any recent CASP warnings from Germany?" → `get_warnings_tool(regulator_id="de-bafin")`
- "What's new from ESMA this week?" → `get_recent_tool(regulator_id="esma", limit=10)`
- "Search for anything about the travel rule" → `search_items_tool(query="travel rule")`
- "Is the collector actually working?" → `get_run_log_tool()`, check `errors` and `status`
