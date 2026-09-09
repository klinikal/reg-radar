"""Page-watch adapter (hash-and-diff a CSS-selected page fragment) - wraps
v1's `_parse_page_watch` unchanged.
"""
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from collector.adapters.base import FetchContext, RawDocument, SourceAdapter
from collector.adapters.http import fetch_html, has_expected_content
from collector.chunking import content_hash


class PageWatchAdapter(SourceAdapter):
    type_id = "page_watch"

    def fetch(self, source_cfg: dict, ctx: FetchContext) -> list[RawDocument]:
        html = self.fetch_raw(source_cfg, ctx)
        return self.parse(html, source_cfg, ctx.regulator_id)

    def fetch_raw(self, source_cfg: dict, ctx: FetchContext) -> str:
        watch_selector = source_cfg.get("watch_selector")
        html = fetch_html(
            source_cfg["url"], ctx.user_agent, ctx.timeout, ctx.retries,
            ctx.flaresolverr_url, watch_selector,
        )
        if html is None:
            raise RuntimeError(f"Failed to fetch {source_cfg['url']} for {ctx.regulator_id}/{source_cfg['label']}")
        # fetch_html()'s expected_selector check only runs inside the
        # FlareSolverr code path - a plain-httpx fetch (the common case for
        # a non-cloudflare-protected page_watch source) never verifies the
        # page actually contains what we're monitoring, so a redirect or
        # maintenance page returning HTTP 200 would otherwise silently hash
        # the wrong content and get recorded as a legitimate "change".
        if not has_expected_content(html, watch_selector):
            raise RuntimeError(f"Expected content not found at {source_cfg['url']} for {ctx.regulator_id}/{source_cfg['label']}")
        return html

    def parse(self, html: str, source_cfg: dict, regulator_id: str) -> list[RawDocument]:
        watch_selector = source_cfg["watch_selector"]
        soup = BeautifulSoup(html, "lxml")
        content_el = soup.select_one(watch_selector)
        if content_el is None:
            return []

        text = " ".join(content_el.get_text(" ", strip=True).split())
        if not text:
            return []

        now = datetime.now(timezone.utc)
        return [RawDocument(
            url=source_cfg["url"],
            title=source_cfg.get("label", "page"),
            published_at=now,
            date_inferred=True,
            body=html.encode("utf-8"),
            mime="text/html",
            content_text=text,
            content_hash=content_hash(text),
            language=source_cfg.get("preferred_language", "en"),
        )]
