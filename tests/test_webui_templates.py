import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from webui.app import templates


def _render_index(regulators):
    template = templates.get_template("index.html")
    return template.render(
        request=None,
        q="", exact=False, date_from="", date_to="",
        regulator_id=[], regulators=regulators, regulator_names={},
        months=[], items=[], total=0, page=1, total_pages=1,
        filter_qs="", filter_qs_no_date="", filter_qs_no_regulator="",
    )


def test_regulator_panel_shows_total_items_and_last_fetched():
    regulators = [{
        "id": "mt-mfsa", "name": "MFSA", "full_name": "Malta Financial Services Authority",
        "jurisdiction": "MT", "jurisdiction_type": "national", "website": "https://mfsa.mt",
        "mica_competent_authority": True, "enabled": True,
        "total_items": 142, "last_fetched_at": "2026-07-02 17:19:23.665000",
        "consecutive_failures": 0,
        "country": "Malta", "display_label": "Malta - MFSA - Malta Financial Services Authority",
        "search_text": "malta mt mfsa malta financial services authority mt-mfsa",
    }]
    html = _render_index(regulators)
    assert "142" in html
    assert "2026-07-02" in html


def test_regulator_panel_flags_consecutive_failures():
    regulators = [{
        "id": "lt-lb", "name": "Bank of Lithuania", "full_name": "Bank of Lithuania",
        "jurisdiction": "LT", "jurisdiction_type": "national", "website": "https://lb.lt",
        "mica_competent_authority": True, "enabled": True,
        "total_items": 10, "last_fetched_at": "2026-07-02 17:19:23.665000",
        "consecutive_failures": 3,
        "country": "Lithuania", "display_label": "Lithuania - Bank of Lithuania - Bank of Lithuania",
        "search_text": "lithuania lt bank of lithuania bank of lithuania lt-lb",
    }]
    html = _render_index(regulators)
    assert "reg-unhealthy" in html
    assert "reg-warning" in html


def test_regulator_panel_healthy_source_has_no_warning_class():
    regulators = [{
        "id": "mt-mfsa", "name": "MFSA", "full_name": "Malta Financial Services Authority",
        "jurisdiction": "MT", "jurisdiction_type": "national", "website": "https://mfsa.mt",
        "mica_competent_authority": True, "enabled": True,
        "total_items": 142, "last_fetched_at": "2026-07-02 17:19:23.665000",
        "consecutive_failures": 0,
        "country": "Malta", "display_label": "Malta - MFSA - Malta Financial Services Authority",
        "search_text": "malta mt mfsa malta financial services authority mt-mfsa",
    }]
    html = _render_index(regulators)
    assert "reg-unhealthy" not in html
    assert "reg-warning" not in html


def test_regulator_panel_handles_never_fetched_regulator():
    regulators = [{
        "id": "xx-new", "name": "New Reg", "full_name": "New Regulator",
        "jurisdiction": "XX", "jurisdiction_type": "national", "website": "https://example.com",
        "mica_competent_authority": False, "enabled": True,
        "total_items": 0, "last_fetched_at": None,
        "consecutive_failures": 0,
        "country": "XX", "display_label": "XX - New Reg - New Regulator",
        "search_text": "xx new reg new regulator xx-new",
    }]
    html = _render_index(regulators)
    assert "0 items" in html


def test_index_renders_source_and_content_tags_separately(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module
    from collector.storage import init_db, insert_item
    from collector.schema import Item, make_item_id
    from datetime import datetime, timezone

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    app_module._engine = None
    engine = app_module.get_engine()
    now = datetime.now(timezone.utc)
    insert_item(engine, Item(
        id=make_item_id("esma", "https://x/a", now), regulator_id="esma", source_label="all_news",
        title="Tagged item", url="https://x/a", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="a", source_type="rss",
        source_tags=["warnings"], content_tags=["mica"],
    ))

    client = TestClient(app_module.app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "warnings" in resp.text
    assert "mica" in resp.text
