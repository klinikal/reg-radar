---
title: reg-radar digest
type: cli
sources: [regradar/digest.py]
last-verified: 2026-09-10
---

## Purpose

Renders a profile's unseen items into a Markdown and HTML digest, grouped
by jurisdiction then regulator, and optionally delivers it to the
channels configured for a named digest in `config/digests.yaml`.

## How it works

`--preview` calls `render_digest()` directly on a profile and prints the
Markdown, sending nothing. `--send` loads every entry (or one named entry)
from `config/digests.yaml` via `load_digests()`, and for each calls
`send_digest()`, which expands the entry's profile, reads its stored
cursor from `collector.storage`'s `digest_state` table
(`get_digest_cursor`), renders via `render_digest()`, and delivers to
every configured channel via `_deliver_channel()`.

`render_digest()` pages through `mcpserver.queries.get_changes()` with
`include_body=True` until nothing new is left (`_fetch_all_changes()`),
then groups items by jurisdiction and regulator (`_group()`) and renders
both a Markdown (`render_markdown()`) and HTML (`render_html()`) version.
It also fetches `get_blocked_sources()` and renders a blocked-source
warning above the items in both formats.

`_fetch_all_changes()` stops pagination early and logs an ERROR if a
page's `next_cursor` doesn't advance past the cursor it was fetched with
(items reclassified rather than freshly inserted can produce a stuck
cursor); the stuck page's items are still included, deduplicated by id.

`_deliver_channel()` dispatches by `channel["type"]`: `email` calls
`regradar.delivery.email.send_email`, `telegram` calls
`regradar.delivery.telegram.send_telegram`, `webhook` calls
`regradar.delivery.webhook.send_webhook` with HMAC signing (env var named
by `secret_env`) and retry settings from `regradar.webhooks`' defaults or
the channel's own `max_retries`/`backoff_seconds`.

`send_digest()` advances the digest's stored cursor (`set_digest_cursor`)
only if every channel succeeds. A partial failure leaves the cursor
untouched, so the whole digest, not just the failed channel, is retried
in full next run. If a digest has no new items and no blocked sources and
`send_empty` is false, nothing is delivered and the cursor is left as-is
(counted as up to date).

## Inputs

- `--profile`: profile name or path (required with `--preview`; ignored
  with `--send`, which reads each entry's own profile).
- `--name`: digest name from `--digests-path` (required with `--send` to
  target one entry; omitted sends every configured digest).
- `--preview` / `--send`: mutually exclusive, one required.
- `--since`: cursor to render from with `--preview` (default: full
  history).
- `--digests-path` (env `DIGESTS_PATH`, default `config/digests.yaml`).
- `--db-path` (env `DB_PATH`, default `/app/data/reg_radar.db`).
- `--taxonomy-path`, `--jurisdictions-path`, `--activities-path`,
  `--profiles-dir` (each with a matching env var default).
- `config/digests.yaml`: a list of entries, each `{name, profile,
  schedule, channels, send_empty}`. Missing file is a no-op (`[]`), not an
  error. Each channel is `{type: email|telegram|webhook, ...}` with
  type-specific required fields (`to` for email, numeric `chat_id` for
  telegram, `url` + `secret_env` for webhook).

## Outputs and side effects

- `--preview`: prints the rendered Markdown digest to stdout. No writes.
- `--send`: delivers to each channel (email send, Telegram message,
  signed webhook POST); on full success, updates the digest's row in the
  `digest_state` table via `set_digest_cursor()`.

## How to run or invoke

```
reg-radar digest --preview --profile <name> [--since <cursor>]
reg-radar digest --send [--name <digest-name>] [--digests-path config/digests.yaml]
```

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_digest.py` or similar and run it via pytest
from `code/`.

## Boundaries

Must never advance a digest's stored cursor on a partial delivery
failure, that would silently drop the failed channel's items from every
future run. Must never write the full item body into the digest itself,
only a truncated snippet (`_snippet()`, `_SNIPPET_LENGTH` = 200 chars),
even though `get_changes()` is called with `include_body=True` to obtain
it.

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [Profiles and Activities](concept-profiles-activities.md)
