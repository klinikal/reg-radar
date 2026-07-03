import json
import logging
import os

from mcp.server.fastmcp import FastMCP

from collector.storage import init_db
from mcpserver.queries import (
    get_item,
    get_recent,
    get_run_log,
    get_warnings,
    list_regulators,
    search_items,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

mcp = FastMCP("casp-radar", host="0.0.0.0", port=8000)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = os.getenv("DB_PATH", "/app/data/casp_radar.db")
        _engine = init_db(db_path)
    return _engine


@mcp.tool()
def search_items_tool(query: str, regulator_id: str = None, language: str = None, tags: list[str] = None,
                       source_tags: list[str] = None, content_tags: list[str] = None,
                       date_from: str = None, date_to: str = None, limit: int = 20) -> str:
    """Full-text search across regulatory items by title and snippet.

    `tags` searches both the notice-type (source_tags) and topic (content_tags)
    buckets combined. Use `source_tags` to filter by notice kind only (e.g.
    "warnings", "press_releases") or `content_tags` to filter by topic only
    (e.g. "mica", "travel-rule") when you need one bucket specifically.
    """
    results = search_items(get_engine(), query=query, regulator_id=regulator_id, language=language,
                            tags=tags, source_tags=source_tags, content_tags=content_tags,
                            date_from=date_from, date_to=date_to, limit=limit)
    return json.dumps(results, default=str)


@mcp.tool()
def get_recent_tool(limit: int = 20, regulator_id: str = None, tags: list[str] = None,
                     source_tags: list[str] = None, content_tags: list[str] = None, language: str = None) -> str:
    """Returns the most recent regulatory items, with optional filters.

    `tags` searches both notice-type (source_tags) and topic (content_tags)
    buckets combined; use `source_tags`/`content_tags` for a specific bucket.
    """
    results = get_recent(get_engine(), limit=limit, regulator_id=regulator_id, tags=tags,
                          source_tags=source_tags, content_tags=content_tags, language=language)
    return json.dumps(results, default=str)


@mcp.tool()
def list_regulators_tool() -> str:
    """Returns all regulators in the registry with live fetch stats."""
    results = list_regulators(get_engine())
    return json.dumps(results, default=str)


@mcp.tool()
def get_item_tool(id: str) -> str:
    """Returns a single item by its ID, including raw_content."""
    result = get_item(get_engine(), id)
    return json.dumps(result, default=str)


@mcp.tool()
def get_run_log_tool(limit: int = 10) -> str:
    """Returns recent collector run statistics."""
    results = get_run_log(get_engine(), limit=limit)
    return json.dumps(results, default=str)


@mcp.tool()
def get_warnings_tool(days: int = 30, regulator_id: str = None) -> str:
    """Returns items tagged 'warnings' at the feed/source level. days=0 returns all, max 365.

    Note: some regulators only have one general news/press-release feed, so
    it's tagged 'warnings' alongside 'news'/'publications' rather than
    leaving genuine warnings uncollected - that feed's entire output shows up
    here, not just its actual warning notices. Sources with a dedicated
    warnings-only feed (e.g. FCA, CNMV's unregistered-firm notices) are
    precise; broad "all_news"-style feeds are not. Treat results from a
    broad feed as "may contain warnings," not a filtered warnings list -
    read the item to judge.
    """
    results = get_warnings(get_engine(), days=days, regulator_id=regulator_id)
    return json.dumps(results, default=str)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
