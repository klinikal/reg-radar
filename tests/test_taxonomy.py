import pytest

from regradar.taxonomy import (
    Taxonomy,
    TaxonomyError,
    build_taxonomy,
    validate_regulator_jurisdictions,
)


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


VALID_TAXONOMY = """\
version: 1
axes:
  - id: document_type
    cardinality: one
    values:
      - id: warning
        name: Warning
      - id: press_release
        name: Press release
  - id: instrument
    cardinality: many
    values:
      - id: mica
        name: MiCA
        jurisdiction: eu
"""

VALID_JURISDICTIONS = """\
- id: eu
  name: European Union
  type: supranational
- id: fr
  name: France
  parents: [eu]
- id: gb
  name: United Kingdom
"""


def _build(tmp_path, taxonomy_yaml=VALID_TAXONOMY, jurisdictions_yaml=VALID_JURISDICTIONS):
    tpath = _write(tmp_path, "taxonomy.yaml", taxonomy_yaml)
    jpath = _write(tmp_path, "jurisdictions.yaml", jurisdictions_yaml)
    return build_taxonomy(tpath, jpath)


def test_build_taxonomy_loads_valid_config(tmp_path):
    t = _build(tmp_path)
    assert t.version() == 1
    assert set(t.axes()) == {"document_type", "instrument"}
    assert t.cardinality("document_type") == "one"
    assert t.cardinality("instrument") == "many"
    assert t.is_valid("document_type", "warning") is True
    assert t.is_valid("document_type", "bogus") is False
    assert t.is_valid("instrument", "mica") is True


def test_source_default_confidence_reads_configured_value(tmp_path):
    taxonomy_yaml = """\
version: 1
axes:
  - id: document_type
    cardinality: one
    source_default_confidence: 1.0
    values:
      - id: warning
        name: Warning
  - id: instrument
    cardinality: many
    source_default_confidence: 0.6
    values:
      - id: mica
        name: MiCA
"""
    t = _build(tmp_path, taxonomy_yaml=taxonomy_yaml)
    assert t.source_default_confidence("document_type") == 1.0
    assert t.source_default_confidence("instrument") == 0.6


def test_source_default_confidence_falls_back_when_axis_omits_it(tmp_path):
    t = _build(tmp_path)  # VALID_TAXONOMY sets no source_default_confidence
    assert t.source_default_confidence("document_type") == 0.6


def test_source_default_confidence_applies_to_a_new_axis_with_no_code_change(tmp_path):
    """A new axis added to a copy of the taxonomy gets its own configured
    source_default_confidence with no code change - same principle as
    list_labels_tool picking up a new axis automatically."""
    taxonomy_yaml = VALID_TAXONOMY.replace(
        "axes:\n",
        "axes:\n  - id: topic\n    cardinality: many\n    source_default_confidence: 0.42\n"
        "    values:\n      - id: staking\n        name: Staking\n",
    )
    t = _build(tmp_path, taxonomy_yaml=taxonomy_yaml)
    assert "topic" in t.axes()
    assert t.source_default_confidence("topic") == 0.42


def test_ancestors_single_level(tmp_path):
    t = _build(tmp_path)
    assert t.ancestors("fr") == ["eu"]


def test_ancestors_eea_efta_state_lists_eu_directly(tmp_path):
    jurisdictions = VALID_JURISDICTIONS + '- id: "no"\n  name: Norway\n  parents: [eu]\n'
    t = _build(tmp_path, jurisdictions_yaml=jurisdictions)
    assert t.ancestors("no") == ["eu"]


def test_ancestors_root_jurisdiction_has_none(tmp_path):
    t = _build(tmp_path)
    assert t.ancestors("gb") == []


def test_ancestors_two_level_chain_returns_both_levels(tmp_path):
    jurisdictions = VALID_JURISDICTIONS + (
        "- id: us\n  name: United States\n"
        "- id: us-ny\n  name: New York\n  parents: [us]\n"
    )
    t = _build(tmp_path, jurisdictions_yaml=jurisdictions)
    assert t.ancestors("us-ny") == ["us"]

    # Also verify against a directly-constructed two-level chain so the
    # closure logic itself (not just this jurisdictions.yaml's shape) is
    # exercised with an ancestor that itself has a parent.
    t2 = Taxonomy(1, {}, {
        "a": {"parents": []},
        "b": {"parents": ["a"]},
        "c": {"parents": ["b"]},
    })
    assert t2.ancestors("c") == ["b", "a"]


def test_descendants_returns_every_jurisdiction_with_it_as_an_ancestor(tmp_path):
    t = _build(tmp_path)
    assert t.descendants("eu") == ["fr"]  # VALID_JURISDICTIONS: eu, fr (parent eu), gb (no parents)


def test_descendants_of_a_leaf_jurisdiction_is_empty(tmp_path):
    t = _build(tmp_path)
    assert t.descendants("fr") == []
    assert t.descendants("gb") == []


def test_descendants_transitive_through_a_multi_level_chain():
    t = Taxonomy(1, {}, {
        "a": {"parents": []},
        "b": {"parents": ["a"]},
        "c": {"parents": ["b"]},
    })
    assert set(t.descendants("a")) == {"b", "c"}
    assert t.descendants("c") == []


def test_missing_taxonomy_file_raises_taxonomy_error(tmp_path):
    jpath = _write(tmp_path, "jurisdictions.yaml", VALID_JURISDICTIONS)
    with pytest.raises(TaxonomyError, match="file not found"):
        build_taxonomy(str(tmp_path / "missing.yaml"), jpath)


def test_invalid_yaml_syntax_raises_taxonomy_error(tmp_path):
    tpath = _write(tmp_path, "taxonomy.yaml", "version: 1\naxes: [\n")
    jpath = _write(tmp_path, "jurisdictions.yaml", VALID_JURISDICTIONS)
    with pytest.raises(TaxonomyError):
        build_taxonomy(tpath, jpath)


def test_duplicate_axis_value_id_raises(tmp_path):
    taxonomy_yaml = """\
version: 1
axes:
  - id: document_type
    cardinality: one
    values:
      - id: warning
        name: Warning
      - id: warning
        name: Duplicate
"""
    tpath = _write(tmp_path, "taxonomy.yaml", taxonomy_yaml)
    jpath = _write(tmp_path, "jurisdictions.yaml", VALID_JURISDICTIONS)
    with pytest.raises(TaxonomyError, match="duplicate value id"):
        build_taxonomy(tpath, jpath)


def test_axis_value_referencing_unknown_jurisdiction_raises(tmp_path):
    taxonomy_yaml = """\
version: 1
axes:
  - id: instrument
    cardinality: many
    values:
      - id: mica
        name: MiCA
        jurisdiction: atlantis
"""
    tpath = _write(tmp_path, "taxonomy.yaml", taxonomy_yaml)
    jpath = _write(tmp_path, "jurisdictions.yaml", VALID_JURISDICTIONS)
    with pytest.raises(TaxonomyError, match="unknown jurisdiction 'atlantis'"):
        build_taxonomy(tpath, jpath)


def test_jurisdiction_with_unknown_parent_raises(tmp_path):
    jurisdictions = VALID_JURISDICTIONS + "- id: xx\n  name: Nowhere\n  parents: [atlantis]\n"
    tpath = _write(tmp_path, "taxonomy.yaml", VALID_TAXONOMY)
    jpath = _write(tmp_path, "jurisdictions.yaml", jurisdictions)
    with pytest.raises(TaxonomyError, match="unknown parent"):
        build_taxonomy(tpath, jpath)


def test_jurisdiction_cycle_raises(tmp_path):
    jurisdictions = "- id: a\n  name: A\n  parents: [b]\n- id: b\n  name: B\n  parents: [a]\n"
    tpath = _write(tmp_path, "taxonomy.yaml", VALID_TAXONOMY)
    jpath = _write(tmp_path, "jurisdictions.yaml", jurisdictions)
    with pytest.raises(TaxonomyError, match="cycle"):
        build_taxonomy(tpath, jpath)


def test_validate_regulator_jurisdictions_passes_for_known_ids(tmp_path):
    t = _build(tmp_path)
    validate_regulator_jurisdictions([{"id": "fr-amf", "jurisdiction": "fr"}], t, "regulators.yaml")


def test_validate_regulator_jurisdictions_raises_for_unknown_id(tmp_path):
    t = _build(tmp_path)
    with pytest.raises(TaxonomyError, match="unknown jurisdiction"):
        validate_regulator_jurisdictions([{"id": "xx-reg", "jurisdiction": "atlantis"}], t, "regulators.yaml")
