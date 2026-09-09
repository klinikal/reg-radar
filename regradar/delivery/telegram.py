"""Digest delivery via the Telegram Bot API (issue #13). Bot token comes
from `TELEGRAM_BOT_TOKEN` (never in config/digests.yaml, matching
regradar.webhooks' secret-in-env convention). `chat_id` is numeric only for
this issue (no /start onboarding flow - see the issue's open questions).
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)

TELEGRAM_MESSAGE_LIMIT = 4096
API_BASE = "https://api.telegram.org"


def chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Splits `text` into chunks no longer than `limit`, breaking on
    paragraph ("\\n\\n") boundaries where possible. A single paragraph
    longer than `limit` is hard-split as a last resort."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        if len(para) <= limit:
            current = para
        else:
            for i in range(0, len(para), limit):
                chunks.append(para[i:i + limit])
            current = ""
    if current:
        chunks.append(current)
    return chunks


def send_telegram(chat_id, text: str, bot_token: str | None = None, client: httpx.Client | None = None) -> bool:
    """Sends `text` as one or more sendMessage calls, chunked at Telegram's
    4096-character limit. Returns False on any missing token or delivery
    failure - never raises."""
    bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("Telegram delivery to %s: TELEGRAM_BOT_TOKEN not set - skipping", chat_id)
        return False
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        logger.error("Telegram delivery: chat_id %r is not numeric - skipping", chat_id)
        return False

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=30)
    url = f"{API_BASE}/bot{bot_token}/sendMessage"
    try:
        for chunk in chunk_text(text):
            try:
                resp = client.post(url, json={"chat_id": chat_id, "text": chunk})
            except httpx.HTTPError as e:
                logger.error("Telegram delivery to %s failed: %s", chat_id, e)
                return False
            if resp.status_code != 200 or not resp.json().get("ok", False):
                logger.error("Telegram delivery to %s failed: HTTP %d %s", chat_id, resp.status_code, resp.text)
                return False
        return True
    finally:
        if owns_client:
            client.close()
