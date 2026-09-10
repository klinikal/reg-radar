---
title: Taxonomy Config
type: config
sources: [config/taxonomy.yaml]
last-verified: 2026-09-10
---

## Purpose

Defines the classification vocabularies for the whole system: the fixed set of axes and values that everything else (rules, activities, profiles) is built on. Code must not hardcode any axis except "jurisdiction"; every other axis and value is read from this file.

## How it works

The file has a `version` integer, bumped on any change to values, and recorded on every classification row written (see `regradar/classification.py`). Under `axes`, each axis has an `id`, a `cardinality` (`one` or `many`), a `source_default_confidence` (how much weight a single-purpose feed's default label carries), and a list of `values`, each with an `id` and a display `name`.

Two axes exist today: `document_type` (cardinality `one`, e.g. consultation, final_guidance, technical_standard, legislation, enforcement, warning, speech, press_release, register_update, page_change) and `instrument` (cardinality `many`, the individual regulations/directives a document may relate to, e.g. mica, mifid2, dora, uk_fsma, sg_dtsp, ae_vara_regulations).

Each `instrument` value can carry `aliases` (alternate names/citations used to detect it in text) and a `jurisdiction` (which jurisdiction id it belongs to, from `config/jurisdictions.yaml`). Alias lists here are compiled into implicit classification rules at weight 0.7 automatically (see `config/rules.yaml`), so most instruments need no explicit rule entry.

## Inputs

Hand-edited YAML, grows by pull request, no code change required to add a value.

## Outputs and side effects

Read by the taxonomy loader (`regradar/taxonomy.py`) and consumed by the classification rules engine, the activities mapping, and profiles. `version` is stamped onto every classification row.

## How to run or invoke

Not directly invoked; loaded via `TAXONOMY_PATH` (see `.env.example`, default `/app/config/taxonomy.yaml`).

## How to test

No dedicated test command documented in this file's own comments; changes are validated by the collector's classification pipeline and its test suite under `tests/`.

## Boundaries

Instrument ids used elsewhere (activities.yaml, rules.yaml) must exactly match ids defined here. One instrument entry, `ae_sca_va_rules`, carries an explicit `[UNVERIFIED]` alias citation in its comment ("SCA Decision 26/RM of 2023") because primary sources could not be confirmed; do not treat that alias as fact.

## Related pages

[Taxonomy](component-taxonomy.md)
