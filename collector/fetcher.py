import ipaddress
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin

import feedparser
import httpx
from bs4 import BeautifulSoup

from collector.language import detect_language
from collector.schema import Item, make_item_id

logger = logging.getLogger(__name__)

# FlareSolverr drives a real browser per request and the target site's own
# rate limiting/bot detection is what actually matters here, not our own
# throughput - a burst of requests (even ones that individually succeed) can
# get the source IP temporarily banned by the target's WAF. These are
# deliberately conservative and env-tunable rather than hardcoded.
_FLARESOLVERR_MIN_INTERVAL_SECONDS = float(os.getenv("FLARESOLVERR_MIN_INTERVAL_SECONDS", "8"))
_FLARESOLVERR_MAX_RETRIES = int(os.getenv("FLARESOLVERR_MAX_RETRIES", "3"))
_FLARESOLVERR_RETRY_BACKOFF_SECONDS = float(os.getenv("FLARESOLVERR_RETRY_BACKOFF_SECONDS", "45"))
_last_flaresolverr_request_at = 0.0


def _throttle_flaresolverr() -> None:
    global _last_flaresolverr_request_at
    elapsed = time.monotonic() - _last_flaresolverr_request_at
    wait = _FLARESOLVERR_MIN_INTERVAL_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_flaresolverr_request_at = time.monotonic()


def _truncate_snippet(text: str) -> str:
    return text[:500]


def _resolve_language(feed_item: dict, source_preferred: str | None, title: str, snippet: str) -> str:
    if feed_item.get("language"):
        return feed_item["language"]
    if feed_item.get("tags"):
        for tag in feed_item.get("tags", []):
            if hasattr(tag, "term") and (tag.get("scheme") or "").endswith("lang"):
                return tag.term
    if source_preferred:
        return source_preferred
    detected = detect_language(f"{title} {snippet}")
    return detected


def _validate_url_not_private(url: str) -> None:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    try:
        addr = ipaddress.ip_address(host)
        if addr.is_private or addr.is_loopback or addr.is_link_local:
            raise ValueError(f"URL targets private address: {host}")
    except ValueError as e:
        if "private" in str(e):
            raise


def _make_ssrf_check():
    def on_request(request):
        host = request.url.host
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_private or addr.is_loopback or addr.is_link_local:
                raise httpx.InvalidURL(f"Request to private address blocked: {host}")
        except ValueError:
            pass  # hostname, not a bare IP — allow
    return on_request


def _looks_like_block_page(html: str) -> bool:
    # FlareSolverr can report status "ok" while the page it actually fetched
    # is a block/rate-limit response, not the real site - e.g. a CDN layer
    # in front of the target (CloudFront, etc.) can reject the request with
    # its own error page independently of the site's Cloudflare challenge,
    # and FlareSolverr has no way to know that's not what we wanted. A
    # successful HTTP round-trip isn't the same as successful content.
    if len(html) > 8000:
        return False
    markers = ("request blocked", "could not be satisfied", "403 error", "access denied", "rate limit")
    lowered = html.lower()
    return any(m in lowered for m in markers)


def _fetch_via_flaresolverr(url: str, flaresolverr_url: str, timeout: int) -> str | None:
    _validate_url_not_private(url)
    for attempt in range(_FLARESOLVERR_MAX_RETRIES):
        _throttle_flaresolverr()
        try:
            resp = httpx.post(
                f"{flaresolverr_url}/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000},
                timeout=timeout + 10,
            )
            resp.raise_for_status()
            data = resp.json()
            is_last_attempt = attempt == _FLARESOLVERR_MAX_RETRIES - 1
            if data.get("status") == "ok":
                html = data["solution"]["response"]
                if not _looks_like_block_page(html):
                    return html
                backoff = _FLARESOLVERR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "FlareSolverr got a block/rate-limit page (not the real site) for %s (attempt %d/%d)%s",
                    url, attempt + 1, _FLARESOLVERR_MAX_RETRIES,
                    "" if is_last_attempt else f" - backing off {backoff:.0f}s",
                )
                if not is_last_attempt:
                    time.sleep(backoff)
                continue
            message = data.get("message", "")
            if "banned" in message.lower() or "blocked" in message.lower():
                # The target site's own rate limiting, not a transient network
                # blip - back off much longer than a normal retry, since
                # hammering it again immediately only makes the ban last longer.
                backoff = _FLARESOLVERR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "FlareSolverr reports %s for %s (attempt %d/%d)%s",
                    message, url, attempt + 1, _FLARESOLVERR_MAX_RETRIES,
                    "" if is_last_attempt else f" - backing off {backoff:.0f}s",
                )
                if not is_last_attempt:
                    time.sleep(backoff)
            else:
                logger.warning("FlareSolverr returned non-ok status for %s: %s", url, message)
                return None
        except Exception as e:
            logger.warning("FlareSolverr request failed for %s: %s", url, e)
            return None
    return None


def _fetch_html(url: str, user_agent: str, timeout: int, retries: int, flaresolverr_url: str | None = None) -> str | None:
    # When a source is already known to need FlareSolverr, don't waste plain
    # HTTP attempts first - a Cloudflare-protected site blocks those too, and
    # each one still counts toward that site's own rate limiting/ban
    # threshold for no benefit (it was never going to succeed).
    if flaresolverr_url:
        return _fetch_via_flaresolverr(url, flaresolverr_url, timeout)

    headers = {"User-Agent": user_agent}
    for attempt in range(retries):
        try:
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks={"request": [_make_ssrf_check()]}) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            logger.warning("HTTP fetch attempt %d/%d failed for %s: %s", attempt + 1, retries, url, e)

    return None


def _fetch_bytes(url: str, user_agent: str, timeout: int, retries: int) -> bytes | None:
    headers = {"User-Agent": user_agent}
    for attempt in range(retries):
        try:
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks={"request": [_make_ssrf_check()]}) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            logger.warning("HTTP fetch attempt %d/%d failed for %s: %s", attempt + 1, retries, url, e)
    return None


def _parse_rss(content: bytes | str, source: dict, regulator_id: str) -> list[Item]:
    feed = feedparser.parse(content)
    items = []
    now = datetime.now(timezone.utc)
    source_preferred_lang = source.get("preferred_language")
    source_label = source["label"]
    tags = source.get("tags", [])
    source_type = source.get("type", "rss")

    for entry in feed.entries:
        title = entry.get("title", "").strip()
        url = entry.get("link", "").strip()
        if not title or not url:
            continue

        published_at = None
        date_inferred = False
        if entry.get("published_parsed"):
            try:
                published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            except Exception:
                pass
        if published_at is None:
            published_at = now
            date_inferred = True

        raw_content = entry.get("summary", "") or entry.get("description", "") or ""
        snippet = _truncate_snippet(BeautifulSoup(raw_content, "lxml").get_text())
        language = _resolve_language(entry, source_preferred_lang, title, snippet)

        item_id = make_item_id(regulator_id, url, published_at if not date_inferred else None)

        items.append(Item(
            id=item_id,
            regulator_id=regulator_id,
            source_label=source_label,
            title=title,
            url=url,
            published_at=published_at if not date_inferred else None,
            fetched_at=now,
            date_inferred=date_inferred,
            language=language,
            content_snippet=snippet,
            source_type=source_type,
            tags=tags,
            raw_content=raw_content,
        ))
    return items


def _extract_pdf_text(content: bytes) -> str | None:
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        return text.strip() or None
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return None


def _parse_scrape(html: str, source: dict, regulator_id: str, config: dict | None = None) -> list[Item]:
    scrape_cfg = source.get("scrape", {})
    item_selector = scrape_cfg.get("item_selector")
    title_selector = scrape_cfg.get("title_selector")
    link_selector = scrape_cfg.get("link_selector")
    date_selector = scrape_cfg.get("date_selector")
    date_attribute = scrape_cfg.get("date_attribute")
    content_selector = scrape_cfg.get("content_selector")
    extract_pdf_text = scrape_cfg.get("extract_pdf_text", False)
    config = config or {}

    if not item_selector or not title_selector or not link_selector:
        logger.error("Scrape source %s/%s missing required selectors", regulator_id, source["label"])
        return []

    soup = BeautifulSoup(html, "lxml")
    items = []
    now = datetime.now(timezone.utc)
    source_preferred_lang = source.get("preferred_language")
    tags = source.get("tags", [])
    source_label = source["label"]

    for element in soup.select(item_selector):
        title_el = element.select_one(title_selector)
        link_el = element.select_one(link_selector)
        if not title_el or not link_el:
            continue

        title = title_el.get_text(strip=True)
        href = link_el.get("href", "").strip()
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin(source["url"], href)

        published_at = None
        date_inferred = True
        if date_selector:
            date_el = element.select_one(date_selector)
            if date_el:
                date_str = date_el.get(date_attribute) if date_attribute else date_el.get_text(strip=True)
                if date_str:
                    try:
                        from dateutil.parser import parse as parse_date
                        # fuzzy: tolerates surrounding text like "Published: 02/07/2026".
                        # dayfirst: EEA/UK sources use DD/MM/YYYY: without this, ambiguous
                        # dates like 02/07/2026 silently misparse as 2 July -> 7 Feb.
                        # Safe for existing sources - they all use unambiguous ISO
                        # datetime attributes, which dayfirst/fuzzy don't affect.
                        parsed = parse_date(date_str, fuzzy=True, dayfirst=True)
                        published_at = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                        date_inferred = False
                    except Exception:
                        pass

        raw_content = ""
        if extract_pdf_text:
            # The item's own link is the document itself (e.g. CNMV's warning
            # notices link straight to a PDF, no separate HTML landing page).
            pdf_bytes = _fetch_bytes(
                href,
                config.get("user_agent", "casp-radar/1.0"),
                int(config.get("timeout", 30)),
                int(config.get("retries", 3)),
            )
            if pdf_bytes:
                raw_content = _extract_pdf_text(pdf_bytes) or ""
        if not raw_content and content_selector:
            content_el = element.select_one(content_selector)
            if content_el:
                raw_content = content_el.get_text(strip=True)

        snippet = _truncate_snippet(raw_content)
        language = _resolve_language({}, source_preferred_lang, title, snippet)
        item_id = make_item_id(regulator_id, href, published_at)

        items.append(Item(
            id=item_id,
            regulator_id=regulator_id,
            source_label=source_label,
            title=title,
            url=href,
            published_at=published_at,
            fetched_at=now,
            date_inferred=date_inferred,
            language=language,
            content_snippet=snippet,
            source_type="scrape",
            tags=tags,
            raw_content=raw_content,
        ))
    return items


def fetch_source(
    source: dict,
    regulator_id: str,
    flaresolverr_url: str | None,
    config: dict,
) -> list[Item]:
    user_agent = config.get("user_agent", "casp-radar/1.0")
    timeout = int(config.get("timeout", 30))
    retries = int(config.get("retries", 3))
    source_type = source.get("type", "rss")
    url = source["url"]

    try:
        if source_type in ("rss", "atom"):
            content = _fetch_bytes(url, user_agent, timeout, retries)
            if content is None:
                logger.error("Failed to fetch %s for %s/%s", url, regulator_id, source["label"])
                return []
            return _parse_rss(content, source, regulator_id)
        elif source_type == "scrape":
            html = _fetch_html(url, user_agent, timeout, retries, flaresolverr_url)
            if html is None:
                logger.error("Failed to fetch %s for %s/%s", url, regulator_id, source["label"])
                return []
            return _parse_scrape(html, source, regulator_id, config)
        else:
            logger.warning("Unknown source type '%s' for %s/%s", source_type, regulator_id, source["label"])
            return []
    except Exception as e:
        logger.error("Unexpected error fetching %s/%s: %s", regulator_id, source["label"], e)
        return []
