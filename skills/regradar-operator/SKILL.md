---
name: regradar-operator
description: Use when operating, querying, or debugging regradar — the regulatory intelligence tool for crypto-asset service providers (CASPs). Triggers on questions about EEA regulator news, MiCA developments, CASP warnings, or direct requests to run/query/debug the regradar MCP server or collector.
---

# regradar Operator

How to run, query, and navigate regradar — the regulatory intelligence tool for crypto-asset service providers (CASPs). It collects news, warnings, and publications from ~33 EEA financial regulators (30 member states + ESMA/EBA/EIOPA) on a schedule, stores them in SQLite, and exposes the data via an MCP server.

**Read your context first:** check `skills/regradar-operator/refs/context.md` (relative to repo root). If it exists, read it — it tells you what entity is operating this instance and what's relevant to them. That file is git-ignored on purpose (it's instance-specific and contains private company details) so it won't exist in a fresh clone. If it's missing, read `refs/example.context.md` instead — a template showing what that file should contain — and treat this as an unconfigured instance until the operator fills one in (see the `regradar-onboarding` skill, in the sibling `skills/regradar-onboarding/` folder, to generate it interactively).

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

This starts four services: `collector` (scheduled fetcher, default 06:00 UTC daily — see `SCHEDULE` in `.env`), `mcp` (the query server), `webui` (a browsable list/search/reader view at `http://localhost:8080` — useful for a human to sanity-check what's actually in the database, or as a quick way to eyeball something before querying it via MCP), and `flaresolverr` (a Cloudflare-bypass helper for a handful of protected sources). To force an immediate collection pass instead of waiting for the schedule:

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

**If you remap the MCP port, also set `MCP_EXTRA_ALLOWED_HOSTS`** in `.env` (comma-separated, e.g. `127.0.0.1:8001,localhost:8001`) — the server validates the `Host` header against the *external* port a client actually connects to, not its internal port (always 8000). Without this, every request 421s with "Invalid Host header" once the port is remapped.

---

## Backups & recovery

The live database lives in a Docker-managed named volume (`regradar_data`), not a bind-mounted host folder — this is deliberate: SQLite's WAL mode needs precise file-locking guarantees that Docker Desktop's macOS bind-mount bridge doesn't fully honour, which caused a real corruption incident in July 2026. You can no longer browse the `.db` file directly from Finder/Terminal; use `docker compose exec mcp sqlite3 /app/data/regradar.db` (or similar) if you need to inspect it.

A `litestream` service continuously replicates the database to `./backups/regradar` on the host (real host disk, outside Docker), keeping roughly constant size (a snapshot plus a rolling ~24h retention window, auto-pruned — not accumulating daily copies). Check `docker compose logs litestream` to confirm it's running.

**`originals/` (fetched HTML/PDF artefacts, under `ARTEFACTS_DIR` on the data volume) is not covered by Litestream** — it only replicates the SQLite file. Include `originals/` in whatever host-level backup you run against the `regradar_data` volume (e.g. a periodic `docker run --rm -v regradar_data:/data -v $(pwd)/backups:/backup alpine tar czf /backup/originals-$(date +%F).tar.gz -C /data originals`), or accept that a lost volume means item text survives (in the DB backup) but the original artefacts don't.

**To restore from backup** (e.g. after corruption or accidental data loss):
```bash
docker compose stop collector mcp webui
docker run --rm -v "$(pwd)/backups:/backups:ro" -v regradar_data:/out litestream/litestream:0.5.9 restore -o /out/regradar.db file:///backups/regradar
# verify before trusting it:
docker run --rm -v regradar_data:/data alpine sh -c "apk add --no-cache sqlite && sqlite3 /data/regradar.db 'PRAGMA integrity_check;'"
# if you also back up originals/, restore it into the same volume:
# docker run --rm -v regradar_data:/data -v $(pwd)/backups:/backup:ro alpine tar xzf /backup/originals-<date>.tar.gz -C /data
docker compose up -d collector mcp webui
```

---

## What's in the data

Registry with per-source status lives at `config/regulators.yaml`. Each item has `tags` (e.g. `casp`, `mica`, `warnings`, `dora`, `news`, `publications`) assigned at the *feed* level, not per-article — treat tags as "this feed generally covers X," not a precise per-item classifier. This tool deliberately does no AI classification or relevance scoring (by design — "bring your own agent"). When asked whether something is actually relevant, read the item and judge it; don't expect pre-filtered data.

**Non-EEA note:** `regulator_id` `uk-fca` (FCA) is included as a deliberate exception to this tool's core EEA/MiCA scope — see the "Non-EEA Additions" section at the bottom of `config/regulators.yaml`. If an operator's context (`refs/context.md`) is EEA/MiCA-only, weigh UK items accordingly rather than assuming they're automatically relevant.

**Historic backfills:** the daily collector only picks up the newest page of any scrape-type source — full historic archives are pulled separately via one-off scripts in `scripts/` (`backfill_esma_library.py`, `backfill_fca_publications.py`), not run automatically. If asked "why is there a gap in older data" for a scrape-type source, check whether its backfill script has actually been run — see the README's "Historic backfills" section for what's been run and what hasn't.

## MCP tools

| Tool | Use for |
|---|---|
| `search_items_tool` | Full-text search across title + snippet. Filters: `regulator_id`, `language`, `tags`, `date_from`, `date_to`. |
| `get_recent_tool` | Most recent N items, optional `regulator_id`/`tags`/`language` filters. |
| `list_regulators_tool` | Full registry + live fetch stats (last success, error counts) — use to check source health. |
| `get_item_tool` | Single item by ID, includes `raw_content`, `body_md`, and a `document` frontmatter object (search/recent only return a snippet). |
| `get_run_log_tool` | Recent collector run stats — use to check if collection is working. |
| `get_warnings_tool` | Items tagged `warnings`, optionally filtered by `regulator_id` and a day window. Some regulators tag their *entire* general news feed as `warnings` (no dedicated warnings feed exists), so results from those regulators are "may contain warnings," not a precise list — read the item, don't assume every result is a genuine warning notice. |

## Typical queries

- "Any recent CASP warnings from Germany?" → `get_warnings_tool(regulator_id="de-bafin")`
- "What's new from ESMA this week?" → `get_recent_tool(regulator_id="esma", limit=10)`
- "Search for anything about the travel rule" → `search_items_tool(query="travel rule")`
- "Is the collector actually working?" → `get_run_log_tool()`, check `errors` and `status`
