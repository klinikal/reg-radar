import json
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from mcpserver.queries import (
    get_item,
    get_month_facets,
    get_recent,
    get_run_log,
    get_warnings,
    list_regulators,
    search_items,
    search_items_advanced,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "casp-radar",
    host="0.0.0.0",
    port=8000,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8000"],
    ),
)

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        db_path = os.getenv("DB_PATH", "/app/data/casp_radar.db")
        if not os.path.exists(db_path):
            logger.error(
                "Database file not found at %s - has the collector run yet? "
                "The MCP server opens the DB read-only and does not create the schema itself.",
                db_path,
            )
            sys.exit(1)
        _engine = create_engine(
            f"sqlite:///file:{db_path}?mode=ro&uri=true",
            connect_args={"timeout": 30},
            poolclass=NullPool,
        )
    return _engine


def _safe(label: str, fn, *args, **kwargs):
    """Run a query function; on failure, log the real exception (with DB
    path/SQL detail) and raise a sanitized RuntimeError so internals never
    reach the MCP client.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.exception("%s failed", label)
        raise RuntimeError(f"{label} failed - see server logs") from None


@mcp.tool()
def search_items_tool(query: str, regulator_id: str | None = None, language: str | None = None,
                       tags: list[str] | None = None, source_tags: list[str] | None = None,
                       content_tags: list[str] | None = None, date_from: str | None = None,
                       date_to: str | None = None, limit: int = 20, offset: int = 0) -> str:
    """Full-text search across regulatory items by title and snippet, paginated.

    `tags` searches both the notice-type (source_tags) and topic (content_tags)
    buckets combined. Use `source_tags` to filter by notice kind only (e.g.
    "warnings", "press_releases") or `content_tags` to filter by topic only
    (e.g. "mica", "travel-rule") when you need one bucket specifically.
    limit defaults to 20, max 100. Use `offset` to page past the first
    `limit` results (e.g. offset=20 for page 2 of a limit=20 search), or use
    count_items_tool first to check the total before paging through it.
    """
    results = _safe("search_items_tool", search_items, get_engine(), query=query, regulator_id=regulator_id,
                     language=language, tags=tags, source_tags=source_tags, content_tags=content_tags,
                     date_from=date_from, date_to=date_to, limit=limit, offset=offset)
    return json.dumps(results, default=str)


@mcp.tool()
def get_recent_tool(limit: int = 20, regulator_id: str | None = None, tags: list[str] | None = None,
                     source_tags: list[str] | None = None, content_tags: list[str] | None = None,
                     language: str | None = None) -> str:
    """Returns the most recent regulatory items, with optional filters.

    `tags` searches both notice-type (source_tags) and topic (content_tags)
    buckets combined; use `source_tags`/`content_tags` for a specific bucket.
    """
    results = _safe("get_recent_tool", get_recent, get_engine(), limit=limit, regulator_id=regulator_id, tags=tags,
                     source_tags=source_tags, content_tags=content_tags, language=language)
    return json.dumps(results, default=str)


@mcp.tool()
def list_regulators_tool() -> str:
    """Returns all regulators in the registry with live fetch stats."""
    results = _safe("list_regulators_tool", list_regulators, get_engine())
    return json.dumps(results, default=str)


@mcp.tool()
def get_item_tool(item_id: str) -> str:
    """Returns a single item by its ID, including raw_content."""
    result = _safe("get_item_tool", get_item, get_engine(), item_id)
    return json.dumps(result, default=str)


@mcp.tool()
def get_run_log_tool(limit: int = 10) -> str:
    """Returns recent collector run statistics."""
    results = _safe("get_run_log_tool", get_run_log, get_engine(), limit=limit)
    return json.dumps(results, default=str)


@mcp.tool()
def get_warnings_tool(days: int = 30, regulator_id: str | None = None, limit: int = 50) -> str:
    """Returns items tagged 'warnings' at the feed/source level, newest first.

    days=0 returns all (no date filter), max 365. limit defaults to 50, max
    200. regulator_id must be a valid id from list_regulators_tool.

    Note: some regulators only have one general news/press-release feed, so
    it's tagged 'warnings' alongside 'news'/'publications' rather than
    leaving genuine warnings uncollected - that feed's entire output shows up
    here, not just its actual warning notices. Sources with a dedicated
    warnings-only feed (e.g. FCA, CNMV's unregistered-firm notices) are
    precise; broad "all_news"-style feeds are not. Treat results from a
    broad feed as "may contain warnings," not a filtered warnings list -
    read the item to judge.
    """
    results = _safe("get_warnings_tool", get_warnings, get_engine(), days=days, regulator_id=regulator_id, limit=limit)
    return json.dumps(results, default=str)


@mcp.tool()
def count_items_tool(query: str = "", exact: bool = False, regulator_id: str | None = None,
                      date_from: str | None = None, date_to: str | None = None) -> str:
    """Returns the total count of items matching a search, without fetching rows.

    Cheaper than search_items_tool when an agent only needs "how many", not
    the items themselves. `query` uses the same AND/quoted-phrase semantics
    as search_items_tool; `exact=True` treats the whole query as one literal
    phrase. date_from/date_to are YYYY-MM-DD. regulator_id must be a valid
    id from list_regulators_tool.
    """
    def _count():
        _, total = search_items_advanced(
            get_engine(), query=query, exact=exact,
            regulator_ids=[regulator_id] if regulator_id else None,
            date_from=date_from, date_to=date_to, limit=1,
        )
        return {"total": total}
    return json.dumps(_safe("count_items_tool", _count), default=str)


@mcp.tool()
def get_month_facets_tool(query: str = "", exact: bool = False, regulator_id: str | None = None) -> str:
    """Returns item counts grouped by publish month (YYYY-MM), newest first.

    Ignores date filters deliberately, so every month stays visible for a
    month-picker regardless of any date range applied separately via
    search_items_tool. query/exact/regulator_id semantics match count_items_tool.
    """
    return json.dumps(
        _safe("get_month_facets_tool", get_month_facets, get_engine(), query=query, exact=exact,
              regulator_ids=[regulator_id] if regulator_id else None),
        default=str,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
