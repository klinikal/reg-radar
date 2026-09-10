"""HTTP plumbing shared by every adapter that fetches over plain HTTP or
FlareSolverr: SSRF guards, FlareSolverr throttle/backoff/block-page
detection, and bounded PDF byte fetch + text extraction. Adapter-agnostic -
moved out of collector/fetcher.py unchanged (docs/issues/08-source-adapter
-framework-registry-split.md) so `rss.py`/`scrape.py`/`page_watch.py` (and
new adapters) share one implementation instead of duplicating it.
"""
import ipaddress
import logging
import os
import random
import socket
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Browser-shaped but honest and attributable: no specific browser version, and
# the project URL stays so site operators can identify the crawler. Several
# hosts block or serve a decoy to a bare tool UA - MAS answers 200 with an
# 854KB "Maintenance" page rather than its real JSON (#37). Defined once here
# because it was previously copied into eight places and drifted.
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; reg-radar/2.0; +https://github.com/klinikal/reg-radar)"

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
# Shared by every adapter that goes through FlareSolverr *or* Playwright
# (collector/adapters/browser.py) - a Playwright launch carries a similar
# per-request cost against the target site, so it uses the same throttle.
_last_flaresolverr_request_at: dict[str, float] = {}

# extract_pdf_text fetches a document per scraped item, synchronously,
# inside the collection loop - an unbounded download or page count on a
# huge/hostile/malformed PDF could stall a whole run. Bounded, not
# unlimited, by design.
MAX_PDF_DOWNLOAD_BYTES = int(os.getenv("MAX_PDF_DOWNLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "50"))


def throttle_flaresolverr(domain: str) -> None:
    last = _last_flaresolverr_request_at.get(domain, 0.0)
    elapsed = time.monotonic() - last
    wait = _FLARESOLVERR_MIN_INTERVAL_SECONDS - elapsed
    if wait > 0:
        time.sleep(wait)
    _last_flaresolverr_request_at[domain] = time.monotonic()


# sec.gov's observed behaviour is stricter than its documented Fair Access
# policy: a burst of ~8 rapid requests earned a site-wide block lasting
# minutes. One host with one verified requirement, so one host-specific
# rule - deliberately not a general per-host rate-limit config.
# ponytail: single global timestamp, fine because the collector fetches
# sequentially in one process; make it a per-process lock if that changes.
_SEC_MIN_INTERVAL_SECONDS = float(os.getenv("SEC_MIN_INTERVAL_SECONDS", "1"))
_last_sec_request_at = float("-inf")


def throttle_sec_gov(url: str) -> None:
    """Space sec.gov requests (any subdomain) >=1s apart; no-op elsewhere."""
    global _last_sec_request_at
    host = (urlparse(url).hostname or "").lower()
    if host != "sec.gov" and not host.endswith(".sec.gov"):
        return
    wait = _SEC_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_sec_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_sec_request_at = time.monotonic()


def _backoff_with_jitter(attempt: int) -> float:
    # Deterministic exponential backoff means every source hitting the same
    # domain at the same time retries in lockstep, re-colliding on the same
    # schedule. Jitter spreads retries out instead of bunching them.
    base = _FLARESOLVERR_RETRY_BACKOFF_SECONDS * (2 ** attempt)
    return base * (0.5 + random.random())


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


def validate_url_not_private(url: str) -> None:
    host = urlparse(url).hostname or ""
    if host and _host_resolves_to_private(host):
        raise ValueError(f"URL targets or resolves to a private/reserved address: {host}")


def make_ssrf_check():
    def on_request(request):
        host = request.url.host
        if host and _host_resolves_to_private(host):
            raise httpx.InvalidURL(f"Request to private/reserved address blocked: {host}")
    return on_request


def looks_like_block_page(html: str) -> bool:
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


def has_expected_content(html: str, expected_selector: str | None) -> bool:
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


def fetch_via_flaresolverr(url: str, flaresolverr_url: str, timeout: int, expected_selector: str | None = None) -> str | None:
    validate_url_not_private(url)
    domain = urlparse(url).hostname or url
    for attempt in range(_FLARESOLVERR_MAX_RETRIES):
        throttle_flaresolverr(domain)
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
                elif looks_like_block_page(html):
                    failure_reason = "block/rate-limit page (content heuristic)"
                elif not has_expected_content(html, expected_selector):
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


def fetch_html(url: str, user_agent: str, timeout: int, retries: int, flaresolverr_url: str | None = None, expected_selector: str | None = None) -> str | None:
    # When a source is already known to need FlareSolverr, don't waste plain
    # HTTP attempts first - a Cloudflare-protected site blocks those too, and
    # each one still counts toward that site's own rate limiting/ban
    # threshold for no benefit (it was never going to succeed).
    if flaresolverr_url:
        return fetch_via_flaresolverr(url, flaresolverr_url, timeout, expected_selector)

    headers = {"User-Agent": user_agent}
    for attempt in range(retries):
        try:
            throttle_sec_gov(url)
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks={"request": [make_ssrf_check()]}) as client:
                resp = client.get(url)
                resp.raise_for_status()
                return resp.text
        except Exception as e:
            logger.warning("HTTP fetch attempt %d/%d failed for %s: %s", attempt + 1, retries, url, e)

    return None


def fetch_bytes(url: str, user_agent: str, timeout: int, retries: int, max_bytes: int | None = None, flaresolverr_url: str | None = None) -> bytes | None:
    if flaresolverr_url:
        # cloudflare_protected: true was previously a no-op for rss/atom
        # sources - this function never accepted a flaresolverr_url at all,
        # so a Cloudflare JS challenge always failed no matter how the
        # source was configured. No expected_selector here since XML feed
        # content has no CSS selector to check against.
        text = fetch_via_flaresolverr(url, flaresolverr_url, timeout)
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
            throttle_sec_gov(url)
            with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True, event_hooks={"request": [make_ssrf_check()]}) as client:
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


def extract_pdf_text(content: bytes) -> str | None:
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = reader.pages[:MAX_PDF_PAGES]
        if len(reader.pages) > MAX_PDF_PAGES:
            logger.warning("PDF has %d pages, only extracting the first %d", len(reader.pages), MAX_PDF_PAGES)
        text = "\n\n".join(page.extract_text() or "" for page in pages)
        return text.strip() or None
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return None
