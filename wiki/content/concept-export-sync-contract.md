---
title: Export and Sync Contract
type: concept
sources: [regradar/changes.py, regradar/exporter.py, regradar/webhooks.py]
last-verified: 2026-09-10
---

## Purpose

The primitive every downstream adapter builds on: "everything that changed
since cursor X." Covers the change cursor, the changes API, the exporter,
outbound feeds and webhooks.

## How it works

Cursor: `updated_at` ISO timestamp plus item id as tiebreak, opaque to
callers, returned by every call.

`GET /api/changes?since=<cursor>&profile=<name>&limit=` returns items
(frontmatter fields as JSON, body optional via `include_body=true`) and the
next cursor. The MCP tool `get_changes_tool` mirrors it and replaces
`get_items_since_tool`.

`regradar export --profile <p> --format markdown|jsonl --out <dir>
[--since <cursor>] [--with-originals]` writes the
[Canonical Document Format](concept-canonical-document-format.md) layout.
Idempotent: unchanged items are not rewritten, so a directory can be
pointed at directly by Obsidian or synced by any file tool.

Outbound feeds: `GET /feeds/<profile>.atom` for anything that reads RSS.
Webhook delivery is a subscriber to the same change stream.

The Obsidian adapter is the exporter plus a folder inside a vault. It is
out of scope to build a plugin, it is in scope to make the exporter output
open cleanly in Obsidian with working frontmatter properties.

## Inputs

A profile (see [Profiles and Activities](concept-profiles-activities.md)),
an optional cursor, and the item store (effective labels plus content).

## Outputs and side effects

Markdown or JSONL files under an output directory (export mode), JSON
responses from `/api/changes` and `get_changes_tool`, an Atom feed per
profile, and webhook deliveries.

## How to run or invoke

`regradar export --profile <p> --format markdown|jsonl --out <dir>
[--since <cursor>] [--with-originals]`. See [Exporter](cli-exporter.md) for
the full command reference, [Changes](component-changes.md) for the
changes API and cursor mechanics, [Webhooks](component-webhooks.md) for
delivery, and [digest](cli-digest.md) for digest-style delivery.

## How to test

Not applicable at this level. See the linked component and CLI pages for
the commands that prove export idempotency, cursor correctness and webhook
delivery.

## Boundaries

Export must be idempotent, unchanged items are never rewritten. The cursor
is opaque to callers and must never be constructed by a caller from raw
fields, only round-tripped from a prior response.

## Related pages

[Principles](concept-principles.md)
[Canonical Document Format](concept-canonical-document-format.md)
[Profiles and Activities](concept-profiles-activities.md)
[Exporter](cli-exporter.md)
[Changes](component-changes.md)
[Webhooks](component-webhooks.md)
[digest](cli-digest.md)
