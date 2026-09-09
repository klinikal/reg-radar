"""The classifications store and write path (architecture doc §4.4-§4.5).

`write_classification()` is the single write path - every caller (the
collector's source defaults, the migration of legacy content_tags, the
`reclassify` command, and human corrections via the web API) goes through
it. It validates the axis/value against the current taxonomy, appends a
`classifications` row, and re-materialises `items.labels` /
`items.updated_at` from the live rows via `materialize_labels()`.
Precedence is implemented once, in `effective_labels()`.
"""
import json
import logging

from sqlalchemy import text

from collector.storage import db_now

logger = logging.getLogger(__name__)

# Tie-break order for automated provenances on a single-valued axis, lowest
# first (architecture doc §4.4: "llm > rule > source_default"; §4.7 ranks
# `import` below source defaults so migrated legacy tags never outrank a
# fresh classification of any other kind).
_BY_RANK = {"import": 0, "source_default": 1, "rule": 2}


def is_human(by: str) -> bool:
    return by.startswith("human:")


def _by_rank(by: str) -> int:
    if by.startswith("llm:"):
        return 3
    return _BY_RANK.get(by, 0)


def winning_classification_rows(rows: list, taxonomy) -> list:
    """Returns the live classification rows that determine the effective
    label for each axis - one row per single-valued axis, one row per
    qualifying value on a multi-valued axis - applying the same precedence
    as effective_labels() (architecture doc §4.4) but returning the winning
    rows themselves (axis, value, by, confidence, taxonomy_version, ...)
    rather than just their values. This is what the canonical document's
    `label_provenance` (architecture doc §3.2) is built from.

    `rows` are dicts with at least axis/value/by/confidence/superseded_at;
    already-superseded rows are filtered out here so callers can pass a
    full history rather than pre-filtering themselves. Axes unknown to
    `taxonomy`, or values no longer in its vocab (e.g. a stale `import`
    row - see import_classification()), are ignored.
    """
    live = [r for r in rows if r.get("superseded_at") is None]
    by_axis: dict = {}
    for row in live:
        by_axis.setdefault(row["axis"], []).append(row)

    winners: list = []
    known_axes = set(taxonomy.axes())
    for axis, axis_rows in by_axis.items():
        if axis not in known_axes:
            continue
        cardinality = taxonomy.cardinality(axis)
        axis_rows = [r for r in axis_rows if taxonomy.is_valid(axis, r["value"])]
        human_rows = [r for r in axis_rows if is_human(r["by"])]
        if human_rows:
            if cardinality == "many":
                by_value = {}
                for r in human_rows:
                    by_value.setdefault(r["value"], r)
                winners.extend(by_value[v] for v in sorted(by_value))
            else:
                best_value = sorted({r["value"] for r in human_rows})[-1]
                winners.append(next(r for r in human_rows if r["value"] == best_value))
            continue

        threshold = taxonomy.min_confidence(axis)
        candidates = [r for r in axis_rows if (r.get("confidence") or 0) >= threshold]
        if not candidates:
            continue
        if cardinality == "many":
            by_value = {}
            for r in candidates:
                if r["value"] not in by_value or (r.get("confidence") or 0) > (by_value[r["value"]].get("confidence") or 0):
                    by_value[r["value"]] = r
            winners.extend(by_value[v] for v in sorted(by_value))
        else:
            best = max(candidates, key=lambda r: ((r.get("confidence") or 0), _by_rank(r["by"])))
            winners.append(best)
    return winners


def effective_labels(rows: list, taxonomy) -> dict:
    """Computes the effective per-axis labels for one item from its
    classification rows, per architecture doc §4.4:

    1. Any live `human:*` row wins outright for that axis. For multi-valued
       axes, ALL live human rows for that axis together define the full set.
    2. Otherwise, live non-human rows with confidence >= the axis's
       min_confidence are considered: multi-valued axes take the union;
       single-valued axes take the highest confidence, ties broken by
       llm > rule > source_default > import.
    3. Otherwise the axis is unset - simply absent from the result dict.
       Unset is a real state, not an error.

    Built from winning_classification_rows() - see that function for the
    full precedence rules and row-shape requirements.
    """
    result: dict = {}
    for w in winning_classification_rows(rows, taxonomy):
        axis = w["axis"]
        if taxonomy.cardinality(axis) == "many":
            result.setdefault(axis, []).append(w["value"])
        else:
            result[axis] = w["value"]
    return result


def _flatten(labels: dict):
    for axis, value in labels.items():
        if isinstance(value, list):
            for v in value:
                yield axis, v
        else:
            yield axis, value


def _materialize_labels_conn(conn, taxonomy, item_id: str) -> dict:
    """Core of materialize_labels() - runs entirely on the given open
    connection, no commit. Shared by materialize_labels() (its own
    connection, commits itself) and classify_batch() (one connection/
    transaction for the whole batch, per architecture doc §4.5's
    transactional write interface)."""
    rows = conn.execute(
        text("SELECT axis, value, by, confidence, superseded_at FROM classifications WHERE item_id = :id"),
        {"id": item_id},
    ).fetchall()
    row_dicts = [
        {"axis": r[0], "value": r[1], "by": r[2], "confidence": r[3], "superseded_at": r[4]}
        for r in rows
    ]
    labels = effective_labels(row_dicts, taxonomy)
    now = db_now()
    conn.execute(
        text("UPDATE items SET labels = :labels, updated_at = :now WHERE id = :id"),
        {"labels": json.dumps(labels), "now": now, "id": item_id},
    )
    conn.execute(text("DELETE FROM item_labels WHERE item_id = :id"), {"id": item_id})
    for axis, value in _flatten(labels):
        conn.execute(
            text("INSERT OR IGNORE INTO item_labels (item_id, axis, value) VALUES (:i, :a, :v)"),
            {"i": item_id, "a": axis, "v": value},
        )
    return labels


def materialize_labels(engine, taxonomy, item_id: str) -> dict:
    """Recomputes effective_labels() for one item from its full
    classification history and writes items.labels (JSON) /
    items.updated_at, plus rebuilds its rows in the item_labels side table
    (the query-speed index recommended in architecture doc §4.4 - see
    docs/issues/03-*.md's "Design notes" and the measurement in the task
    report)."""
    with engine.connect() as conn:
        labels = _materialize_labels_conn(conn, taxonomy, item_id)
        conn.commit()
    return labels


def _write_classification_row_conn(conn, taxonomy, item_id: str, axis: str, value: str, by: str,
                                    confidence: float, classifier_id: str | None) -> bool:
    """Core of write_classification() - appends the row (or supersedes a
    prior same-provenance one) entirely on the given open connection, no
    commit and no materialize call. Returns False if an identical live row
    already covered this (nothing to write), True otherwise. Shared by
    write_classification() (its own connection, commits itself) and
    classify_batch() (one connection/transaction for the whole batch).

    Caller must have already validated axis/value against the taxonomy -
    this assumes both are valid. `item_id` is checked here, on the shared
    path, because a typo'd or stale id otherwise inserted an orphan
    classification row against an item that does not exist and reported it
    as a successful write.
    """
    if conn.execute(text("SELECT 1 FROM items WHERE id=:i"), {"i": item_id}).fetchone() is None:
        raise ValueError(f"unknown item {item_id!r}")
    cardinality = taxonomy.cardinality(axis)
    now = db_now()
    existing = conn.execute(
        text(
            "SELECT id, by, value FROM classifications "
            "WHERE item_id=:i AND axis=:a AND superseded_at IS NULL"
        ),
        {"i": item_id, "a": axis},
    ).fetchall()

    for row_id, existing_by, existing_value in existing:
        same_provenance = (is_human(existing_by) and is_human(by)) or existing_by == by
        if not same_provenance:
            continue
        if existing_value == value:
            return False  # identical live row already covers this
        if cardinality == "one":
            conn.execute(
                text("UPDATE classifications SET superseded_at=:now WHERE id=:id"),
                {"now": now, "id": row_id},
            )

    conn.execute(
        text(
            "INSERT INTO classifications (item_id, axis, value, by, confidence, "
            "taxonomy_version, classifier_id, created_at, superseded_at) "
            "VALUES (:item_id, :axis, :value, :by, :confidence, :taxonomy_version, "
            ":classifier_id, :created_at, NULL)"
        ),
        {
            "item_id": item_id, "axis": axis, "value": value, "by": by,
            "confidence": confidence, "taxonomy_version": taxonomy.version(),
            "classifier_id": classifier_id, "created_at": now,
        },
    )
    return True


def write_classification(engine, taxonomy, item_id: str, axis: str, value: str, by: str,
                          confidence: float, classifier_id: str | None = None) -> None:
    """Validates axis/value against the current taxonomy (raises ValueError
    on an unknown axis or value), appends a live classifications row, and
    re-materialises items.labels/updated_at.

    A new row supersedes prior live rows of the *same provenance* (both
    `human:*`, or an exact `by` match) for that (item, axis) when the axis
    is single-valued, so a correction replaces rather than accumulates.
    Multi-valued axes never supersede on write - adding is additive; use
    remove_classification() to retract one value. Writing an identical live
    row again is a no-op.
    """
    if axis not in taxonomy.axes():
        raise ValueError(f"unknown axis {axis!r} - valid axes: {sorted(taxonomy.axes())}")
    if not taxonomy.is_valid(axis, value):
        raise ValueError(f"unknown value {value!r} for axis {axis!r}")

    with engine.connect() as conn:
        changed = _write_classification_row_conn(conn, taxonomy, item_id, axis, value, by, confidence, classifier_id)
        conn.commit()

    if changed:
        materialize_labels(engine, taxonomy, item_id)


def remove_classification(engine, taxonomy, item_id: str, axis: str, value: str, by: str) -> bool:
    """Supersedes one live row (item_id, axis, value, by exact match).
    Returns True if a row was found and superseded. Used by the web UI's
    "remove instrument" control on a multi-valued axis."""
    now = db_now()
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "UPDATE classifications SET superseded_at=:now "
                "WHERE item_id=:i AND axis=:a AND value=:v AND by=:b AND superseded_at IS NULL"
            ),
            {"now": now, "i": item_id, "a": axis, "v": value, "b": by},
        )
        conn.commit()
        found = result.rowcount > 0
    if found:
        materialize_labels(engine, taxonomy, item_id)
    return found


def import_classification(engine, item_id: str, axis: str, value: str, taxonomy_version: int,
                           confidence: float = 0.5, classifier_id: str | None = None,
                           by: str = "import") -> None:
    """Inserts one classification row WITHOUT taxonomy validation - used by
    scripts/migrate_content_tags.py (always `by="import"`, the default) to
    preserve legacy content_tags as history even where a value no longer
    maps to a current taxonomy value (architecture doc §4.7: imported rows
    "survive but rank below anything better"; effective_labels() already
    ignores axes/values it doesn't recognise, so an unmappable import is
    inert rather than an error), and by regradar/dataset.py's
    `import-dataset` command, which passes the original `by` recorded in a
    published dataset's `label_provenance` (source_default/rule/llm:*/
    human:*) so a re-imported classification keeps its real provenance
    rather than being flattened to "import". Does not supersede anything
    and does not materialise labels itself - call materialize_labels() once
    per item after importing all of its tags.
    """
    now = db_now()
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO classifications (item_id, axis, value, by, confidence, "
                "taxonomy_version, classifier_id, created_at, superseded_at) "
                "VALUES (:item_id, :axis, :value, :by, :confidence, :taxonomy_version, "
                ":classifier_id, :created_at, NULL)"
            ),
            {
                "item_id": item_id, "axis": axis, "value": value, "by": by, "confidence": confidence,
                "taxonomy_version": taxonomy_version, "classifier_id": classifier_id, "created_at": now,
            },
        )
        conn.commit()


def supersede_axis(engine, item_id: str, axis: str, by: str | None = None) -> int:
    """Supersedes all live non-human rows for (item_id, axis), optionally
    restricted to a specific `by` value. Human rows are never touched by
    any automated path (architecture doc §4.4). Returns the number of rows
    superseded. Used by `regradar reclassify`."""
    now = db_now()
    conditions = "item_id=:i AND axis=:a AND superseded_at IS NULL AND by NOT LIKE 'human:%'"
    params = {"now": now, "i": item_id, "a": axis}
    if by:
        conditions += " AND by=:b"
        params["b"] = by
    with engine.connect() as conn:
        result = conn.execute(text(f"UPDATE classifications SET superseded_at=:now WHERE {conditions}"), params)
        conn.commit()
        return result.rowcount


MAX_BATCH_SIZE = 100


def _known_item_ids(engine, item_ids: list) -> set:
    """Which of `item_ids` actually exist in `items` - one query, so
    classify_batch() can report an unknown id per entry during validation
    instead of raising inside the batch transaction."""
    wanted = [i for i in dict.fromkeys(item_ids) if i]
    if not wanted:
        return set()
    placeholders = ", ".join(f":i_{n}" for n in range(len(wanted)))
    params = {f"i_{n}": v for n, v in enumerate(wanted)}
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT id FROM items WHERE id IN ({placeholders})"), params).fetchall()
    return {r[0] for r in rows}


def classify_batch(engine, taxonomy, classifications: list) -> dict:
    """Batch write interface (docs/issues/04-rules-engine-write-interface
    -llm-classifier-skill.md): validates every entry against the current
    taxonomy before writing any of them, so one bad entry in a large batch
    (e.g. an agent working through 100 unclassified items) never loses the
    rest. Returns {"written": [item_id, ...], "errors": [{"index", "item_id",
    "error"}, ...]} - `written` lists item_ids for entries that were
    validated and written (each via write_classification(), so the same
    provenance-supersession rules apply); `errors` covers every entry that
    failed validation, by its position in the input list.

    Each entry is a dict: {"item_id", "axis", "value", "by",
    "confidence"?: float (default 1.0), "classifier_id"?: str}.

    Raises ValueError if `classifications` has more than MAX_BATCH_SIZE (100)
    entries - a caller-side mistake (e.g. dumping an unpaged query straight
    in), not a per-entry validation problem, so it fails the whole call
    rather than producing 100+ per-entry errors.

    Transactional per architecture doc §4.5: every valid entry is written
    (row insert/supersede plus its item's materialize_labels()) inside ONE
    connection/transaction - if a write fails partway through, the whole
    transaction rolls back and NONE of the batch's rows are written, rather
    than leaving a partial batch committed. Validation happens entirely
    before this transaction opens, so a bad *value* never reaches it (that's
    reported per-entry in `errors`, not a rollback case).
    """
    if len(classifications) > MAX_BATCH_SIZE:
        raise ValueError(f"batch of {len(classifications)} exceeds the {MAX_BATCH_SIZE}-entry limit")

    known_item_ids = _known_item_ids(engine, [e.get("item_id") for e in classifications])
    validated = []
    errors = []
    for i, entry in enumerate(classifications):
        item_id = entry.get("item_id")
        axis = entry.get("axis")
        value = entry.get("value")
        by = entry.get("by")
        if not item_id or not axis or not value or not by:
            errors.append({"index": i, "item_id": item_id, "error": "item_id, axis, value and by are all required"})
            continue
        if axis not in taxonomy.axes():
            errors.append({"index": i, "item_id": item_id, "error": f"unknown axis {axis!r}"})
            continue
        if not taxonomy.is_valid(axis, value):
            errors.append({"index": i, "item_id": item_id, "error": f"unknown value {value!r} for axis {axis!r}"})
            continue
        if item_id not in known_item_ids:
            # Reported per entry like any other validation failure, so one
            # stale id never rolls back the rest of a batch.
            errors.append({"index": i, "item_id": item_id, "error": f"unknown item {item_id!r}"})
            continue
        try:
            confidence = float(entry.get("confidence", 1.0))
        except (TypeError, ValueError):
            errors.append({"index": i, "item_id": item_id, "error": "confidence must be a number"})
            continue
        if not (0.0 <= confidence <= 1.0):
            errors.append({"index": i, "item_id": item_id, "error": "confidence must be between 0.0 and 1.0"})
            continue
        validated.append((item_id, axis, value, by, confidence, entry.get("classifier_id")))

    written = []
    if validated:
        # engine.begin(): one transaction for the whole batch - commits once
        # at the end of the `with` block, or rolls back everything if any
        # write below raises (a DB-level failure, not a validation one -
        # those are already filtered into `errors` above).
        with engine.begin() as conn:
            changed_item_ids = []
            for item_id, axis, value, by, confidence, classifier_id in validated:
                changed = _write_classification_row_conn(
                    conn, taxonomy, item_id, axis, value, by, confidence, classifier_id
                )
                written.append(item_id)
                if changed:
                    changed_item_ids.append(item_id)
            for item_id in dict.fromkeys(changed_item_ids):  # de-duped, order-preserved
                _materialize_labels_conn(conn, taxonomy, item_id)

    return {"written": written, "errors": errors}
