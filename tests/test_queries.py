import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator, record_run_start, record_run_complete
from mcpserver.queries import (
    search_items,
    get_recent,
    list_regulators,
    get_item,
    get_run_log,
    get_warnings,
)


def setup_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    engine = init_db(f.name)
    return engine, f.name


def make_item(regulator_id="esma", title="MiCA warning issued", url=None, tags=None, language="en", days_ago=0):
    now = datetime.now(timezone.utc) - timedelta(days=days_ago)
    url = url or f"https://example.com/{regulator_id}/{now.timestamp()}"
    return Item(
        id=make_item_id(regulator_id, url, now),
        regulator_id=regulator_id,
        source_label="all_news",
        title=title,
        url=url,
        published_at=now,
        fetched_at=now,
        date_inferred=False,
        language=language,
        content_snippet="Content about crypto assets and MiCA.",
        source_type="rss",
        tags=tags or ["casp"],
        raw_content="raw",
    )


def test_search_items_finds_by_title():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        insert_item(engine, make_item(title="Press release on interest rates", url="https://example.com/other"))
        results = search_items(engine, "MiCA warning")
        assert len(results) == 1
        assert "MiCA warning" in results[0]["title"]
    finally:
        os.unlink(db_path)


def test_get_recent_returns_latest():
    engine, db_path = setup_engine()
    try:
        for i in range(5):
            insert_item(engine, make_item(url=f"https://example.com/{i}"))
        results = get_recent(engine, limit=3)
        assert len(results) == 3
    finally:
        os.unlink(db_path)


def test_list_regulators_includes_stats():
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU", "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu", "mica_competent_authority": True, "enabled": True,
        })
        insert_item(engine, make_item(regulator_id="esma"))
        results = list_regulators(engine)
        esma = next(r for r in results if r["id"] == "esma")
        assert esma["total_items"] == 1
        assert "consecutive_failures" in esma
    finally:
        os.unlink(db_path)


def test_get_item_by_id():
    engine, db_path = setup_engine()
    try:
        item = make_item()
        insert_item(engine, item)
        result = get_item(engine, item.id)
        assert result is not None
        assert result["id"] == item.id
        assert "raw_content" in result
    finally:
        os.unlink(db_path)


def test_get_item_missing_returns_none():
    engine, db_path = setup_engine()
    try:
        result = get_item(engine, "nonexistent-id")
        assert result is None
    finally:
        os.unlink(db_path)


def test_get_run_log():
    engine, db_path = setup_engine()
    try:
        run_id = record_run_start(engine)
        record_run_complete(engine, run_id, 10, 5, 0, [])
        results = get_run_log(engine, limit=5)
        assert len(results) == 1
        assert results[0]["total_fetched"] == 10
    finally:
        os.unlink(db_path)


def test_get_warnings_filters_by_tag():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings", "casp"]))
        insert_item(engine, make_item(tags=["news"], url="https://example.com/news"))
        results = get_warnings(engine, days=30)
        assert len(results) == 1
        assert "warnings" in json.loads(results[0]["tags"])
    finally:
        os.unlink(db_path)


def test_get_warnings_days_zero_returns_all():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings"], days_ago=100))
        results = get_warnings(engine, days=0)
        assert len(results) == 1
    finally:
        os.unlink(db_path)
