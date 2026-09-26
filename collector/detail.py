"""Shared "fetch source_url -> select detail HTML -> convert to markdown"
step for a regulator's `detail_selector` config (issue #17,
config/regulators/*.yaml). Two callers share this: scripts/reprocess_bodies.py
--refetch (backfilling existing empty-body items) and collector.main's
run_collection (fetching the full body for a brand-new RSS item at
collection time, issue #109) - this module is the one implementation both
use, so there is nowhere for the two to drift apart.
"""
from bs4 import BeautifulSoup

from collector.adapters.http import (
    BlockedFetchError,
    fetch_html as _fetch_html,
    observe_fetch_statuses as _observe_fetch_statuses,
)
from regradar.document import convert_body


def select_detail_html(html: str, detail_selector: str, detail_exclude: list[str] | None = None) -> str | None:
    """The matched element's own HTML, with any `detail_exclude` selector's
    matches removed from inside it (e.g. a related-links block that lives
    nested inside the content container, as opposed to a true page sibling,
    which selecting the container alone already excludes). None if
    `detail_selector` matches nothing on this page."""
    soup = BeautifulSoup(html, "lxml")
    el = soup.select_one(detail_selector)
    if el is None:
        return None
    for exclude_selector in (detail_exclude or []):
        for node in el.select(exclude_selector):
            node.decompose()
    return str(el)


def fetch_detail_body(
    url: str,
    detail_selector: str,
    detail_exclude: list[str] | None,
    user_agent: str,
    timeout: int,
    retries: int,
    flaresolverr_url: str | None = None,
    fetch_html=_fetch_html,
    observe_fetch_statuses=_observe_fetch_statuses,
) -> dict:
    """Fetches `url`, selects `detail_selector` and converts it to Markdown.

    Returns a dict. On success:
        {"status": "recovered", "body_md": str, "extractor": str,
         "content_html": str, "raw_content": str}
    On failure, "status" is one of "blocked" / "not_found" / "fetch_failed" /
    "selector_miss" (matching scripts/reprocess_bodies.py's refetch() report
    keys) and "reason" is a human-readable string.

    `fetch_html`/`observe_fetch_statuses` are injectable so a caller whose
    own tests monkeypatch its module-level import of either keeps working
    unchanged - pass the caller's own (possibly-patched) module globals
    through rather than relying on this module's.
    """
    try:
        with observe_fetch_statuses() as statuses:
            html = fetch_html(url, user_agent, timeout, retries, flaresolverr_url, detail_selector)
    except BlockedFetchError as e:
        return {"status": "blocked", "reason": f"blocked: {e}"}

    if html is None:
        if 404 in statuses:
            return {"status": "not_found", "reason": "404 not found"}
        return {"status": "fetch_failed", "reason": "fetch failed"}

    content_html = select_detail_html(html, detail_selector, detail_exclude)
    if content_html is None:
        return {
            "status": "selector_miss",
            "reason": f"detail_selector {detail_selector!r} matched nothing on the fetched page",
        }

    body_md, extractor = convert_body(content_html=content_html, source_url=url)
    raw_content = BeautifulSoup(content_html, "lxml").get_text(strip=True)
    return {
        "status": "recovered",
        "body_md": body_md,
        "extractor": extractor,
        "content_html": content_html,
        "raw_content": raw_content,
    }
