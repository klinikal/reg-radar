import importlib
import typing
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text

from collector.schema import Item, make_item_id


def _fresh_server_module():
    import mcpserver.server as server
    importlib.reload(server)
    return server


def _server_with_db(tmp_path, monkeypatch):
    """Creates a real on-disk DB via the collector's init_db (read-write),
    points DB_PATH at it, and returns (server, write_engine): server is a
    freshly-reloaded mcpserver.server module whose get_engine() opens that
    same file read-only - mirroring how the collector and MCP server share
    a DB file in production.
    """
    from collector.storage import init_db
    db_path = tmp_path / "test.db"
    write_engine = init_db(str(db_path))
    monkeypatch.setenv("DB_PATH", str(db_path))
    server = _fresh_server_module()
    return server, write_engine


def test_get_engine_exits_if_db_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "missing.db"))
    server = _fresh_server_module()
    with pytest.raises(SystemExit):
        server.get_engine()


def test_get_engine_opens_existing_db_read_only_and_rejects_writes(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    engine = server.get_engine()
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM items")).scalar() == 0
    with pytest.raises(Exception):
        with engine.connect() as conn:
            conn.execute(text("INSERT INTO regulators (id, name) VALUES ('x', 'x')"))
            conn.commit()


def test_transport_security_enabled_with_allowed_hosts():
    # FastMCP only auto-enables DNS-rebinding protection when host is left
    # at 127.0.0.1/localhost/::1 - this project binds "0.0.0.0" (needed for
    # Docker networking), which silently falls through to protection
    # disabled unless configured explicitly.
    server = _fresh_server_module()
    ts = server.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is True
    assert "127.0.0.1:8000" in ts.allowed_hosts


def test_search_items_tool_signature_uses_optional_types():
    server = _fresh_server_module()
    hints = typing.get_type_hints(server.search_items_tool)
    for name in ("regulator_id", "language", "tags", "source_tags", "content_tags", "date_from", "date_to"):
        assert type(None) in typing.get_args(hints[name]), f"{name} is not Optional"


def test_get_recent_tool_signature_uses_optional_types():
    server = _fresh_server_module()
    hints = typing.get_type_hints(server.get_recent_tool)
    for name in ("regulator_id", "tags", "source_tags", "content_tags", "language"):
        assert type(None) in typing.get_args(hints[name]), f"{name} is not Optional"


def test_get_item_tool_param_does_not_shadow_builtin():
    server = _fresh_server_module()
    assert "item_id" in typing.get_type_hints(server.get_item_tool)
    assert "id" not in typing.get_type_hints(server.get_item_tool)


def test_search_items_tool_raises_sanitized_error_on_failure(tmp_path, monkeypatch, caplog):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError(f"SQL error against {tmp_path}")

    monkeypatch.setattr(server, "search_items", _boom)
    with caplog.at_level("ERROR"):
        with pytest.raises(RuntimeError) as exc_info:
            server.search_items_tool(query="x")
    assert str(tmp_path) not in str(exc_info.value)
    assert "search_items_tool" in str(exc_info.value)
    assert str(tmp_path) in caplog.text


def _item(title, regulator_id="esma", tags=None):
    now = datetime.now(timezone.utc)
    url = f"https://example.com/{title}"
    return Item(
        id=make_item_id(regulator_id, url, now), regulator_id=regulator_id, source_label="all_news",
        title=title, url=url, published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet=title, source_type="rss", source_tags=tags or [], content_tags=[],
    )


def test_count_items_tool_returns_total(tmp_path, monkeypatch):
    import json
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("MiCA warning issued"))
    insert_item(write_engine, _item("MiCA travel rule notice"))
    insert_item(write_engine, _item("unrelated press release"))
    result = json.loads(server.count_items_tool(query="MiCA"))
    assert result["total"] == 2


def test_get_month_facets_tool_returns_facets(tmp_path, monkeypatch):
    import json
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("MiCA warning issued"))
    result = json.loads(server.get_month_facets_tool(query="MiCA"))
    assert isinstance(result, list)
    assert result[0]["count"] == 1


def test_list_tags_tool_returns_counts(tmp_path, monkeypatch):
    import json
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("a", tags=["warnings"]))
    insert_item(write_engine, _item("b", tags=["warnings"]))
    result = json.loads(server.list_tags_tool())
    source_by_tag = {e["tag"]: e["count"] for e in result["source_tags"]}
    assert source_by_tag["warnings"] == 2


def test_get_items_since_tool_returns_newer_items(tmp_path, monkeypatch):
    import json
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    old = _item("old")
    insert_item(write_engine, old)
    cutoff = old.fetched_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    new = _item("new")
    new.fetched_at = old.fetched_at + timedelta(seconds=5)
    insert_item(write_engine, new)
    result = json.loads(server.get_items_since_tool(since=cutoff))
    assert len(result) == 1
    assert result[0]["title"] == "new"


def test_get_health_tool_returns_status(tmp_path, monkeypatch):
    import json
    from collector.storage import record_run_start, record_run_complete
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    run_id = record_run_start(write_engine)
    record_run_complete(write_engine, run_id, 10, 5, 0, [])
    result = json.loads(server.get_health_tool())
    assert result["status"] == "success"


def test_get_recent_tool_docstring_documents_limit_and_language():
    server = _fresh_server_module()
    doc = server.get_recent_tool.__doc__
    assert "max 100" in doc
    assert "ISO 639-1" in doc


def test_list_regulators_tool_docstring_documents_id_usage():
    server = _fresh_server_module()
    assert "regulator_id" in server.list_regulators_tool.__doc__


def test_get_run_log_tool_docstring_documents_status_values():
    server = _fresh_server_module()
    doc = server.get_run_log_tool.__doc__
    assert "partial" in doc and "failed" in doc


def test_get_item_tool_docstring_documents_id_source():
    server = _fresh_server_module()
    assert "search_items_tool" in server.get_item_tool.__doc__
