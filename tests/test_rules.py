import hashlib

import pytest

from regradar.rules import RulesEngine, load_raw_rules, write_raw_rules
from regradar.taxonomy import build_taxonomy

TAXONOMY_PATH = "config/taxonomy.yaml"
JURISDICTIONS_PATH = "config/jurisdictions.yaml"


def _taxonomy():
    return build_taxonomy(TAXONOMY_PATH, JURISDICTIONS_PATH)


def _write_rules(tmp_path, content: str):
    path = tmp_path / "rules.yaml"
    path.write_text(content)
    return str(path)


# --- scoring: design notes cases (single match, title boost, cap, case flag,
# no-match-inside-a-longer-word) ---

# These use `document_type`/`consultation` deliberately - that axis's values
# carry no `aliases` in config/taxonomy.yaml, so scores here reflect only the
# explicit rule under test, not an interfering alias-derived implicit rule
# (see the dedicated "alias-derived implicit rules" tests below for those).

def test_single_match_scores_the_pattern_weight(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'consultation', weight: 0.6}]\n"
        "    fields: [body]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    scores = engine.score("An unrelated title", "This notice is a consultation on new rules.")
    assert scores[("document_type", "consultation")] == 0.6


def test_title_multiplier_boosts_title_field_only(tmp_path):
    path = _write_rules(tmp_path, (
        "min_confidence: 0.3\n"
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'consultation', weight: 0.4, flags: i}]\n"
        "    fields: [title, body]\n"
        "    title_multiplier: 1.5\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    title_scores = engine.score("Consultation paper", "no mention here")
    body_scores = engine.score("no mention here", "Consultation paper")
    assert title_scores[("document_type", "consultation")] == pytest.approx(0.6)  # 0.4 * 1.5
    assert body_scores[("document_type", "consultation")] == pytest.approx(0.4)  # unmultiplied


def test_score_caps_at_one(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match:\n"
        "      - {pattern: 'Consultation', weight: 0.9}\n"
        "      - {pattern: 'quarterly consultation', weight: 0.6, flags: i}\n"
        "    fields: [title, body]\n"
        "    title_multiplier: 1.5\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    scores = engine.score("Consultation and the quarterly consultation paper", "Consultation again")
    assert scores[("document_type", "consultation")] == 1.0


def test_case_flag_makes_pattern_case_insensitive(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'consultation', weight: 0.6, flags: i}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    assert engine.score("CONSULTATION paper", "") == {("document_type", "consultation"): 0.6}


def test_pattern_without_case_flag_is_case_sensitive(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'Consultation', weight: 0.6}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    assert engine.score("consultation paper", "") == {}


def test_word_boundary_pattern_does_not_match_inside_a_longer_word(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: '\\bCP\\b', weight: 0.6}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    # "CPX26" contains "CP" as a substring but not as a whole word.
    assert engine.score("CPX26 update", "") == {}
    assert engine.score("CP26 quarterly paper", "") == {}  # "CP26" isn't "CP" either
    assert engine.score("the CP process", "") == {("document_type", "consultation"): 0.6}


def test_below_min_confidence_is_not_returned(tmp_path):
    path = _write_rules(tmp_path, (
        "min_confidence: 0.8\n"
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'consultation', weight: 0.5}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    assert engine.score("consultation notice", "") == {}


def test_min_confidence_defaults_to_point_five(tmp_path):
    path = _write_rules(tmp_path, "rules: []\n")
    engine = RulesEngine.load(path, _taxonomy())
    assert engine.min_confidence == 0.5


def test_unknown_axis_value_rule_is_skipped(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: not-a-real-value\n"
        "    match: [{pattern: 'x', weight: 0.9}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    assert engine.score("x", "") == {}


def test_invalid_regex_pattern_is_skipped_not_fatal(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: '(unclosed', weight: 0.9}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())  # must not raise
    assert engine.score("anything", "") == {}


def test_missing_rules_file_still_builds_alias_rules(tmp_path):
    engine = RulesEngine.load(str(tmp_path / "does_not_exist.yaml"), _taxonomy())
    # config/taxonomy.yaml's "mica" instrument carries the alias "MiCA".
    assert engine.score("A note on MiCA compliance", "") == {("instrument", "mica"): 0.7}


# --- alias-derived implicit rules (../wiki/content/component-rules.md:
# weight 0.7, word-boundary matched) ---

def test_alias_rule_matches_at_implicit_weight(tmp_path):
    path = _write_rules(tmp_path, "rules: []\n")
    engine = RulesEngine.load(path, _taxonomy())
    scores = engine.score("MiCAR obligations for CASPs", "")
    assert scores[("instrument", "mica")] == 0.7


def test_explicit_and_alias_rule_for_same_value_sum_and_cap_at_one(tmp_path):
    # An explicit rule scoring 0.9 for "mica" plus the 0.7 alias-derived rule
    # both matching sum to 1.6, capped at 1.0 (../wiki/content/component-rules.md:
    # "Scores sum per (axis, value), capped at 1.0").
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: instrument\n"
        "    value: mica\n"
        "    match: [{pattern: 'MiCA', weight: 0.9}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    scores = engine.score("MiCA update", "")
    assert scores[("instrument", "mica")] == 1.0


def test_two_sub_threshold_rules_for_the_same_value_sum_above_threshold(tmp_path):
    # Neither rule alone reaches the default min_confidence (0.5), but their
    # sum (0.3 + 0.4 = 0.7) does.
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'consultation', weight: 0.3, flags: i}]\n"
        "    fields: [title]\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'quarterly', weight: 0.4, flags: i}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    scores = engine.score("Quarterly consultation paper", "")
    assert scores[("document_type", "consultation")] == pytest.approx(0.7)


# --- classifier_id ---

def test_classifier_id_is_sha256_of_file_bytes_plus_taxonomy_version(tmp_path):
    path = _write_rules(tmp_path, "rules: []\n")
    taxonomy = _taxonomy()
    engine = RulesEngine.load(path, taxonomy)
    expected = hashlib.sha256(open(path, "rb").read() + str(taxonomy.version()).encode()).hexdigest()
    assert engine.classifier_id == expected


def test_classifier_id_changes_when_rules_file_content_changes(tmp_path):
    taxonomy = _taxonomy()
    path1 = _write_rules(tmp_path, "rules: []\n")
    id1 = RulesEngine.load(path1, taxonomy).classifier_id
    path2 = _write_rules(tmp_path, "rules: []\n# a comment\n")
    id2 = RulesEngine.load(path2, taxonomy).classifier_id
    assert id1 != id2


# --- raw load/write round trip (webui /rules backing) ---

def test_load_raw_rules_missing_file_returns_defaults(tmp_path):
    min_confidence, rules = load_raw_rules(str(tmp_path / "missing.yaml"))
    assert min_confidence == 0.5
    assert rules == []


def test_write_raw_rules_round_trips_with_load_raw_rules(tmp_path):
    path = tmp_path / "rules.yaml"
    rules = [{"axis": "instrument", "value": "mica", "match": [{"pattern": "MiCA", "weight": 0.9}], "fields": ["title"]}]
    write_raw_rules(str(path), 0.6, rules)
    min_confidence, loaded = load_raw_rules(str(path))
    assert min_confidence == 0.6
    assert loaded == rules


def test_write_raw_rules_includes_header_comment(tmp_path):
    path = tmp_path / "rules.yaml"
    write_raw_rules(str(path), 0.5, [])
    content = path.read_text()
    assert content.startswith("#")
    assert "classification rules" in content.lower()


# --- regex safety (issue #20 part B): oversized/catastrophic user patterns ---

def test_oversized_pattern_is_skipped_at_load_time(tmp_path):
    from regradar.rules import MAX_PATTERN_LENGTH

    oversized = "a" * (MAX_PATTERN_LENGTH + 1)
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        f"    match: [{{pattern: '{oversized}', weight: 0.9}}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())  # must not raise
    assert engine.score(oversized, "") == {}


def test_catastrophic_pattern_times_out_and_is_skipped_but_collection_continues(tmp_path):
    # (a|aa)+$ exhibits classic exponential backtracking; the `(a+)+$`
    # example from the issue is actually optimized away by the `regex`
    # engine, so this pattern is used to force a real timeout instead.
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: '(a|aa)+$', weight: 0.9}]\n"
        "    fields: [title]\n"
        "  - axis: instrument\n"
        "    value: dora\n"
        "    match: [{pattern: 'DORA', weight: 0.9}]\n"
        "    fields: [title]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    evil_title = "a" * 40 + "!"
    # The catastrophic rule times out and contributes nothing, but the
    # unrelated DORA rule still scores against a different item.
    assert engine.score(evil_title, "") == {}
    # DORA's explicit rule (0.9) plus its taxonomy alias rule (0.7) sum and
    # cap at 1.0 - see test_explicit_and_alias_rule_for_same_value_sum_and_cap_at_one.
    assert engine.score("DORA update", "") == {("instrument", "dora"): 1.0}


def test_normal_rules_still_behave_with_regex_engine(tmp_path):
    path = _write_rules(tmp_path, (
        "rules:\n"
        "  - axis: document_type\n"
        "    value: consultation\n"
        "    match: [{pattern: 'consultation', weight: 0.6, flags: i}]\n"
        "    fields: [title, body]\n"
    ))
    engine = RulesEngine.load(path, _taxonomy())
    assert engine.score("A Consultation paper", "") == {("document_type", "consultation"): 0.6}
    assert engine.score("nothing here", "no mention either") == {}
