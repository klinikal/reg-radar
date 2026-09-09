# Export, changes cursor, outbound feeds and webhooks (the adapter contract)

**Size:** M  **Depends on:** #4 canonical document format, #7 profiles and activities  **Design doc:** docs/design/regradar-architecture.md §6

## Why

RegRadar's value to any downstream tool (Obsidian, a vector store, a webhook consumer, an RSS reader) rests on one primitive: everything that changed since cursor X. Today the closest thing is `get_items_since_tool`, which polls `fetched_at` only, so a reclassification or human correction that bumps `updated_at` without a new fetch is invisible. There is no HTTP equivalent, no file export, no feed, no webhook. This issue builds the one change-stream primitive and every adapter on top of it, so later work (digests in #13, the published dataset in #12) has a single contract to depend on.

## Scope

- Opaque cursor: `updated_at` ISO timestamp plus item id as tiebreak (design doc §6). One encode/decode point; treated as an opaque string everywhere else.
- `GET /api/changes?since=&profile=&limit=&include_body=` in `webui/app.py`, returning frontmatter-shaped JSON and a `next_cursor`.
- `get_changes_tool` in `mcpserver/server.py`, mirroring the same query and cursor shape.
- Deprecate `get_items_since_tool`: keep it working, mark it deprecated in its docstring and the README's tool table, log a warning naming `get_changes_tool` as the replacement. Do not remove it here.
- `regradar export --profile <p> --format markdown|jsonl --out <dir> [--since <cursor>] [--with-originals]`, writing the section 3 layout. Idempotent: unchanged items are not rewritten; a stale or missing `--since` falls back to a full export rather than corrupting the target directory.
- `GET /feeds/<profile>.atom`.
- Webhook subscriber: config-defined URL per profile, HMAC-SHA256 signed payload, retry with backoff, delivery log.
- README: new "Export and sync" section; update the tools table for `get_changes_tool`.

## Non-goals

- Digests, email or Telegram delivery (#13).
- Packaging the export as a dated release (#12): this issue only makes `regradar export` correct and idempotent.
- An Obsidian plugin.
- A UI for managing webhook subscriptions; config file only.

## Design notes

- `mcpserver/queries.py`: add `get_changes(engine, since=None, profile=None, limit=100, include_body=False)`, ordered by `updated_at, id`, filtered through `regradar/profiles.py`'s `expand_profile` (issue #7) when `profile` is set. Leave `get_items_since` unchanged.
- `mcpserver/server.py`: add `get_changes_tool`; add the deprecation warning to `get_items_since_tool`.
- `webui/app.py`: add `/api/changes` and `/feeds/<profile>.atom`.
- New `regradar/exporter.py`: builds the section 3 tree from `get_changes`, using `regradar.document`'s `render()` (issue #4) for frontmatter and body, and `regradar/profiles.py`'s `load_profile` for `--profile` resolution. Do not reimplement either.
- New `regradar/webhooks.py`: reads `config/webhooks.yaml`, calls `get_changes` per subscriber's own stored cursor, POSTs the batch, advances the cursor only on a 2xx.
- `regradar/cli.py` (created in #4): add `export` as a subcommand. Later issues (`import-dataset` in #12, `classify` in #14, `digest` in #13) do the same.

Cursor: `<updated_at ISO 8601>|<item_id>`, opaque to callers.

```yaml
# config/webhooks.yaml
- profile: uk-equities-dealer
  url: https://example.org/hooks/regradar
  secret_env: WEBHOOK_SECRET_UK_EQUITIES   # secret lives in env, never in this file
  batch_size: 25
  max_retries: 5
  backoff_seconds: 30                       # doubles per retry
```

Signature header: `X-RegRadar-Signature: sha256=<hex hmac of raw body>`. Payload: `{"cursor": "...", "next_cursor": "...", "items": [...]}`, same item shape as `/api/changes`.

`/feeds/<profile>.atom`: one entry per item, newest first, capped at a config count (default 50); `<updated>` from `updated_at`; summary content only, linking to the item, not the full body.

## Acceptance criteria

- [ ] `GET /api/changes?since=<cursor>` returns only items with `updated_at` after the cursor (id as tiebreak on equal timestamps), oldest first, with a `next_cursor` that round-trips.
- [ ] `get_changes_tool` returns the same shape as `/api/changes` for equivalent parameters.
- [ ] `get_items_since_tool` still works, logs a deprecation warning, and its docstring points to `get_changes_tool`.
- [ ] `regradar export --format markdown --out <dir>` produces the section 3 layout; a second run with no new data touches no files; a third run after one new item writes only that item.
- [ ] `regradar export --format jsonl` produces one JSON object per line matching the frontmatter schema.
- [ ] `--with-originals` copies artefacts into `originals/`; omitting it does not.
- [ ] Obsidian acceptance test: export a profile to a directory, open it as an Obsidian vault (or subfolder), confirm frontmatter renders under Obsidian's Properties panel with no plugin installed.
- [ ] `GET /feeds/<profile>.atom` validates as Atom and updates when new items match the profile.
- [ ] A configured webhook subscriber receives a signed POST for a new matching item within one collector run; a test receiver rejects an invalid signature; a receiver returning 500 retries with backoff and logs failure after `max_retries` without blocking other subscribers.
- [ ] README's tools table and a new "Export and sync" section are updated.

## Open questions

- Should `/api/changes` support streaming for very large catch-ups, or is cursor pagination enough? Leaning towards pagination only here; revisit if #12 shows real demand.
- Fixed `batch_size` per webhook subscriber, or should consumers negotiate a smaller batch dynamically? Fixed is simpler and matches the config-over-code principle.
- Should `regradar export` persist its own cursor file (e.g. `.regradar-cursor`) inside `--out`, so a scheduled export doesn't need external state tracking? Recommend yes.
