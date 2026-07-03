import ipaddress
import logging
import os
import random
import socket
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from collector.chunking import content_hash
from collector.language import detect_language
from collector.schema import Item, make_item_id, make_page_watch_item_id

logger = logging.getLogger(__name__)

# FlareSolverr drives a real browser per request and the target site's own
# rate limiting/bot detection is what actually matters here, not our own
# throughput - a burst of requests (even ones that individually succeed) can
# get the source IP temporarily banned by the target's WAF. These are
# deliberately conservative and env-tunable rather than hardcoded.
_FLARESOLVERR_MIN_INTERVAL_SECONDS = float(os.getenv("FLARESOLVERR_MIN_INTERVAL_SECONDS", "8"))
_FLARESOLVERR_MAX_RETRIES = int(os.getenv("FLARESOLVERR_MAX_RETRIES", "3"))
_FLARESOLVERR_RETRY_BACKOFF_SECONDS = float(os.getenv("FLARESOLVERR_RETRY_BACKOFF_SECONDS", "45"))
# Keyed by domain, not one global timestamp - a slow/heavily-throttled
# domain (e.g. mid-backoff) shouldn't also throttle every other domain's
# requests, and a domain that's been failing shouldn't get retried on every
# single run forever without limit (see consecutive-failure note below).
_last_flaresolverr_request_at: dict[str, float] = {}

# extract_pdf_text fetches a document per scraped item, synchronously,
# inside the collection loop - an unbounded download or page count on a
# huge/hostile/malformed PDF could stall a whole run. Bounded, not
# unlimited, by design.
_MAX_PDF_DOWNLOAD_BYTES = int(os.getenv("MAX_PDF_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))  # 25 MB
_MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "50"))


def _throttle_flaresolverr(domain: str) -> None:
    last = _last_flaresolverr_request_at.get(domain, 0.0)
    elapsed = time.monotonic() - last
    wait = _FLARESOLVERR_MIN_INTERVAL_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_flaresolverr_request_at[domain] = time.monotonic()


def _backoff_with_jitter(attempt: int) -> float:
    # Deterministic exponential backoff means every source hitting the same
    # domain at the same time retries in lockstep, re-colliding on the same
    # schedule. Jitter spreads retries out instead of bunching them.
    base = _FLARESOLVERR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
    return base * (0.5 + random.random())


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


def _ip_is_private_or_reserved(addr_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(addr_str)
    except ValueError:
        return False
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _host_resolves_to_private(host: str) -> bool:
    """True if `host` (an IP literal or a hostname) is, or resolves to, a
    private/loopback/link-local/reserved/multicast address.

    A bare-IP-literal check alone (the old behaviour) misses DNS rebinding:
    a scraped page can link to a plain hostname like "internal.example.com"
    that resolves to 169.254.169.254 (cloud metadata) or a private IP,
    which would sail straight through a check that only looks at literals.
    """
    if _ip_is_private_or_reserved(host):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return any(_ip_is_private_or_reserved(info[4][0]) for info in infos)


def _validate_url_not_private(url: str) -> None:
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    if host and _host_resolves_to_private(host):
        raise ValueError(f"URL targets or resolves to a private/reserved address: {host}")


def _make_ssrf_check():
    def on_request(request):
        host = request.url.host
        if host and _host_resolves_to_private(host):
            raise httpx.InvalidURL(f"Request to private/reserved address blocked: {host}")
    return on_request


def _looks_like_block_page(html: str) -> bool:
    # FlareSolverr can report status "ok" while the page it actually fetched
    # is a block/rate-limit response, not the real site - e.g. a CDN layer
    # in front of the target (CloudFront, etc.) can reject the request with
    # its own error page independently of the site's Cloudflare challenge,
    # and FlareSolverr has no way to know that's not what we wanted. A
    # successful HTTP round-trip isn't the same as successful content. This
    # is a secondary/fallback check - _fetch_via_flaresolverr checks the
    # upstream HTTP status code first, which is a far more reliable signal
    # when FlareSolverr's response exposes it; this content-sniffing only
    # matters when that status isn't available or the block page happens to
    # carry a 200.
    if len(html) > 8000:
        return False
    markers = ("request blocked", "could not be satisfied", "403 error", "access denied", "rate limit")
    lowered = html.lower()
    return any(m in lowered for m in markers)


def _has_expected_content(html: str, expected_selector: str | None) -> bool:
    """True if expected_selector is unset, or it actually matches something.

    A positive check, not a denylist: rather than guessing at every possible
    way a block/error page might be phrased, confirm the page contains what
    we actually asked for. Catches an unexpected-but-200 response that isn't
    an obvious block page either (a redirect to an unrelated page, a
    maintenance notice, a captcha page using none of the known markers).
    """
    if not expected_selector:
        return True
    try:
        soup = BeautifulSoup(html, "lxml")
        return soup.select_one(expected_selector) is not None
    except Exception:
        return True  # a selector error here shouldn't itself block valid content


def _fetch_via_flaresolverr(url: str, flaresolverr_url: str, timeout: int, expected_selector: str | None = None) -> str | None:
    _validate_url_not_private(url)
    domain = urlparse(url).hostname or url
    for attempt in range(_FLARESOLVERR_MAX_RETRIES):
        _throttle_flaresolverr(domain)
        is_last_attempt = attempt == _FLARESOLVERR_MAX_RETRIES - 1
        try:
            resp = httpx.post(
                f"{flaresolverr_url}/v1",
                json={"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000},
                timeout=timeout + 10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "ok":
                solution = data.get("solution", {})
                html = solution.get("response", "")
                # solution.status is the actual upstream HTTP status code
                # FlareSolverr's browser received - distinct from the
                # top-level "status": "ok", which only means "FlareSolverr
                # itself ran without error". A block page (e.g. a CDN 403)
                # shows up here directly, which is a far more reliable
                # signal than guessing at block-page wording. Checked
                # defensively (.get, not indexing) in case this field isn't
                # present in a given FlareSolverr version - falls through to
                # the content-based checks below if so.
                upstream_status = solution.get("status")
                if upstream_status is not None and not (200 <= upstream_status < 300):
                    failure_reason = f"upstream HTTP {upstream_status}"
                elif _looks_like_block_page(html):
                    failure_reason = "block/rate-limit page (content heuristic)"
                elif not _has_expected_content(html, expected_selector):
                    failure_reason = f"page loaded but doesn't contain the expected content ({expected_selector!r} not found)"
                else:
                    failure_reason = None

                if failure_reason is None:
                    return html

                backoff = _backoff_with_jitter(attempt)
                logger.warning(
                    "FlareSolverr got %s for %s (attempt %d/%d)%s",
                    failure_reason, url, attempt + 1, _FLARESOLVERR_MAX_RETRIES,
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
                backoff = _backoff_with_jitter(attempt)
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


def _fetch_html(url: str, user_agent: str, timeout: int, retries: int, flaresolverr_url: str | None = None, expected_selector: str | None = None) -> str | None:
    # When a source is already known to need FlareSolverr, don't waste plain
    # HTTP attempts first - a Cloudflare-protected site blocks those too, and
    # each one still counts toward that site's own rate limiting/ban
    # threshold for no benefit (it was never going to succeed).
    if flaresolverr_url:
        return _fetch_via_flaresolverr(url, flaresolverr_url, timeout, expected_selector)

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


def _fetch_bytes(url: str, user_agent: str, timeout: int, retries: int, max_bytes: int | None = None, flaresolverr_url: str | None = None) -> bytes | None:
    if flaresolverr_url:
        # cloudflare_protected: true was previously a no-op for rss/atom
        # sources - this function never accepted a flaresolverr_url at all,
        # so a Cloudflare JS challenge always failed no matter how the
        # source was configured. No expected_selector here since XML feed
        # content has no CSS selector to check against.
        text = _fetch_via_flaresolverr(url, flaresolverr_url, timeout)
        if text is None:
            return None
        content = text.encode()
        if max_bytes is not None and len(content) > max_bytes:
            logger.warning("Discarding FlareSolverr response from %s - %d bytes exceeds %d byte cap", url, len(content), max_bytes)
            return None
        return content

    headers = {"User-Agent": user_agent}
    for attempt in range(retries):
        try:
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks={"request": [_make_ssrf_check()]}) as client:
                # Stream and stop as soon as max_bytes is exceeded - a plain
                # client.get() downloads the full response into memory before
                # any size check runs, so a huge PDF was still fully fetched
                # (bandwidth + memory cost paid) and only discarded after the
                # fact, defeating the point of the cap.
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    chunks = []
                    total = 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            logger.warning("Aborting download from %s - exceeded %d byte cap", url, max_bytes)
                            return None
                    return b"".join(chunks)
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
        pages = reader.pages[:_MAX_PDF_PAGES]
        if len(reader.pages) > _MAX_PDF_PAGES:
            logger.warning("PDF has %d pages, only extracting the first %d", len(reader.pages), _MAX_PDF_PAGES)
        text = "\n\n".join(page.extract_text() or "" for page in pages)
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
    date_fuzzy = scrape_cfg.get("date_fuzzy", False)
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
                        # dayfirst: EEA/UK sources render free-text dates as
                        # DD/MM/YYYY - without this, ambiguous dates like
                        # 02/07/2026 silently misparse as 2 July -> 7 Feb.
                        # Only applied when reading free text (no
                        # date_attribute): dateutil's dayfirst=True does NOT
                        # leave an unambiguous ISO datetime="..." attribute
                        # alone - verified directly that dayfirst=True still
                        # swaps month/day on a full "YYYY-MM-DDTHH:MM:SSZ"
                        # string whenever both are <=12 (e.g.
                        # "2026-07-02T12:00:00Z" parses as 7 Feb, not 2 July).
                        # A prior version of this comment claimed ISO
                        # attributes were "unaffected by dayfirst" - that was
                        # wrong and silently corrupted any date_attribute
                        # source's day<=12 dates (confirmed live against
                        # ESMA's library scrape, which uses date_attribute).
                        #
                        # fuzzy is NOT a safe global default - it silently
                        # extracts a date-shaped substring from surrounding text
                        # and never fails loudly, so an unrelated "reference
                        # 03/2024" in body text could get accepted as a real
                        # date. Opt in per-source via date_fuzzy: true only for
                        # sources that genuinely need it (e.g. FCA's
                        # "Published: DD/MM/YYYY" text, which has no clean
                        # sub-element isolating just the date).
                        parsed = parse_date(date_str, fuzzy=date_fuzzy, dayfirst=not date_attribute)
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
                max_bytes=_MAX_PDF_DOWNLOAD_BYTES,
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


def _parse_page_watch(html: str, source: dict, regulator_id: str) -> list[Item]:
    watch_selector = source["watch_selector"]
    soup = BeautifulSoup(html, "lxml")
    content_el = soup.select_one(watch_selector)
    if content_el is None:
        return []

    text = " ".join(content_el.get_text(" ", strip=True).split())
    if not text:
        return []

    hash_value = content_hash(text)
    now = datetime.now(timezone.utc)
    return [Item(
        id=make_page_watch_item_id(regulator_id, source["url"], hash_value),
        regulator_id=regulator_id,
        source_label=source["label"],
        title=source.get("label", "page"),
        url=source["url"],
        published_at=now,
        fetched_at=now,
        date_inferred=True,
        language=source.get("preferred_language", "en"),
        content_snippet=_truncate_snippet(text),
        source_type="page_watch",
        tags=source.get("tags", []),
        raw_content=text,
    )]


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

    if source_type in ("rss", "atom"):
        content = _fetch_bytes(url, user_agent, timeout, retries, flaresolverr_url=flaresolverr_url)
        if content is None:
            # A genuine fetch failure (network error, block, timeout) must be
            # distinguishable from "fetched fine, feed just has 0 new entries
            # today" - the latter is normal for RSS, the former is not.
            # Raising lets run_collection()'s existing except block record it
            # as a source error via consecutive_failures; returning [] here
            # used to make both cases look identical and silently reset any
            # real failure streak (e.g. a 403 logged as an ERROR but treated
            # as a clean, empty run).
            raise RuntimeError(f"Failed to fetch {url} for {regulator_id}/{source['label']}")
        return _parse_rss(content, source, regulator_id)
    elif source_type == "scrape":
        expected_selector = source.get("scrape", {}).get("item_selector")
        html = _fetch_html(url, user_agent, timeout, retries, flaresolverr_url, expected_selector)
        if html is None:
            raise RuntimeError(f"Failed to fetch {url} for {regulator_id}/{source['label']}")
        return _parse_scrape(html, source, regulator_id, config)
    elif source_type == "page_watch":
        watch_selector = source.get("watch_selector")
        html = _fetch_html(url, user_agent, timeout, retries, flaresolverr_url, watch_selector)
        if html is None:
            raise RuntimeError(f"Failed to fetch {url} for {regulator_id}/{source['label']}")
        # _fetch_html()'s expected_selector check only runs inside the
        # FlareSolverr code path - a plain-httpx fetch (the common case for
        # a non-cloudflare-protected page_watch source) never verifies the
        # page actually contains what we're monitoring, so a redirect or
        # maintenance page returning HTTP 200 would otherwise silently hash
        # the wrong content and get recorded as a legitimate "change".
        if not _has_expected_content(html, watch_selector):
            raise RuntimeError(f"Expected content not found at {url} for {regulator_id}/{source['label']}")
        return _parse_page_watch(html, source, regulator_id)
    else:
        logger.warning("Unknown source type '%s' for %s/%s", source_type, regulator_id, source["label"])
        return []
