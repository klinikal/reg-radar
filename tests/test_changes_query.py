import tempfile
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator
from mcpserver.queries import get_changes, get_latest_changes
from regradar.changes import decode_cursor, encode_cursor


def setup_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def make_item(item_id, regulator_id="esma", title="Notice", days_ago=0, url=None):
    now = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title=title,
        url=url or f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="snippet", source_type="rss",
        raw_content="raw", body_md="Body.", extractor="raw/1",
    )


def _set_updated_at(engine, item_id, dt):
    # Format explicitly before binding via raw text() SQL, matching
    # SQLAlchemy's own SQLite DATETIME storage format exactly (see
    # mcpserver.queries._format_dt) - Python's sqlite3 default adapter
    # (used when a raw datetime object is bound directly) stores an aware
    # datetime with a "+00:00" suffix and drops the fractional part
    # entirely when microsecond is 0, neither of which matches what the
    # ORM write path (insert_item et al) actually produces.
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    formatted = dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    with engine.connect() as conn:
        conn.execute(text("UPDATE items SET updated_at = :dt WHERE id = :id"), {"dt": formatted, "id": item_id})
        conn.commit()


def test_get_changes_orders_oldest_first_and_returns_next_cursor():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    insert_item(engine, make_item("i1"))
    insert_item(engine, make_item("i2"))
    insert_item(engine, make_item("i3"))
    _set_updated_at(engine, "i1", base)
    _set_updated_at(engine, "i2", base + timedelta(seconds=10))
    _set_updated_at(engine, "i3", base + timedelta(seconds=20))

    items, next_cursor = get_changes(engine)
    assert [i["id"] for i in items] == ["i1", "i2", "i3"]
    assert next_cursor == encode_cursor(base + timedelta(seconds=20), "i3")


def test_get_changes_since_excludes_up_to_and_including_cursor():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    insert_item(engine, make_item("i1"))
    insert_item(engine, make_item("i2"))
    insert_item(engine, make_item("i3"))
    _set_updated_at(engine, "i1", base)
    _set_updated_at(engine, "i2", base + timedelta(seconds=10))
    _set_updated_at(engine, "i3", base + timedelta(seconds=20))

    cursor = encode_cursor(base, "i1")
    items, next_cursor = get_changes(engine, since=cursor)
    assert [i["id"] for i in items] == ["i2", "i3"]

    # round trips
    dt, item_id = decode_cursor(next_cursor)
    assert item_id == "i3"


def test_get_changes_id_tiebreak_on_equal_timestamps():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    same = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    insert_item(engine, make_item("zzz"))
    insert_item(engine, make_item("aaa"))
    _set_updated_at(engine, "zzz", same)
    _set_updated_at(engine, "aaa", same)

    items, next_cursor = get_changes(engine)
    # Same timestamp - ordered by id ascending as the tiebreak.
    assert [i["id"] for i in items] == ["aaa", "zzz"]

    # Cursor sitting between the two (same timestamp, id "aaa") returns
    # only the item whose id sorts after it.
    cursor = encode_cursor(same, "aaa")
    items2, _ = get_changes(engine, since=cursor)
    assert [i["id"] for i in items2] == ["zzz"]


def test_get_changes_empty_result_has_no_next_cursor():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    items, next_cursor = get_changes(engine)
    assert items == []
    assert next_cursor is None


def test_get_changes_include_body_toggle():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, make_item("i1"))

    without_body, _ = get_changes(engine, include_body=False)
    assert "body_md" not in without_body[0]

    with_body, _ = get_changes(engine, include_body=True)
    assert with_body[0]["body_md"] == "Body."


def test_get_changes_frontmatter_shape_matches_get_item_document():
    from mcpserver.queries import get_item

    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, make_item("i1"))

    changes_items, _ = get_changes(engine)
    changes_doc = changes_items[0]

    item = get_item(engine, "i1")
    document = item["document"]

    # get_changes() omits jurisdiction_ancestors/label_provenance (no
    # taxonomy threaded through the hot polling path) - everything else
    # matches get_item_tool's document shape field for field.
    for key in ("id", "regulator", "regulator_name", "jurisdiction", "title",
                "published_at", "fetched_at", "updated_at", "source_url", "content_hash"):
        assert changes_doc[key] == document[key], key


def test_get_latest_changes_orders_newest_first():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    insert_item(engine, make_item("i1"))
    insert_item(engine, make_item("i2"))
    _set_updated_at(engine, "i1", base)
    _set_updated_at(engine, "i2", base + timedelta(seconds=10))

    items = get_latest_changes(engine, limit=10)
    assert [i["id"] for i in items] == ["i2", "i1"]


def test_get_latest_changes_respects_limit():
    engine = setup_engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    for i in range(5):
        insert_item(engine, make_item(f"i{i}"))
    items = get_latest_changes(engine, limit=2)
    assert len(items) == 2


def test_get_changes_picks_up_a_rule_classification_updated_at_bump():
    """Integration between the rules engine (task 6) and the changes cursor
    (task 8): a classify write for an existing item bumps items.updated_at
    via materialize_labels(), so get_changes() (which orders/filters on
    COALESCE(updated_at, fetched_at)) sees it as a change even though the
    item itself was fetched long before and nothing about fetched_at moved."""
    from regradar.classification import write_classification
    from regradar.taxonomy import build_taxonomy

    engine = setup_engine()
    old = datetime.now(timezone.utc) - timedelta(days=30)
    item = make_item("i1", days_ago=30)
    insert_item(engine, item)
    _set_updated_at(engine, "i1", old)

    documents, cursor_before = get_changes(engine, since=None, limit=10)
    assert cursor_before is not None
    # Nothing else changes since this cursor - the next call should be empty.
    documents_none, cursor_none = get_changes(engine, since=cursor_before, limit=10)
    assert documents_none == []
    assert cursor_none is None

    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    write_classification(engine, taxonomy, "i1", "instrument", "mica", by="rule", confidence=0.9,
                          classifier_id="test-classifier")

    documents_after, cursor_after = get_changes(engine, since=cursor_before, limit=10)
    assert [d["id"] for d in documents_after] == ["i1"]
    assert documents_after[0]["labels"]["instrument"] == ["mica"]
    assert cursor_after is not None
    assert cursor_after != cursor_before

    # The regression this guards: a raw-SQL updated_at write (materialize_
    # labels' UPDATE) that doesn't match the ORM's naive string format
    # sorts differently, so a cursor sitting past the reclassified item
    # never advances past it and a paginating caller (task 13's digest)
    # sees it forever. Calling again with cursor_after must be empty.
    documents_again, cursor_again = get_changes(engine, since=cursor_after, limit=10)
    assert documents_again == []
    assert cursor_again is None
