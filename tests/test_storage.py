import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from collector.schema import Item, make_item_id
from collector.storage import (
    init_db,
    upsert_regulator,
    insert_item,
    record_run_start,
    record_run_complete,
    record_source_error,
    reset_source_errors,
)
from mcpserver.queries import get_recent


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
        tags=["casp"],
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
