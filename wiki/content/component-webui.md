---
title: Web UI
type: component
sources: [webui/]
last-verified: 2026-09-10
---

## Purpose

A FastAPI app (`webui/app.py`) that lets an operator browse, search and
classify collected regulatory items in a browser, and exposes the same query
layer as a small JSON API and Atom feeds for other consumers.

## How it works

`app.py` builds one `FastAPI(title="reg-radar")` app with a `_lifespan`
context manager that eagerly loads the taxonomy, activities and DB engine at
startup (a broken config or bad `DB_PATH` kills the process at boot instead
of surfacing as a per-request 500 later). Static files are mounted at
`/static` from `webui/static/`; Jinja2 templates load from
`webui/templates/`.

Three lazily-cached singletons mirror the MCP server's pattern:
`get_engine()` (via `collector.storage.init_db`, so unlike the MCP server
this one is read-write and does ensure the schema), `get_taxonomy()`,
`get_activities()`. A fourth, `get_module_state()`, is cached but
re-evaluated whenever `config/modules.yaml`'s mtime changes, via
`webui/modules.py`'s `load_module_state()`.

`webui/modules.py` distinguishes two independent filters that look identical
to a user when they produce an empty page: **modules** (`config/modules.yaml`)
decide what is *fetched* by the collector - coarse, not freely reversible;
**profiles** decide what is *shown* in this UI - fine-grained, instantly
reversible. It reuses `collector.main`'s `_load_modules_file`/
`_resolve_modules` (not reimplemented here) to resolve the operator's
selection, and its `diagnose()` function picks one of four reasons an empty
result set occurred (`no_modules_file`, `modules_invalid`, `not_selected`,
`registry_disabled`, or the ordinary "no matching items") so the empty state
never blames the wrong cause.

`_resolve_profile()` in `app.py` is the HTTP-facing profile resolver: unlike
the MCP tools/CLI, `profile` here is unauthenticated user input, and
`regradar.profiles.load_profile` treats any non-shipped name as a literal
filesystem path - so this function only ever accepts a name from
`list_shipped_profiles()`, returning a generic 400 for anything else (never
echoing the input or file content).

### Routes

| Route | Renders / returns |
|---|---|
| `GET /` | `index.html` - search form, month facets, paginated results table, regulator/jurisdiction multi-select filters, empty-state explanation. |
| `GET /item/{item_id}` | `item.html` (or `not_found.html` on a 404) - full item text (HTML-stripped via BeautifulSoup, then Jinja2-autoescaped), classification controls for `document_type` and `instrument`. |
| `POST /item/{item_id}/classify` | Writes/removes one classification as `by="human:web-ui"`, redirects back to the item page. |
| `GET /alerts` | 308 redirect to `/rules` (legacy path from the pre-rules keyword-alerts system). |
| `GET /modules` | `modules.html` - read-only table of every registry regulator's collection state, cadence, last/next run; not an editor. |
| `GET /rules` | `rules.html` - the weighted regex classification rules in `config/rules.yaml`, with an add/delete form. |
| `POST /rules` | Appends one rule (validated: known axis/value, non-empty compiled regex pattern) and rewrites `config/rules.yaml`. |
| `POST /rules/{index}/delete` | Removes one rule by list index and rewrites the file. |
| `GET /api/unclassified` | JSON: `get_unclassified()` backlog for one axis. |
| `GET /api/items` | JSON: same `search_items_advanced()` query layer as `/`. |
| `GET /api/months` | JSON: `get_month_facets()`. |
| `GET /api/items/{item_id}` | JSON: one item via `get_item()`, 404 if missing. |
| `GET /api/regulators` | JSON: `list_regulators()`. |
| `POST /api/items/{item_id}/classifications` | JSON classification write endpoint (the HTTP equivalent of the MCP `classify_item_tool`). |
| `GET /api/changes` | JSON: `get_changes()` cursor-paginated change stream, mirrors `get_changes_tool`. |
| `GET /feeds/{profile_name}.atom` | Atom XML feed of the newest items matching a shipped profile, via `get_latest_changes()`; entries are title/link/summary only, never the full body (feeds are a discovery mechanism, not a bulk-export channel). |

### Templates (`webui/templates/`)

`base.html` is the shared shell (header, theme toggle, `/static/style.css`
and `/static/app.js`). `index.html`, `item.html`, `modules.html` and
`rules.html` each extend it and are rendered by the matching route above.
`_empty_state.html` is included by `index.html` when a search returns zero
results; it branches on `empty_state.reason` to render one of the four
`modules.py` diagnoses. `not_found.html` renders the 404 for an unknown item
id. `alerts.html` (a keyword/tag rule editor) still exists in the templates
directory but no route renders it - `/alerts` only 308-redirects to `/rules`,
so this template is dead code as of this page's last verification.

### Static assets (`webui/static/`)

`app.js` auto-submits `form.filters` as the operator types (debounced 400ms
on the text query) or toggles a date/checkbox/select (immediate), restores
focus to the search box after that reload via a one-shot `sessionStorage`
flag, drives the light/dark theme toggle (persisted to `localStorage`), and
implements the regulator/jurisdiction multi-select dropdown panels
(client-side filter-as-you-type, "select all", reopen-after-submit via
another one-shot flag). `style.css` is the page's styling, including the
light/dark theme variables the inline `<script>` in `base.html` applies
before first paint.

## Inputs

- `DB_PATH`, `TAXONOMY_PATH`, `JURISDICTIONS_PATH`, `ACTIVITIES_PATH`,
  `PROFILES_DIR`, `REGULATORS_PATH`, `MODULES_PATH`, `RULES_PATH`,
  `FEED_MAX_ITEMS`, `SCHEDULE` env vars.
- HTTP query params / form bodies on the routes above.

## Outputs and side effects

Renders HTML pages and JSON/Atom responses. `POST /item/{item_id}/classify`
and `POST /api/items/{item_id}/classifications` write to the
`classifications`/`item_labels` tables. `POST /rules` and
`POST /rules/{index}/delete` rewrite `config/rules.yaml` in place via
`regradar.rules.write_raw_rules`.

## How to run or invoke

```
docker compose up -d webui
```

Then browse `http://localhost:8080`.

## How to test

`code/tests/` (pytest, run from `code/`) exercises the FastAPI app; a
`TestClient` not used as a context manager triggers `get_engine()`/
`get_taxonomy()`/`get_activities()`'s lazy-load fallback (the `_lifespan`
startup hook never runs in that case), which is how tests inject their own
config paths.

## Boundaries

- Must never render scraped `raw_content` as HTML - `_clean_text()` strips it
  to plain text via BeautifulSoup before Jinja2's own autoescaping.
- `_resolve_profile()` must never pass raw HTTP input to
  `regradar.profiles.load_profile` - only a name from `list_shipped_profiles()`
  is accepted; anything else is a generic 400 with no echoed input.
- `/modules` must stay read-only - no disable affordance exists, so no
  irreversibility warning is needed there (it lives on
  `config/modules.example.yaml` instead).
- Atom feed entries must never include the full item body, only a summary
  and the source link.

## Related pages

[MCP Server](component-mcpserver.md), [Modules](cli-modules.md)
