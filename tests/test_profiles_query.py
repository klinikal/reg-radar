import tempfile
from datetime import datetime, timezone

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator
from mcpserver.queries import get_items_since, get_recent, get_warnings, search_items, search_items_advanced
from regradar.classification import write_classification
from regradar.profiles import expand_profile, load_activities, load_profile
from regradar.taxonomy import build_taxonomy


def _engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _taxonomy():
    return build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")


def _activities():
    return load_activities("config/activities.yaml", _taxonomy())


def _expanded(name_or_path):
    taxonomy = _taxonomy()
    activities = _activities()
    return expand_profile(load_profile(name_or_path, taxonomy, activities), taxonomy, activities)


def _item(regulator_id, item_id, url=None):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="Notice",
        url=url or f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
    )


def _seed_esma_and_bafin(engine):
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "de-bafin", "name": "BaFin", "jurisdiction": "de"})
    insert_item(engine, _item("esma", "i-esma"))
    insert_item(engine, _item("de-bafin", "i-bafin"))


def test_eu_casp_with_wildcard_jurisdiction_matches_esma_and_bafin():
    engine = _engine()
    _seed_esma_and_bafin(engine)
    expanded = _expanded("eu-casp")  # jurisdictions: [eu/*]

    results = search_items(engine, "", profile=expanded)
    assert {r["regulator_id"] for r in results} == {"esma", "de-bafin"}


def test_eu_jurisdiction_without_wildcard_matches_only_esma(tmp_path):
    engine = _engine()
    _seed_esma_and_bafin(engine)
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "eu-only.yaml"
    p.write_text("name: eu-only\njurisdictions: [eu]\nactivities: [casp]\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)

    results = search_items(engine, "", profile=expanded)
    assert {r["regulator_id"] for r in results} == {"esma"}


def test_include_unclassified_true_includes_item_with_no_instrument_label():
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("esma", "i1"))  # no classification written at all
    expanded = _expanded("eu-casp")
    assert expanded.include_unclassified is True

    results = search_items(engine, "", profile=expanded)
    assert [r["id"] for r in results] == ["i1"]


def test_include_unclassified_false_excludes_item_with_no_instrument_label(tmp_path):
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("esma", "i1"))
    taxonomy = _taxonomy()
    activities = _activities()
    p = tmp_path / "strict.yaml"
    p.write_text("name: strict\njurisdictions: [eu]\nactivities: [casp]\ninclude_unclassified: false\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)

    results = search_items(engine, "", profile=expanded)
    assert results == []


def test_include_unclassified_false_still_includes_item_with_matching_instrument(tmp_path):
    engine = _engine()
    taxonomy = _taxonomy()
    activities = _activities()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("esma", "i1"))
    write_classification(engine, taxonomy, "i1", "instrument", "mica", by="rule", confidence=0.9)
    p = tmp_path / "strict.yaml"
    p.write_text("name: strict\njurisdictions: [eu]\nactivities: [casp]\ninclude_unclassified: false\n")
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)

    results = search_items(engine, "", profile=expanded)
    assert [r["id"] for r in results] == ["i1"]


def test_zero_instruments_and_include_unclassified_false_matches_nothing(tmp_path):
    engine = _engine()
    taxonomy = _taxonomy()
    activities = _activities()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, _item("esma", "i1"))
    p = tmp_path / "empty.yaml"
    p.write_text("name: empty\njurisdictions: [eu]\ninclude_unclassified: false\n")  # no activities/instruments
    expanded = expand_profile(load_profile(str(p), taxonomy, activities), taxonomy, activities)

    assert search_items(engine, "", profile=expanded) == []


def test_omitting_profile_leaves_existing_behaviour_unchanged():
    engine = _engine()
    _seed_esma_and_bafin(engine)
    results = search_items(engine, "")
    assert {r["regulator_id"] for r in results} == {"esma", "de-bafin"}


def test_uk_equities_dealer_exclude_instruments_removes_mica_only_items(tmp_path):
    engine = _engine()
    taxonomy = _taxonomy()
    activities = _activities()
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "jurisdiction": "gb"})
    insert_item(engine, _item("uk-fca", "i-mica-only"))
    insert_item(engine, _item("uk-fca", "i-mifid2"))
    write_classification(engine, taxonomy, "i-mica-only", "instrument", "mica", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i-mifid2", "instrument", "mifid2", by="rule", confidence=0.9)
    expanded = _expanded("uk-equities-dealer")

    results = search_items(engine, "", profile=expanded)
    assert [r["id"] for r in results] == ["i-mifid2"]


def test_profile_param_on_get_recent_search_items_advanced_get_warnings_get_items_since():
    """Every query function the design notes list gets a profile param that
    actually filters (../wiki/content/concept-profiles-activities.md design notes)."""
    engine = _engine()
    _seed_esma_and_bafin(engine)
    expanded = _expanded("eu-casp")

    assert {r["regulator_id"] for r in get_recent(engine, profile=expanded)} == {"esma", "de-bafin"}
    items, total = search_items_advanced(engine, profile=expanded)
    assert {r["regulator_id"] for r in items} == {"esma", "de-bafin"}
    assert total == 2
    assert get_items_since(engine, "2000-01-01 00:00:00", profile=expanded)

    p = _expanded("eu-casp")
    # get_warnings filters on source_tags='warnings' independently of profile;
    # confirm the profile param is at least accepted and composes (AND).
    assert get_warnings(engine, days=0, profile=p) == []
