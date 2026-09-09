"""Precision/recall of the classification store against a hand-labelled
gold sample (docs/issues/04-rules-engine-write-interface-llm-classifier
-skill.md): "This is how we find out the early classifications are wrong."

Reads `tests/fixtures/classification/gold.csv` (item_id, axis,
expected_value - see that file's header for how rows were chosen) and the
live `classifications` table, and reports precision/recall per axis and per
value, separately for `by: rule` rows and (when present) `by: llm:*` rows.
Confidence from an LLM is self-reported and not calibrated - this script is
how that gets checked, not assumed.

Treats each (item_id, axis) as a multi-label set comparison: predicted =
every live row of the given provenance on that axis for that item; expected
= every gold row for that item/axis. Only items present in the gold sample
are scored - this is not a coverage check, just an accuracy check on the
labels we have ground truth for.

Run from the repository root:

    .venv/bin/python -m scripts.eval_classifier
    .venv/bin/python -m scripts.eval_classifier --gold path/to/gold.csv --db-path data/reg_radar.db
"""
import argparse
import csv
import os
from collections import defaultdict

from sqlalchemy import bindparam, text

from collector.storage import init_db


def load_gold(path: str) -> list[dict]:
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if not r["item_id"].startswith("#")]
    return rows


def _matches_provenance(by: str, provenance: str) -> bool:
    if provenance == "rule":
        return by == "rule"
    if provenance == "llm":
        return by.startswith("llm:")
    return by == provenance


def score(engine, gold_rows: list[dict], provenance: str) -> dict:
    """Returns {(axis, value): {"tp": n, "fp": n, "fn": n}}."""
    item_ids = sorted({r["item_id"] for r in gold_rows})
    if not item_ids:
        return {}
    sql = text("SELECT item_id, axis, value, by FROM classifications "
               "WHERE superseded_at IS NULL AND item_id IN :ids").bindparams(bindparam("ids", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(sql, {"ids": item_ids}).fetchall()

    predictions = defaultdict(set)
    for item_id, axis, value, by in rows:
        if _matches_provenance(by, provenance):
            predictions[(item_id, axis)].add(value)

    gold = defaultdict(set)
    for r in gold_rows:
        gold[(r["item_id"], r["axis"])].add(r["expected_value"])

    stats = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for (item_id, axis), expected in gold.items():
        predicted = predictions.get((item_id, axis), set())
        for value in predicted | expected:
            key = (axis, value)
            if value in predicted and value in expected:
                stats[key]["tp"] += 1
            elif value in predicted:
                stats[key]["fp"] += 1
            else:
                stats[key]["fn"] += 1
    return stats


def _precision_recall(tp: int, fp: int, fn: int) -> tuple:
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    return precision, recall


def _fmt(x) -> str:
    return f"{x:.2f}" if x is not None else "n/a"


def report(stats: dict, provenance: str) -> None:
    if not stats:
        print(f"\n=== by: {provenance} - no rows to evaluate ===")
        return
    print(f"\n=== by: {provenance} ===")
    by_axis = defaultdict(lambda: {"tp": 0, "fp": 0, "fn": 0})
    for (axis, value), counts in sorted(stats.items()):
        p, r = _precision_recall(**counts)
        print(f"  {axis:<15} {value:<30} precision={_fmt(p)} recall={_fmt(r)} "
              f"(tp={counts['tp']} fp={counts['fp']} fn={counts['fn']})")
        for k in ("tp", "fp", "fn"):
            by_axis[axis][k] += counts[k]
    print(f"  --- per-axis micro-average ---")
    for axis, counts in sorted(by_axis.items()):
        p, r = _precision_recall(**counts)
        print(f"  {axis:<15} {'(all values)':<30} precision={_fmt(p)} recall={_fmt(r)} "
              f"(tp={counts['tp']} fp={counts['fp']} fn={counts['fn']})")


def run(gold_path: str, db_path: str) -> None:
    engine = init_db(db_path)
    gold_rows = load_gold(gold_path)
    print(f"Loaded {len(gold_rows)} gold row(s) from {gold_path} "
          f"across {len({r['item_id'] for r in gold_rows})} item(s)")

    report(score(engine, gold_rows, "rule"), "rule")
    report(score(engine, gold_rows, "llm"), "llm:*")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", default="tests/fixtures/classification/gold.csv")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "data/reg_radar.db"))
    args = parser.parse_args()
    run(args.gold, args.db_path)


if __name__ == "__main__":
    main()
