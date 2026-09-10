"""Sitemap adapter: parses a sitemap.xml's <url><loc>/<lastmod> entries.

Config block (../../wiki/content/concept-source-adapter-framework.md):

    sources:
      - type: sitemap
        url: https://example.com/sitemap.xml
        label: publications
        sitemap:
          url_pattern: "/publications/"   # optional: only <loc> entries containing this substring
          title_from: last_segment        # only supported value today - title is the last URL path segment, humanised
"""
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree

from collector.adapters.base import FetchContext, RawDocument, SourceAdapter
from collector.adapters.http import fetch_bytes

logger = logging.getLogger(__name__)

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _title_from_last_segment(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    segment = path.rsplit("/", 1)[-1] if path else url
    segment = segment.rsplit(".", 1)[0]  # drop a file extension like .html
    return segment.replace("-", " ").replace("_", " ").strip().capitalize() or url


def _parse_lastmod(value: str | None) -> tuple[datetime | None, bool]:
    if not value:
        return None, True
    try:
        # sitemap lastmod is W3C datetime: YYYY-MM-DD or full ISO 8601.
        value = value.strip()
        if len(value) == 10:
            dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc), False
    except ValueError:
        return None, True


class SitemapAdapter(SourceAdapter):
    type_id = "sitemap"

    def fetch(self, source_cfg: dict, ctx: FetchContext) -> list[RawDocument]:
        content = self.fetch_raw(source_cfg, ctx)
        return self.parse(content, source_cfg, ctx.regulator_id)

    def fetch_raw(self, source_cfg: dict, ctx: FetchContext) -> bytes:
        content = fetch_bytes(
            source_cfg["url"], ctx.user_agent, ctx.timeout, ctx.retries,
            flaresolverr_url=ctx.flaresolverr_url,
        )
        if content is None:
            raise RuntimeError(f"Failed to fetch {source_cfg['url']} for {ctx.regulator_id}/{source_cfg['label']}")
        return content

    def parse(self, content: bytes | str, source_cfg: dict, regulator_id: str) -> list[RawDocument]:
        sitemap_cfg = source_cfg.get("sitemap", {})
        url_pattern = sitemap_cfg.get("url_pattern")

        if isinstance(content, str):
            content = content.encode("utf-8")
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as e:
            logger.error("Sitemap source %s/%s: invalid XML - %s", regulator_id, source_cfg["label"], e)
            return []

        # ElementTree Elements are falsy when they have no children (even
        # with text set), so `find(...) or find(...)` silently discards a
        # real match with no sub-elements - explicit None checks instead.
        url_els = root.findall("sm:url", _SITEMAP_NS)
        if not url_els:
            url_els = root.findall("url")

        docs = []
        for url_el in url_els:
            loc_el = url_el.find("sm:loc", _SITEMAP_NS)
            if loc_el is None:
                loc_el = url_el.find("loc")
            if loc_el is None or not (loc_el.text or "").strip():
                continue
            loc = loc_el.text.strip()
            if url_pattern and url_pattern not in loc:
                continue

            lastmod_el = url_el.find("sm:lastmod", _SITEMAP_NS)
            if lastmod_el is None:
                lastmod_el = url_el.find("lastmod")
            published_at, date_inferred = _parse_lastmod(lastmod_el.text if lastmod_el is not None else None)

            docs.append(RawDocument(
                url=loc,
                title=_title_from_last_segment(loc),
                published_at=published_at,
                date_inferred=date_inferred,
            ))
        return docs
