"""Tests scripts/eval_classifier.py against a small, hand-built DB - not the
full gold.csv/production database (../wiki/content/component-classification.md:
"for the test suite, build a small DB
from a handful of those items")."""
import csv
from datetime import datetime, timezone

from collector.schema import Item
from collector.storage import init_db, insert_item
from regradar.classification import write_classification
from regradar.taxonomy import build_taxonomy
from scripts.eval_classifier import load_gold, score

TAXONOMY_PATH = "config/taxonomy.yaml"
JURISDICTIONS_PATH = "config/jurisdictions.yaml"


def _engine(tmp_path):
    return init_db(str(tmp_path / "test.db"))


def _insert(engine, item_id, title="Notice"):
    now = datetime.now(timezone.utc)
    insert_item(engine, Item(
        id=item_id, regulator_id="esma", source_label="all_news", title=title,
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet=title, source_type="rss",
    ))


def _gold_path(tmp_path, rows):
    path = tmp_path / "gold.csv"
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["item_id", "axis", "expected_value"])
        w.writerows(rows)
    return str(path)


def test_load_gold_reads_item_id_axis_expected_value(tmp_path):
    path = _gold_path(tmp_path, [("i1", "document_type", "warning"), ("i2", "instrument", "mica")])
    rows = load_gold(path)
    assert rows == [
        {"item_id": "i1", "axis": "document_type", "expected_value": "warning"},
        {"item_id": "i2", "axis": "instrument", "expected_value": "mica"},
    ]


def test_score_true_positive_and_false_negative(tmp_path):
    engine = _engine(tmp_path)
    taxonomy = build_taxonomy(TAXONOMY_PATH, JURISDICTIONS_PATH)
    _insert(engine, "i1")
    _insert(engine, "i2")
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)
    # i2 has no rule row at all - a false negative against gold.

    gold = [
        {"item_id": "i1", "axis": "document_type", "expected_value": "warning"},
        {"item_id": "i2", "axis": "document_type", "expected_value": "enforcement"},
    ]
    stats = score(engine, gold, "rule")
    assert stats[("document_type", "warning")] == {"tp": 1, "fp": 0, "fn": 0}
    assert stats[("document_type", "enforcement")] == {"tp": 0, "fp": 0, "fn": 1}


def test_score_false_positive_when_predicted_but_not_expected(tmp_path):
    engine = _engine(tmp_path)
    taxonomy = build_taxonomy(TAXONOMY_PATH, JURISDICTIONS_PATH)
    _insert(engine, "i1")
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="rule", confidence=0.9)

    gold = [{"item_id": "i1", "axis": "document_type", "expected_value": "enforcement"}]
    stats = score(engine, gold, "rule")
    assert stats[("document_type", "warning")] == {"tp": 0, "fp": 1, "fn": 0}
    assert stats[("document_type", "enforcement")] == {"tp": 0, "fp": 0, "fn": 1}


def test_score_only_matches_the_requested_provenance(tmp_path):
    engine = _engine(tmp_path)
    taxonomy = build_taxonomy(TAXONOMY_PATH, JURISDICTIONS_PATH)
    _insert(engine, "i1")
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="human:alice", confidence=1.0)

    gold = [{"item_id": "i1", "axis": "document_type", "expected_value": "warning"}]
    rule_stats = score(engine, gold, "rule")
    assert rule_stats.get(("document_type", "warning"), {"tp": 0, "fp": 0, "fn": 1})["fn"] == 1


def test_score_llm_provenance_matches_any_llm_prefixed_by():
    from scripts.eval_classifier import _matches_provenance
    assert _matches_provenance("llm:anthropic/claude-sonnet-5", "llm") is True
    assert _matches_provenance("llm:openai/gpt-5", "llm") is True
    assert _matches_provenance("rule", "llm") is False


def test_eval_classifier_run_against_a_small_real_db(tmp_path, capsys):
    """End-to-end smoke test: a small DB built from a handful of gold rows,
    with the rules engine's actual output written in, printing precision/
    recall as scripts/eval_classifier.py's run() does."""
    from scripts.eval_classifier import run
    from regradar.rules import RulesEngine

    engine = _engine(tmp_path)
    taxonomy = build_taxonomy(TAXONOMY_PATH, JURISDICTIONS_PATH)
    rules_engine = RulesEngine.load(str(tmp_path / "no_rules.yaml"), taxonomy)  # alias rules only

    _insert(engine, "mica-item", title="New MiCA guidance published")
    _insert(engine, "unrelated-item", title="Annual staffing report")

    for item_id, title in [("mica-item", "New MiCA guidance published"), ("unrelated-item", "Annual staffing report")]:
        for (axis, value), confidence in rules_engine.score(title, "").items():
            write_classification(engine, taxonomy, item_id, axis, value, by="rule",
                                  confidence=confidence, classifier_id=rules_engine.classifier_id)

    gold_path = _gold_path(tmp_path, [
        ("mica-item", "instrument", "mica"),
        ("unrelated-item", "instrument", "mica"),  # a deliberate false negative to exercise recall < 1.0
    ])

    run(gold_path, str(tmp_path / "test.db"))
    out = capsys.readouterr().out
    assert "by: rule" in out
    assert "instrument" in out
    assert "precision=" in out and "recall=" in out
