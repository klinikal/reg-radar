---
title: Webhooks
type: component
sources: [regradar/webhooks.py]
last-verified: 2026-09-10
---

## Purpose

Delivers change-stream batches to subscribers configured in
`config/webhooks.yaml`, pulling from the same `get_changes()` primitive the
mcpserver and exporter use. Each subscriber's cursor and delivery outcomes
live in SQLite rather than a file.

## How it works

`load_subscribers(path)` reads `config/webhooks.yaml`: a list of entries,
each `{profile, url, secret_env, batch_size, max_retries,
backoff_seconds}`. The secret itself is never read from this file, only the
name of the env var holding it. A missing file returns `[]` (webhooks are
opt-in, not an error). An empty or `null` file also returns `[]`. A
non-list file, or an entry missing `profile`, `url`, or `secret_env`, raises
`WebhookConfigError`. Defaults: `batch_size=25`, `max_retries=5`,
`backoff_seconds=30`.

`subscriber_id(sub)` returns `"<profile>|<url>"`, used as the unique key
since the config format has no explicit id field.

`sign_payload(body, secret)` returns `sha256=<hex hmac>` over the JSON body.

`_deliver_with_retry(...)` POSTs the body with `Content-Type:
application/json` and `X-Reg-Radar-Signature` headers, retrying up to
`max_retries` times with a delay that doubles each attempt starting from
`backoff_seconds`. Returns `True` on any 2xx response, `False` once retries
are exhausted. `sleep_fn` is injectable for tests.

`dispatch_one(engine, sub, taxonomy, activities, profiles_dir, client)`
delivers one subscriber's next batch:

1. Reads the secret from `os.getenv(sub["secret_env"])`; skips (logs an
   error) if unset.
2. Loads the subscriber's profile via `get_expanded_profile`; skips on
   failure.
3. Reads the subscriber's stored cursor (`get_webhook_cursor`), calls
   `get_changes(engine, since=cursor, profile=expanded,
   limit=sub["batch_size"], include_body=True, taxonomy=taxonomy)`.
4. Checks `get_blocked_sources(engine)`. If there are no new items and no
   blocked sources, returns without sending anything. A blocked source
   alone is enough to trigger a send, so the alarm is still reachable when
   there is nothing new to deliver.
5. Builds `{cursor, next_cursor, items, blocked_sources}`, signs it, and
   delivers with retry.
6. On success: advances the stored cursor only if `items` was non-empty (a
   blocked-source-only delivery must not skip past changes that arrive
   later), and records a `"success"` delivery.
7. On failure: records a `"failed"` delivery with a detail message; the
   cursor is not advanced.

`dispatch_all(engine, config_path, taxonomy, activities, profiles_dir,
client=None)` loads all subscribers and runs `dispatch_one` for each,
catching and logging any unexpected exception per subscriber so one
subscriber's failure never blocks another. A missing config or empty
subscriber list is a no-op. Opens and closes its own `httpx.Client(timeout=
30)` unless one is injected (for tests).

## Inputs

- `config/webhooks.yaml`: list of subscriber entries.
- Env var named by each subscriber's `secret_env`, holding the HMAC secret.
- The SQLite `engine`, a `taxonomy`, an `activities` dict, and
  `profiles_dir` (same shapes as consumed by `regradar.profiles`).

## Outputs and side effects

- HTTP POST to each subscriber's `url` with a signed JSON body.
- Writes to `collector.storage`'s `webhook_subscriptions` (cursor) and
  `webhook_deliveries` (outcome) tables via `set_webhook_cursor` and
  `record_webhook_delivery`.
- Logs errors/warnings for missing secrets, unknown profiles, failed
  `get_changes` calls, non-2xx responses, and exhausted retries.

## How to run or invoke

Dispatch is triggered from `collector/main.py` after `run_collection()`
completes, calling `dispatch_all(...)`. Not invoked directly by an operator.

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_webhooks.py`-style file, likely using
`httpx.Client(transport=MockTransport(...))` given `dispatch_all`'s
injectable `client` parameter.

## Boundaries

- Never raises out of `dispatch_one` for an expected failure mode (missing
  secret, unknown profile, delivery failure); `dispatch_all` isolates
  subscribers from each other regardless, but `dispatch_one` avoids logging
  the same failure twice.
- Never advances a subscriber's cursor on a failed delivery, or on a
  blocked-source-only delivery with no items.
- Never treats a missing `config/webhooks.yaml` as an error; webhooks are
  opt-in.

## Related pages

[Export and Sync Contract](concept-export-sync-contract.md)
