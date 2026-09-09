import importlib
from datetime import datetime, timezone

import pytest

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator


def _server_with_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    write_engine = init_db(str(db_path))
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    monkeypatch.setenv("ACTIVITIES_PATH", "config/activities.yaml")
    monkeypatch.setenv("PROFILES_DIR", "config/profiles")
    import mcpserver.server as server
    importlib.reload(server)
    return server, write_engine


def _item(regulator_id, item_id, url=None):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="Notice",
        url=url or f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
    )


def _seed(engine):
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "jurisdiction": "gb"})
    insert_item(engine, _item("esma", "i-esma"))
    insert_item(engine, _item("uk-fca", "i-fca"))


def test_search_items_tool_profile_param_filters_to_eu(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    results = server.search_items_tool(query="", profile="eu-casp")
    assert {r["regulator_id"] for r in results} == {"esma"}


def test_search_items_tool_omitting_profile_leaves_behaviour_unchanged(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    results = server.search_items_tool(query="")
    assert {r["regulator_id"] for r in results} == {"esma", "uk-fca"}


def test_get_recent_tool_profile_param(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    results = server.get_recent_tool(profile="eu-casp")
    assert {r["regulator_id"] for r in results} == {"esma"}


def test_get_warnings_tool_profile_param(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    results = server.get_warnings_tool(days=0, profile="eu-casp")
    assert results == []  # no item has source_tags "warnings", regardless of profile


def test_count_items_tool_profile_param(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    assert server.count_items_tool(profile="eu-casp") == {"total": 1}
    assert server.count_items_tool() == {"total": 2}


def test_get_month_facets_tool_profile_param(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    facets = server.get_month_facets_tool(profile="eu-casp")
    assert sum(f["count"] for f in facets) == 1


def test_get_items_since_tool_profile_param(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    results = server.get_items_since_tool(since="2000-01-01T00:00:00", profile="eu-casp")
    assert {r["regulator_id"] for r in results} == {"esma"}


def test_unknown_profile_name_raises_valueerror(tmp_path, monkeypatch):
    server, engine = _server_with_db(tmp_path, monkeypatch)
    _seed(engine)
    with pytest.raises(ValueError):
        server.search_items_tool(query="", profile="not-a-real-profile")
