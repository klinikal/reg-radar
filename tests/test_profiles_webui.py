from datetime import datetime, timezone

from fastapi.testclient import TestClient

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator


def _client(tmp_path, monkeypatch):
    import webui.app as app_module
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    monkeypatch.setenv("ACTIVITIES_PATH", "config/activities.yaml")
    monkeypatch.setenv("PROFILES_DIR", "config/profiles")
    app_module._engine = None
    app_module._taxonomy = None
    app_module._activities = None
    app_module.PROFILES_DIR = "config/profiles"
    engine = app_module.get_engine()
    return TestClient(app_module.app), engine, app_module


def _item(regulator_id, item_id, url=None):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="Notice",
        url=url or f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
    )


def _seed(engine):
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "full_name": "FCA", "jurisdiction": "gb"})
    insert_item(engine, _item("esma", "i-esma"))
    insert_item(engine, _item("uk-fca", "i-fca"))


def test_api_items_profile_param_filters(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/api/items", params={"profile": "eu-casp"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert {i["regulator_id"] for i in body["items"]} == {"esma"}


def test_api_items_omitting_profile_returns_everything(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/api/items")
    assert resp.json()["total"] == 2


def test_api_items_unknown_profile_returns_400(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/api/items", params={"profile": "not-a-real-profile"})
    assert resp.status_code == 400


def test_api_months_profile_param_filters(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/api/months", params={"profile": "eu-casp"})
    assert resp.status_code == 200
    assert sum(m["count"] for m in resp.json()["months"]) == 1


def test_index_profile_picker_round_trips_through_page_links(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/", params={"profile": "eu-casp"})
    assert resp.status_code == 200
    html = resp.text
    assert 'value="eu-casp" selected' in html
    assert "profile=eu-casp" in html  # threaded into filter_qs-based links


def test_index_profile_picker_lists_shipped_templates(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/")
    html = resp.text
    for name in ("uk-equities-dealer", "eu-casp", "eu-emi", "eu-bank"):
        assert f'value="{name}"' in html
