import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, ItemChunkRow
from collector.enrichment import run_chunking


def _make_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    engine = init_db(f.name)
    return engine, f.name


def _make_item(regulator_id, url, raw_content):
    now = datetime.now(timezone.utc)
    return Item(
        id=make_item_id(regulator_id, url, now), regulator_id=regulator_id, source_label="all_news",
        title="Item title", url=url, published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="snippet", source_type="rss", tags=["casp"],
        raw_content=raw_content,
    )


def _chunk_count(engine, item_id=None):
    with engine.connect() as conn:
        if item_id:
            return conn.execute(text("SELECT COUNT(*) FROM item_chunks WHERE item_id=:id"), {"id": item_id}).scalar()
        return conn.execute(text("SELECT COUNT(*) FROM item_chunks")).scalar()


def test_run_chunking_chunks_every_item_on_first_run():
    engine, db_path = _make_engine()
    try:
        item = _make_item("esma", "https://example.com/1", "Some content here about MiCA rules.")
        insert_item(engine, item)
        chunked, skipped = run_chunking(engine)
        assert chunked == 1
        assert skipped == 0
        assert _chunk_count(engine, item.id) >= 1
    finally:
        os.unlink(db_path)


def test_run_chunking_is_idempotent_on_unchanged_content():
    engine, db_path = _make_engine()
    try:
        item = _make_item("esma", "https://example.com/1", "Some content here about MiCA rules.")
        insert_item(engine, item)
        run_chunking(engine)
        before_ids = set()
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, content_hash FROM item_chunks")).fetchall()
            before_ids = set(rows)

        chunked, skipped = run_chunking(engine)
        assert chunked == 0
        assert skipped == 1
        with engine.connect() as conn:
            after_ids = set(conn.execute(text("SELECT id, content_hash FROM item_chunks")).fetchall())
        assert before_ids == after_ids
    finally:
        os.unlink(db_path)


def test_run_chunking_skips_empty_content_item_permanently():
    engine, db_path = _make_engine()
    try:
        item = _make_item("esma", "https://example.com/1", "")
        item.content_snippet = ""
        insert_item(engine, item)

        chunked, skipped = run_chunking(engine)
        assert (chunked, skipped) == (0, 1)
        assert _chunk_count(engine, item.id) == 0

        chunked, skipped = run_chunking(engine)
        assert (chunked, skipped) == (0, 1)
        assert _chunk_count(engine, item.id) == 0
    finally:
        os.unlink(db_path)


def test_run_chunking_rechunks_only_the_changed_item():
    engine, db_path = _make_engine()
    try:
        item1 = _make_item("esma", "https://example.com/1", "Content for item one about custody.")
        item2 = _make_item("esma", "https://example.com/2", "Content for item two about passporting.")
        insert_item(engine, item1)
        insert_item(engine, item2)
        run_chunking(engine)

        with Session(engine) as session:
            session.execute(
                text("UPDATE items SET raw_content=:c WHERE id=:id"),
                {"c": "Completely new content about staking services now.", "id": item1.id},
            )
            session.commit()

        chunked, skipped = run_chunking(engine)
        assert chunked == 1
        assert skipped == 1

        with engine.connect() as conn:
            item1_texts = [r[0] for r in conn.execute(
                text("SELECT text FROM item_chunks WHERE item_id=:id"), {"id": item1.id}
            ).fetchall()]
        assert any("staking" in t for t in item1_texts)
        assert not any("custody" in t for t in item1_texts)
    finally:
        os.unlink(db_path)


def test_run_chunking_clears_stale_chunks_when_content_becomes_empty():
    engine, db_path = _make_engine()
    try:
        item = _make_item("esma", "https://example.com/1", "Some content here about MiCA rules.")
        insert_item(engine, item)
        run_chunking(engine)
        assert _chunk_count(engine, item.id) >= 1

        with Session(engine) as session:
            session.execute(
                text("UPDATE items SET raw_content='', content_snippet='' WHERE id=:id"),
                {"id": item.id},
            )
            session.commit()

        chunked, skipped = run_chunking(engine)
        assert (chunked, skipped) == (0, 1)
        assert _chunk_count(engine, item.id) == 0
    finally:
        os.unlink(db_path)
