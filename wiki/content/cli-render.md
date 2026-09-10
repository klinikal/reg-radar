---
title: reg-radar render
type: cli
sources: [regradar/render.py]
last-verified: 2026-09-10
---

## Purpose

Writes one or all items in the database to the canonical document format (section 3.1 layout) on disk, as Markdown files with frontmatter.

## How it works

`render_item_to_dir(engine, item_id, out_dir, taxonomy=None)` loads the item row and its regulator row (`_load_item`), fetches the item's artefact (`collector.storage.get_artefact`), and, if a `taxonomy` was passed, also loads that item's live `classifications` rows. It joins all of this via `mcpserver.queries._document_fields`, the same helper `get_item()`/`_documents_for_items()` use, so the rendered document can't drift from what the MCP tools report for the same item. `regradar.document.render_item()` then produces the Markdown text, and `regradar.document.document_path()` gives the file's path under `out_dir`. The write is skipped if the target file already exists with byte-identical content, so re-running `--all` doesn't touch mtimes of unchanged files. Returns the path written/checked, or `None` if the item doesn't exist.

`run()` requires either an `item_id` positional argument or `--all`; with neither it prints a usage error to stderr and returns exit code 2. It loads the taxonomy via `regradar.taxonomy.load_and_exit_on_error`, then either renders every item's id (queried `ORDER BY id`) when `--all` is set, or renders the single `item_id`.

## Inputs

- `item_id` (positional, optional): render a single item by id.
- `--all`: render every item in the database.
- `--out` (default `export`): output directory.
- `--db-path` (default from `DB_PATH` env var, else `/app/data/reg_radar.db`).
- `--taxonomy-path` (default from `TAXONOMY_PATH` env var, else `config/taxonomy.yaml`).
- `--jurisdictions-path` (default from `JURISDICTIONS_PATH` env var, else `config/jurisdictions.yaml`).

## Outputs and side effects

Writes Markdown document files under `--out`, at the path `document_path()` computes for each item. Prints the written path (single-item mode) or a summary count (`--all` mode) to stdout. No database writes.

## How to run or invoke

```
reg-radar render <item_id> --out export
reg-radar render --all --out export
```

## How to test

Run `reg-radar render <item_id> --out <dir>` against a database containing that item, then check the printed path exists and its content matches `regradar.document.render_item()`'s output for that item.

## Boundaries

Must never write a database change, this command only reads items/regulators/classifications and writes files. Must never rewrite a file whose content is unchanged (the byte-identical check exists specifically to preserve mtimes).

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [component: Document](component-document.md)
