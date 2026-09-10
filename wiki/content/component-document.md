---
title: Document
type: component
sources: [regradar/document.py, regradar/schema/document-v1.json]
last-verified: 2026-09-10
---

## Purpose

Renders one item (plus its optional artefact, labels and label provenance)
to the canonical Markdown-with-frontmatter file, and parses that file back
into a frontmatter dict and a body string. Does no database or filesystem
I/O itself.

## How it works

`build_frontmatter()` builds the schema-1 frontmatter dict from an `item`
dict: id, regulator, regulator_name, jurisdiction (lower-cased) and its
ancestors, title, published_at (date, falling back to fetched_at),
fetched_at/updated_at (UTC datetimes, microseconds dropped), date_inferred,
language, source_url, source_label, source_type, a `content_hash` (sha256 of
the effective body), and an `original` block when an artefact is passed.
`labels` and `label_provenance` are only included when non-empty.

`render_item()` calls `build_frontmatter()`, formats `fetched_at`/
`updated_at` as `YYYY-MM-DDTHH:MM:SSZ` (PyYAML's own datetime representer
uses a different, space-separated format, so this is done explicitly),
dumps the rest via `yaml.safe_dump`, and appends the body
(`effective_body_md(item)`) below a `---` frontmatter block.

`parse()` is the inverse: splits the `---`-delimited frontmatter from the
body via regex, `yaml.safe_load`s it, and restores `fetched_at`/
`updated_at` from their Z-suffixed strings back to timezone-aware UTC
datetimes.

`effective_body_md(item)` returns `item["body_md"]` when present, otherwise
derives a body from `item["raw_content"]` for pre-v2 rows that predate
`items.body_md`: RSS/Atom raw_content (unconverted HTML summary) goes
through `rss_body_markdown()`, everything else through
`normalize_pdf_text()`. Returns `""` if neither field is present.

Body conversion helpers:
- `html_to_markdown()`: HTML to Markdown via `markdownify` (headings,
  paragraphs, lists, links, simple tables; script/style stripped). Empty
  input returns `""`. This is the only importer of `markdownify` in the
  codebase.
- `normalize_pdf_text()`: joins pypdf's line-per-visual-line extraction into
  paragraphs (split on blank lines, lines within a paragraph joined with
  spaces). No heading detection, pypdf's `extract_text()` carries no
  font-size information.
- `rss_body_markdown()`: converts an HTML summary (or falls back to a plain
  snippet) and appends `> Full text not collected. See <source_url>.`.
  Returns `(body_md, extractor)`.
- `convert_body()`: for scrape/page_watch items, prefers an HTML fragment,
  then extracted PDF text, then flattened plain text, then the "not
  collected" fallback. Returns `(body_md, extractor)`.

`slugify()`: lower-cased, ASCII-folded (NFKD normalize then ASCII-encode
with `ignore`), non-alphanumerics collapsed to single hyphens, truncated to
80 characters. A title that folds away to nothing (e.g. Greek or Bulgarian
script) falls back to `"untitled"`.

`document_path()`: builds
`export/<jurisdiction>/<regulator_id>/<yyyy>/<yyyy-mm-dd>-<slug>-<id8>.md`.
Uses `published_at` if present, else `fetched_at`. `id8` is the first 8
characters of `item["id"]`. Accepts either datetime objects or ISO strings
(raw SQL from `regradar.render`/`mcpserver.queries` returns strings).

`frontmatter_to_json_safe()` converts a frontmatter dict's date/datetime
values to ISO strings (JSON-native types) for JSON Schema validation.

## Inputs

An `item` dict carrying at minimum: id, regulator_id, regulator_name,
jurisdiction, title, url, published_at, fetched_at, updated_at,
date_inferred, language, source_label, source_type, and body_md or
raw_content. Optionally an `artefact` dict (path, mime, sha256, bytes), a
`labels` dict, and a `label_provenance` list.

## Outputs and side effects

Pure functions, no I/O. `render_item()` returns the full document text as a
string. `parse()` returns `(frontmatter_dict, body_str)`.
`frontmatter_to_json_safe()` returns a JSON-serialisable dict for schema
validation against `regradar/schema/document-v1.json`.

## How to run or invoke

Not invoked directly. `regradar/render.py`'s CLI command loads items from
the database and calls `render_item()`/`document_path()` to write files
under `export/`.

## How to test

Round-trip a rendered document through `parse()` and compare against the
original `item`/`labels`/`label_provenance`. Validate `render_item()`'s
output frontmatter (via `frontmatter_to_json_safe()`) against
`regradar/schema/document-v1.json` with `jsonschema`.

## Boundaries

Must never write raw per-classifier rows to `labels`, only effective
(post-precedence) values. Must never write activities to frontmatter, they
are derived at filter time from `instrument`. `SCHEMA_VERSION` (currently 1)
must bump if a frontmatter key is removed or renamed; adding a key under the
same schema version is fine.

## Related pages

[Canonical Document Format](concept-canonical-document-format.md)
