import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator


def _client(tmp_path, monkeypatch):
    import webui.app as app_module
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    app_module._engine = None
    app_module._taxonomy = None
    engine = app_module.get_engine()
    return TestClient(app_module.app), engine, app_module


def _insert_item(engine, item_id="i1", regulator_id="esma"):
    now = datetime.now(timezone.utc)
    item = Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="MiCA notice",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="...", source_type="rss",
    )
    insert_item(engine, item)


def test_api_classify_item_writes_and_returns_updated_labels(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    resp = client.post(f"/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning", "by": "human:alice",
    })
    assert resp.status_code == 200
    assert resp.json()["labels"] == {"document_type": "warning"}


def test_api_classify_item_rejects_unknown_value(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "not-a-real-value", "by": "human:alice",
    })
    assert resp.status_code == 400


def test_api_classify_item_remove_supersedes_row(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)
    client.post("/api/items/i1/classifications", json={
        "axis": "instrument", "value": "mica", "by": "human:alice",
    })
    resp = client.post("/api/items/i1/classifications", json={
        "axis": "instrument", "value": "mica", "by": "human:alice", "remove": True,
    })
    assert resp.status_code == 200
    assert resp.json()["labels"] == {}


def test_item_classify_form_redirects_and_persists(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    resp = client.post(
        "/item/i1/classify",
        data={"axis": "document_type", "value": "warning", "action": "set"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = client.get("/item/i1")
    assert page.status_code == 200
    assert 'value="warning" selected' in page.text


def test_human_correction_survives_reclassify(tmp_path, monkeypatch):
    """Acceptance criterion: a human correction via the API changes
    items.labels immediately and survives reclassify --axis instrument."""
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    client.post("/api/items/i1/classifications", json={
        "axis": "instrument", "value": "mica", "by": "human:alice",
    })

    from regradar.classification import supersede_axis, materialize_labels
    from regradar.taxonomy import build_taxonomy
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    supersede_axis(engine, "i1", "instrument", by="source_default")
    labels = materialize_labels(engine, taxonomy, "i1")
    assert labels == {"instrument": ["mica"]}


def test_index_jurisdiction_filter_narrows_regulators(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch)
    upsert_regulator(engine, {"id": "fr-amf", "name": "AMF", "full_name": "Autorite des Marches Financiers", "jurisdiction": "fr"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "full_name": "Financial Conduct Authority", "jurisdiction": "gb"})
    _insert_item(engine, "i-amf", "fr-amf")
    _insert_item(engine, "i-fca", "uk-fca")

    resp = client.get("/?jurisdiction=gb")
    assert resp.status_code == 200
    assert "i-fca" not in resp.text or "MiCA notice" in resp.text  # sanity: page rendered
    body = resp.text
    assert "/item/i-fca" in body
    assert "/item/i-amf" not in body
