import json
from datetime import datetime, timezone, timedelta

from sqlalchemy import text


def _row_to_dict(row, keys):
    return dict(zip(keys, row))


def search_items(engine, query: str, regulator_id=None, language=None, tags=None, date_from=None, date_to=None, limit: int = 20) -> list[dict]:
    limit = min(int(limit), 100)
    conditions = ["(title LIKE :q OR content_snippet LIKE :q)"]
    params = {"q": f"%{query}%", "limit": limit}
    if regulator_id:
        conditions.append("regulator_id = :regulator_id")
        params["regulator_id"] = regulator_id
    if language:
        conditions.append("language = :language")
        params["language"] = language
    if date_from:
        conditions.append("published_at >= :date_from")
        params["date_from"] = date_from
    if date_to:
        conditions.append("published_at <= :date_to")
        params["date_to"] = date_to

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


def get_recent(engine, limit: int = 20, regulator_id=None, tags=None, language=None) -> list[dict]:
    limit = min(int(limit), 100)
    conditions = []
    params = {"limit": limit}
    if regulator_id:
        conditions.append("regulator_id = :regulator_id")
        params["regulator_id"] = regulator_id
    if language:
        conditions.append("language = :language")
        params["language"] = language

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
    results = [_row_to_dict(r, keys) for r in rows]

    if tags:
        filtered = []
        for r in results:
            item_tags = json.loads(r["tags"] or "[]")
            if any(t in item_tags for t in tags):
                filtered.append(r)
        return filtered
    return results


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
    limit = min(int(limit), 100)
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
    days = min(int(days), 365)
    conditions = ["tags LIKE '%warnings%'"]
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
