import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from collector.schema import Item, make_item_id
from collector.storage import (
    init_db,
    upsert_regulator,
    insert_item,
    record_run_start,
    record_run_complete,
    record_source_error,
    reset_source_errors,
    check_db_integrity,
)
from mcpserver.queries import get_recent


def _make_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    engine = init_db(f.name)
    return engine, f.name


def make_test_item(url="https://example.com/1", regulator_id="esma"):
    now = datetime.now(timezone.utc)
    return Item(
        id=make_item_id(regulator_id, url, now),
        regulator_id=regulator_id,
        source_label="all_news",
        title="Test item",
        url=url,
        published_at=now,
        fetched_at=now,
        date_inferred=False,
        language="en",
        content_snippet="snippet",
        source_type="rss",
        source_tags=["casp"],
        raw_content="<p>raw</p>",
    )


def test_init_db_creates_tables():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        assert "items" in tables
        assert "regulators" in tables
        assert "runs" in tables
        assert "source_errors" in tables
    finally:
        os.unlink(db_path)


def test_insert_item_returns_true_for_new():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)
        item = make_test_item()
        result = insert_item(engine, item)
        assert result is True
    finally:
        os.unlink(db_path)


def test_insert_item_returns_false_for_duplicate():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)
        item = make_test_item()
        insert_item(engine, item)
        result = insert_item(engine, item)
        assert result is False
    finally:
        os.unlink(db_path)


def test_upsert_regulator_and_run_lifecycle():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)
        upsert_regulator(engine, {
            "id": "esma",
            "name": "ESMA",
            "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU",
            "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu",
            "mica_competent_authority": True,
            "enabled": True,
        })
        run_id = record_run_start(engine)
        assert isinstance(run_id, str) and len(run_id) > 0
        record_run_complete(engine, run_id, total_fetched=10, total_new=5, total_errors=0, error_detail=[])
    finally:
        os.unlink(db_path)


def test_source_error_tracking():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)
        record_source_error(engine, "esma", "all_news", "timeout")
        record_source_error(engine, "esma", "all_news", "timeout")
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT consecutive_failures FROM source_errors WHERE regulator_id='esma' AND source_label='all_news'")
            ).fetchone()
        assert row[0] == 2
        reset_source_errors(engine, "esma", "all_news")
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT consecutive_failures FROM source_errors WHERE regulator_id='esma' AND source_label='all_news'")
            ).fetchone()
        assert row[0] == 0
    finally:
        os.unlink(db_path)


def test_upsert_regulator_updates_existing():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)
        entry = {
            "id": "esma",
            "name": "ESMA",
            "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU",
            "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu",
            "mica_competent_authority": True,
            "enabled": True,
        }
        upsert_regulator(engine, entry)
        # Update: disable the regulator
        upsert_regulator(engine, {**entry, "enabled": False})
        from sqlalchemy import text
        with engine.connect() as conn:
            row = conn.execute(text("SELECT enabled FROM regulators WHERE id='esma'")).fetchone()
        assert row[0] == False
    finally:
        os.unlink(db_path)


def test_concurrent_read_write_no_lock_error():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        engine = init_db(db_path)

        reader_errors = []
        reader_results = []

        def reader():
            for _ in range(10):
                try:
                    results = get_recent(engine, limit=10)
                    reader_results.append(results)
                except Exception as e:
                    reader_errors.append(e)
                time.sleep(0.02)

        t = threading.Thread(target=reader)
        t.start()

        for i in range(50):
            insert_item(engine, make_test_item(url=f"https://example.com/{i}"))

        t.join(timeout=5)

        assert not reader_errors, f"Reader raised errors: {reader_errors}"
        assert len(reader_results) > 0
    finally:
        os.unlink(db_path)


def test_init_db_creates_items_fts_table():
    engine, db_path = _make_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='items_fts'")
            ).fetchone()
        assert row is not None
    finally:
        os.unlink(db_path)


def test_init_db_is_idempotent_for_items_fts():
    engine, db_path = _make_engine()
    try:
        init_db(db_path)
        init_db(db_path)
        with engine.connect() as conn:
            count = conn.execute(
                text("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='items_fts'")
            ).scalar()
        assert count == 1
    finally:
        os.unlink(db_path)


def test_insert_item_populates_items_fts():
    engine, db_path = _make_engine()
    try:
        item = Item(
            id=make_item_id("esma", "https://example.com/x", None),
            regulator_id="esma", source_label="all_news",
            title="MiCA warning issued", url="https://example.com/x",
            published_at=None, fetched_at=datetime.now(timezone.utc),
            date_inferred=False, language="en",
            content_snippet="Content about crypto assets.",
            source_type="rss", source_tags=["casp"], raw_content="Full raw text here.",
        )
        insert_item(engine, item)
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT item_id, title FROM items_fts WHERE item_id=:id"),
                {"id": item.id},
            ).fetchone()
        assert row is not None
        assert row[1] == "MiCA warning issued"
    finally:
        os.unlink(db_path)


def test_init_db_creates_item_chunks_table():
    engine, db_path = _make_engine()
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name='item_chunks'")
            ).fetchone()
        assert row is not None
    finally:
        os.unlink(db_path)


def test_item_chunks_row_round_trips():
    engine, db_path = _make_engine()
    try:
        from collector.storage import ItemChunkRow
        now = datetime.now(timezone.utc)
        with Session(engine) as session:
            session.add(ItemChunkRow(
                id="item1:0", item_id="item1", chunk_index=0,
                text="First chunk of text.", token_count=4,
                content_hash="abc123", created_at=now,
            ))
            session.commit()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT item_id, chunk_index, text, token_count, content_hash FROM item_chunks WHERE id=:id"),
                {"id": "item1:0"},
            ).fetchone()
        assert row == ("item1", 0, "First chunk of text.", 4, "abc123")
    finally:
        os.unlink(db_path)


def test_init_db_adds_source_tags_and_content_tags_columns_to_pre_existing_db(tmp_path):
    from sqlalchemy import create_engine, text as sa_text
    db_path = str(tmp_path / "legacy_tags.db")
    legacy_engine = create_engine(f"sqlite:///{db_path}")
    with legacy_engine.connect() as conn:
        conn.execute(sa_text("CREATE TABLE items (id TEXT PRIMARY KEY, regulator_id TEXT, title TEXT)"))
        conn.commit()
    legacy_engine.dispose()

    engine = init_db(db_path)
    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(sa_text("PRAGMA table_info(items)")).fetchall()]
    assert "source_tags" in cols
    assert "content_tags" in cols


def test_insert_item_writes_source_tags_and_content_tags(tmp_path):
    from datetime import datetime, timezone
    from collector.schema import Item, make_item_id
    from sqlalchemy import text as sa_text
    import json

    engine = init_db(str(tmp_path / "test.db"))
    now = datetime.now(timezone.utc)
    item = Item(
        id=make_item_id("esma", "https://x/a", now), regulator_id="esma", source_label="all_news",
        title="a", url="https://x/a", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="a", source_type="rss",
        source_tags=["warnings"], content_tags=["mica"],
    )
    insert_item(engine, item)

    with engine.connect() as conn:
        row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = :id"), {"id": item.id}).fetchone()
    assert json.loads(row[0]) == ["warnings"]
    assert json.loads(row[1]) == ["mica"]


def test_check_db_integrity_true_for_healthy_db(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    assert check_db_integrity(engine) is True


def test_check_db_integrity_false_for_corrupted_file(tmp_path):
    # Insert real rows first - a freshly-initialised, empty database is
    # mostly unallocated freelist pages, and integrity_check doesn't
    # validate the *content* of those, so clobbering bytes in an empty file
    # doesn't reliably trigger a failure. With real data pages present,
    # overwriting the file's last page (where data actually lives) does.
    db_path = tmp_path / "test.db"
    engine = init_db(str(db_path))
    now = datetime.now(timezone.utc)
    for i in range(50):
        insert_item(engine, Item(
            id=make_item_id("esma", f"https://x/{i}", now), regulator_id="esma", source_label="all_news",
            title=f"Item {i} " * 20, url=f"https://x/{i}", published_at=now, fetched_at=now,
            date_inferred=False, language="en", content_snippet="content " * 50, source_type="rss",
            source_tags=["casp"], raw_content="raw content here " * 100,
        ))
    engine.dispose()

    size = os.path.getsize(db_path)
    with open(db_path, "r+b") as f:
        f.seek(max(0, size - 4096))
        f.write(b"\xff" * 4096)

    from sqlalchemy import create_engine as _create_engine
    corrupt_engine = _create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 30})
    assert check_db_integrity(corrupt_engine) is False


def test_record_run_complete_stores_db_integrity_ok(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    run_id = record_run_start(engine)
    record_run_complete(engine, run_id, total_fetched=1, total_new=1, total_errors=0, error_detail=[], db_integrity_ok=True)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT db_integrity_ok FROM runs WHERE run_id = :id"), {"id": run_id}).fetchone()
    assert bool(row[0]) is True


def test_record_run_complete_db_integrity_defaults_to_none(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    run_id = record_run_start(engine)
    record_run_complete(engine, run_id, total_fetched=1, total_new=1, total_errors=0, error_detail=[])
    with engine.connect() as conn:
        row = conn.execute(text("SELECT db_integrity_ok FROM runs WHERE run_id = :id"), {"id": run_id}).fetchone()
    assert row[0] is None
