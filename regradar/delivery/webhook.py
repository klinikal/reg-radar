"""Digest delivery via the generic webhook transport (issue #13). A thin
wrapper around regradar.webhooks' own signing and retry/backoff code -
reused as-is, not reimplemented, per the issue's design notes.
"""
import json
import time

import httpx

from regradar.webhooks import _deliver_with_retry, sign_payload


def send_webhook(client: httpx.Client, url: str, secret: str, payload: dict,
                  max_retries: int = 5, backoff_seconds: int = 30, sleep_fn=time.sleep) -> bool:
    body = json.dumps(payload).encode("utf-8")
    signature = sign_payload(body, secret)
    return _deliver_with_retry(client, url, body, signature, max_retries, backoff_seconds, sleep_fn=sleep_fn)
