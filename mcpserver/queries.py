import re
from datetime import datetime, timezone, timedelta

from sqlalchemy import text


def _row_to_dict(row, keys):
    return dict(zip(keys, row))


def _escape_like(value: str) -> str:
    # Escape LIKE special chars using ! as the escape character.
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


_QUOTED_RE = re.compile(r'"([^"]+)"')


def _parse_search_terms(query: str, exact: bool) -> list[str]:
    """Split a search query into AND-ed terms.

    exact=True: the whole query is one literal phrase.
    exact=False: "quoted phrases" are kept whole; everything else is split
    on whitespace/plus signs into individual required terms (so `this+this`
    and `this this` both mean "this" AND "this").
    """
    query = query.strip()
    if not query:
        return []
    if exact:
        return [query]
    terms = _QUOTED_RE.findall(query)
    remainder = _QUOTED_RE.sub(" ", query)
    terms.extend(t for t in re.split(r"[\s+]+", remainder) if t)
    return terms


def search_items(engine, query: str, regulator_id=None, language=None, tags=None, date_from=None, date_to=None, limit: int = 20) -> list[dict]:
    try:
        limit = min(int(limit), 100)
    except (TypeError, ValueError):
        limit = 20
    # Also checks raw_content, not just the 500-char content_snippet: a
    # multi-entity document (e.g. a regulator's batch warning notice naming
    # several firms) can easily exceed that truncation, leaving later-listed
    # entities unsearchable via title/snippet alone even though the full text
    # is stored. Widening the search only adds matches, never removes any.
    conditions = ["(title LIKE :q ESCAPE '!' OR content_snippet LIKE :q ESCAPE '!' OR raw_content LIKE :q ESCAPE '!')"]
    params = {"q": f"%{_escape_like(query)}%", "limit": limit}
    if regulator_id:
        conditions.append("regulator_id = :regulator_id")
        params["regulator_id"] = regulator_id
    if language:
        conditions.append("language = :language")
        params["language"] = language
    if tags:
        for i, tag in enumerate(tags):
            conditions.append(f"tags LIKE :tag_{i} ESCAPE '!'")
            params[f"tag_{i}"] = f'%"{_escape_like(tag)}"%'
    if date_from:
        conditions.append("published_at >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("published_at <= :date_to")
        params["date_to"] = _extend_date_to(date_to)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, date_inferred, language, content_snippet, source_type, tags
        FROM items WHERE {where}
        ORDER BY published_at DESC NULLS LAST
        LIMIT :limit
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "tags"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def _search_conditions(query: str, exact: bool, regulator_ids) -> tuple[list[str], dict]:
    """Shared query+regulator (not date) conditions, reused by search and month facets."""
    conditions = []
    params: dict = {}
    for i, term in enumerate(_parse_search_terms(query, exact)):
        # Also checks raw_content - see search_items() for why content_snippet
        # alone isn't enough (multi-entity documents can exceed its 500-char
        # truncation).
        conditions.append(
            f"(title LIKE :term_{i} ESCAPE '!' OR content_snippet LIKE :term_{i} ESCAPE '!' OR raw_content LIKE :term_{i} ESCAPE '!')"
        )
        params[f"term_{i}"] = f"%{_escape_like(term)}%"
    if regulator_ids:
        placeholders = []
        for i, rid in enumerate(regulator_ids):
            key = f"reg_{i}"
            placeholders.append(f":{key}")
            params[key] = rid
        conditions.append(f"regulator_id IN ({', '.join(placeholders)})")
    return conditions, params


def _extend_date_to(date_to: str) -> str:
    # A plain "YYYY-MM-DD" is a lexicographic prefix of that day's
    # timestamped rows, so a naive <= comparison would exclude them
    # (a prefix string sorts before the longer strings it prefixes).
    # Extend to end-of-day so the whole date_to day is included.
    return date_to if len(date_to) > 10 else f"{date_to} 23:59:59.999999"


def search_items_advanced(engine, query: str = "", exact: bool = False, regulator_ids=None,
                           date_from=None, date_to=None, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
    """List-view search: multi-term AND with quoted-phrase/exact-match support,
    multi-regulator OR, paginated.

    Distinct from search_items() (the MCP tool) so that tool's contract stays
    unchanged for existing MCP clients - this is used by the web UI.
    Returns (items, total_count).
    """
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    conditions, params = _search_conditions(query, exact, regulator_ids)
    if date_from:
        conditions.append("published_at >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("published_at <= :date_to")
        params["date_to"] = _extend_date_to(date_to)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "tags"]
    with engine.connect() as conn:
        total = conn.execute(text(f"SELECT COUNT(*) FROM items {where}"), params).scalar()
        rows = conn.execute(
            text(f"""
                SELECT id, regulator_id, source_label, title, url, published_at,
                       fetched_at, date_inferred, language, content_snippet, source_type, tags
                FROM items {where}
                ORDER BY published_at DESC NULLS LAST
                LIMIT :limit OFFSET :offset
            """),
            {**params, "limit": limit, "offset": offset},
        ).fetchall()
    return [_row_to_dict(r, keys) for r in rows], total


def get_month_facets(engine, query: str = "", exact: bool = False, regulator_ids=None) -> list[dict]:
    """Item counts grouped by publish month (YYYY-MM), for a month-picker sidebar.

    Applies the same search/regulator filters as search_items_advanced but
    deliberately ignores any date filter, so every month remains clickable
    regardless of the currently selected date range.
    """
    conditions, params = _search_conditions(query, exact, regulator_ids)
    conditions.append("published_at IS NOT NULL")
    where = "WHERE " + " AND ".join(conditions)
    sql = f"""
        SELECT strftime('%Y-%m', published_at) as month, COUNT(*) as count
        FROM items {where}
        GROUP BY month
        ORDER BY month DESC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [{"month": r[0], "count": r[1]} for r in rows]


def get_recent(engine, limit: int = 20, regulator_id=None, tags=None, language=None) -> list[dict]:
    try:
        limit = min(int(limit), 100)
    except (TypeError, ValueError):
        limit = 20
    conditions = []
    params = {"limit": limit}
    if regulator_id:
        conditions.append("regulator_id = :regulator_id")
        params["regulator_id"] = regulator_id
    if language:
        conditions.append("language = :language")
        params["language"] = language
    if tags:
        for i, tag in enumerate(tags):
            conditions.append(f"tags LIKE :rtag_{i} ESCAPE '!'")
            params[f"rtag_{i}"] = f'%"{_escape_like(tag)}"%'

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, date_inferred, language, content_snippet, source_type, tags
        FROM items {where}
        ORDER BY fetched_at DESC
        LIMIT :limit
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "tags"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def list_regulators(engine) -> list[dict]:
    sql = """
        SELECT r.id, r.name, r.full_name, r.jurisdiction, r.jurisdiction_type,
               r.website, r.mica_competent_authority, r.enabled,
               COUNT(i.id) as total_items,
               MAX(i.fetched_at) as last_fetched_at,
               COALESCE(se.consecutive_failures, 0) as consecutive_failures
        FROM regulators r
        LEFT JOIN items i ON i.regulator_id = r.id
        LEFT JOIN (
            SELECT regulator_id, MAX(consecutive_failures) as consecutive_failures
            FROM source_errors
            GROUP BY regulator_id
        ) se ON se.regulator_id = r.id
        GROUP BY r.id
        ORDER BY r.jurisdiction, r.name
    """
    keys = ["id", "name", "full_name", "jurisdiction", "jurisdiction_type",
            "website", "mica_competent_authority", "enabled",
            "total_items", "last_fetched_at", "consecutive_failures"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def get_item(engine, item_id: str) -> dict | None:
    sql = """
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, date_inferred, language, content_snippet, source_type, tags, raw_content
        FROM items WHERE id = :id
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "tags", "raw_content"]
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"id": item_id}).fetchone()
    if row is None:
        return None
    return _row_to_dict(row, keys)


def get_run_log(engine, limit: int = 10) -> list[dict]:
    try:
        limit = min(int(limit), 100)
    except (TypeError, ValueError):
        limit = 10
    sql = """
        SELECT run_id, started_at, completed_at, status,
               total_fetched, total_new, total_errors, error_detail
        FROM runs
        ORDER BY started_at DESC
        LIMIT :limit
    """
    keys = ["run_id", "started_at", "completed_at", "status",
            "total_fetched", "total_new", "total_errors", "error_detail"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"limit": limit}).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def get_warnings(engine, days: int = 30, regulator_id=None) -> list[dict]:
    try:
        days = max(0, min(int(days), 365))
    except (TypeError, ValueError):
        days = 30
    conditions = ['tags LIKE \'%"warnings"%\'']
    params: dict = {}
    if days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        conditions.append("(published_at >= :cutoff OR (published_at IS NULL AND fetched_at >= :cutoff))")
        params["cutoff"] = cutoff
    if regulator_id:
        conditions.append("regulator_id = :regulator_id")
        params["regulator_id"] = regulator_id

    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, language, content_snippet, tags
        FROM items WHERE {where}
        ORDER BY published_at DESC NULLS LAST
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "language", "content_snippet", "tags"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r, keys) for r in rows]
