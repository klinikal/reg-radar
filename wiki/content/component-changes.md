---
title: Changes
type: component
sources: [regradar/changes.py]
last-verified: 2026-09-10
---

## Purpose

Defines the opaque sync cursor used by the change-stream primitive: encoding
and decoding a `<updated_at>|<item_id>` string. This is the one place that
builds or parses a cursor; every consumer treats the return value of
`encode_cursor()` as opaque.

## How it works

A cursor is the string `<updated_at in ISO 8601 Z, microsecond precision>|<item_id>`,
format `%Y-%m-%dT%H:%M:%S.%fZ`. Microsecond precision (matching the
database's own storage precision, not `regradar.document`'s whole-second
frontmatter rendering) is deliberate: item ids are content hashes, not
monotonic, so two items updated within the same whole second (routine during
one collection run) would otherwise collide on the id tiebreak and one could
be silently skipped forever.

`_to_utc(value)` normalises a datetime, an ISO string (Z-suffixed or not),
or a plain SQLite-format string into a UTC-aware `datetime`.

`encode_cursor(updated_at, item_id)` builds the cursor string. Raises
`CursorError` if `item_id` is falsy.

`decode_cursor(cursor)` splits on the first `|`, parses the timestamp with
the fixed format, and returns `(datetime, item_id)`. Raises `CursorError` on
a missing separator, an empty item id, or an unparsable timestamp.

`DATASET_IMPORT_CURSOR_KEY` is the `collector.storage` sync_state key that
holds the cursor `regradar import-dataset` stores (the published dataset's
`generated_at`). It lives in this module, alongside the cursor codec,
because the writer (`regradar.dataset`) and both readers
(`mcpserver.queries.get_health`, `regradar.exporter.resolve_since`) would
otherwise have to import each other.

## Inputs

- `encode_cursor`: `updated_at` (datetime, ISO string, or SQLite DATETIME
  string) and `item_id` (str, required).
- `decode_cursor`: a cursor string.

## Outputs and side effects

- `encode_cursor` returns a cursor string.
- `decode_cursor` returns `(datetime, str)`.
- No side effects; pure functions plus one module-level constant.

## How to run or invoke

Not invoked directly. Used by `mcpserver.queries.get_changes`,
`get_changes_tool`, `webui.app`'s `/api/changes`, `regradar.exporter`, and
`regradar.webhooks`, all of which pass a cursor through opaquely.

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_changes.py`-style file exercising
`encode_cursor`/`decode_cursor` round-trips and the same-second tiebreak
case the docstring describes.

## Boundaries

- Never build or parse a cursor anywhere else in the codebase; go through
  `encode_cursor`/`decode_cursor`.
- Never drop to whole-second precision; that was the exact bug this module
  fixes (same-second updates silently skipped).

## Related pages

[Export and Sync Contract](concept-export-sync-contract.md)
[reg-radar export](cli-exporter.md)
