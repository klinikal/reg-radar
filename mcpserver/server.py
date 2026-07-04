import logging
import os
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from mcpserver.queries import (
    get_health,
    get_item,
    get_items_since,
    get_month_facets,
    get_recent,
    get_run_log,
    get_warnings,
    list_regulators,
    list_tags,
    search_items,
    search_items_advanced,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

# The Host header a client sends reflects the EXTERNAL port it connected to
# (e.g. "127.0.0.1:8001"), not this container's internal port (always 8000)
# - so an operator who remaps the published port via docker-compose.override.yml
# (documented in skills/casp-radar-operator/SKILL.md for port conflicts) needs
# their remapped host:port added here too, via MCP_EXTRA_ALLOWED_HOSTS
# (comma-separated, e.g. "127.0.0.1:8001,localhost:8001").
_extra_allowed_hosts = [
    h.strip() for h in os.getenv("MCP_EXTRA_ALLOWED_HOSTS", "").split(",") if h.strip()
]

mcp = FastMCP(
    "casp-radar",
    host="0.0.0.0",
    port=8000,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # Assumes loopback-only exposure (docker-compose.yml publishes
        # 127.0.0.1:8000:8000 by default). If this server is ever reached by
        # Docker service name from another container, add that hostname too.
        allowed_hosts=["127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8000"] + _extra_allowed_hosts,
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
    reach the MCP client. ValueError is re-raised verbatim - the only
    deliberate ValueError in this codebase (get_items_since's timestamp
    parse error) contains only the caller's own input, never internal
    detail, and the specific message is what lets an agent self-correct.
    """
    try:
        return fn(*args, **kwargs)
    except ValueError:
        raise
    except Exception:
        logger.exception("%s failed", label)
        raise RuntimeError(f"{label} failed - see server logs") from None


@mcp.tool()
def search_items_tool(query: str, regulator_id: str | None = None, language: str | None = None,
                       tags: list[str] | None = None, source_tags: list[str] | None = None,
                       content_tags: list[str] | None = None, date_from: str | None = None,
                       date_to: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
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
    return results


@mcp.tool()
def get_recent_tool(limit: int = 20, regulator_id: str | None = None, tags: list[str] | None = None,
                     source_tags: list[str] | None = None, content_tags: list[str] | None = None,
                     language: str | None = None) -> list[dict]:
    """Returns the most recent regulatory items (ordered by fetched_at,
    newest first), with optional filters.

    limit defaults to 20, max 100. regulator_id must be a valid id from
    list_regulators_tool. language is an ISO 639-1 code (e.g. "en", "de").
    `tags` searches both notice-type (source_tags) and topic (content_tags)
    buckets combined; use `source_tags`/`content_tags` to filter one bucket
    only. Call list_tags_tool to see valid tag values with their counts.
    """
    results = _safe("get_recent_tool", get_recent, get_engine(), limit=limit, regulator_id=regulator_id, tags=tags,
                     source_tags=source_tags, content_tags=content_tags, language=language)
    return results


@mcp.tool()
def list_regulators_tool() -> list[dict]:
    """Returns all regulators in the registry with live fetch stats.

    Each entry's `id` is the value to pass as `regulator_id` to every other
    tool in this server. `last_fetched_at` and `consecutive_failures`
    reflect the collector's health for that regulator specifically - see
    get_health_tool for overall collector freshness.
    """
    results = _safe("list_regulators_tool", list_regulators, get_engine())
    return results


@mcp.tool()
def get_item_tool(item_id: str, max_content_chars: int | None = None) -> dict | None:
    """Returns a single item by its id (as returned by search_items_tool,
    get_recent_tool, get_warnings_tool, etc.), including its full raw_content.

    raw_content can be large (a full regulator notice/PDF text) - pass
    max_content_chars to truncate it and protect your context window; omit
    for the full text. Returns None if the id doesn't exist.
    """
    result = _safe("get_item_tool", get_item, get_engine(), item_id)
    if result and max_content_chars is not None and result.get("raw_content"):
        result["raw_content"] = result["raw_content"][:max_content_chars]
    return result


@mcp.tool()
def get_run_log_tool(limit: int = 10) -> list[dict]:
    """Returns recent collector run statistics, most recent first.

    limit defaults to 10, max 100. `status` on each run is one of "running",
    "success", "partial", or "failed"; `error_detail` lists per-source
    failures when status is "partial" or "failed". Pair with
    get_items_since_tool's `since` param, using a run's completed_at, to
    fetch exactly what that run collected.
    """
    results = _safe("get_run_log_tool", get_run_log, get_engine(), limit=limit)
    return results


@mcp.tool()
def get_warnings_tool(days: int = 30, regulator_id: str | None = None, limit: int = 50) -> list[dict]:
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
    return results


@mcp.tool()
def count_items_tool(query: str = "", exact: bool = False, regulator_id: str | None = None,
                      date_from: str | None = None, date_to: str | None = None) -> dict:
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
    return _safe("count_items_tool", _count)


@mcp.tool()
def get_month_facets_tool(query: str = "", exact: bool = False, regulator_id: str | None = None) -> list[dict]:
    """Returns item counts grouped by publish month (YYYY-MM), newest first.

    Ignores date filters deliberately, so every month stays visible for a
    month-picker regardless of any date range applied separately via
    search_items_tool. query/exact/regulator_id semantics match count_items_tool.
    """
    return _safe("get_month_facets_tool", get_month_facets, get_engine(), query=query, exact=exact,
                 regulator_ids=[regulator_id] if regulator_id else None)


@mcp.tool()
def list_tags_tool() -> dict:
    """Returns every distinct source_tags and content_tags value currently
    in use, each with its item count, sorted most-common first. Use this to
    discover valid values for the tags/source_tags/content_tags filters on
    search_items_tool and get_recent_tool before guessing.
    """
    return _safe("list_tags_tool", list_tags, get_engine())


@mcp.tool()
def get_items_since_tool(since: str, limit: int = 100) -> list[dict]:
    """Returns items collected (fetched_at) strictly after `since`, oldest
    first, up to `limit` (default 100, max 200). This is the polling
    primitive: save the fetched_at of the last item you saw and pass it
    back in as `since` on the next call to get everything new. Pair with
    get_run_log_tool's run_ids if you need to correlate results with a
    specific collector run.
    """
    return _safe("get_items_since_tool", get_items_since, get_engine(), since=since, limit=limit)


@mcp.tool()
def get_health_tool() -> dict:
    """Returns the most recent collector run's status and freshness (age in
    seconds since it completed), so an agent can caveat "data may be stale"
    without separately calling get_run_log_tool and doing the date math
    itself.

    Also includes `db_integrity`: "ok", "corrupted", or "unknown" (no runs
    yet). The collector runs a full integrity check on the database once per
    collection cycle specifically so corruption is caught here - if this
    ever reads "corrupted", search results and other reads may already be
    unreliable; see "Backups & recovery" in
    skills/casp-radar-operator/SKILL.md to restore from the Litestream
    backup rather than trusting query results.
    """
    return _safe("get_health_tool", get_health, get_engine())


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
