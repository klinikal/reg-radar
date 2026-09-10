"""Webhook delivery: config/webhooks.yaml subscribers pulling from the
same change stream get_changes() exposes (../wiki/content/concept-export-sync-contract.md).
Each subscriber's own cursor and delivery outcomes live in SQLite
(collector.storage's webhook_subscriptions/webhook_deliveries tables)
rather than a file, so a later digest feature (issue #13) can reuse the
same mechanism.

Dispatch is triggered from collector/main.py after run_collection()
completes - see dispatch_all(). A missing config/webhooks.yaml is a no-op,
not an error: webhooks are opt-in.
"""
import hashlib
import hmac
import json
import logging
import os
import time

import httpx
import yaml

from collector.storage import get_webhook_cursor, record_webhook_delivery, set_webhook_cursor
from mcpserver.queries import get_blocked_sources, get_changes
from regradar.profiles import get_expanded_profile

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 5
DEFAULT_BACKOFF_SECONDS = 30
DEFAULT_BATCH_SIZE = 25


class WebhookConfigError(ValueError):
    """Raised when config/webhooks.yaml is structurally invalid."""


def load_subscribers(path: str) -> list[dict]:
    """Returns [] if `path` doesn't exist - webhooks are opt-in, a missing
    file is not an error (see module docstring). Each entry:
    {profile, url, secret_env, batch_size, max_retries, backoff_seconds, id} -
    the secret itself is never read from this file, only the name of the
    env var that holds it (see ../wiki/content/concept-export-sync-contract.md
    for the config shape).

    `id` is optional: a stable identifier for the subscriber that does not
    change if `url` is later rotated. Subscribers without one fall back to
    a `profile|url` derived id (see subscriber_id) - unchanged pre-existing
    behaviour, but that fallback id changes if `url` changes, resetting the
    subscriber's delivery cursor (issue #25). New subscribers should set an
    explicit `id`; so should an existing subscriber before its `url` is
    rotated, using its current `profile|url` value to preserve the cursor
    already stored under that key.
    """
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise WebhookConfigError(f"{path}: invalid YAML - {e}")
    if data is None:
        return []
    if not isinstance(data, list):
        raise WebhookConfigError(f"{path}: must be a list of subscriber entries")
    subscribers = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict) or not raw.get("profile") or not raw.get("url") or not raw.get("secret_env"):
            raise WebhookConfigError(f"{path}: entry {i} must have profile, url and secret_env")
        explicit_id = raw.get("id")
        if explicit_id is not None and (not isinstance(explicit_id, str) or not explicit_id):
            raise WebhookConfigError(f"{path}: entry {i} id must be a non-empty string")
        subscribers.append({
            "id": explicit_id,
            "profile": raw["profile"],
            "url": raw["url"],
            "secret_env": raw["secret_env"],
            "batch_size": int(raw.get("batch_size", DEFAULT_BATCH_SIZE)),
            "max_retries": int(raw.get("max_retries", DEFAULT_MAX_RETRIES)),
            "backoff_seconds": int(raw.get("backoff_seconds", DEFAULT_BACKOFF_SECONDS)),
        })

    # Validate uniqueness of the *effective* id (explicit id, or the
    # profile|url fallback) - an explicit id could otherwise collide with
    # another subscriber's derived id, merging their delivery cursors.
    seen_ids = set()
    for sub in subscribers:
        eid = subscriber_id(sub)
        if eid in seen_ids:
            raise WebhookConfigError(f"{path}: duplicate subscriber id {eid!r}")
        seen_ids.add(eid)
    return subscribers


def subscriber_id(sub: dict) -> str:
    # An explicit `id` is stable across a `url` rotation - prefer it.
    # Falling back to profile+url matches this function's pre-#25 behaviour
    # for subscribers that haven't set one, but that derived id changes
    # (and orphans the stored cursor) if `url` changes later.
    return sub.get("id") or f"{sub['profile']}|{sub['url']}"


def sign_payload(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _deliver_with_retry(client: httpx.Client, url: str, body: bytes, signature: str,
                         max_retries: int, backoff_seconds: int, sleep_fn=time.sleep) -> bool:
    """POSTs `body` with retry and doubling backoff. Returns True on a 2xx
    response, False once max_retries attempts are exhausted. `sleep_fn` is
    injectable for tests that need to assert the backoff actually doubles
    without a real sleep."""
    delay = backoff_seconds
    for attempt in range(1, max_retries + 1):
        try:
            resp = client.post(
                url, content=body,
                headers={"Content-Type": "application/json", "X-Reg-Radar-Signature": signature},
            )
            if 200 <= resp.status_code < 300:
                return True
            logger.warning("Webhook POST to %s returned %d (attempt %d/%d)", url, resp.status_code, attempt, max_retries)
        except httpx.HTTPError as e:
            logger.warning("Webhook POST to %s failed (attempt %d/%d): %s", url, attempt, max_retries, e)
        if attempt < max_retries:
            sleep_fn(delay)
            delay *= 2
    return False


def dispatch_one(engine, sub: dict, taxonomy, activities: dict, profiles_dir: str, client: httpx.Client) -> None:
    """Delivers one subscriber's next batch, if any: loads its stored
    cursor, pulls get_changes() since that cursor for its profile, signs
    and POSTs the batch, and advances the cursor only on a 2xx response.
    Never raises for an expected failure mode (missing secret, unknown
    profile, delivery failure) - callers (dispatch_all) isolate one
    subscriber's exception from another regardless, this just avoids
    logging the same failure twice.
    """
    sid = subscriber_id(sub)
    secret = os.getenv(sub["secret_env"])
    if not secret:
        logger.error("Webhook subscriber %s: env var %s is not set - skipping", sid, sub["secret_env"])
        return

    try:
        expanded = get_expanded_profile(sub["profile"], taxonomy, activities, profiles_dir=profiles_dir)
    except Exception as e:
        logger.error("Webhook subscriber %s: could not load profile %r: %s", sid, sub["profile"], e)
        return

    cursor = get_webhook_cursor(engine, sid)
    try:
        items, next_cursor = get_changes(
            engine, since=cursor, profile=expanded, limit=sub["batch_size"], include_body=True,
            taxonomy=taxonomy,
        )
    except Exception as e:
        logger.error("Webhook subscriber %s: get_changes failed: %s", sid, e)
        return
    # A blocked source is exactly the case where there are no new items to
    # deliver, so "nothing new -> send nothing" would make the alarm
    # unreachable through this path (issue #39). Deliver on either signal.
    blocked_sources = get_blocked_sources(engine)
    if not items and not blocked_sources:
        return

    payload = {"cursor": cursor, "next_cursor": next_cursor, "items": items,
               "blocked_sources": blocked_sources}
    body = json.dumps(payload).encode("utf-8")
    signature = sign_payload(body, secret)

    ok = _deliver_with_retry(client, sub["url"], body, signature, sub["max_retries"], sub["backoff_seconds"])
    if ok:
        # Only an actual item batch moves the cursor - a blocked-source-only
        # delivery must not skip past changes that arrive later.
        if items:
            set_webhook_cursor(engine, sid, next_cursor)
        record_webhook_delivery(engine, sid, sub["url"], "success")
        logger.info(
            "Webhook subscriber %s: delivered %d item(s), %d blocked source(s)",
            sid, len(items), len(blocked_sources),
        )
    else:
        record_webhook_delivery(engine, sid, sub["url"], "failed", detail=f"exhausted {sub['max_retries']} retries")
        logger.error(
            "Webhook subscriber %s: delivery failed after %d retries - cursor not advanced",
            sid, sub["max_retries"],
        )


def dispatch_all(engine, config_path: str, taxonomy, activities: dict, profiles_dir: str,
                  client: httpx.Client | None = None) -> None:
    """Runs every configured subscriber once, isolating failures so one
    subscriber's exception never blocks another. A no-op if `config_path`
    doesn't exist or has no subscribers. `client` is injectable for tests
    (e.g. an httpx.Client(transport=MockTransport(...))); a real one is
    opened and closed here otherwise.
    """
    try:
        subscribers = load_subscribers(config_path)
    except WebhookConfigError as e:
        logger.error("Webhook config error: %s - skipping webhook dispatch this run", e)
        return
    if not subscribers:
        return

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=30)
    try:
        for sub in subscribers:
            try:
                dispatch_one(engine, sub, taxonomy, activities, profiles_dir, client)
            except Exception:
                logger.exception(
                    "Webhook subscriber %s raised unexpectedly - continuing with next subscriber",
                    subscriber_id(sub),
                )
    finally:
        if owns_client:
            client.close()
