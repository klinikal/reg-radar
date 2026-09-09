import tempfile
from datetime import datetime, timezone

import pytest

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from regradar.classification import (
    effective_labels,
    import_classification,
    materialize_labels,
    remove_classification,
    supersede_axis,
    write_classification,
)
from regradar.taxonomy import Taxonomy, Axis, AxisValue


def _make_taxonomy():
    document_type = Axis(
        id="document_type", cardinality="one",
        values={
            "warning": AxisValue(id="warning", name="Warning"),
            "press_release": AxisValue(id="press_release", name="Press release"),
            "consultation": AxisValue(id="consultation", name="Consultation"),
        },
    )
    instrument = Axis(
        id="instrument", cardinality="many",
        values={
            "mica": AxisValue(id="mica", name="MiCA"),
            "dora": AxisValue(id="dora", name="DORA"),
            "mifid2": AxisValue(id="mifid2", name="MiFID II"),
        },
    )
    return Taxonomy(1, {"document_type": document_type, "instrument": instrument}, {})


def row(axis, value, by, confidence, superseded_at=None):
    return {"axis": axis, "value": value, "by": by, "confidence": confidence, "superseded_at": superseded_at}


# --- effective_labels() case table (docs/issues/03-*.md scope) -------------

def test_human_override_wins_regardless_of_confidence():
    taxonomy = _make_taxonomy()
    rows = [
        row("document_type", "press_release", "rule", 0.95),
        row("document_type", "warning", "human:alice", 0.0),
    ]
    assert effective_labels(rows, taxonomy) == {"document_type": "warning"}


def test_multi_valued_axis_takes_union_of_live_qualifying_rows():
    taxonomy = _make_taxonomy()
    rows = [
        row("instrument", "mica", "rule", 0.7),
        row("instrument", "dora", "source_default", 0.6),
    ]
    assert effective_labels(rows, taxonomy) == {"instrument": ["dora", "mica"]}


def test_confidence_below_threshold_is_excluded():
    taxonomy = _make_taxonomy()
    rows = [row("document_type", "warning", "rule", 0.4)]  # below default 0.5 threshold
    assert effective_labels(rows, taxonomy) == {}


def test_confidence_at_threshold_is_included():
    taxonomy = _make_taxonomy()
    rows = [row("document_type", "warning", "rule", 0.5)]
    assert effective_labels(rows, taxonomy) == {"document_type": "warning"}


def test_single_valued_tie_broken_by_provenance_llm_beats_rule_beats_source_default():
    taxonomy = _make_taxonomy()
    rows = [
        row("document_type", "press_release", "source_default", 0.8),
        row("document_type", "consultation", "rule", 0.8),
        row("document_type", "warning", "llm:anthropic/claude", 0.8),
    ]
    assert effective_labels(rows, taxonomy) == {"document_type": "warning"}


def test_single_valued_tie_broken_by_provenance_rule_beats_source_default():
    taxonomy = _make_taxonomy()
    rows = [
        row("document_type", "press_release", "source_default", 0.8),
        row("document_type", "consultation", "rule", 0.8),
    ]
    assert effective_labels(rows, taxonomy) == {"document_type": "consultation"}


def test_higher_confidence_wins_over_provenance_rank():
    taxonomy = _make_taxonomy()
    rows = [
        row("document_type", "warning", "source_default", 0.9),
        row("document_type", "consultation", "llm:anthropic/claude", 0.6),
    ]
    assert effective_labels(rows, taxonomy) == {"document_type": "warning"}


def test_all_rows_superseded_gives_unset():
    taxonomy = _make_taxonomy()
    now = datetime.now(timezone.utc)
    rows = [
        row("document_type", "warning", "rule", 0.9, superseded_at=now),
        row("instrument", "mica", "source_default", 0.6, superseded_at=now),
    ]
    assert effective_labels(rows, taxonomy) == {}


def test_import_rows_rank_below_everything_else():
    taxonomy = _make_taxonomy()
    rows = [
        row("document_type", "consultation", "import", 0.5),
        row("document_type", "warning", "source_default", 0.5),
    ]
    assert effective_labels(rows, taxonomy) == {"document_type": "warning"}


def test_unknown_axis_in_rows_is_ignored():
    taxonomy = _make_taxonomy()
    rows = [row("not_a_real_axis", "x", "rule", 0.9)]
    assert effective_labels(rows, taxonomy) == {}


def test_human_rows_on_multi_valued_axis_form_the_full_union_set():
    taxonomy = _make_taxonomy()
    rows = [
        row("instrument", "mica", "human:alice", 1.0),
        row("instrument", "dora", "human:bob", 1.0),
        row("instrument", "mifid2", "rule", 0.9),  # non-human ignored once any human row is live
    ]
    assert effective_labels(rows, taxonomy) == {"instrument": ["dora", "mica"]}


# --- write_classification() / materialize_labels() / DB integration -------

def _engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _insert_item(engine, item_id="i1"):
    now = datetime.now(timezone.utc)
    item = Item(
        id=item_id, regulator_id="esma", source_label="all_news", title="MiCA update",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="MiCA update", source_type="rss",
    )
    insert_item(engine, item)
    return item


def test_write_classification_rejects_unknown_axis():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    with pytest.raises(ValueError, match="unknown axis"):
        write_classification(engine, taxonomy, "i1", "not_an_axis", "x", by="rule", confidence=0.9)


def test_write_classification_rejects_unknown_value():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    with pytest.raises(ValueError, match="unknown value"):
        write_classification(engine, taxonomy, "i1", "document_type", "not_a_value", by="rule", confidence=0.9)


def test_write_classification_materialises_items_labels_and_updated_at():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        labels, updated_at = conn.execute(
            sa_text("SELECT labels, updated_at FROM items WHERE id='i1'")
        ).fetchone()
    import json
    assert json.loads(labels) == {"document_type": "warning"}
    assert updated_at is not None


def test_write_classification_single_valued_axis_supersedes_same_provenance():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i1", "document_type", "consultation", by="rule", confidence=0.9)

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        live = conn.execute(
            sa_text("SELECT value FROM classifications WHERE item_id='i1' AND superseded_at IS NULL")
        ).fetchall()
    assert [r[0] for r in live] == ["consultation"]


def test_human_correction_survives_reclassify_of_non_human_rows():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    write_classification(engine, taxonomy, "i1", "document_type", "press_release", by="source_default", confidence=1.0)
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="human:alice", confidence=1.0)

    # A reclassify-style supersede of non-human rows must never touch the human row.
    supersede_axis(engine, "i1", "document_type", by="source_default")
    write_classification(engine, taxonomy, "i1", "document_type", "consultation", by="source_default", confidence=1.0)

    labels = materialize_labels(engine, taxonomy, "i1")
    assert labels == {"document_type": "warning"}


def test_remove_classification_supersedes_one_value_on_multi_valued_axis():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    write_classification(engine, taxonomy, "i1", "instrument", "mica", by="human:alice", confidence=1.0)
    write_classification(engine, taxonomy, "i1", "instrument", "dora", by="human:alice", confidence=1.0)

    found = remove_classification(engine, taxonomy, "i1", "instrument", "mica", by="human:alice")
    assert found is True

    labels = materialize_labels(engine, taxonomy, "i1")
    assert labels == {"instrument": ["dora"]}


def test_item_labels_side_table_matches_materialised_labels():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    write_classification(engine, taxonomy, "i1", "instrument", "mica", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i1", "instrument", "dora", by="rule", confidence=0.9)

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        rows = conn.execute(
            sa_text("SELECT axis, value FROM item_labels WHERE item_id='i1' ORDER BY value")
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("instrument", "dora"), ("instrument", "mica")]


def test_import_classification_bypasses_validation_and_is_inert_for_unknown_value():
    engine = _engine()
    _insert_item(engine)
    taxonomy = _make_taxonomy()
    import_classification(engine, "i1", "instrument", "some-old-tag-not-in-vocab", taxonomy.version())
    labels = materialize_labels(engine, taxonomy, "i1")
    # Not a recognised instrument value, so it never surfaces in effective labels...
    assert labels == {}

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        count = conn.execute(
            sa_text("SELECT COUNT(*) FROM classifications WHERE item_id='i1' AND by='import'")
        ).scalar()
    # ...but the historical row itself is preserved.
    assert count == 1


# --- classify_batch (docs/issues/04-rules-engine-write-interface-llm-classifier-skill.md) ---

def test_classify_batch_writes_valid_entries_and_reports_invalid():
    from regradar.classification import classify_batch
    engine = _engine()
    _insert_item(engine, "i1")
    _insert_item(engine, "i2")
    taxonomy = _make_taxonomy()

    result = classify_batch(engine, taxonomy, [
        {"item_id": "i1", "axis": "document_type", "value": "warning", "by": "human:alice"},
        {"item_id": "i2", "axis": "document_type", "value": "press_release", "by": "human:alice"},
        {"item_id": "i1", "axis": "document_type", "value": "not-a-real-value", "by": "human:alice"},
    ])

    assert sorted(result["written"]) == ["i1", "i2"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 2

    with engine.connect() as conn:
        from sqlalchemy import text as sa_text
        rows = conn.execute(sa_text(
            "SELECT item_id, value FROM classifications WHERE superseded_at IS NULL ORDER BY item_id"
        )).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("i1", "warning"), ("i2", "press_release")]


def test_classify_batch_rejects_missing_required_fields():
    from regradar.classification import classify_batch
    engine = _engine()
    _insert_item(engine, "i1")
    taxonomy = _make_taxonomy()

    result = classify_batch(engine, taxonomy, [{"item_id": "i1", "axis": "document_type", "value": "warning"}])
    assert result["written"] == []
    assert "by" in result["errors"][0]["error"] or "required" in result["errors"][0]["error"]


def test_classify_batch_rejects_out_of_range_confidence():
    from regradar.classification import classify_batch
    engine = _engine()
    _insert_item(engine, "i1")
    taxonomy = _make_taxonomy()

    result = classify_batch(engine, taxonomy, [
        {"item_id": "i1", "axis": "document_type", "value": "warning", "by": "human:alice", "confidence": 1.5},
    ])
    assert result["written"] == []
    assert len(result["errors"]) == 1


def test_classify_batch_over_max_size_raises():
    from regradar.classification import MAX_BATCH_SIZE, classify_batch
    engine = _engine()
    taxonomy = _make_taxonomy()
    entries = [{"item_id": "i", "axis": "document_type", "value": "warning", "by": "rule"}] * (MAX_BATCH_SIZE + 1)
    with pytest.raises(ValueError):
        classify_batch(engine, taxonomy, entries)


def test_classify_batch_rolls_back_all_writes_on_mid_batch_failure(monkeypatch):
    """A DB-level failure on the second entry's write must roll back the
    first entry's write too - classify_batch's transactional guarantee
    (architecture doc §4.5) is "all valid entries written, or none are",
    not "whatever happened to complete before the failure"."""
    import regradar.classification as classification_module

    engine = _engine()
    _insert_item(engine, "i1")
    _insert_item(engine, "i2")
    taxonomy = _make_taxonomy()

    real_write = classification_module._write_classification_row_conn
    calls = {"n": 0}

    def failing_write(conn, taxonomy_arg, item_id, axis, value, by, confidence, classifier_id):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated DB failure on the second write")
        return real_write(conn, taxonomy_arg, item_id, axis, value, by, confidence, classifier_id)

    monkeypatch.setattr(classification_module, "_write_classification_row_conn", failing_write)

    with pytest.raises(RuntimeError):
        classification_module.classify_batch(engine, taxonomy, [
            {"item_id": "i1", "axis": "document_type", "value": "warning", "by": "human:alice"},
            {"item_id": "i2", "axis": "document_type", "value": "press_release", "by": "human:alice"},
        ])

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications")).scalar()
        i1_labels = conn.execute(sa_text("SELECT labels FROM items WHERE id='i1'")).scalar()
        i2_labels = conn.execute(sa_text("SELECT labels FROM items WHERE id='i2'")).scalar()
    assert count == 0  # the first (successful) write was rolled back too
    assert i1_labels in (None, "{}")
    assert i2_labels in (None, "{}")
