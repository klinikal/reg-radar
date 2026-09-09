# Taxonomy, jurisdictions and classification store with provenance

**Size:** M  **Depends on:** #3 rename  **Design doc:** docs/design/regradar-architecture.md §4.1, §4.2, §4.4, §4.7

## Why

Tags today are two flat string lists. `mica` and `casp` sit on 108 of 114 tagged sources regardless of content, so filtering by them is meaningless, and there is no record of why any tag is there. Early classifications will be wrong. We need vocabularies that live in config, a jurisdiction hierarchy so "France" means France plus EU, and a store that records who said what with what confidence so reclassifying never destroys a human correction.

## Scope

- `config/taxonomy.yaml` per §4.1, seeded with the `document_type` vocabulary as specified and an initial `instrument` vocabulary of at least 25 entries covering EU and UK: MiCA, MiFID II/MiFIR, MAR, CSDR, EMIR, SFTR, DORA, the EU AML framework, PSD2/PSD3/PSR, EMD2, CRR/CRD, BRRD, DGSD, UCITS, AIFMD, PRIIPs, SFDR, Prospectus Regulation, Transfer of Funds Regulation (travel rule), FSMA 2000, Consumer Duty, MLR 2017, UK financial promotions regime, UK PSRs 2017, UK MAR. Each with `name`, `aliases`, `jurisdiction`.
- `config/jurisdictions.yaml` per §4.2 with every jurisdiction currently in `regulators.yaml`, lower-cased, plus `eu`, and `gb`. EEA-EFTA states list `eu` as parent with the note shown in the design doc.
- A `regradar.taxonomy` module that loads and validates both files at startup: unique ids, referenced parents exist, no cycles, every regulator's `jurisdiction` is a known id. Validation failure exits with a message naming the file and line. Exposes `ancestors(jurisdiction_id)`, `axes()`, `is_valid(axis, value)`, `version()`.
- `classifications` table per §4.4 and `items.labels` JSON column materialised per the precedence rules in §4.4. Precedence is implemented once, in `regradar.classification.effective_labels(rows) -> dict`, and unit-tested against a table of cases including: human overrides everything, multi-valued union, threshold cut-off, single-valued tie broken by provenance order, all rows superseded gives unset.
- `write_classification(item_id, axis, value, by, confidence, classifier_id)` as the single write path, validating against the taxonomy and re-materialising `items.labels` and `items.updated_at`. Rejects unknown axes and values with a clear error.
- `regradar reclassify --axis <a> [--by <provenance>] [--dry-run]` per §4.4, superseding and rewriting non-human rows. In this issue it re-applies source defaults only; #6 adds rules and the LLM path to the same command.
- Source defaults: `regulators.yaml` `sources[].tags` renamed to `sources[].labels` with the shape in §4.3, migrated per §4.7 by a script that rewrites the YAML and prints a table of what it decided per source for review. The collector writes source defaults on insert with `by: source_default`.
- Migration of existing per-item `content_tags` to `classifications` rows with `by: import`, confidence 0.5, per §4.7. Keep `source_tags` and `content_tags` columns readable for one release; every query path that reads them gains a deprecation warning in the log.
- Query layer: `mcpserver/queries.py` filters gain `jurisdictions` (expanded through ancestors), and per-axis filters `labels={"instrument": [...], "document_type": [...]}`. Existing `tags`, `source_tags`, `content_tags` parameters keep working against the old columns for one release. `list_tags_tool` gains a `list_labels_tool` sibling that returns each axis, its values and counts; the web UI regulator panel gains a jurisdiction filter that uses ancestors.
- Human corrections: `POST /api/items/{id}/classifications` accepts `by: human:<name>`; the web UI item page gets a minimal form to set document_type and add or remove instruments, writing through the same path.

## Non-goals

- The rules engine, the LLM classifier skill and `get_unclassified_tool` are #6. This issue makes the store and the write path exist and populates it from source defaults and imports only.
- Activities and profiles are #7.
- No attempt to make the initial instrument vocabulary complete. It grows by PR.

## Design notes

Vocabularies are config, so the code must not name any axis except `jurisdiction`. Anything that iterates axes reads them from `taxonomy.yaml`. A test asserts that adding a new axis to a copy of the config makes it appear in `list_labels_tool` output with no code change.

`items.labels` is denormalised for query speed. Filtering on a multi-valued axis in SQLite: use `json_each` over the labels column, or maintain a small `item_labels(item_id, axis, value)` table alongside the JSON. Recommendation: the side table, indexed on `(axis, value)`, rebuilt by the same materialisation function. Measure both on the existing dataset before choosing; record the numbers in the PR.

`taxonomy_version` is written on every row. When the file's version bumps, nothing is invalidated automatically; `reclassify --dry-run` reports how many live rows carry an older version so the operator can decide.

The migration script for `regulators.yaml` must preserve comments and formatting where the YAML library allows (use `ruamel.yaml` for this one script) because the notes fields are the documentation of why sources are configured as they are.

Jurisdiction ids in `regulators.yaml` are currently upper-case ISO alpha-2 or `EU`. The migration lower-cases them. The `regulators` table `jurisdiction` column is rewritten in the same migration.

## Acceptance criteria

- [ ] Startup fails with a clear message if `taxonomy.yaml` or `jurisdictions.yaml` is invalid, or a regulator references an unknown jurisdiction.
- [ ] `ancestors("fr") == ["eu"]`, `ancestors("no") == ["eu"]`, `ancestors("gb") == []`, and a test with a two-level chain returns both levels.
- [ ] `effective_labels` unit tests cover every case listed in Scope and pass.
- [ ] After migration, every enabled source has a `labels` block, no `tags` key remains, and the printed decision table has been reviewed in the PR.
- [ ] After migration, `SELECT COUNT(*) FROM classifications WHERE by='import'` equals the number of distinct (item, content_tag) pairs before migration.
- [ ] A human correction via the API changes `items.labels` immediately and survives `reclassify --axis instrument`.
- [ ] `search_items_tool(jurisdictions=["fr"])` returns AMF items and ESMA items; `jurisdictions=["gb"]` returns FCA items only.
- [ ] Filtering by `labels={"document_type": ["warning"]}` on the existing dataset returns at least the items previously found by `get_warnings_tool`, and the README documents the difference.
- [ ] Full test suite passes.

## Open questions

- Whether `get_warnings_tool` should be removed now or kept as an alias for `document_type=warning`. Recommendation: alias for one release, then remove.
- Whether the EEA should be its own jurisdiction id with `eu` as parent, so Norway lists `eea` rather than `eu`. It is cleaner but adds a level nobody filters on. Specified as direct `eu` parent; change if a real use appears.
