---
title: Delivery Channels
type: component
sources: [regradar/delivery/]
last-verified: 2026-09-10
---

## Purpose

Three delivery channels for `reg-radar digest`: SMTP email, the Telegram Bot API, and a
generic webhook. Each is a thin, independently-failing sender; none raises on a delivery
or config problem, they return `False` and log why.

## How it works

- `email.py`: `smtp_config_from_env()` reads `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
  `SMTP_PASSWORD_ENV` (the name of the env var holding the password, never the password
  itself), `SMTP_FROM`, `SMTP_STARTTLS`. `send_email()` sends a multipart
  plain-text+HTML message via `smtplib`, using `ssl.create_default_context()` for
  STARTTLS so the session gets certificate and hostname verification.
- `telegram.py`: `send_telegram()` posts to the Telegram Bot API's `sendMessage`
  endpoint using `TELEGRAM_BOT_TOKEN`. `chunk_text()` splits messages longer than the
  4096-character Telegram limit, breaking on paragraph boundaries where possible.
  `chat_id` must be numeric; no `/start` onboarding flow.
- `webhook.py`: `send_webhook()` is a thin wrapper reusing `regradar.webhooks`' own
  `sign_payload()` and `_deliver_with_retry()` (HMAC signing, retry with backoff) rather
  than reimplementing delivery.

## Inputs

Email: `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASSWORD_ENV`/`SMTP_FROM`/`SMTP_STARTTLS`
env vars, plus `to`/`subject`/`text_body`/`html_body`. Telegram: `TELEGRAM_BOT_TOKEN` env
var, plus `chat_id`/`text`. Webhook: a target URL, an HMAC secret, and a JSON payload.

## Outputs and side effects

An outbound network call (SMTP send, HTTPS POST) per invocation. No local state.

## How to run or invoke

Not run directly; called by `regradar/digest.py` per subscriber in `config/digests.yaml`
during `reg-radar digest --send`.

## How to test

[UNVERIFIED], no test file for `regradar/delivery/` was checked directly; see
`tests/` for `test_digest*.py` coverage.

## Boundaries

Never raises on a missing config value or delivery failure, always returns `False` and
logs instead, so one subscriber's misconfiguration never breaks a digest run for others.
Never logs or stores the SMTP password or bot token themselves.

## Related pages

[reg-radar digest](cli-digest.md), [Webhooks](component-webhooks.md), [Environment Variables](config-env-vars.md)
