import importlib
from datetime import datetime, timedelta, timezone

from collector.schema import Item, make_item_id


def _fresh_server_module():
    import mcpserver.server as server
    importlib.reload(server)
    return server


def _server_with_db(tmp_path, monkeypatch):
    from collector.storage import init_db
    db_path = tmp_path / "test.db"
    write_engine = init_db(str(db_path))
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    server = _fresh_server_module()
    return server, write_engine


def _item(title, regulator_id="esma"):
    now = datetime.now(timezone.utc)
    url = f"https://example.com/{title}"
    return Item(
        id=make_item_id(regulator_id, url, now), regulator_id=regulator_id, source_label="all_news",
        title=title, url=url, published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet=title, source_type="rss", source_tags=[], content_tags=[],
        raw_content="raw", body_md="Body.", extractor="raw/1",
    )


def test_get_changes_tool_returns_items_and_next_cursor(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("first"))

    result = server.get_changes_tool()
    assert len(result["items"]) == 1
    assert result["items"][0]["title"] == "first"
    assert result["next_cursor"] is not None


def test_get_changes_tool_matches_api_changes_shape(tmp_path, monkeypatch):
    from collector.storage import insert_item, upsert_regulator
    from fastapi.testclient import TestClient

    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    upsert_regulator(write_engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(write_engine, _item("shared-shape"))

    tool_result = server.get_changes_tool()

    import webui.app as app_module
    monkeypatch.setenv("ACTIVITIES_PATH", "config/activities.yaml")
    monkeypatch.setenv("PROFILES_DIR", "config/profiles")
    app_module._engine = None
    app_module._taxonomy = None
    app_module._activities = None
    app_module.PROFILES_DIR = "config/profiles"
    client = TestClient(app_module.app)
    http_result = client.get("/api/changes").json()

    assert tool_result["items"] == http_result["items"]
    assert tool_result["next_cursor"] == http_result["next_cursor"]


def test_get_changes_tool_since_round_trips(tmp_path, monkeypatch):
    from sqlalchemy import text

    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    one = _item("first")
    insert_item(write_engine, one)  # updated_at defaults to fetched_at
    first = server.get_changes_tool()

    two = _item("second")
    insert_item(write_engine, two)
    # Force "second" to be unambiguously later, formatted exactly as
    # SQLAlchemy's own SQLite DATETIME storage would (see
    # mcpserver.queries._format_dt) - a raw datetime object bound directly
    # via text() would store differently (offset suffix, dropped
    # fractional part at :00) than the ORM write path does.
    later = (one.fetched_at + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S.%f")
    with write_engine.connect() as conn:
        conn.execute(text("UPDATE items SET updated_at = :dt WHERE id = :id"), {"dt": later, "id": two.id})
        conn.commit()

    second = server.get_changes_tool(since=first["next_cursor"])
    assert [i["title"] for i in second["items"]] == ["second"]


def test_get_items_since_tool_logs_deprecation_warning(tmp_path, monkeypatch, caplog):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    old = _item("old")
    insert_item(write_engine, old)
    cutoff = old.fetched_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    with caplog.at_level("WARNING"):
        server.get_items_since_tool(since=cutoff)
    assert "deprecated" in caplog.text
    assert "get_changes_tool" in caplog.text


def test_get_items_since_tool_docstring_points_to_get_changes_tool():
    server = _fresh_server_module()
    assert "get_changes_tool" in server.get_items_since_tool.__doc__
    assert "deprecated" in server.get_items_since_tool.__doc__.lower()
