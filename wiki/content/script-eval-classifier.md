---
title: Eval Classifier
type: script
sources: [scripts/eval_classifier.py]
last-verified: 2026-09-10
---

## Purpose

Measures precision and recall of the classification store against a hand-labelled gold
sample, so classifier accuracy (rule-based and LLM-based) is checked rather than assumed.

## How it works

`load_gold()` reads `tests/fixtures/classification/gold.csv` (columns: `item_id`, `axis`,
`expected_value`), skipping comment rows starting with `#`. `score()` pulls every live,
non-superseded `classifications` row for the gold sample's item ids, filters to rows matching
a given provenance (`_matches_provenance()`: `"rule"` means `by == "rule"`, `"llm"` means
`by` starts with `"llm:"`), and treats each `(item_id, axis)` pair as a multi-label set
comparison between predicted labels and gold labels, accumulating true positive/false
positive/false negative counts per `(axis, value)`.

Only items present in the gold sample are scored, this is an accuracy check on the labels
with ground truth, not a coverage check. `report()` prints precision and recall per axis and
value, plus a per-axis micro-average, for `by: rule` and `by: llm:*` separately.

## Inputs

- `tests/fixtures/classification/gold.csv` (default path, overridable via `--gold`).
- The live `classifications` table at `DB_PATH` (default `data/reg_radar.db`, overridable via
  `--db-path`).

## Outputs and side effects

Read-only: prints a precision/recall report to stdout. Writes nothing to the database or
filesystem.

## How to run or invoke

    .venv/bin/python -m scripts.eval_classifier
    .venv/bin/python -m scripts.eval_classifier --gold path/to/gold.csv --db-path data/reg_radar.db

## How to test

Run it directly and read the printed precision/recall numbers; there is no separate test
harness, the script's own output is the verification artifact. A "no rows to evaluate"
message for a provenance means no matching gold-labelled items have that kind of
classification yet, not an error.

## Boundaries

Must never write to the `classifications` table or any other part of the database, it is
read-only. Must only score items present in the gold sample, never treat an item's absence
from the gold sample as a false negative.

## Related pages

[reg-radar classify](cli-classify.md)
