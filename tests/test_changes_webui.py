import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator

ATOM_NS = "{http://www.w3.org/2005/Atom}"


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


def _item(item_id, regulator_id="esma", title="Notice"):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title=title,
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
        raw_content="raw", body_md="Body.", extractor="raw/1",
    )


def _seed(engine):
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "full_name": "FCA", "jurisdiction": "gb"})


def test_api_changes_returns_items_and_next_cursor(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    insert_item(engine, _item("i1"))

    resp = client.get("/api/changes")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    assert body["next_cursor"] is not None


def test_api_changes_bad_cursor_returns_400(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)

    resp = client.get("/api/changes", params={"since": "not-a-cursor"})
    assert resp.status_code == 400


def test_api_changes_profile_filters(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    insert_item(engine, _item("i-esma", regulator_id="esma"))
    insert_item(engine, _item("i-fca", regulator_id="uk-fca"))

    resp = client.get("/api/changes", params={"profile": "eu-casp"})
    assert resp.status_code == 200
    body = resp.json()
    assert {i["id"] for i in body["items"]} == {"i-esma"}


def test_api_changes_unknown_profile_returns_400(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    resp = client.get("/api/changes", params={"profile": "../../etc/passwd"})
    assert resp.status_code == 400


def test_feed_atom_is_well_formed_with_required_elements(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    insert_item(engine, _item("i-esma", regulator_id="esma", title="A notice"))

    resp = client.get("/feeds/eu-casp.atom")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/atom+xml")

    root = ET.fromstring(resp.content)
    assert root.tag == f"{ATOM_NS}feed"
    assert root.find(f"{ATOM_NS}title") is not None
    assert root.find(f"{ATOM_NS}id") is not None
    assert root.find(f"{ATOM_NS}link") is not None
    assert root.find(f"{ATOM_NS}updated") is not None

    entries = root.findall(f"{ATOM_NS}entry")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.find(f"{ATOM_NS}title").text == "A notice"
    assert entry.find(f"{ATOM_NS}id") is not None
    assert entry.find(f"{ATOM_NS}link") is not None
    assert entry.find(f"{ATOM_NS}updated") is not None
    summary = entry.find(f"{ATOM_NS}summary")
    assert summary is not None
    assert "Body." not in (summary.text or "")  # summary only, never the full body


def test_feed_atom_updates_when_new_matching_item_added(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    insert_item(engine, _item("i1", regulator_id="esma"))

    first = ET.fromstring(client.get("/feeds/eu-casp.atom").content)
    assert len(first.findall(f"{ATOM_NS}entry")) == 1

    insert_item(engine, _item("i2", regulator_id="esma"))
    second = ET.fromstring(client.get("/feeds/eu-casp.atom").content)
    assert len(second.findall(f"{ATOM_NS}entry")) == 2


def test_feed_atom_newest_first(tmp_path, monkeypatch):
    from sqlalchemy import text

    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    older = _item("older", regulator_id="esma")
    newer = _item("newer", regulator_id="esma")
    insert_item(engine, older)
    insert_item(engine, newer)
    with engine.connect() as conn:
        conn.execute(text("UPDATE items SET updated_at = :dt WHERE id = :id"),
                     {"dt": older.fetched_at, "id": "older"})
        conn.execute(text("UPDATE items SET updated_at = :dt WHERE id = :id"),
                     {"dt": older.fetched_at + timedelta(seconds=30), "id": "newer"})
        conn.commit()

    root = ET.fromstring(client.get("/feeds/eu-casp.atom").content)
    entry_ids = [e.find(f"{ATOM_NS}id").text for e in root.findall(f"{ATOM_NS}entry")]
    assert entry_ids == ["regradar:item:newer", "regradar:item:older"]


def test_feed_atom_unknown_profile_returns_400(tmp_path, monkeypatch):
    client, engine, _ = _client(tmp_path, monkeypatch)
    _seed(engine)
    resp = client.get("/feeds/not-a-real-profile.atom")
    assert resp.status_code == 400


def test_feed_atom_respects_feed_max_items(tmp_path, monkeypatch):
    monkeypatch.setenv("FEED_MAX_ITEMS", "2")
    client, engine, app_module = _client(tmp_path, monkeypatch)
    app_module.FEED_MAX_ITEMS = 2
    _seed(engine)
    for i in range(5):
        insert_item(engine, _item(f"i{i}", regulator_id="esma"))

    root = ET.fromstring(client.get("/feeds/eu-casp.atom").content)
    assert len(root.findall(f"{ATOM_NS}entry")) == 2
