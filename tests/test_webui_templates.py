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
        jurisdiction=[], jurisdiction_options=[], profile="", profile_options=[],
        months=[], items=[], total=0, page=1, total_pages=1,
        filter_qs="", filter_qs_no_date="", filter_qs_no_regulator="",
        filter_qs_no_jurisdiction="",
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


def test_regulator_panel_flags_stalled_source_distinctly_from_failures():
    regulators = [{
        "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
        "jurisdiction": "EU", "jurisdiction_type": "supranational", "website": "https://www.esma.europa.eu",
        "mica_competent_authority": True, "enabled": True,
        "total_items": 50, "last_fetched_at": "2026-07-02 17:19:23.665000",
        "consecutive_failures": 0, "stalled": True,
        "country": "EU", "display_label": "EU - ESMA - European Securities and Markets Authority",
        "search_text": "eu esma european securities and markets authority esma",
    }]
    html = _render_index(regulators)
    assert "reg-stalled" in html
    assert "reg-unhealthy" in html
    assert "reg-warning" not in html  # distinct from the consecutive-failure warning


def test_regulator_panel_not_stalled_shows_no_stall_warning():
    regulators = [{
        "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
        "jurisdiction": "EU", "jurisdiction_type": "supranational", "website": "https://www.esma.europa.eu",
        "mica_competent_authority": True, "enabled": True,
        "total_items": 50, "last_fetched_at": "2026-07-02 17:19:23.665000",
        "consecutive_failures": 0, "stalled": False,
        "country": "EU", "display_label": "EU - ESMA - European Securities and Markets Authority",
        "search_text": "eu esma european securities and markets authority esma",
    }]
    html = _render_index(regulators)
    assert "reg-stalled" not in html
    assert "reg-unhealthy" not in html


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
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    app_module._engine = None
    app_module._taxonomy = None
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


def test_alerts_redirects_to_rules(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module

    client = TestClient(app_module.app, follow_redirects=False)
    resp = client.get("/alerts")
    assert resp.status_code == 308
    assert resp.headers["location"] == "/rules"


def test_rules_view_lists_current_rules(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "min_confidence: 0.5\n"
        "rules:\n"
        "  - axis: instrument\n"
        "    value: mica\n"
        "    match:\n"
        "      - pattern: MiCA\n"
        "        weight: 0.9\n"
        "    fields: [title, body]\n"
    )
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app)
    resp = client.get("/rules")
    assert resp.status_code == 200
    assert "instrument" in resp.text
    assert "mica" in resp.text
    assert "MiCA" in resp.text


def test_rules_add_appends_rule_and_redirects(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module
    from regradar.rules import load_raw_rules

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("")
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app, follow_redirects=False)
    resp = client.post("/rules", data={
        "axis": "instrument", "value": "dora", "pattern": "DORA", "weight": "0.9",
        "flags": "i", "fields": ["title", "body"], "title_multiplier": "1.0",
    })
    assert resp.status_code == 303
    assert resp.headers["location"] == "/rules"
    _, rules = load_raw_rules(str(rules_path))
    assert rules == [{
        "axis": "instrument", "value": "dora", "match": [{"pattern": "DORA", "weight": 0.9, "flags": "i"}],
        "fields": ["title", "body"], "title_multiplier": 1.0,
    }]


def test_rules_add_rejects_unknown_axis_or_value(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("")
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app)
    resp = client.post("/rules", data={"axis": "instrument", "value": "not-a-real-value", "pattern": "x"})
    assert resp.status_code == 400


def test_rules_add_rejects_blank_pattern(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("")
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app)
    resp = client.post("/rules", data={"axis": "instrument", "value": "mica", "pattern": "   "})
    assert resp.status_code == 400


def test_rules_delete_removes_rule_by_index(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module
    from regradar.rules import load_raw_rules

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "min_confidence: 0.5\n"
        "rules:\n"
        "  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
        "  - axis: instrument\n    value: mifid2\n    match: [{pattern: MiFID, weight: 0.9}]\n    fields: [title]\n"
    )
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app, follow_redirects=False)
    resp = client.post("/rules/0/delete")
    assert resp.status_code == 303
    _, rules = load_raw_rules(str(rules_path))
    assert len(rules) == 1
    assert rules[0]["value"] == "mifid2"


def test_rules_delete_out_of_range_index_is_a_no_op(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module
    from regradar.rules import load_raw_rules

    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "min_confidence: 0.5\n"
        "rules:\n  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
    )
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app, follow_redirects=False)
    resp = client.post("/rules/5/delete")
    assert resp.status_code == 303
    _, rules = load_raw_rules(str(rules_path))
    assert len(rules) == 1


def test_api_unclassified_returns_items_with_no_live_row(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module
    from collector.storage import init_db, insert_item
    from collector.schema import Item, make_item_id
    from datetime import datetime, timezone

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    app_module._engine = None
    app_module._taxonomy = None
    engine = app_module.get_engine()
    now = datetime.now(timezone.utc)
    insert_item(engine, Item(
        id=make_item_id("esma", "https://x/unclassified", now), regulator_id="esma", source_label="all_news",
        title="An unclassified item", url="https://x/unclassified", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="a", source_type="rss",
    ))

    client = TestClient(app_module.app)
    resp = client.get("/api/unclassified", params={"axis": "document_type"})
    assert resp.status_code == 200
    ids = [i["id"] for i in resp.json()["items"]]
    assert make_item_id("esma", "https://x/unclassified", now) in ids


def test_rules_add_rejects_invalid_regex(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    import webui.app as app_module

    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    app_module._taxonomy = None
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("")
    monkeypatch.setattr(app_module, "RULES_PATH", str(rules_path))

    client = TestClient(app_module.app)
    resp = client.post("/rules", data={"axis": "instrument", "value": "mica", "pattern": "(unclosed"})
    assert resp.status_code == 400


def test_lifespan_fails_at_boot_on_invalid_taxonomy(monkeypatch, tmp_path):
    """A broken taxonomy must kill the process at startup, not turn every
    request into a 500 (load_and_exit_on_error calls sys.exit)."""
    import asyncio
    import pytest
    import webui.app as app_module

    bad = tmp_path / "taxonomy.yaml"
    bad.write_text("version: 1\naxes: []\n")  # empty axes list is invalid
    monkeypatch.setenv("TAXONOMY_PATH", str(bad))
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    app_module._taxonomy = None
    app_module._activities = None

    async def boot():
        async with app_module._lifespan(app_module.app):
            pass

    try:
        with pytest.raises(SystemExit):
            asyncio.run(boot())
    finally:
        app_module._taxonomy = None
        app_module._activities = None


def test_with_search_fields_names_countries_from_the_jurisdiction_config(monkeypatch):
    """Regulator jurisdictions are lower-case taxonomy ids (gb, not UK).
    A hand-maintained upper-case name map here silently lost the country
    name for every regulator once the ids moved."""
    import webui.app as app_module

    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    app_module._taxonomy = None

    reg = app_module._with_search_fields({
        "id": "uk-fca", "name": "FCA", "full_name": "Financial Conduct Authority", "jurisdiction": "gb",
    })

    assert reg["country"] == "United Kingdom"
    assert "united kingdom" in reg["search_text"]
    app_module._taxonomy = None
