# Changelog

All notable changes to casp-radar are documented here, grouped by day. Entries are outcome-focused (what changed and why it matters), not a commit-by-commit log — see `git log` for full detail.

## 2026-07-04

**MCP server security/correctness audit** — a full external review of `mcpserver/` produced 9 security/correctness findings and 7 improvement opportunities, all implemented:
- Fixed a timestamp-format bug in `get_warnings` that silently dropped items published on the cutoff day itself.
- Floored `limit` clamps across `search_items`, `get_recent`, and `get_run_log` — a negative limit could previously disable SQLite's `LIMIT` entirely, returning the whole table.
- Added a `limit` to `get_warnings` (previously unbounded) and offset-based pagination to `search_items`.
- Fixed `search_items` to use AND search semantics for multi-word queries (it was treating them as one exact phrase).
- The MCP server now opens the database read-only and fails fast if the database file is missing, instead of silently creating an empty schema.
- Enabled DNS-rebinding protection on the HTTP transport (FastMCP doesn't enable this automatically when binding `0.0.0.0`).
- Tool errors are now sanitized before reaching MCP clients — internal details (DB paths, SQL text) no longer leak into error messages.
- Fixed `Optional` type annotations across tool signatures.
- Five new MCP tools: `count_items_tool`, `get_month_facets_tool`, `list_tags_tool`, `get_items_since_tool` (a polling primitive), `get_health_tool` (collector freshness/readiness).
- All 11 tools now return structured content (dicts/lists) instead of JSON-encoded strings; `get_item_tool` gained a `max_content_chars` option to protect agent context windows.

**Production database corruption incident** — live testing surfaced real SQLite corruption in the `items` table (not the FTS search index, as first suspected). Root cause: the database very likely lived on a macOS Docker Desktop bind-mount, a known corruption risk for SQLite's WAL-mode locking. Recovery was attempted (an initial "fully recoverable" estimate didn't hold up under independent re-verification — the real number was ~83%), and the decision was made to scratch the corrupted database and let the collector rebuild it fresh, given the data is cheaply re-collected from public RSS/scrape sources.

**Infrastructure hardening**, shipped the same day to prevent a repeat:
- Moved the database off the macOS bind-mount onto a genuine Docker-managed named volume.
- Added Litestream as a continuous-backup sidecar, streaming the live database to a plain folder on the host disk — restore path verified end-to-end, not just configured.
- The collector now runs a full integrity check (`PRAGMA integrity_check`) at the start of every collection cycle and surfaces the result loudly through `get_health_tool`'s new `db_integrity` field, instead of corruption sitting silently undetected until a search happened to fail.
- Fixed a live regression the DNS-rebinding fix introduced: operators who remap the MCP server's published port (a documented workaround for port conflicts) got every request rejected with a 421 — the allowed-hosts list is now extensible via `MCP_EXTRA_ALLOWED_HOSTS`.
- Fixed `docker-compose.yml`: the `mcp` and `webui` services had never actually loaded `.env` — only `collector` had, so any environment-variable configuration for the other two services was silently a no-op.
- Fixed a dependency version conflict (`httpx`/`uvicorn` pins one version below what `mcp` requires) that blocked installing all three services' dependencies together for local testing.

## 2026-07-03

- Split the single `tags` column into `source_tags` (notice type, e.g. "warnings", "press_releases") and `content_tags` (topic, e.g. "mica", "travel-rule"), with a backfill migration and combined-by-default filtering across the MCP tools and web UI.
- Added a keyword-alerts system: `config/alerts.yaml` maps keywords to tags, applied automatically to items during collection; manageable from the web UI at `/alerts` without editing the file directly.
- Added a `page_watch` source type for tracking static pages (like ESMA's MiCA hub) via content-hash change detection, not just RSS/scrape.
- Added Malta FIAU and AMLA as tracked regulators.
- Fixed a date-parsing bug where `dayfirst=True` was corrupting already-ISO-formatted datetime strings.
- Routed RSS/Atom fetches through FlareSolverr when configured, for feeds behind Cloudflare.

## 2026-07-02

Initial build. Core collector (RSS/Atom + HTML scrape fetcher, SQLite storage, APScheduler-driven daily runs), MCP server (6 initial read-only tools), and web UI (browsable list/search/reader view) for EEA financial regulator news, warnings, and publications. Seeded `config/regulators.yaml` with full EEA coverage (30 member states + ESMA/EBA/EIOPA), plus dedicated scrapers for ESMA's document library, FCA publications, CNMV warnings (via PDF extraction), and MFSA (Malta). Added FTS5 full-text search with a LIKE fallback, paragraph-aware content chunking, and a disaster-recovery script for rebuilding the search index from source data. Hardened based on multiple external code reviews: SSRF protection on scrape-path redirects, non-root Docker users, loopback-only port binding, input validation/clamping on MCP tool parameters, and WAL mode with busy-timeout tuning to reduce lock contention.
