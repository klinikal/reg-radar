---
title: Environment Variables
type: config
sources: [.env.example]
last-verified: 2026-09-10
---

## Purpose

The full list of environment variables reg-radar reads, as a template for the gitignored `.env` file, each documented with the comment beside it in `.env.example`.

## How it works

| Variable | Description |
|---|---|
| `DB_PATH` | Path to the SQLite database file. |
| `SCHEDULE` | Default cron schedule, used as the fallback for any module without its own `schedule:` in `config/modules.yaml`. |
| `LOG_LEVEL` | Logging verbosity. |
| `USER_AGENT` | HTTP User-Agent header sent on collector requests. |
| `REQUEST_TIMEOUT_SECONDS` | HTTP request timeout. |
| `REQUEST_RETRIES` | HTTP request retry count. |
| `REGULATORS_PATH` | Path to the `config/regulators` directory. |
| `TAXONOMY_PATH` | Path to `config/taxonomy.yaml`. |
| `JURISDICTIONS_PATH` | Path to `config/jurisdictions.yaml`. |
| `ACTIVITIES_PATH` | Path to `config/activities.yaml`. |
| `PROFILES_DIR` | Path to the `config/profiles` directory. |
| `FLARESOLVERR_URL` | Internal service address for FlareSolverr, included in `docker-compose.yml` by default; leave blank to disable it (any `cloudflare_protected` regulator is then skipped, not errored). |
| `RULES_PATH` | Path to `config/rules.yaml`, the classification rules engine's config file. |
| `ANTHROPIC_API_KEY` | API key for the batch LLM classifier runner (`reg-radar classify`) when using the Anthropic provider; only needed for the provider actually used; never commit real keys. |
| `OPENAI_API_KEY` | API key for the batch LLM classifier runner when using the OpenAI provider; same caveats as above. |
| `LLM_PRICES_PATH` | Path to `config/llm_prices.yaml`. |
| `WEBHOOKS_PATH` | Path to an optional webhook subscribers config (see README's "Export and sync" section); a missing file disables webhook delivery entirely, no config needed. |
| `DIGESTS_PATH` | Path to an optional digests config (see README's "Digests" section); a missing file disables digests entirely, no config needed. |
| `SMTP_HOST` | SMTP host for the "email" digest channel; only needed if a digest uses one. |
| `SMTP_PORT` | SMTP port for the "email" digest channel. |
| `SMTP_USER` | SMTP username for the "email" digest channel. |
| `SMTP_PASSWORD_ENV` | Names the env var holding the SMTP password (the password itself is never in config), matching the `secret_env` convention. |
| `SMTP_FROM` | SMTP "from" address for the "email" digest channel. |
| `SMTP_STARTTLS` | Whether to use STARTTLS for the "email" digest channel. |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for the "telegram" digest channel; only needed if a digest uses one; never put it in `digests.yaml`. |
| `FEED_MAX_ITEMS` | Number of entries returned by `GET /feeds/<profile>.atom`. |
| `MCP_EXTRA_ALLOWED_HOSTS` | Only needed if remapping the MCP server's published port (see README); comma-separated `host:port` values added to the Host-header allowlist, e.g. `127.0.0.1:8001,localhost:8001`. |

## Inputs

Copied from `.env.example` to `.env` (`cp .env.example .env`) and hand-edited; `.env` is gitignored and must never be committed.

## Outputs and side effects

Read at process startup by the collector, MCP server, and webui services to configure paths, credentials, schedules, and optional features.

## How to run or invoke

```
cp .env.example .env
```
Then edit `.env` before `docker compose up -d`.

## How to test

No dedicated test command in the file's own comments.

## Boundaries

Real secrets (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`, SMTP password) must never be committed; `.env` is gitignored for this reason, and `SMTP_PASSWORD_ENV` deliberately names an env var rather than storing the password value in YAML config.

## Related pages

[CLI Entrypoint](cli-entrypoint.md)
