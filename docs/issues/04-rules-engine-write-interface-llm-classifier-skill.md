# Rules engine, classification write interface and LLM classifier skill

**Size:** M  **Depends on:** #5 taxonomy and classification store  **Design doc:** docs/design/regradar-architecture.md §4.3, §4.5, §4.6

## Why

With the store in place (#5), nothing yet populates `instrument` labels beyond source defaults, and `document_type` is unset for every mixed feed. Keyword matching today is a case-insensitive substring check that writes a bare tag with no confidence. This issue replaces it with a weighted rules engine, exposes the write interface to agents, and ships the LLM classifier as a skill so any MCP-connected agent can work the backlog without the repo holding keys or picking a vendor.

## Scope

- `config/rules.yaml` per §4.3, replacing `config/alerts.yaml`. A migration script converts each existing `keyword: X, tag: Y` rule into a rule on the nearest axis and value where one exists (for example `MiCA` becomes `instrument: mica`) and prints the rest for manual handling. `ALERTS_PATH` becomes `RULES_PATH`.
- A `regradar.rules` module: compile patterns once per run, score per (axis, value) with field weighting and the title multiplier, cap at 1.0, write rows through `write_classification` with `by: rule` and `classifier_id` set to the sha256 of the rules file plus taxonomy version. Alias lists from `taxonomy.yaml` compile into implicit rules at weight 0.7 with word-boundary matching.
- Rules run on insert in the collector and via `regradar reclassify --by rule`, which supersedes prior rule rows and rewrites them.
- Write interface per §4.5: `POST /api/items/{id}/classifications` (already created in #5 for human writes) accepts any `by` value; `classify_item_tool(item_id, axis, value, by, confidence, classifier_id)` on the MCP server; both return the item's new effective labels. Batch variant `classify_items_tool(classifications: list)` accepting up to 100 in one call, transactional.
- `get_unclassified_tool(axis, limit, offset, regulator_id=None, since=None)` and `GET /api/unclassified` returning items with no live row on the axis, oldest first, including `title`, `body_md` truncated to a configurable number of characters (default 6000), `regulator`, `jurisdiction`, and the current labels on other axes.
- `get_taxonomy_tool()` returning the current axes, values, names, aliases and version so an agent never needs filesystem access to the config.
- `skills/regradar-classifier/SKILL.md`: a plain-Markdown skill instructing an agent to call `get_taxonomy_tool`, page through `get_unclassified_tool` for a chosen axis, classify each item, and write back via `classify_items_tool` with `by: llm:<provider>/<model>` and a confidence. The skill must tell the agent to prefer "unset" over a guess below 0.5, to label instruments only where the document is substantively about them rather than mentioning them in passing, and to use `document_type` values strictly as defined in the taxonomy names. Include five worked examples in the skill with the expected output.
- A `scripts/eval_classifier.py` that takes a labelled sample (a CSV of item_id, axis, expected value, maintained by hand under `tests/fixtures/classification/gold.csv`, at least 100 rows across both axes) and reports precision and recall per axis and per value for `rule` rows and, when present, `llm:*` rows. This is how we find out the early classifications are wrong.
- Web UI: the `/alerts` page becomes `/rules`, listing rules per axis and value with add and delete, writing to `rules.yaml`.

## Non-goals

- The unattended batch runner with provider SDKs is #14. This issue's LLM path is agent-driven via the skill.
- No embedding or semantic classification.
- No changes to profiles (#7) or export (#8).

## Design notes

Rule scoring: for each rule, for each field in `fields`, count matches of each pattern; score contribution is `weight` for one or more matches (not multiplied by count, which over-rewards long documents), times `title_multiplier` when the field is `title`. Sum contributions across the rule's patterns and fields, cap at 1.0. Tests cover: single match, title boost, cap, case flag, a pattern that must not match inside a longer word.

The `min_confidence` for writing a rule row defaults to 0.5 at the top of `rules.yaml`. The threshold for a row to count towards effective labels is separate and lives in `taxonomy.yaml` (§4.4). Keep them separate: the first controls noise in the store, the second controls what users see.

`classify_items_tool` must validate every entry before writing any, and return per-entry errors rather than failing the batch on one bad value, so an agent working through 100 items does not lose 99 good labels to one typo. The transactional guarantee is that either all valid entries are written or none are.

The skill lives alongside the existing operator and onboarding skills and follows their format. It should reference the operator context file so an agent classifying on behalf of a Maltese CASP is not tempted to over-label MiCA on everything; the taxonomy is objective and the agent's job is to say what the document is about, not whether the operator cares.

Confidence from an LLM is self-reported and not calibrated. The eval script exists to measure this. Record the first eval results in the PR so the baseline is known.

## Acceptance criteria

- [ ] `rules.yaml` migrated from `alerts.yaml`; the migration output has been reviewed and unmapped rules listed in the PR.
- [ ] Running the collector on the existing dataset with `reclassify --by rule` writes `instrument` rows for MiCA-related items with confidence above 0.5 and none for an FCA Consumer Duty item, verified on a named sample of 10 items each.
- [ ] `classify_items_tool` with one invalid value in a batch of three writes the two valid rows and reports the third.
- [ ] `get_unclassified_tool(axis="document_type")` count decreases by exactly the number of items an agent classifies in a session using the skill, on a test run of at least 50 items.
- [ ] `scripts/eval_classifier.py` runs against the gold sample and prints per-axis precision and recall; the baseline numbers are recorded in the PR.
- [ ] A human row written before `reclassify --by rule` is unchanged after it.
- [ ] The `/rules` page round-trips a rule add and delete to `rules.yaml` and the next collector run applies it.
- [ ] Full test suite passes.

## Open questions

- Whether rules should also be allowed to write negative evidence (this pattern means it is not a consultation). Not in v2 unless the gold sample shows a specific need.
- Whether `get_unclassified_tool` should optionally return items whose only live rows are below the effective threshold, since those are also effectively unclassified. Recommendation: yes, behind a flag `include_low_confidence`.
