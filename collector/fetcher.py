"""fetch_source(): looks up the registered adapter for a source's `type`,
fetches it, and runs the result through the shared pipeline
(collector/pipeline.py) to produce Items.

The fetch logic itself (rss/scrape/page_watch/sitemap/json_api/browser) and
the HTTP plumbing they share (SSRF guards, FlareSolverr, PDF extraction)
live under collector/adapters/ - see docs/issues/08-source-adapter
-framework-registry-split.md.
"""
import logging

from collector.adapters.base import FetchContext
from collector.adapters.registry import get as get_adapter
from collector.pipeline import process_raw_documents
from collector.schema import Item

logger = logging.getLogger(__name__)


def fetch_source(
    source: dict,
    regulator_id: str,
    flaresolverr_url: str | None,
    config: dict,
) -> list[Item]:
    user_agent = config.get("user_agent", "regradar/2.0")
    timeout = int(config.get("timeout", 30))
    retries = int(config.get("retries", 3))
    source_type = source.get("type", "rss")

    adapter = get_adapter(source_type)
    if adapter is None:
        logger.warning("Unknown source type '%s' for %s/%s", source_type, regulator_id, source.get("label", "<unknown>"))
        return []

    ctx = FetchContext(
        user_agent=user_agent, timeout=timeout, retries=retries,
        flaresolverr_url=flaresolverr_url, regulator_id=regulator_id,
    )
    raw_docs = adapter.fetch(source, ctx)
    return process_raw_documents(raw_docs, source, regulator_id)
