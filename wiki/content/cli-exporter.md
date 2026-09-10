---
title: reg-radar export
type: cli
sources: [regradar/exporter.py]
last-verified: 2026-09-10
---

## Purpose

Exports items matching an optional profile to the canonical document layout and/or a JSONL snapshot, tracking progress with a persisted cursor so repeated runs only export what changed.

## How it works

`export_profile()` is the core routine `run()` calls. It resolves `--out/.reg-radar-cursor` (via `resolve_since()`, which prefers a CLI `--since` outright, otherwise reads the persisted cursor, falling back to a full export if the persisted cursor is missing or unparseable). It then loops `mcpserver.queries.get_changes()` in pages of 200, and for each changed item: writes the Markdown form via `regradar.render.render_item_to_dir()` (reused, not reimplemented) when `"markdown"` is in the requested formats, and/or folds the item into an in-memory JSONL snapshot dict (`{item_id: json_line}`) when `"jsonl"` is requested. `exclude_regulators` items are skipped from output entirely (but the cursor still advances past them). The JSONL snapshot is rewritten to `export.jsonl` at the end only if its content actually changed, mirroring the Markdown path's idempotent-write behaviour.

Both formats share one pass over `get_changes()` and one cursor. The module docstring calls out a trap: running markdown and jsonl as two separate invocations into the same `--out` breaks, because the first invocation's cursor advances past everything, leaving the second with nothing left to export. Pass `--format markdown --format jsonl` together instead. As a safety net, `export_profile()` detects a persisted cursor paired with a format that has no prior output in `--out` (no `export.jsonl` for jsonl, no `*.md` under `export/` for markdown) and falls back to a full export rather than trusting a cursor that was never advanced for that format.

`run()` loads the taxonomy, expands `--profile` via `regradar.profiles.get_expanded_profile()` if given, loads `--exclude-regulator-file` exclusions via `regradar.dataset.load_exclusions()` if given, deduplicates `--format` values while preserving order (`dict.fromkeys`), then calls `export_profile()` and persists the returned cursor if it advanced.

## Inputs

- `--profile`: shipped profile template name or path (default: all items).
- `--format` (repeatable, choices `markdown` | `jsonl`, default `markdown`): output format(s).
- `--out` (required): output directory.
- `--since`: cursor to export from (default: the persisted `.reg-radar-cursor` in `--out`, or a full export if none).
- `--with-originals`: also copy artefacts into `<out>/originals/`.
- `--exclude-regulator-file`: path to a YAML file listing regulator ids (flat list) to drop from this export entirely.
- `--db-path` (default from `DB_PATH` env var, else `/app/data/reg_radar.db`).
- `--taxonomy-path` (default from `TAXONOMY_PATH` env var, else `config/taxonomy.yaml`).
- `--jurisdictions-path` (default from `JURISDICTIONS_PATH` env var, else `config/jurisdictions.yaml`).
- `--activities-path` (default from `ACTIVITIES_PATH` env var, else `config/activities.yaml`).
- `--profiles-dir` (default from `PROFILES_DIR` env var, else `config/profiles`).

## Outputs and side effects

Writes Markdown document files (section 3.1 layout, via `render_item_to_dir`) and/or `<out>/export.jsonl` (full current-state snapshot, one line per item id, latest wins). Writes/updates `<out>/.reg-radar-cursor`. With `--with-originals`, also copies artefact files into `<out>/originals/`. Prints a count of items written to stdout.

## How to run or invoke

```
reg-radar export --out export --format markdown
reg-radar export --out export --format markdown --format jsonl --profile <profile>
reg-radar export --out export --format jsonl --with-originals --exclude-regulator-file exclude.yaml
```

## How to test

Run once into an empty `--out`, confirm `.reg-radar-cursor` and the expected output files exist; run again with no changes and confirm it reports 0 written and does not rewrite unchanged files (mtimes preserved).

## Boundaries

Must never export both formats via two separate invocations into the same `--out`, the shared-cursor design breaks that. Must never trust a persisted cursor when the requested format has no prior output in `--out`, it falls back to a full export instead. Must never write an unchanged JSONL snapshot (byte-identical content is skipped).

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [concept: Export Sync Contract](concept-export-sync-contract.md)
