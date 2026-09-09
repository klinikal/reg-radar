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
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
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


def test_transport_security_allows_extra_hosts_from_env(monkeypatch):
    # Operators remapping the published port (e.g. 127.0.0.1:8001:8000, per
    # skills/regradar-operator/SKILL.md's port-conflict guidance) send a
    # Host header reflecting that external port - it must be explicitly
    # allow-listed via MCP_EXTRA_ALLOWED_HOSTS or every request 421s.
    monkeypatch.setenv("MCP_EXTRA_ALLOWED_HOSTS", "127.0.0.1:8001, localhost:8001")
    server = _fresh_server_module()
    ts = server.mcp.settings.transport_security
    assert "127.0.0.1:8001" in ts.allowed_hosts
    assert "localhost:8001" in ts.allowed_hosts
    # Defaults must still be present - this is additive, not a replacement.
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


def test_get_items_since_tool_surfaces_specific_parse_error(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="could not parse 'since' timestamp"):
        server.get_items_since_tool(since="not-a-timestamp")


def _item(title, regulator_id="esma", tags=None):
    now = datetime.now(timezone.utc)
    url = f"https://example.com/{title}"
    return Item(
        id=make_item_id(regulator_id, url, now), regulator_id=regulator_id, source_label="all_news",
        title=title, url=url, published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet=title, source_type="rss", source_tags=tags or [], content_tags=[],
    )


def test_count_items_tool_returns_total(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("MiCA warning issued"))
    insert_item(write_engine, _item("MiCA travel rule notice"))
    insert_item(write_engine, _item("unrelated press release"))
    result = server.count_items_tool(query="MiCA")
    assert result["total"] == 2


def test_get_month_facets_tool_returns_facets(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("MiCA warning issued"))
    result = server.get_month_facets_tool(query="MiCA")
    assert isinstance(result, list)
    assert result[0]["count"] == 1


def test_list_tags_tool_returns_counts(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("a", tags=["warnings"]))
    insert_item(write_engine, _item("b", tags=["warnings"]))
    result = server.list_tags_tool()
    source_by_tag = {e["tag"]: e["count"] for e in result["source_tags"]}
    assert source_by_tag["warnings"] == 2


def test_get_items_since_tool_returns_newer_items(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    old = _item("old")
    insert_item(write_engine, old)
    cutoff = old.fetched_at.strftime("%Y-%m-%d %H:%M:%S.%f")
    new = _item("new")
    new.fetched_at = old.fetched_at + timedelta(seconds=5)
    insert_item(write_engine, new)
    result = server.get_items_since_tool(since=cutoff)
    assert len(result) == 1
    assert result[0]["title"] == "new"


def test_get_health_tool_returns_status(tmp_path, monkeypatch):
    from collector.storage import record_run_start, record_run_complete
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    run_id = record_run_start(write_engine)
    record_run_complete(write_engine, run_id, 10, 5, 0, [])
    result = server.get_health_tool()
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


def test_search_items_tool_returns_list_not_json_string(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    insert_item(write_engine, _item("MiCA warning issued"))
    result = server.search_items_tool(query="MiCA")
    assert isinstance(result, list)
    assert result[0]["title"] == "MiCA warning issued"


def test_get_item_tool_max_content_chars_truncates(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    item = _item("MiCA warning issued")
    item.raw_content = "x" * 1000
    insert_item(write_engine, item)
    result = server.get_item_tool(item_id=item.id, max_content_chars=10)
    assert len(result["raw_content"]) == 10


def test_get_item_tool_missing_id_returns_none(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    result = server.get_item_tool(item_id="nonexistent")
    assert result is None


def test_get_taxonomy_tool_returns_axes_and_values(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    result = server.get_taxonomy_tool()
    assert result["version"] >= 1
    axis_ids = {a["id"] for a in result["axes"]}
    assert {"document_type", "instrument"} <= axis_ids
    instrument = next(a for a in result["axes"] if a["id"] == "instrument")
    assert instrument["cardinality"] == "many"
    mica = next(v for v in instrument["values"] if v["id"] == "mica")
    assert "MiCA" in mica["aliases"]


def test_get_unclassified_tool_excludes_items_with_a_live_row(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    unclassified = _item("Never classified item")
    classified = _item("Already classified item")
    insert_item(write_engine, unclassified)
    insert_item(write_engine, classified)
    server.write_classification(write_engine, server.get_taxonomy(), classified.id, "document_type", "warning",
                                 by="rule", confidence=0.9)

    result = server.get_unclassified_tool(axis="document_type")
    ids = [i["id"] for i in result]
    assert unclassified.id in ids
    assert classified.id not in ids


def test_get_unclassified_tool_returns_title_body_and_other_axis_labels(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    item = _item("An item needing document_type")
    item.body_md = "full body text"
    insert_item(write_engine, item)
    server.write_classification(write_engine, server.get_taxonomy(), item.id, "instrument", "mica",
                                 by="rule", confidence=0.9)

    result = server.get_unclassified_tool(axis="document_type")
    row = next(r for r in result if r["id"] == item.id)
    assert row["title"] == "An item needing document_type"
    assert "regulator" in row and "jurisdiction" in row
    assert row["labels"] == {"instrument": ["mica"]}


def test_get_unclassified_tool_include_low_confidence(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    item = _item("Weakly-scored item")
    insert_item(write_engine, item)
    # Below document_type's effective-label threshold - no entry in
    # item_labels, but a live row exists.
    server.write_classification(write_engine, server.get_taxonomy(), item.id, "document_type", "warning",
                                 by="rule", confidence=0.2)

    without_flag = server.get_unclassified_tool(axis="document_type")
    with_flag = server.get_unclassified_tool(axis="document_type", include_low_confidence=True)
    assert item.id not in [i["id"] for i in without_flag]
    assert item.id in [i["id"] for i in with_flag]


def test_classify_item_tool_writes_and_returns_labels(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    item = _item("An item to classify")
    insert_item(write_engine, item)

    result = server.classify_item_tool(item_id=item.id, axis="document_type", value="warning",
                                        by="llm:anthropic/claude-sonnet-5", confidence=0.9)
    assert result["item_id"] == item.id
    assert result["labels"] == {"document_type": "warning"}


def test_classify_items_tool_writes_valid_and_reports_invalid(tmp_path, monkeypatch):
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    item1 = _item("First item")
    item2 = _item("Second item")
    insert_item(write_engine, item1)
    insert_item(write_engine, item2)

    result = server.classify_items_tool(classifications=[
        {"item_id": item1.id, "axis": "document_type", "value": "warning", "by": "llm:anthropic/claude-sonnet-5",
         "confidence": 0.9},
        {"item_id": item2.id, "axis": "document_type", "value": "press_release", "by": "llm:anthropic/claude-sonnet-5",
         "confidence": 0.9},
        {"item_id": item1.id, "axis": "document_type", "value": "not-a-real-value", "by": "llm:anthropic/claude-sonnet-5",
         "confidence": 0.9},
    ])
    assert sorted(result["written"]) == sorted([item1.id, item2.id])
    assert len(result["errors"]) == 1
    assert result["errors"][0]["index"] == 2


def test_classify_items_tool_batch_over_limit_raises(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    batch = [{"item_id": "x", "axis": "document_type", "value": "warning", "by": "rule"} for _ in range(101)]
    import pytest
    with pytest.raises(ValueError):
        server.classify_items_tool(classifications=batch)


def test_get_unclassified_tool_count_decreases_by_exactly_n_after_n_classify_writes(tmp_path, monkeypatch):
    """Acceptance criterion: get_unclassified_tool(axis="document_type")
    count decreases by exactly the number of items classified in a session -
    stands in for the "agent classifies 50 items with the skill" criterion,
    which needs a live agent and can't run in this test suite (per the task
    brief)."""
    from collector.storage import insert_item
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    items = [_item(f"Backlog item {i}") for i in range(50)]
    for item in items:
        insert_item(write_engine, item)

    before = len(server.get_unclassified_tool(axis="document_type", limit=200))
    assert before == 50

    classifications = [
        {"item_id": item.id, "axis": "document_type", "value": "press_release", "by": "llm:anthropic/claude-sonnet-5",
         "confidence": 0.8}
        for item in items
    ]
    result = server.classify_items_tool(classifications=classifications)
    assert len(result["written"]) == 50
    assert result["errors"] == []

    after = len(server.get_unclassified_tool(axis="document_type", limit=200))
    assert before - after == 50
    assert after == 0
