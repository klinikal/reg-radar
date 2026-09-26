import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator


def _client(tmp_path, monkeypatch, auth_token=None):
    import webui.app as app_module
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    if auth_token is None:
        monkeypatch.delenv("WEBUI_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("WEBUI_AUTH_TOKEN", auth_token)
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
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)

    resp = client.post(f"/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning",
    }, auth=("editor", "secret"))
    assert resp.status_code == 200
    assert resp.json()["labels"] == {"document_type": "warning"}


def test_api_classify_item_rejects_unknown_value(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "not-a-real-value",
    }, auth=("editor", "secret"))
    assert resp.status_code == 400


def test_api_classify_item_remove_supersedes_row(tmp_path, monkeypatch):
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)
    client.post("/api/items/i1/classifications", json={
        "axis": "instrument", "value": "mica",
    }, auth=("editor", "secret"))
    resp = client.post("/api/items/i1/classifications", json={
        "axis": "instrument", "value": "mica", "remove": True,
    }, auth=("editor", "secret"))
    assert resp.status_code == 200
    assert resp.json()["labels"] == {}


def test_api_classify_item_unauthenticated_can_use_non_human_provenance(tmp_path, monkeypatch):
    """Current default (no WEBUI_AUTH_TOKEN configured): writes still work
    for an unauthenticated API/MCP-style caller, as long as it isn't
    claiming human provenance (see test_api_classify_item_unauthenticated_rejects_human_by)."""
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning", "by": "llm:anthropic/claude-sonnet-5",
    })
    assert resp.status_code == 200
    assert resp.json()["labels"] == {"document_type": "warning"}


def test_api_classify_item_unauthenticated_rejects_human_by(tmp_path, monkeypatch):
    """Acceptance check: API clients cannot impersonate arbitrary human:* editors."""
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning", "by": "human:alice",
    })
    assert resp.status_code == 403


def test_api_classify_item_with_token_requires_auth(tmp_path, monkeypatch):
    """Acceptance check: with a token enabled, unauthenticated edits fail
    (401, prompting the browser via WWW-Authenticate) and authenticated
    edits succeed."""
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)

    unauthed = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning", "by": "llm:anthropic/claude-sonnet-5",
    })
    assert unauthed.status_code == 401
    assert unauthed.headers.get("www-authenticate", "").lower().startswith("basic")

    wrong_password = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning",
    }, auth=("editor", "wrong"))
    assert wrong_password.status_code == 401

    authed = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning",
    }, auth=("editor", "secret"))
    assert authed.status_code == 200


def test_api_classify_item_forces_human_web_ui_provenance_when_authenticated(tmp_path, monkeypatch):
    """An authenticated caller can't set its own `by` - it always writes as
    human:web-ui, even if it tries to claim a different identity."""
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning", "by": "human:someone-else",
    }, auth=("editor", "secret"))
    assert resp.status_code == 200

    with engine.connect() as conn:
        from sqlalchemy import text as sa_text
        by = conn.execute(sa_text(
            "SELECT by FROM classifications WHERE item_id='i1' AND superseded_at IS NULL"
        )).scalar()
    assert by == "human:web-ui"


def test_cross_site_origin_rejected_on_write(tmp_path, monkeypatch):
    """Acceptance check: a cross-site form submission is rejected."""
    client, engine, app_module = _client(tmp_path, monkeypatch)
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "document_type", "value": "warning", "by": "llm:anthropic/claude-sonnet-5",
    }, headers={"origin": "https://evil.example.com"})
    assert resp.status_code == 403


def test_reads_stay_public_with_token_configured(tmp_path, monkeypatch):
    """Acceptance check: public read/search routes still work without a
    password, even when a write token is configured."""
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)

    assert client.get("/item/i1").status_code == 200
    assert client.get("/api/items/i1").status_code == 200
    assert client.get("/rules").status_code == 200


def test_startup_warns_when_auth_token_unset(tmp_path, monkeypatch, caplog):
    """Acceptance check: with no token, a clear startup warning is logged."""
    import logging
    monkeypatch.setenv("ACTIVITIES_PATH", "config/activities.yaml")
    client, engine, app_module = _client(tmp_path, monkeypatch)
    with caplog.at_level(logging.WARNING, logger="webui.app"):
        with client:
            pass
    assert any("WEBUI_AUTH_TOKEN" in r.message for r in caplog.records)


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
    client, engine, app_module = _client(tmp_path, monkeypatch, auth_token="secret")
    _insert_item(engine)

    resp = client.post("/api/items/i1/classifications", json={
        "axis": "instrument", "value": "mica",
    }, auth=("editor", "secret"))
    assert resp.status_code == 200

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
