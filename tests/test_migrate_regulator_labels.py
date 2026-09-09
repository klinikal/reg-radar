import tempfile

from ruamel.yaml import YAML

from scripts.migrate_regulator_labels import decide_labels, migrate_regulators, run, _map_jurisdiction


def _yaml():
    y = YAML()
    y.preserve_quotes = True
    return y


def test_map_jurisdiction_lowercases_and_maps_uk_to_gb():
    assert _map_jurisdiction("EU") == "eu"
    assert _map_jurisdiction("FR") == "fr"
    assert _map_jurisdiction("UK") == "gb"


def test_decide_labels_maps_warnings_tag_to_document_type():
    labels, reason = decide_labels("fr-amf", {"label": "warnings", "tags": ["casp", "mica", "warnings"]})
    assert labels == {"document_type": "warning"}
    assert "warning" in reason


def test_decide_labels_warnings_wins_over_news_when_both_present():
    labels, _ = decide_labels("pl-knf", {"label": "all_news", "tags": ["casp", "mica", "news", "warnings"]})
    assert labels["document_type"] == "warning"


def test_decide_labels_publications_alone_gets_no_document_type():
    labels, reason = decide_labels("mt-mfsa", {"label": "library", "tags": ["casp", "mica", "publications"]})
    assert "document_type" not in labels
    assert "instrument" not in labels


def test_decide_labels_single_purpose_source_gets_instrument_default():
    labels, reason = decide_labels("esma", {"label": "mica_hub_page", "tags": ["casp", "mica", "page_watch"]})
    assert labels == {"document_type": "page_change", "instrument": ["mica"]}


def test_decide_labels_blanket_instrument_tag_on_non_single_purpose_source_is_dropped():
    labels, reason = decide_labels("esma", {"label": "all_news", "tags": ["casp", "mica", "warnings"]})
    assert "instrument" not in labels
    assert "dropped" in reason


def test_migrate_regulators_renames_tags_to_labels_and_lowercases_jurisdiction(tmp_path):
    yaml = _yaml()
    source_yaml = """\
- id: esma
  name: ESMA
  jurisdiction: EU
  sources:
    - type: scrape
      url: https://example.com/mica-hub
      label: mica_hub_page
      tags: [casp, mica, page_watch]
      notes: "the MiCA hub page"  # trailing comment preserved
- id: uk-fca
  name: FCA
  jurisdiction: UK
  sources:
    - type: rss
      url: https://example.com/warnings
      label: warnings
      tags: [casp, warnings]
"""
    src = tmp_path / "regulators.yaml"
    src.write_text(source_yaml)

    with open(src) as f:
        data = yaml.load(f)

    table = migrate_regulators(data)

    assert data[0]["jurisdiction"] == "eu"
    assert data[1]["jurisdiction"] == "gb"

    esma_source = data[0]["sources"][0]
    assert "tags" not in esma_source
    assert dict(esma_source["labels"]) == {"document_type": "page_change", "instrument": ["mica"]}

    fca_source = data[1]["sources"][0]
    assert "tags" not in fca_source
    assert dict(fca_source["labels"]) == {"document_type": "warning"}

    assert len(table) == 2

    # Comments survive a round-trip dump.
    out = tmp_path / "out.yaml"
    with open(out, "w") as f:
        yaml.dump(data, f)
    assert "trailing comment preserved" in out.read_text()


def test_migrate_regulators_gives_every_source_a_labels_key_even_when_empty():
    yaml = _yaml()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("""\
- id: mt-mfsa
  name: MFSA
  jurisdiction: MT
  sources:
    - type: scrape
      url: https://example.com/library
      label: library
      tags: [casp, mica, publications]
""")
        path = f.name

    with open(path) as f:
        data = yaml.load(f)
    migrate_regulators(data)
    source = data[0]["sources"][0]
    assert "tags" not in source
    assert dict(source["labels"]) == {}


def test_migrate_regulators_second_pass_leaves_already_migrated_source_untouched():
    yaml = _yaml()
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write("""\
- id: esma
  name: ESMA
  jurisdiction: EU
  sources:
    - type: scrape
      url: https://example.com/warnings
      label: warnings
      tags: [casp, mica, warnings]
""")
        path = f.name

    with open(path) as f:
        data = yaml.load(f)
    migrate_regulators(data)
    source = data[0]["sources"][0]
    assert dict(source["labels"]) == {"document_type": "warning"}

    # A second pass over the already-migrated structure must not recompute
    # `labels` from a now-absent `tags` key and silently wipe it out.
    migrate_regulators(data)
    assert dict(data[0]["sources"][0]["labels"]) == {"document_type": "warning"}


def test_run_is_idempotent_a_second_run_changes_nothing_on_disk(tmp_path):
    src = tmp_path / "regulators.yaml"
    src.write_text("""\
- id: esma
  name: ESMA
  jurisdiction: EU
  sources:
    - type: scrape
      url: https://example.com/warnings
      label: warnings
      tags: [casp, mica, warnings]
""")

    run(str(src), str(tmp_path / "nonexistent.db"), dry_run=False)
    first_pass_content = src.read_text()

    run(str(src), str(tmp_path / "nonexistent.db"), dry_run=False)
    second_pass_content = src.read_text()

    assert second_pass_content == first_pass_content
    assert "document_type: warning" in second_pass_content
