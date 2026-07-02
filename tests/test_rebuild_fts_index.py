import os
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from sqlalchemy import text
from scripts.rebuild_fts_index import rebuild


def _make_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    engine = init_db(f.name)
    return engine, f.name


def _make_item(title, url):
    now = datetime.now(timezone.utc)
    return Item(
        id=make_item_id("esma", url, now), regulator_id="esma", source_label="all_news",
        title=title, url=url, published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="snippet", source_type="rss", tags=["casp"],
        raw_content="raw",
    )


def test_rebuild_repopulates_items_fts_from_scratch():
    engine, db_path = _make_engine()
    try:
        insert_item(engine, _make_item("First item", "https://example.com/1"))
        insert_item(engine, _make_item("Second item", "https://example.com/2"))
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM items_fts"))
            conn.commit()
        rebuild(engine, dry_run=False)
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM items_fts")).scalar()
        assert count == 2
    finally:
        os.unlink(db_path)


def test_rebuild_dry_run_writes_nothing():
    engine, db_path = _make_engine()
    try:
        insert_item(engine, _make_item("First item", "https://example.com/1"))
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM items_fts"))
            conn.commit()
        rebuild(engine, dry_run=True)
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM items_fts")).scalar()
        assert count == 0
    finally:
        os.unlink(db_path)
