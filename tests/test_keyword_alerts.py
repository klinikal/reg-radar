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


from collector.keyword_alerts import match_keywords


def test_match_keywords_returns_matching_tags():
    rules = [{"keyword": "MiCA", "tag": "mica"}, {"keyword": "travel rule", "tag": "travel-rule"}]
    assert match_keywords("New MiCA guidance published", rules) == ["mica"]


def test_match_keywords_is_case_insensitive():
    rules = [{"keyword": "MiCA", "tag": "mica"}]
    assert match_keywords("new mica update", rules) == ["mica"]


def test_match_keywords_no_match_returns_empty():
    rules = [{"keyword": "MiCA", "tag": "mica"}]
    assert match_keywords("Unrelated staffing notice", rules) == []


def test_match_keywords_dedupes_when_two_rules_share_a_tag():
    rules = [{"keyword": "MiCA", "tag": "mica"}, {"keyword": "Markets in Crypto-Assets", "tag": "mica"}]
    assert match_keywords("This is about Markets in Crypto-Assets, aka MiCA", rules) == ["mica"]


def test_match_keywords_handles_empty_text_and_rules():
    assert match_keywords("", [{"keyword": "MiCA", "tag": "mica"}]) == []
    assert match_keywords("some text", []) == []
