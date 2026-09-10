---
name: reg-radar-operator
description: Use when operating, querying, or debugging reg-radar — the regulatory intelligence tool for firms tracking European financial regulators. Triggers on questions about EU/UK regulator news, MiCA developments, CASP warnings, or direct requests to run/query/debug the reg-radar MCP server, collector, exporter or digests.
---

# reg-radar Operator

How to run, query, and navigate reg-radar — a regulatory intelligence tool that collects news, warnings, and publications from 40-odd European financial regulators (every EU/EEA member state plus ESMA/EBA/EIOPA and AMLA, and the UK FCA) on a schedule, stores them in SQLite, classifies them against a configurable taxonomy, and exposes the data via an MCP server, a web UI, an export/change stream and digests.

**Read your context first:** check `skills/reg-radar-operator/refs/context.md` (relative to repo root). If it exists, read it — it tells you what entity is operating this instance and what's relevant to them. That file is git-ignored on purpose (it's instance-specific and contains private company details) so it won't exist in a fresh clone. If it's missing, read `refs/example.context.md` instead — a template showing what that file should contain — and treat this as an unconfigured instance until the operator fills one in (see the `reg-radar-onboarding` skill, in the sibling `skills/reg-radar-onboarding/` folder, to generate it interactively).

For component-level detail beyond this skill (every module, config file, script and runbook), see [`wiki/README.md`](../../wiki/README.md).

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

**A bad config now fails at boot.** `mcp` and `webui` load `config/taxonomy.yaml`, `config/jurisdictions.yaml` and `config/activities.yaml` at startup. If one of them is invalid the container exits immediately instead of serving errors — check `docker compose logs mcp` / `logs webui` for the parse error rather than assuming a network problem.

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

The live database lives in a Docker-managed named volume (`reg_radar_data`), not a bind-mounted host folder — this is deliberate: SQLite's WAL mode needs precise file-locking guarantees that Docker Desktop's macOS bind-mount bridge doesn't fully honour, which caused a real corruption incident in July 2026. You can no longer browse the `.db` file directly from Finder/Terminal; use `docker compose exec mcp sqlite3 /app/data/reg_radar.db` (or similar) if you need to inspect it.

A `litestream` service continuously replicates the database to `./backups/reg_radar` on the host (real host disk, outside Docker), keeping roughly constant size (a snapshot plus a rolling ~24h retention window, auto-pruned — not accumulating daily copies). Check `docker compose logs litestream` to confirm it's running.

**`originals/` (fetched HTML/PDF artefacts, under `ARTEFACTS_DIR` on the data volume) is not covered by Litestream** — it only replicates the SQLite file. Include `originals/` in whatever host-level backup you run against the `reg_radar_data` volume (e.g. a periodic `docker run --rm -v reg_radar_data:/data -v $(pwd)/backups:/backup alpine tar czf /backup/originals-$(date +%F).tar.gz -C /data originals`), or accept that a lost volume means item text survives (in the DB backup) but the original artefacts don't.

**To restore from backup** (e.g. after corruption or accidental data loss):
```bash
docker compose stop collector mcp webui
docker run --rm -v "$(pwd)/backups:/backups:ro" -v reg_radar_data:/out litestream/litestream:0.5.9 restore -o /out/reg_radar.db file:///backups/reg_radar
# verify before trusting it:
docker run --rm -v reg_radar_data:/data alpine sh -c "apk add --no-cache sqlite && sqlite3 /data/reg_radar.db 'PRAGMA integrity_check;'"
# if you also back up originals/, restore it into the same volume:
# docker run --rm -v reg_radar_data:/data -v $(pwd)/backups:/backup:ro alpine tar xzf /backup/originals-<date>.tar.gz -C /data
docker compose up -d collector mcp webui
```

---

## What's in the data

Registry with per-source status lives at `config/regulators/` (one file per jurisdiction). Every item is a canonical document: title, source URL, a Markdown `body_md`, the regulator and its jurisdiction, and a set of classification **labels**.

### Classification and labels

Labels are not free-text tags. `config/taxonomy.yaml` defines the **axes** (currently `document_type`, single-valued, and `instrument`, multi-valued) and the valid values on each; nothing in the code hard-codes an axis name except `jurisdiction`. Call `get_taxonomy_tool()` before writing anything — it returns every axis, its cardinality, and every valid value with aliases, so you never need filesystem access to the config.

Every label is written into a provenance-tracked classification store, never straight onto the item. A row records `by` (who), `confidence` (0.0–1.0) and the `taxonomy_version` in force at the time. Five kinds of writer, in descending authority:

| `by` | Written by |
|---|---|
| `human:<name>` (e.g. `human:web-ui`) | A person correcting a label in the web UI or through the API. Never overwritten by any automated path. |
| `llm:<provider>/<model>` | An agent classifying the backlog through `classify_item_tool`/`classify_items_tool`, or the `reg-radar classify` runner. |
| `rule` | `config/rules.yaml`, a deterministic weighted regex engine that scores each item on collection. |
| `source_default` | A `sources[].labels` block in `config/regulators/*.yaml`, for a genuinely single-purpose feed. |
| `import` | Migrated v1 content tags, kept at low confidence so anything better outranks them. |

The **effective** label on an axis is the highest-confidence live row that clears the axis's `min_confidence` threshold. An item can therefore be unlabelled on an axis even though rows exist for it, if nothing scored high enough. `get_item_tool` returns `label_provenance` alongside `labels` — read it before trusting a label, and say where a label came from when it matters.

The v1 blanket tags (`casp`, `mica`, `dora`, `aml` stamped on whole feeds) are gone: they were stripped during the v2 migration precisely because they were feed-level guesses, not per-item facts. The old `source_tags`/`content_tags` columns still exist and the tools still accept them, but they are deprecated — prefer `labels` and `jurisdictions`.

### Jurisdictions

`config/jurisdictions.yaml` is a hierarchy, not a flat list of country codes. Ids are lower-case (`fr`, `de`, `gb`, `eu`). Every EU member state lists `eu` as a parent; the EEA-EFTA states (`is`, `no`) also list `eu`; the EEA itself is not a jurisdiction id. A jurisdiction filter is expanded through **ancestors**, so `jurisdictions=["fr"]` returns AMF (`fr`) *and* ESMA (`eu`) — the supranational regulator that covers France. `gb` has no parents, so it matches FCA only. Documents carry `jurisdiction_ancestors` so a consumer can see the expansion without re-deriving it.

**Non-EU note:** `uk-fca` is in scope, not an exception any more. If an operator's context (`refs/context.md`) is EU/MiCA-only, weigh UK items accordingly rather than assuming they're automatically relevant.

### Profiles

A **profile** is a saved filter — jurisdictions, activities, instruments, document types, regulators, languages — that every surface accepts identically: the `profile` parameter on MCP query tools, the web UI's profile picker, the JSON API, the exporter, the Atom feeds, webhooks and digests. Shipped templates live in `config/profiles/` (`eu-casp`, `eu-bank`, `eu-emi`, `uk-equities-dealer`); a user's own profile is a YAML file passed by path. `activities` (in `config/activities.yaml`) expand to instrument sets, so a profile says "I am a CASP" rather than listing every instrument.

`include_unclassified` (default true) decides whether items with no instrument label yet are included. Leave it true unless the operator explicitly wants a strict view — with it false and no instruments selected, a profile matches nothing at all.

**Historic backfills:** the daily collector only picks up the newest page of any scrape-type source — full historic archives are pulled separately via one-off scripts in `scripts/` (`backfill_esma_library.py`, `backfill_fca_publications.py`), not run automatically. A new install can skip most of that by importing the published dataset (`reg-radar import-dataset`) instead. If asked "why is there a gap in older data" for a scrape-type source, check whether its backfill script has actually been run — see the README's "Historic backfills" section.

## MCP tools

Read-only:

| Tool | Use for |
|---|---|
| `search_items_tool` | Full-text search across title + snippet. Filters: `regulator_id`, `language`, `jurisdictions`, `labels`, `profile`, `date_from`, `date_to`, plus the deprecated `tags`/`source_tags`/`content_tags`. Paginated via `limit`/`offset`. |
| `get_recent_tool` | Most recent N items, optional `regulator_id`/`language`/`profile` filters. |
| `count_items_tool` | Total matching a filter, before paging through it. |
| `get_month_facets_tool` | Per-month counts for a query — the date histogram behind the web UI. |
| `get_item_tool` | Single item by id: `raw_content`, `body_md`, and a `document` frontmatter object with `labels`, `label_provenance`, `jurisdiction_ancestors` and any stored artefact. `max_content_chars` protects your context window. |
| `list_regulators_tool` | Full registry plus live fetch stats and a `stalled` flag — the first place to look for source health. |
| `get_run_log_tool` | Recent collector run stats — is collection working at all. |
| `get_health_tool` | Collector freshness, `db_integrity`, stalled sources, and `dataset_import` (the sync cursor if this DB was seeded from a published dataset). |
| `list_labels_tool` | Every axis/value currently in use with counts — the vocabulary actually present in this database. |
| `list_tags_tool` | Same, for the deprecated v1 tag columns. |
| `get_taxonomy_tool` | The full taxonomy: axes, cardinality, valid values, aliases, version. Call before classifying. |
| `get_unclassified_tool` | The backlog for one axis, oldest first, with `title`, `body_md` and the item's labels on other axes. `include_low_confidence=True` also returns items scored below the threshold. |
| `get_changes_tool` | Everything changed since an opaque cursor, oldest first, with `next_cursor` to resume. Catches reclassifications and human corrections, not just new fetches — this is the sync primitive. |
| `get_warnings_tool` | Kept as an alias for `labels={"document_type": ["warning"]}` for one release; prefer the label filter. |
| `get_items_since_tool` | Deprecated — only sees `fetched_at`. Use `get_changes_tool`. |

Write:

| Tool | Use for |
|---|---|
| `classify_item_tool` | Write one classification. `by` should be `llm:<provider>/<model>` for an agent-driven label. An unknown `item_id` raises rather than recording an orphan row. |
| `classify_items_tool` | Up to 100 at once, all validated before anything is written. Returns `written` plus a per-entry `errors` list, so one bad entry never costs the batch. |
| `export_tool` | Write a markdown/JSONL export to disk — the MCP equivalent of `reg-radar export`, see below. Confined to a directory under the export root (`EXPORT_DIR`, default `/app/data/exports`); no separate auth gate beyond what already covers the classify tools above. |

See the sibling `skills/reg-radar-classifier/` skill for the classification loop itself (how to pick a confidence, when to leave an axis unset).

## The change stream, export and digests

Everything downstream is built on one primitive: "what changed since cursor X", where the cursor is an opaque `<updated_at>|<item_id>` string. It is exposed as `get_changes_tool` and `GET /api/changes`, and consumed by:

- **`export_tool(profile, format=["markdown"], subdir=None, since=None, with_originals=False)`** (MCP) — an agent's way to get this: writes the canonical document layout and/or `export.jsonl` (a full current-state snapshot, one line per item), idempotent, tracking its own `.reg-radar-cursor` inside the output directory. Output is confined to a directory under the export root (`EXPORT_DIR` env, default `/app/data/exports`); `subdir` is an optional relative name under it, not a free path — an absolute path, `..` traversal, or a symlink escaping the root is rejected. Returns `{"written", "cursor", "out_dir"}`. **Pass both formats in one call**: calling it twice with different formats into the same `subdir` means the first call's cursor has already advanced past everything (the exporter detects that case and falls back to a full export, but the one-call form is the intended usage).
- **`reg-radar export --profile <p> --format markdown --format jsonl --out <dir>`** (CLI) — the same exporter, for writing to an arbitrary path outside the export root (e.g. a bind mount, a path the container doesn't own) or from a non-agent context. `--with-originals` also copies stored artefacts. Same one-call-per-directory caveat as above.
- **`GET /feeds/<profile>.atom`** — a per-profile Atom feed for any RSS reader. Summaries and links only, never full bodies.
- **`config/webhooks.yaml`** — subscribers that get an HMAC-signed POST of new matching items after every collection run, with per-subscriber cursors and retry backoff. Opt-in; a delivery failure never fails the collection run.
- **`config/digests.yaml` / `reg-radar digest`** — scheduled digests grouped by jurisdiction then regulator, delivered by email (SMTP, verified TLS), Telegram, or webhook. `reg-radar digest --preview --profile <p>` renders one to stdout and sends nothing; `reg-radar digest --send --name <n>` delivers and only advances that digest's cursor if every channel succeeded. The collector's own scheduler runs these — there is no second cron.
- **`reg-radar import-dataset <url|path>`** — seeds a fresh database from the published nightly dataset release instead of backfilling by hand. Verifies the tarball's sha256, refuses a non-empty database without `--force`, and records a sync cursor you can hand to any change-stream consumer.

## Other CLI commands

Run these inside the collector container (`docker compose exec collector reg-radar <command>`), or locally against a `--db-path`.

| Command | Use for |
|---|---|
| `reg-radar reclassify --axis <a> [--by rule\|source_default] [--dry-run]` | Re-apply source defaults or re-score every item against the current `config/rules.yaml` after a config change. Human corrections are never touched. Always dry-run first. |
| `reg-radar classify --provider <p> [--axis <a>] [--limit N] [--dry-run]` | Batch LLM classifier runner: works through `get_unclassified` for an axis via a pluggable provider. Run `reg-radar classify --help` for the providers this build supports, including `command`, which shells out to any CLI that reads a prompt on stdin. Writes `by: llm:<provider>/<model>` provenance. |
| `reg-radar render --out <dir>` | Render items to the canonical document format without the export cursor machinery. |
| `reg-radar export` / `reg-radar digest` / `reg-radar import-dataset` | See above. |

## Source health

A source that quietly stops returning items is the failure mode that matters most here — nothing errors, the feed just goes empty.

- `get_health_tool()` reports `stalled_sources`: sources that have completed several consecutive successful-but-empty runs. That is distinct from `consecutive_failures`, which only counts outright fetch errors.
- `list_regulators_tool()` carries the same `stalled` flag per regulator, plus `last_fetched_at` and `consecutive_failures`.
- The web UI's regulator panel surfaces both.
- `tests/fixtures/sources/` holds a last-known-good response per source, and a nightly contract check (`scripts/source_health.py`, driven by the `source-health-nightly` workflow) re-fetches each source live and compares the parser's output against it, filing a GitHub issue when a site's markup changes. `scripts/capture_fixture.py` re-captures a fixture deliberately — it is a manual tool, never automatic.

When a regulator looks like it has gone quiet, check `get_health_tool()` first, then the nightly issues, then the fixture, before assuming the regulator simply published nothing.

## Typical queries

- "Any recent warnings from Germany?" → `search_items_tool(query="", regulator_id="de-bafin", labels={"document_type": ["warning"]})`
- "Everything MiCA-related across the EU" → `search_items_tool(query="", jurisdictions=["eu"], labels={"instrument": ["mica"]})`
- "What matters to a French CASP this week?" → `get_recent_tool(profile="eu-casp", limit=20)` — `eu-casp` expands to the EU plus every member state
- "What's changed since I last checked?" → `get_changes_tool(since="<your saved next_cursor>")`
- "What still needs classifying?" → `get_unclassified_tool(axis="document_type", limit=50)`
- "Is the collector actually working?" → `get_health_tool()`, then `get_run_log_tool()`
- "Why is this item labelled a warning?" → `get_item_tool(item_id=...)`, read `label_provenance`
