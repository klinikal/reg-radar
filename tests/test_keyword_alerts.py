from collector.keyword_alerts import load_alert_rules


def test_load_alert_rules_missing_file_returns_empty(tmp_path):
    assert load_alert_rules(str(tmp_path / "does_not_exist.yaml")) == []


def test_load_alert_rules_parses_valid_entries(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text(
        '- keyword: "MiCA"\n'
        "  tag: mica\n"
        '- keyword: "travel rule"\n'
        "  tag: travel-rule\n"
    )
    rules = load_alert_rules(str(path))
    assert rules == [
        {"keyword": "MiCA", "tag": "mica"},
        {"keyword": "travel rule", "tag": "travel-rule"},
    ]


def test_load_alert_rules_invalid_yaml_disables_rather_than_crashes(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text("not: valid: yaml: [")
    assert load_alert_rules(str(path)) == []


def test_load_alert_rules_non_list_top_level_disables(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text("just_a_string")
    assert load_alert_rules(str(path)) == []


def test_load_alert_rules_skips_entries_missing_required_fields(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text(
        '- keyword: "MiCA"\n'  # missing tag - skipped
        "- tag: orphan-tag\n"  # missing keyword - skipped
        '- keyword: "valid"\n'
        "  tag: valid-tag\n"  # this one is kept
    )
    rules = load_alert_rules(str(path))
    assert rules == [{"keyword": "valid", "tag": "valid-tag"}]


def test_load_alert_rules_empty_file_returns_empty(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text("")
    assert load_alert_rules(str(path)) == []
