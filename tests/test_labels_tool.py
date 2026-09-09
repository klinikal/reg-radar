import importlib
from datetime import datetime, timezone

from collector.schema import Item, make_item_id
from collector.storage import insert_item, upsert_regulator
from regradar.classification import write_classification
from regradar.taxonomy import build_taxonomy


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


def _item(regulator_id, item_id):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="Notice",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
    )


def test_list_labels_tool_returns_every_axis(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    result = server.list_labels_tool()
    assert set(result.keys()) == {"document_type", "instrument"}


def test_list_labels_tool_reflects_live_classifications(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    insert_item(write_engine, _item("esma", "i1"))
    write_classification(write_engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)

    result = server.list_labels_tool()
    assert result["document_type"] == [{"value": "warning", "count": 1}]


def test_search_items_tool_jurisdictions_filter_end_to_end(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    upsert_regulator(write_engine, {"id": "fr-amf", "name": "AMF", "jurisdiction": "fr"})
    upsert_regulator(write_engine, {"id": "uk-fca", "name": "FCA", "jurisdiction": "gb"})
    insert_item(write_engine, _item("fr-amf", "i-amf"))
    insert_item(write_engine, _item("uk-fca", "i-fca"))

    results = server.search_items_tool(query="", jurisdictions=["gb"])
    assert [r["id"] for r in results] == ["i-fca"]


def test_search_items_tool_labels_filter_end_to_end(tmp_path, monkeypatch):
    server, write_engine = _server_with_db(tmp_path, monkeypatch)
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    insert_item(write_engine, _item("esma", "i1"))
    insert_item(write_engine, _item("esma", "i2"))
    write_classification(write_engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(write_engine, taxonomy, "i2", "document_type", "press_release", by="rule", confidence=0.9)

    results = server.search_items_tool(query="", labels={"document_type": ["warning"]})
    assert [r["id"] for r in results] == ["i1"]
