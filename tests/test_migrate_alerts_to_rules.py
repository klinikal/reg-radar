from regradar.rules import load_raw_rules
from regradar.taxonomy import build_taxonomy
from scripts.migrate_alerts_to_rules import find_target, load_legacy_alert_rules, migrate, run

TAXONOMY_PATH = "config/taxonomy.yaml"
JURISDICTIONS_PATH = "config/jurisdictions.yaml"


def _taxonomy():
    return build_taxonomy(TAXONOMY_PATH, JURISDICTIONS_PATH)


def test_find_target_matches_tag_equal_to_value_id():
    assert find_target("MiCA", "mica", _taxonomy()) == ("instrument", "mica")


def test_find_target_matches_keyword_equal_to_an_alias():
    assert find_target("travel rule", "travel-rule", _taxonomy()) == ("instrument", "transfer_of_funds_regulation")


def test_find_target_matches_tag_as_prefix_of_value_id():
    assert find_target("MiFID", "mifid", _taxonomy()) == ("instrument", "mifid2")


def test_find_target_matches_document_type_axis_too():
    assert find_target("enforcement action", "enforcement", _taxonomy()) == ("document_type", "enforcement")


def test_find_target_returns_none_when_nothing_matches():
    assert find_target("VASP", "vasp", _taxonomy()) is None


def test_migrate_maps_and_reports_unmapped():
    alert_rules = [
        {"keyword": "MiCA", "tag": "mica"},
        {"keyword": "Strike", "tag": "strike"},
    ]
    migrated, unmapped = migrate(alert_rules, _taxonomy())
    assert len(migrated) == 1
    assert migrated[0]["axis"] == "instrument"
    assert migrated[0]["value"] == "mica"
    assert migrated[0]["match"] == [{"pattern": "MiCA", "weight": 0.7, "flags": "i"}]
    assert unmapped == [{"keyword": "Strike", "tag": "strike"}]


def test_load_legacy_alert_rules_parses_the_old_flat_format(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text('- keyword: "MiCA"\n  tag: mica\n- keyword: "travel rule"\n  tag: travel-rule\n')
    rules = load_legacy_alert_rules(str(path))
    assert rules == [{"keyword": "MiCA", "tag": "mica"}, {"keyword": "travel rule", "tag": "travel-rule"}]


def test_load_legacy_alert_rules_missing_file_returns_empty(tmp_path):
    assert load_legacy_alert_rules(str(tmp_path / "missing.yaml")) == []


def test_run_writes_rules_yaml_from_alerts_yaml(tmp_path):
    alerts_path = tmp_path / "alerts.yaml"
    alerts_path.write_text('- keyword: "MiCA"\n  tag: mica\n- keyword: "CASP"\n  tag: casp\n')
    rules_path = tmp_path / "rules.yaml"

    run(str(alerts_path), str(rules_path), TAXONOMY_PATH, JURISDICTIONS_PATH, dry_run=False)

    min_confidence, rules = load_raw_rules(str(rules_path))
    assert min_confidence == 0.5
    assert len(rules) == 1
    assert rules[0]["value"] == "mica"


def test_run_dry_run_does_not_write(tmp_path):
    alerts_path = tmp_path / "alerts.yaml"
    alerts_path.write_text('- keyword: "MiCA"\n  tag: mica\n')
    rules_path = tmp_path / "rules.yaml"

    run(str(alerts_path), str(rules_path), TAXONOMY_PATH, JURISDICTIONS_PATH, dry_run=True)

    assert not rules_path.exists()


def test_real_alerts_migration_matches_mfsa_v1_defaults():
    """Sanity check against the actual (now-committed) config/rules.yaml
    migration: MiCA/MiFID/travel-rule/enforcement-action map, Strike/CASP/
    VASP don't - see the task report for the reviewed migration output."""
    alert_rules = [
        {"keyword": "MiCA", "tag": "mica"},
        {"keyword": "MiFID", "tag": "mifid"},
        {"keyword": "travel rule", "tag": "travel-rule"},
        {"keyword": "enforcement action", "tag": "enforcement"},
        {"keyword": "Strike", "tag": "strike"},
        {"keyword": "CASP", "tag": "casp"},
        {"keyword": "VASP", "tag": "vasp"},
    ]
    migrated, unmapped = migrate(alert_rules, _taxonomy())
    assert len(migrated) == 4
    assert {(r["axis"], r["value"]) for r in migrated} == {
        ("instrument", "mica"), ("instrument", "mifid2"),
        ("instrument", "transfer_of_funds_regulation"), ("document_type", "enforcement"),
    }
    assert {r["tag"] for r in unmapped} == {"strike", "casp", "vasp"}
