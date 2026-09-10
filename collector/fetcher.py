"""fetch_source(): looks up the registered adapter for a source's `type`,
fetches it, and runs the result through the shared pipeline
(collector/pipeline.py) to produce Items.

The fetch logic itself (rss/scrape/page_watch/sitemap/json_api/browser) and
the HTTP plumbing they share (SSRF guards, FlareSolverr, PDF extraction)
live under collector/adapters/ - see docs/issues/08-source-adapter
-framework-registry-split.md.
"""
import logging
import os

from collector.adapters.base import FetchContext
from collector.adapters.registry import get as get_adapter
from collector.pipeline import process_raw_documents
from collector.schema import Item
from collector.adapters.http import BlockedFetchError, DEFAULT_USER_AGENT, observe_response_sizes

logger = logging.getLogger(__name__)

# The decoy heuristic from issue #39 B.1: a large response that parses to
# nothing is a block until proven otherwise. MAS answers a non-browser UA
# with HTTP 200 and 853,817 bytes of "Maintenance" HTML instead of its real
# JSON, and neither raise_for_status() nor the empty-run counter can tell
# that apart from a quiet day.
#
# The signal is *large body AND zero parsed items*, never size alone: MAS's
# genuine JSON is ~114KB at rows=100 and parses fine, so it never reaches
# this check. The threshold only has to sit above the largest body a source
# can legitimately return while yielding nothing - well above a real feed,
# well below the decoy.
# ASSUMPTION: no enabled source legitimately serves >256KB and parses to
# zero items. If one does, give it a per-source override rather than
# raising the global floor.
LARGE_BODY_ZERO_ITEMS_BYTES = int(os.getenv("BLOCKED_LARGE_BODY_BYTES", str(256 * 1024)))


def fetch_source(
    source: dict,
    regulator_id: str,
    flaresolverr_url: str | None,
    config: dict,
    rules_engine=None,
) -> list[Item]:
    user_agent = config.get("user_agent", DEFAULT_USER_AGENT)
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
    # observe_response_sizes() only measures; the header/body block
    # signatures are classified inside collector/adapters/http.py, before
    # any parse() call, so a challenge page never reaches a parser.
    with observe_response_sizes() as response_sizes:
        raw_docs = adapter.fetch(source, ctx)

    if not raw_docs and response_sizes and max(response_sizes) >= LARGE_BODY_ZERO_ITEMS_BYTES:
        raise BlockedFetchError(
            f"{max(response_sizes)} bytes fetched but the parser found 0 items "
            f"- likely a decoy/challenge page rather than the real content "
            f"({source.get('url', '')})"
        )

    return process_raw_documents(raw_docs, source, regulator_id, rules_engine=rules_engine)
