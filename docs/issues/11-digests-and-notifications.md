# Digests and notifications: per-profile daily email, Telegram and webhook

**Size:** S  **Depends on:** #8 export, changes cursor, outbound feeds and webhooks; #6 rules engine (for the `/alerts` page migration)  **Design doc:** docs/design/regradar-architecture.md §6, §8 (build order, row 11)

## Why

RegRadar has a change stream (`get_changes`, §6) and a per-item corpus, but nothing turns "what changed since I last looked" into something a person reads without querying an agent or watching a feed reader. `config/alerts.yaml`'s own comment says push notifications are "NOT implemented yet, tagging only, for now," pointing at a parked plan (`docs/superpowers/plans/2026-07-03-feature-01b-webhook-notifications.md`). This issue builds that missing piece: a digest generator per profile, delivered by email, Telegram or webhook on a schedule, plus the migration of the web UI's keyword alert page onto `rules.yaml`.

## Scope

- A digest generator that, for a profile, renders every item with `updated_at` after that profile's last-sent cursor as both Markdown and HTML, grouped by jurisdiction then regulator, each item showing title, `document_type`, `instrument` labels, a one-line snippet, and a link.
- Three delivery channels: email via SMTP, a Telegram bot, and a generic webhook that reuses #8's signed-POST mechanism (`config/webhooks.yaml`'s shape, HMAC signature, retry/backoff) rather than a second implementation.
- A schedule per digest in config (cron expression), independent of the collector's own `SCHEDULE`.
- `regradar digest --profile <name> --preview` (renders and prints/opens, sends nothing) and `--send` (renders and delivers).
- Digest state, keyed per digest, recording the last successfully sent cursor, so nothing is sent twice and a missed scheduled run catches up on the next one.
- Migration: the web UI's keyword alert page (`http://localhost:8080/alerts`, backed by `config/alerts.yaml`) is repointed to read and write `config/rules.yaml`, per the migration design doc §4.7 describes.

## Non-goals

- A new outbound transport beyond email/Telegram/webhook. Slack, Discord etc. are follow-ons if requested.
- Any change to the change-stream primitive, cursor format, or webhook signing scheme: reuse #8's exactly.
- Rewriting `alerts.yaml`'s content itself (values becoming `rules.yaml` entries, `content_tags` importing as `by: import`): that classification-side migration belongs to #6. This issue only repoints the existing `/alerts` page at the new file and schema.
- Digest personalisation beyond a profile (per-item scoring, "top 5 only", etc.).

## Design notes

- New `regradar/digest.py` (next to wherever #8 put `regradar/exporter.py`, reusing its Markdown-rendering approach from design doc §3): `render_digest(profile, since_cursor) -> (markdown_str, html_str, next_cursor)`, calling `get_changes` with `include_body=False`.
- New `regradar/delivery/` package: `email.py` (SMTP via `smtplib`), `telegram.py` (Bot API `sendMessage`, chunked if over Telegram's message length limit), `webhook.py` (thin wrapper around #8's `regradar/webhooks.py` sender, reusing its signing and retry code rather than reimplementing it).
- New `config/digests.yaml`:

```yaml
- name: uk-equities-daily
  profile: uk-equities-dealer      # config/profiles/<name>.yaml, per design doc §5.2
  schedule: "0 7 * * *"            # cron, evaluated same as collector's SCHEDULE
  channels:
    - type: email
      to: ops@example.org
    - type: telegram
      chat_id: "-1001234567890"
    - type: webhook
      url: https://example.org/hooks/regradar-digest
      secret_env: WEBHOOK_SECRET_DIGEST_UK
```

- SMTP config via env vars matching the `.env.example` convention (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD_ENV`, `SMTP_FROM`). Telegram bot token via `TELEGRAM_BOT_TOKEN`, never in `digests.yaml`, matching #8's `secret_env` pattern; no secrets in config files.
- Digest state: a table or file (match whatever #8 chose for webhook subscriber cursors) keyed by digest `name`, storing `last_sent_cursor` and `last_sent_at`. `--send` advances the cursor only once delivery to *every* configured channel succeeds. A partial failure (email sent, Telegram failed) must not advance it; log the failure and retry the whole digest next run rather than reconciling per-channel cursors.
- Scheduling: reuse whatever scheduling mechanism the collector or #8 already runs (a scheduler thread reading cron fields, or a documented cron/systemd-timer entry calling `regradar digest --send`). Don't introduce a second mechanism.
- CLI: add `digest` as a subcommand alongside #8's `export`.
- Web UI migration: `webui/app.py`'s `/alerts` route and template currently read/write `config/alerts.yaml`'s flat `keyword`/`tag` pairs. Repoint to `config/rules.yaml`, whose entries are `axis`/`value`/`match` (a list of `pattern`/`weight`/optional `flags`), `fields`, `title_multiplier` per design doc §4.3, a genuine schema change, not a path swap. The page needs a form for axis, value, one or more weighted patterns, and target fields, validating `axis`/`value` against `config/taxonomy.yaml` on save (same validation §4.5's write interface uses). This is why #6 is a dependency: `rules.yaml`'s schema and loader come from there.

## Acceptance criteria

- [ ] `--preview` renders a digest of everything with `updated_at` after the profile's stored cursor, grouped by jurisdiction then regulator, with title, document_type, instrument labels, snippet and link, in Markdown and HTML, and sends nothing.
- [ ] `--send` delivers to every channel configured for that digest and only then advances the stored cursor.
- [ ] Running `--send` twice with no new items in between sends an empty-state digest, or skips per config (pick one, document it), but never re-sends already-delivered items.
- [ ] A digest scheduled but missed catches up fully on the next run, with no gap, verified by simulating a missed run and checking the next run's cursor start matches the last successfully sent cursor exactly.
- [ ] Email delivery works against a real or test SMTP server with correct `To`/`From`/subject and both plain-text and HTML parts.
- [ ] Telegram delivery works against the Bot API (or a mock) and chunks correctly when the digest exceeds the message length limit.
- [ ] Webhook delivery reuses #8's signing/retry code (no duplicated HMAC or backoff logic); a test asserts the signature validates the same way #8's own webhook test does.
- [ ] A partial delivery failure (one channel succeeds, another fails) does not advance the cursor and is retried in full next run.
- [ ] `webui/app.py`'s `/alerts` page reads and writes `config/rules.yaml` in its §4.3 schema, validates `axis`/`value` against `config/taxonomy.yaml` on save, and no longer touches `config/alerts.yaml`.
- [ ] README's Configuration and Keyword alerts sections are updated to reflect `rules.yaml`, plus a new "Digests" section covering `config/digests.yaml`, the CLI, and the three channels.

## Open questions

- Should a digest with zero new items still send an empty "nothing new" message, or skip silently? Recommend configurable per digest (`send_empty: false` default): a daily "all clear" reassures some operators and is noise for others.
- Does the web UI need a dedicated digest-management page, or is config-file-plus-CLI enough for this issue, with a UI as a follow-on? Recommend config-file-plus-CLI, matching webhook config's own precedent in #8.
- Telegram chat resolution: a numeric `chat_id` supplied directly (simplest, as above), or a `/start`-based onboarding flow to discover it? Recommend numeric `chat_id` only for this issue; a friendlier flow is a follow-on if requested.
