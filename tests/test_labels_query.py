import tempfile
from datetime import datetime, timezone

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator
from mcpserver.queries import list_labels, search_items
from regradar.classification import write_classification
from regradar.taxonomy import build_taxonomy


def _engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _taxonomy():
    return build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")


def _item(regulator_id, item_id):
    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id=regulator_id, source_label="all_news", title="Notice",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="...", source_type="rss",
    )


def test_list_labels_includes_every_taxonomy_axis_even_with_no_data():
    engine = _engine()
    taxonomy = _taxonomy()
    result = list_labels(engine, taxonomy)
    assert set(result.keys()) == {"document_type", "instrument"}
    assert result["document_type"] == []
    assert result["instrument"] == []


def test_list_labels_counts_sorted_most_common_first():
    engine = _engine()
    taxonomy = _taxonomy()
    insert_item(engine, _item("esma", "i1"))
    insert_item(engine, _item("esma", "i2"))
    insert_item(engine, _item("esma", "i3"))
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i2", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i3", "document_type", "press_release", by="rule", confidence=0.9)

    result = list_labels(engine, taxonomy)
    assert result["document_type"] == [
        {"value": "warning", "count": 2},
        {"value": "press_release", "count": 1},
    ]


def test_list_labels_appears_for_a_newly_added_axis_with_no_code_change(tmp_path):
    """A test asserts that adding a new axis to a copy of the config makes
    it appear in list_labels output with no code change (design notes,
    ../wiki/content/concept-taxonomy-classification.md)."""
    extra_taxonomy = tmp_path / "taxonomy.yaml"
    extra_taxonomy.write_text("""\
version: 1
axes:
  - id: topic
    cardinality: many
    values:
      - id: staking
        name: Staking
""")
    jurisdictions = tmp_path / "jurisdictions.yaml"
    jurisdictions.write_text("- id: eu\n  name: European Union\n")
    taxonomy = build_taxonomy(str(extra_taxonomy), str(jurisdictions))

    engine = _engine()
    result = list_labels(engine, taxonomy)
    assert "topic" in result


def test_search_items_jurisdiction_filter_expands_through_ancestors():
    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "fr-amf", "name": "AMF", "jurisdiction": "fr"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "jurisdiction": "gb"})
    insert_item(engine, _item("esma", "i-esma"))
    insert_item(engine, _item("fr-amf", "i-amf"))
    insert_item(engine, _item("uk-fca", "i-fca"))

    taxonomy = _taxonomy()

    fr_results = search_items(engine, "", jurisdictions=["fr"], taxonomy=taxonomy)
    assert {r["regulator_id"] for r in fr_results} == {"esma", "fr-amf"}

    gb_results = search_items(engine, "", jurisdictions=["gb"], taxonomy=taxonomy)
    assert {r["regulator_id"] for r in gb_results} == {"uk-fca"}


def test_search_items_labels_filter_by_document_type():
    engine = _engine()
    taxonomy = _taxonomy()
    insert_item(engine, _item("esma", "i1"))
    insert_item(engine, _item("esma", "i2"))
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i2", "document_type", "press_release", by="rule", confidence=0.9)

    results = search_items(engine, "", labels={"document_type": ["warning"]})
    assert [r["id"] for r in results] == ["i1"]


def test_search_items_labels_filter_multiple_axes_and_together():
    engine = _engine()
    taxonomy = _taxonomy()
    insert_item(engine, _item("esma", "i1"))
    insert_item(engine, _item("esma", "i2"))
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i1", "instrument", "mica", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i2", "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, "i2", "instrument", "dora", by="rule", confidence=0.9)

    results = search_items(engine, "", labels={"document_type": ["warning"], "instrument": ["mica"]})
    assert [r["id"] for r in results] == ["i1"]
