---
title: MCP Server
type: component
sources: [mcpserver/server.py, mcpserver/queries.py]
last-verified: 2026-09-10
---

## Purpose

`mcpserver/server.py` exposes the collected regulatory database to an MCP
client (an AI agent) as a set of tools. `mcpserver/queries.py` holds the
SQL/query logic each tool calls.

## How it works

`server.py` builds a `FastMCP("reg-radar", ...)` instance with DNS-rebinding
protection and a fixed `allowed_hosts` list (`127.0.0.1`, `localhost`, both
with `:8000`), extendable via the `MCP_EXTRA_ALLOWED_HOSTS` env var for an
operator who remaps the published port.

Three lazily-initialized, process-cached singletons back every tool:

- `get_engine()`: a read-only SQLAlchemy engine (`mode=ro`) against `DB_PATH`
  (default `/app/data/reg_radar.db`). Raises if the file doesn't exist; the
  server never creates the schema itself.
- `get_taxonomy()`: loaded via `regradar.taxonomy.load_and_exit_on_error`
  from `TAXONOMY_PATH`/`JURISDICTIONS_PATH`.
- `get_activities()`: loaded via `regradar.profiles.load_activities`.

A separate `get_write_engine()` (a second, read-write engine via
`collector.storage.init_db`) backs the classification write tools only, kept
apart from the read-only `get_engine()` deliberately.

Every tool body is wrapped in `_safe(label, fn, ...)`: it runs the query
function, re-raises `ValueError` as-is (the one deliberate case is
`get_items_since_tool`'s unparsable `since`, since that message is safe to
show a caller), and on any other exception logs the real error and raises a
sanitized `RuntimeError(f"{label} failed - see server logs")` so internal
detail (DB paths, SQL) never reaches the MCP client.

A `profile` parameter on most tools resolves through `_resolve_profile()` to
a `regradar.profiles.ExpandedProfile` (a shipped template id, e.g. `eu-casp`,
or a path to a custom profile YAML), which every query function turns into
extra SQL conditions.

At process start (`__main__` block), taxonomy, activities and the engine are
all loaded eagerly, so a broken config or missing DB kills the process at
boot rather than surfacing as a `SystemExit` inside a tool call (which
`_safe()` does not catch). Module import itself stays side-effect free so
tests can import the module and point `get_taxonomy()`/`get_engine()` at
their own paths.

## Inputs

- `DB_PATH` (default `/app/data/reg_radar.db`), read-only.
- `TAXONOMY_PATH`, `JURISDICTIONS_PATH`, `ACTIVITIES_PATH`, `PROFILES_DIR` env vars.
- `MCP_EXTRA_ALLOWED_HOSTS` (comma-separated `host:port` pairs).
- `EXPORT_DIR` (default `/app/data/exports`), for `export_tool`.
- Tool call arguments from the MCP client.

## Outputs and side effects

Every read tool returns JSON-serializable dicts/lists built from
`mcpserver/queries.py`. `classify_item_tool`, `classify_items_tool` and
`export_tool` write: the first two to the `classifications`/`item_labels`
tables via `regradar.classification`, the third to files under a directory
confined beneath `EXPORT_DIR` (`subdir` is validated against `..` traversal
and symlink escape).

### Tools (`mcpserver/queries.py`, wired in `server.py`)

Read-only:

| Tool | One-line description |
|---|---|
| `search_items_tool` | Full-text search (FTS with LIKE fallback) across title/snippet/raw_content, with regulator, language, tag, jurisdiction, label and profile filters, paginated. |
| `get_recent_tool` | Most recent items by `fetched_at`, with the same filter set as search minus the query text. |
| `list_regulators_tool` | Every regulator in the registry with live item counts, `last_fetched_at`, `stalled` and `blocked` flags. |
| `get_item_tool` | One item by id, including `raw_content`, `body_md` and a `document` frontmatter object; `max_content_chars` truncates both large text fields. |
| `get_run_log_tool` | Recent collector run rows (status, counts, `error_detail`), most recent first. |
| `get_warnings_tool` | Items tagged `warnings` at the feed level (deprecated alias for `labels={"document_type": ["warning"]}`). |
| `count_items_tool` | Total count matching a search, without fetching rows. |
| `get_month_facets_tool` | Item counts grouped by publish month, ignoring date filters. |
| `list_tags_tool` | Distinct `source_tags`/`content_tags` values with counts (deprecated v1 columns). |
| `list_labels_tool` | Every taxonomy axis with its distinct classified values and counts. |
| `get_items_since_tool` | Deprecated polling primitive: items with `fetched_at` after `since`. Superseded by `get_changes_tool`. |
| `get_health_tool` | Latest run status/freshness, `db_integrity`, `stalled_sources`, `blocked_sources`, `dataset_import` cursor. |
| `get_taxonomy_tool` | The full taxonomy (axes, cardinality, values, aliases, version). |
| `get_unclassified_tool` | Backlog of items missing a live classification on one axis, oldest first, for a classifier to work through. |
| `get_changes_tool` | Cursor-based change stream (new items and reclassifications), the sync primitive; returns `{"items", "next_cursor"}`. |

Write:

| Tool | One-line description |
|---|---|
| `classify_item_tool` | Writes one classification (axis/value/by/confidence) and returns the item's new effective labels. |
| `classify_items_tool` | Writes up to 100 classifications in one call; every entry validated before anything is written. |
| `export_tool` | Writes a markdown/JSONL export of items matching a profile to disk, confined under `EXPORT_DIR`, with cursor bookkeeping. |

This is 18 tools total. The `skills/reg-radar-operator/SKILL.md` "MCP tools"
table lists the same 14 read tools but only 2 of the 3 write tools (it
omits `export_tool`) - cross-checked against `server.py`'s `@mcp.tool()`
decorators directly for this page.

## How to run or invoke

```
docker compose up -d mcp
```

Then connect a client, e.g.:

```
claude mcp add
```

The server listens on `streamable-http` transport, bound to `0.0.0.0:8000`
inside the container (published as `127.0.0.1:8000` by default per
`docker-compose.yml`).

## How to test

`code/tests/` (pytest, run from `code/`) covers `mcpserver/queries.py`;
`_safe()`'s sanitization and the tool wiring can be exercised by importing
`mcpserver.server` and calling a tool function directly against a test
`get_engine()`/`get_taxonomy()`.

## Boundaries

- Must never leak internal exception detail (DB paths, SQL text) to an MCP
  client - only `_safe()`'s sanitized message and the deliberate `ValueError`
  case are allowed through.
- Must never write through `get_engine()` (read-only, `mode=ro`) - all writes
  go through the separate `get_write_engine()`.
- `export_tool` must never write outside the resolved `EXPORT_DIR` root - no
  absolute `subdir`, no `..` traversal, no symlink escape.
- Must not create the database schema itself - `get_engine()` raises if
  `DB_PATH` doesn't exist rather than initializing it.

## Related pages

[Web UI](component-webui.md), [Changes](component-changes.md)
