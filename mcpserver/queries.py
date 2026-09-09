import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timezone, timedelta

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from regradar.classification import winning_classification_rows
from regradar.document import build_frontmatter, frontmatter_to_json_safe
from regradar.profiles import DOCUMENT_TYPE_AXIS, INSTRUMENT_AXIS

logger = logging.getLogger(__name__)

# Consecutive zero-item runs (despite a successful fetch) before a source is
# considered stalled rather than just having a quiet day - see
# collector.storage.record_source_empty_run. Env-configurable to match the
# FLARESOLVERR_* convention.
STALL_THRESHOLD_RUNS = int(os.getenv("STALL_THRESHOLD_RUNS", "7"))


def _row_to_dict(row, keys):
    return dict(zip(keys, row))


def _escape_like(value: str) -> str:
    # Escape LIKE special chars using ! as the escape character.
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _format_dt(dt: datetime) -> str:
    """Format a datetime to match SQLAlchemy's SQLite DATETIME storage format
    ("YYYY-MM-DD HH:MM:SS.ffffff", space-separated, no timezone offset), so
    string comparisons against published_at/fetched_at columns are correct.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


def _tag_filter_conditions(tags, source_tags, content_tags, prefix: str = "") -> tuple[list[str], dict]:
    """Builds SQL conditions/params for the three tag-filter parameter shapes
    shared by search_items() and get_recent(): `tags` searches source_tags OR
    content_tags combined (backward-compatible default), `source_tags` and
    `content_tags` each filter their own column specifically."""
    if tags or source_tags or content_tags:
        logger.warning(
            "Query used deprecated tags/source_tags/content_tags filter - these read the old "
            "flat columns; prefer jurisdictions/labels, which read the classification store "
            "with provenance (architecture doc §4.4)."
        )
    conditions = []
    params = {}
    if tags:
        for i, tag in enumerate(tags):
            key = f"{prefix}tag_{i}"
            conditions.append(f"(source_tags LIKE :{key} ESCAPE '!' OR content_tags LIKE :{key} ESCAPE '!')")
            params[key] = f'%"{_escape_like(tag)}"%'
    if source_tags:
        for i, tag in enumerate(source_tags):
            key = f"{prefix}stag_{i}"
            conditions.append(f"source_tags LIKE :{key} ESCAPE '!'")
            params[key] = f'%"{_escape_like(tag)}"%'
    if content_tags:
        for i, tag in enumerate(content_tags):
            key = f"{prefix}ctag_{i}"
            conditions.append(f"content_tags LIKE :{key} ESCAPE '!'")
            params[key] = f'%"{_escape_like(tag)}"%'
    return conditions, params


def _sanitize_fts_terms(terms: list[str]) -> str:
    quoted = []
    for term in terms:
        if term:
            escaped = term.replace('"', '""')
            quoted.append(f'"{escaped}"*')
    return " ".join(quoted)


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


def search_items(engine, query: str, regulator_id=None, language=None, tags=None, source_tags=None,
                  content_tags=None, jurisdictions=None, labels=None, taxonomy=None, profile=None,
                  date_from=None, date_to=None, limit: int = 20, offset: int = 0) -> list[dict]:
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    try:
        offset = max(0, int(offset))
    except (TypeError, ValueError):
        offset = 0

    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "source_tags", "content_tags"]

    def _run(use_fts: bool):
        terms = _parse_search_terms(query, exact=False)
        if use_fts:
            conditions = ["id IN (SELECT item_id FROM items_fts WHERE items_fts MATCH :fts_q)"] if terms else []
            params = {"fts_q": _sanitize_fts_terms(terms), "limit": limit, "offset": offset} if terms else {"limit": limit, "offset": offset}
        else:
            # Also checks raw_content, not just the 500-char content_snippet: a
            # multi-entity document (e.g. a regulator's batch warning notice naming
            # several firms) can easily exceed that truncation, leaving later-listed
            # entities unsearchable via title/snippet alone even though the full text
            # is stored. Widening the search only adds matches, never removes any.
            conditions = []
            params = {"limit": limit, "offset": offset}
            for i, term in enumerate(terms):
                conditions.append(
                    f"(title LIKE :q_{i} ESCAPE '!' OR content_snippet LIKE :q_{i} ESCAPE '!' OR raw_content LIKE :q_{i} ESCAPE '!')"
                )
                params[f"q_{i}"] = f"%{_escape_like(term)}%"
        if regulator_id:
            conditions.append("regulator_id = :regulator_id")
            params["regulator_id"] = regulator_id
        if language:
            conditions.append("language = :language")
            params["language"] = language
        tag_conditions, tag_params = _tag_filter_conditions(tags, source_tags, content_tags)
        conditions.extend(tag_conditions)
        params.update(tag_params)
        if jurisdictions and taxonomy is not None:
            jur_conditions, jur_params = _jurisdiction_filter_conditions(jurisdictions, taxonomy)
            conditions.extend(jur_conditions)
            params.update(jur_params)
        if labels:
            label_conditions, label_params = _labels_filter_conditions(labels)
            conditions.extend(label_conditions)
            params.update(label_params)
        if profile is not None:
            profile_conditions, profile_params = _profile_filter_conditions(profile)
            conditions.extend(profile_conditions)
            params.update(profile_params)
        if date_from:
            conditions.append("published_at >= :date_from")
            params["date_from"] = date_from
        if date_to:
            conditions.append("published_at <= :date_to")
            params["date_to"] = _extend_date_to(date_to)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT id, regulator_id, source_label, title, url, published_at,
                   fetched_at, date_inferred, language, content_snippet, source_type, source_tags, content_tags
            FROM items WHERE {where}
            ORDER BY published_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset
        """
        with engine.connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
        return [_row_to_dict(r, keys) for r in rows]

    if not query or not query.strip():
        return _run(use_fts=False)
    try:
        return _run(use_fts=True)
    except OperationalError as e:
        logger.warning("FTS query failed (%s), falling back to LIKE for query=%r", e, query)
        return _run(use_fts=False)


def _search_conditions(query: str, exact: bool, regulator_ids, use_fts: bool = True) -> tuple[list[str], dict]:
    """Shared query+regulator (not date) conditions, reused by search and month facets."""
    conditions = []
    params: dict = {}
    terms = _parse_search_terms(query, exact)
    if terms:
        if use_fts:
            fts_query = _sanitize_fts_terms(terms)
            conditions.append("id IN (SELECT item_id FROM items_fts WHERE items_fts MATCH :fts_q)")
            params["fts_q"] = fts_query
        else:
            for i, term in enumerate(terms):
                # Also checks raw_content - see search_items() for why
                # content_snippet alone isn't enough (multi-entity documents
                # can exceed its 500-char truncation).
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


def _jurisdiction_filter_conditions(jurisdictions, taxonomy, prefix: str = "") -> tuple[list[str], dict]:
    """Expands each requested jurisdiction through taxonomy.ancestors() (a
    profile selecting "fr" also matches "eu" - architecture doc §4.2) and
    builds a condition matching any item whose regulator's jurisdiction (or
    an ancestor of it) is in that expanded set."""
    if not jurisdictions:
        return [], {}
    expanded = set()
    for j in jurisdictions:
        j = j.lower()
        expanded.add(j)
        expanded.update(taxonomy.ancestors(j))
    params = {}
    placeholders = []
    for i, j in enumerate(sorted(expanded)):
        key = f"{prefix}jur_{i}"
        placeholders.append(f":{key}")
        params[key] = j
    condition = (
        "regulator_id IN (SELECT id FROM regulators WHERE LOWER(jurisdiction) IN "
        f"({', '.join(placeholders)}))"
    )
    return [condition], params


def _labels_filter_conditions(labels, prefix: str = "") -> tuple[list[str], dict]:
    """Builds one `id IN (SELECT item_id FROM item_labels WHERE axis=... AND
    value IN (...))` condition per axis in `labels` (ANDed across axes, ORed
    within an axis's values) - see regradar/classification.py's item_labels
    side table."""
    conditions = []
    params = {}
    for axis_i, (axis, values) in enumerate(sorted((labels or {}).items())):
        if not values:
            continue
        placeholders = []
        for i, value in enumerate(values):
            key = f"{prefix}lbl_{axis_i}_{i}"
            placeholders.append(f":{key}")
            params[key] = value
        axis_key = f"{prefix}lbl_axis_{axis_i}"
        params[axis_key] = axis
        conditions.append(
            f"id IN (SELECT item_id FROM item_labels WHERE axis=:{axis_key} AND value IN ({', '.join(placeholders)}))"
        )
    return conditions, params


def _profile_filter_conditions(expanded, prefix: str = "") -> tuple[list[str], dict]:
    """Builds the WHERE conditions for one expanded profile (architecture
    doc §5.3 step 3), shared by every query function that accepts a
    `profile` parameter. `expanded` is a regradar.profiles.ExpandedProfile -
    the union/intersection/exclusion is already computed once by
    expand_profile(); this only turns those sets into SQL.

    Instrument matching is "intersects instrument_ids OR has no live
    instrument label and include_unclassified is true" - the "no live
    label" branch is its own `NOT IN (SELECT item_id FROM item_labels
    WHERE axis=...)` condition, never a non-matching LIKE, so it can never
    be conflated with a genuine mismatch (docs/issues/05-*.md design
    notes). If neither branch applies (no instruments selected in the
    profile and include_unclassified is false), nothing can match.
    """
    conditions = []
    params: dict = {}

    # Deliberately not calling _jurisdiction_filter_conditions() here: that
    # helper expands ancestors from a raw jurisdiction list itself, but
    # expanded.jurisdiction_ids is already the fully-expanded (ancestors +
    # descendants) set from expand_profile() - re-running ancestor
    # expansion over an already-closed set would be harmless but pointless,
    # and would need a taxonomy object this function doesn't otherwise
    # take. Same "regulator_id IN (SELECT ... WHERE LOWER(jurisdiction)
    # IN (...))" shape, just against a pre-computed set.
    if expanded.jurisdiction_ids:
        placeholders = []
        for i, j in enumerate(sorted(expanded.jurisdiction_ids)):
            key = f"{prefix}pjur_{i}"
            placeholders.append(f":{key}")
            params[key] = j
        conditions.append(
            "regulator_id IN (SELECT id FROM regulators WHERE LOWER(jurisdiction) IN "
            f"({', '.join(placeholders)}))"
        )

    instrument_branches = []
    if expanded.instrument_ids or expanded.include_unclassified:
        axis_key = f"{prefix}pinst_axis"
        params[axis_key] = INSTRUMENT_AXIS
        if expanded.instrument_ids:
            placeholders = []
            for i, inst in enumerate(sorted(expanded.instrument_ids)):
                key = f"{prefix}pinst_{i}"
                placeholders.append(f":{key}")
                params[key] = inst
            instrument_branches.append(
                f"id IN (SELECT item_id FROM item_labels WHERE axis=:{axis_key} AND value IN ({', '.join(placeholders)}))"
            )
        if expanded.include_unclassified:
            instrument_branches.append(f"id NOT IN (SELECT item_id FROM item_labels WHERE axis=:{axis_key})")
    conditions.append("(" + " OR ".join(instrument_branches) + ")" if instrument_branches else "1=0")

    if expanded.document_types:
        dt_conditions, dt_params = _labels_filter_conditions(
            {DOCUMENT_TYPE_AXIS: sorted(expanded.document_types)}, prefix=f"{prefix}pdt_"
        )
        conditions.extend(dt_conditions)
        params.update(dt_params)

    if expanded.regulators:
        placeholders = []
        for i, r in enumerate(sorted(expanded.regulators)):
            key = f"{prefix}preg_{i}"
            placeholders.append(f":{key}")
            params[key] = r
        conditions.append(f"regulator_id IN ({', '.join(placeholders)})")

    if expanded.languages:
        placeholders = []
        for i, lang in enumerate(sorted(expanded.languages)):
            key = f"{prefix}plang_{i}"
            placeholders.append(f":{key}")
            params[key] = lang
        conditions.append(f"language IN ({', '.join(placeholders)})")

    return conditions, params


def _extend_date_to(date_to: str) -> str:
    # A plain "YYYY-MM-DD" is a lexicographic prefix of that day's
    # timestamped rows, so a naive <= comparison would exclude them
    # (a prefix string sorts before the longer strings it prefixes).
    # Extend to end-of-day so the whole date_to day is included.
    return date_to if len(date_to) > 10 else f"{date_to} 23:59:59.999999"


def search_items_advanced(engine, query: str = "", exact: bool = False, regulator_ids=None,
                           date_from=None, date_to=None, profile=None,
                           limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
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

    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "source_tags", "content_tags"]

    def _run(use_fts: bool):
        conditions, params = _search_conditions(query, exact, regulator_ids, use_fts=use_fts)
        if date_from:
            conditions.append("published_at >= :date_from")
            params["date_from"] = date_from
        if date_to:
            conditions.append("published_at <= :date_to")
            params["date_to"] = _extend_date_to(date_to)
        if profile is not None:
            profile_conditions, profile_params = _profile_filter_conditions(profile)
            conditions.extend(profile_conditions)
            params.update(profile_params)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        with engine.connect() as conn:
            total = conn.execute(text(f"SELECT COUNT(*) FROM items {where}"), params).scalar()
            rows = conn.execute(
                text(f"""
                    SELECT id, regulator_id, source_label, title, url, published_at,
                           fetched_at, date_inferred, language, content_snippet, source_type, source_tags, content_tags
                    FROM items {where}
                    ORDER BY published_at DESC NULLS LAST
                    LIMIT :limit OFFSET :offset
                """),
                {**params, "limit": limit, "offset": offset},
            ).fetchall()
        return [_row_to_dict(r, keys) for r in rows], total

    try:
        return _run(use_fts=True)
    except OperationalError as e:
        logger.warning("FTS query failed (%s), falling back to LIKE for query=%r", e, query)
        return _run(use_fts=False)


def get_month_facets(engine, query: str = "", exact: bool = False, regulator_ids=None, profile=None) -> list[dict]:
    """Item counts grouped by publish month (YYYY-MM), for a month-picker sidebar.

    Applies the same search/regulator/profile filters as search_items_advanced
    but deliberately ignores any date filter, so every month remains clickable
    regardless of the currently selected date range.
    """
    def _run(use_fts: bool):
        conditions, params = _search_conditions(query, exact, regulator_ids, use_fts=use_fts)
        conditions.append("published_at IS NOT NULL")
        if profile is not None:
            profile_conditions, profile_params = _profile_filter_conditions(profile)
            conditions.extend(profile_conditions)
            params.update(profile_params)
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

    try:
        return _run(use_fts=True)
    except OperationalError as e:
        logger.warning("FTS query failed (%s), falling back to LIKE for query=%r", e, query)
        return _run(use_fts=False)


def get_recent(engine, limit: int = 20, regulator_id=None, tags=None, source_tags=None, content_tags=None,
                language=None, profile=None) -> list[dict]:
    try:
        limit = max(1, min(int(limit), 100))
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
    tag_conditions, tag_params = _tag_filter_conditions(tags, source_tags, content_tags)
    conditions.extend(tag_conditions)
    params.update(tag_params)
    if profile is not None:
        profile_conditions, profile_params = _profile_filter_conditions(profile)
        conditions.extend(profile_conditions)
        params.update(profile_params)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, date_inferred, language, content_snippet, source_type, source_tags, content_tags
        FROM items {where}
        ORDER BY fetched_at DESC
        LIMIT :limit
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "source_tags", "content_tags"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def list_regulators(engine) -> list[dict]:
    sql = """
        SELECT r.id, r.name, r.full_name, r.jurisdiction, r.jurisdiction_type,
               r.website, r.mica_competent_authority, r.enabled,
               COUNT(i.id) as total_items,
               MAX(i.fetched_at) as last_fetched_at,
               COALESCE(se.consecutive_failures, 0) as consecutive_failures,
               COALESCE(se.consecutive_empty_runs, 0) as consecutive_empty_runs
        FROM regulators r
        LEFT JOIN items i ON i.regulator_id = r.id
        LEFT JOIN (
            SELECT regulator_id, MAX(consecutive_failures) as consecutive_failures,
                   MAX(consecutive_empty_runs) as consecutive_empty_runs
            FROM source_errors
            GROUP BY regulator_id
        ) se ON se.regulator_id = r.id
        GROUP BY r.id
        ORDER BY r.jurisdiction, r.name
    """
    keys = ["id", "name", "full_name", "jurisdiction", "jurisdiction_type",
            "website", "mica_competent_authority", "enabled",
            "total_items", "last_fetched_at", "consecutive_failures", "consecutive_empty_runs"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()
    results = [_row_to_dict(r, keys) for r in rows]
    for r in results:
        # True if any source under this regulator has hit the stall
        # threshold (STALL_THRESHOLD_RUNS consecutive successful-but-empty
        # runs) - distinct from consecutive_failures, which only tracks
        # outright fetch errors.
        r["stalled"] = r.pop("consecutive_empty_runs") >= STALL_THRESHOLD_RUNS
    return results


def get_item(engine, item_id: str, taxonomy=None) -> dict | None:
    """Returns the item, including `body_md` (the Markdown body alongside
    the existing `raw_content`) and a `document` object holding the same
    frontmatter fields a rendered file would have (section 3.2) - built
    from whatever `labels`/artefact data exists for this item.

    `taxonomy` (a regradar.taxonomy.Taxonomy) is optional but needed to
    populate `document.jurisdiction_ancestors` (via ancestors()) and
    `document.label_provenance` (via winning_classification_rows() over
    this item's live classifications); without it those come back empty/
    omitted rather than erroring, so existing callers that don't have a
    taxonomy handy keep working.
    """
    sql = """
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, updated_at, date_inferred, language, content_snippet, source_type,
               source_tags, content_tags, labels, raw_content, body_md, extractor
        FROM items WHERE id = :id
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "updated_at", "date_inferred", "language", "content_snippet", "source_type",
            "source_tags", "content_tags", "labels", "raw_content", "body_md", "extractor"]
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"id": item_id}).fetchone()
        if row is None:
            return None
        item = _row_to_dict(row, keys)
        reg_row = conn.execute(
            text("SELECT name, full_name, jurisdiction FROM regulators WHERE id = :id"),
            {"id": item["regulator_id"]},
        ).fetchone()
        artefact_row = conn.execute(
            text("SELECT path, mime, sha256, bytes, fetched_at FROM item_artefacts WHERE item_id = :id"),
            {"id": item_id},
        ).fetchone()
        classification_rows = (
            conn.execute(
                text("SELECT axis, value, by, confidence, taxonomy_version, superseded_at "
                     "FROM classifications WHERE item_id = :id"),
                {"id": item_id},
            ).fetchall()
            if taxonomy is not None else []
        )

    doc_item = dict(item)
    jurisdiction = None
    if reg_row is not None:
        reg = dict(reg_row._mapping)
        doc_item["regulator_name"] = reg.get("full_name") or reg.get("name")
        jurisdiction = reg.get("jurisdiction")
        doc_item["jurisdiction"] = jurisdiction
    artefact = dict(artefact_row._mapping) if artefact_row is not None else None

    labels = None
    label_provenance = None
    if taxonomy is not None:
        if jurisdiction:
            doc_item["jurisdiction_ancestors"] = taxonomy.ancestors(jurisdiction.lower())
        labels = json.loads(item["labels"]) if item.get("labels") else None
        winners = winning_classification_rows([dict(r._mapping) for r in classification_rows], taxonomy)
        if winners:
            label_provenance = [
                {"axis": w["axis"], "value": w["value"], "by": w["by"],
                 "confidence": w["confidence"], "taxonomy_version": w["taxonomy_version"]}
                for w in winners
            ]

    fm = build_frontmatter(doc_item, artefact=artefact, labels=labels, label_provenance=label_provenance)
    item["document"] = frontmatter_to_json_safe(fm)
    return item


def get_run_log(engine, limit: int = 10) -> list[dict]:
    try:
        limit = max(1, min(int(limit), 100))
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


def get_warnings(engine, days: int = 30, regulator_id=None, limit: int = 50, profile=None) -> list[dict]:
    try:
        days = max(0, min(int(days), 365))
    except (TypeError, ValueError):
        days = 30
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 50
    logger.warning(
        "get_warnings() read the deprecated source_tags column - prefer filtering on "
        "labels={'document_type': ['warning']}, which reads the classification store."
    )
    conditions = ['source_tags LIKE \'%"warnings"%\'']
    params: dict = {"limit": limit}
    if days > 0:
        cutoff = _format_dt(datetime.now(timezone.utc) - timedelta(days=days))
        conditions.append("(published_at >= :cutoff OR (published_at IS NULL AND fetched_at >= :cutoff))")
        params["cutoff"] = cutoff
    if regulator_id:
        conditions.append("regulator_id = :regulator_id")
        params["regulator_id"] = regulator_id
    if profile is not None:
        profile_conditions, profile_params = _profile_filter_conditions(profile)
        conditions.extend(profile_conditions)
        params.update(profile_params)

    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, language, content_snippet, source_tags, content_tags
        FROM items WHERE {where}
        ORDER BY published_at DESC NULLS LAST
        LIMIT :limit
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "language", "content_snippet", "source_tags", "content_tags"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def list_tags(engine) -> dict:
    """Returns distinct source_tags and content_tags values with item
    counts, each bucket sorted most-common first."""
    logger.warning(
        "list_tags() read the deprecated source_tags/content_tags columns - prefer "
        "list_labels(), which reads the classification store with provenance."
    )
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT source_tags, content_tags FROM items")).fetchall()
    source_counts = Counter()
    content_counts = Counter()
    for source_tags_json, content_tags_json in rows:
        for tag in json.loads(source_tags_json or "[]"):
            source_counts[tag] += 1
        for tag in json.loads(content_tags_json or "[]"):
            content_counts[tag] += 1
    return {
        "source_tags": [{"tag": t, "count": c} for t, c in source_counts.most_common()],
        "content_tags": [{"tag": t, "count": c} for t, c in content_counts.most_common()],
    }


def list_labels(engine, taxonomy) -> dict:
    """Returns every axis known to the current taxonomy (config-driven -
    adding an axis to taxonomy.yaml makes it appear here with no code
    change) with its distinct values and item counts, most-common first.
    An axis with no live rows yet still appears, with an empty list."""
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT axis, value, COUNT(*) as c FROM item_labels GROUP BY axis, value")).fetchall()
    counts: dict = {}
    for axis, value, count in rows:
        counts.setdefault(axis, []).append((value, count))
    result = {}
    for axis in taxonomy.axes():
        values = sorted(counts.get(axis, []), key=lambda t: -t[1])
        result[axis] = [{"value": v, "count": c} for v, c in values]
    return result


def get_items_since(engine, since: str, limit: int = 100, profile=None) -> list[dict]:
    """Returns items with fetched_at strictly after `since`, oldest first -
    the natural polling primitive ("everything new since my last check").
    `since` should be a fetched_at value as returned by this or other tools
    (e.g. "2026-07-01 10:41:36.000000"); ISO 8601 with a "T" separator
    and/or trailing "Z" is also accepted and normalized.
    """
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit = 100
    raw = since.strip().replace("T", " ")
    if raw.endswith("Z"):
        raw = raw[:-1]
    try:
        since_dt = datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"could not parse 'since' timestamp: {since!r}")
    cutoff = _format_dt(since_dt)
    conditions = ["fetched_at > :cutoff"]
    params = {"cutoff": cutoff, "limit": limit}
    if profile is not None:
        profile_conditions, profile_params = _profile_filter_conditions(profile)
        conditions.extend(profile_conditions)
        params.update(profile_params)
    where = " AND ".join(conditions)
    sql = f"""
        SELECT id, regulator_id, source_label, title, url, published_at,
               fetched_at, date_inferred, language, content_snippet, source_type, source_tags, content_tags
        FROM items WHERE {where}
        ORDER BY fetched_at ASC
        LIMIT :limit
    """
    keys = ["id", "regulator_id", "source_label", "title", "url", "published_at",
            "fetched_at", "date_inferred", "language", "content_snippet", "source_type", "source_tags", "content_tags"]
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).fetchall()
    return [_row_to_dict(r, keys) for r in rows]


def get_health(engine) -> dict:
    """Returns the most recent collector run's status and how long ago it
    completed (or started, if still running), so callers can caveat "data
    may be stale" without separately calling get_run_log and doing the date
    math themselves. Also surfaces db_integrity - the collector runs
    PRAGMA integrity_check once per collection cycle specifically so
    corruption is caught here loudly, not discovered later as a failed
    search.

    `stalled_sources` lists every regulator_id/source_label that has hit
    STALL_THRESHOLD_RUNS (default 7, env-configurable) consecutive
    successful-but-empty fetches - the "200 OK, selectors matching nothing"
    case a single run's zero-items check can't distinguish from a quiet day.
    """
    sql = """
        SELECT run_id, started_at, completed_at, status, total_fetched, total_new, total_errors, db_integrity_ok
        FROM runs ORDER BY started_at DESC LIMIT 1
    """
    keys = ["run_id", "started_at", "completed_at", "status", "total_fetched", "total_new", "total_errors", "db_integrity_ok"]
    stalled_sql = """
        SELECT regulator_id, source_label, consecutive_empty_runs
        FROM source_errors WHERE consecutive_empty_runs >= :threshold
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql)).fetchone()
        stalled_rows = conn.execute(text(stalled_sql), {"threshold": STALL_THRESHOLD_RUNS}).fetchall()
    stalled_sources = [
        {"regulator_id": r[0], "source_label": r[1], "consecutive_empty_runs": r[2]}
        for r in stalled_rows
    ]
    if row is None:
        return {"status": "no_runs", "latest_run": None, "age_seconds": None, "db_integrity": "unknown",
                "stalled_sources": stalled_sources}
    latest = _row_to_dict(row, keys)
    reference = latest["completed_at"] or latest["started_at"]
    age_seconds = None
    if reference:
        ref_dt = datetime.fromisoformat(str(reference).replace("T", " ")).replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - ref_dt).total_seconds()
    integrity_flag = latest.pop("db_integrity_ok")
    if integrity_flag is None:
        db_integrity = "unknown"
    else:
        db_integrity = "ok" if integrity_flag else "corrupted"
    return {"status": latest["status"], "latest_run": latest, "age_seconds": age_seconds, "db_integrity": db_integrity,
            "stalled_sources": stalled_sources}
