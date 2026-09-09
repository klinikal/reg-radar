"""`type_id`-keyed adapter registry (docs/issues/08-source-adapter-framework
-registry-split.md). Importing this module must not require optional heavy
deps (e.g. playwright) - collector/adapters/browser.py guards its own import
so the registry stays usable even without browser binaries installed.
"""
from collector.adapters.base import SourceAdapter
from collector.adapters.browser import BrowserAdapter
from collector.adapters.json_api import JsonApiAdapter
from collector.adapters.page_watch import PageWatchAdapter
from collector.adapters.rss import RssAdapter
from collector.adapters.scrape import ScrapeAdapter
from collector.adapters.sitemap import SitemapAdapter

REGISTRY: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> None:
    REGISTRY[adapter.type_id] = adapter


def get(type_id: str) -> SourceAdapter | None:
    return REGISTRY.get(type_id)


register(RssAdapter())
register(ScrapeAdapter())
register(PageWatchAdapter())
register(SitemapAdapter())
register(JsonApiAdapter())
register(BrowserAdapter())
# "atom" sources use the same adapter as "rss" (feedparser handles both).
REGISTRY["atom"] = REGISTRY["rss"]
