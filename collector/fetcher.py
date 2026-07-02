import logging
import time
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup

from collector.language import detect_language
from collector.schema import Item, make_item_id

logger = logging.getLogger(__name__)


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


def _fetch_via_flaresolverr(url: str, flaresolverr_url: str, timeout: int) -> str | None:
    try:
        resp = httpx.post(
            f"{flaresolverr_url}/v1",
            json={"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000},
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "ok":
            return data["solution"]["response"]
    except Exception as e:
        logger.warning("FlareSolverr request failed for %s: %s", url, e)
    return None


def _fetch_html(url: str, user_agent: str, timeout: int, retries: int, flaresolverr_url: str | None = None) -> str | None:
    headers = {"User-Agent": user_agent}
    for attempt in range(retries):
        try:
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            logger.warning("HTTP fetch attempt %d/%d failed for %s: %s", attempt + 1, retries, url, e)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    if flaresolverr_url:
        logger.info("Retrying %s via FlareSolverr", url)
        return _fetch_via_flaresolverr(url, flaresolverr_url, timeout)

    return None


def _fetch_bytes(url: str, user_agent: str, timeout: int, retries: int, flaresolverr_url: str | None = None) -> bytes | None:
    headers = {"User-Agent": user_agent}
    for attempt in range(retries):
        try:
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.content
        except Exception as e:
            logger.warning("HTTP fetch attempt %d/%d failed for %s: %s", attempt + 1, retries, url, e)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)

    if flaresolverr_url:
        logger.info("Retrying %s via FlareSolverr", url)
        result = _fetch_via_flaresolverr(url, flaresolverr_url, timeout)
        return result.encode() if result else None

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


def _parse_scrape(html: str, source: dict, regulator_id: str) -> list[Item]:
    scrape_cfg = source.get("scrape", {})
    item_selector = scrape_cfg.get("item_selector")
    title_selector = scrape_cfg.get("title_selector")
    link_selector = scrape_cfg.get("link_selector")
    date_selector = scrape_cfg.get("date_selector")
    date_attribute = scrape_cfg.get("date_attribute")
    content_selector = scrape_cfg.get("content_selector")

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
        if not href.startswith("http"):
            base = source["url"].rsplit("/", 1)[0]
            href = f"{base}/{href.lstrip('/')}"

        published_at = None
        date_inferred = True
        if date_selector:
            date_el = element.select_one(date_selector)
            if date_el:
                date_str = date_el.get(date_attribute) if date_attribute else date_el.get_text(strip=True)
                if date_str:
                    try:
                        from dateutil.parser import parse as parse_date
                        published_at = parse_date(date_str).replace(tzinfo=timezone.utc)
                        date_inferred = False
                    except Exception:
                        pass

        raw_content = ""
        if content_selector:
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
            content = _fetch_bytes(url, user_agent, timeout, retries, flaresolverr_url)
            if content is None:
                logger.error("Failed to fetch %s for %s/%s", url, regulator_id, source["label"])
                return []
            return _parse_rss(content, source, regulator_id)
        elif source_type == "scrape":
            html = _fetch_html(url, user_agent, timeout, retries, flaresolverr_url)
            if html is None:
                logger.error("Failed to fetch %s for %s/%s", url, regulator_id, source["label"])
                return []
            return _parse_scrape(html, source, regulator_id)
        else:
            logger.warning("Unknown source type '%s' for %s/%s", source_type, regulator_id, source["label"])
            return []
    except Exception as e:
        logger.error("Unexpected error fetching %s/%s: %s", regulator_id, source["label"], e)
        return []
