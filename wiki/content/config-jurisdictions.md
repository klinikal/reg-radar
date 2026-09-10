---
title: Jurisdictions Config
type: config
sources: [config/jurisdictions.yaml]
last-verified: 2026-09-10
---

## Purpose

Defines the jurisdiction hierarchy used throughout reg-radar: a list of jurisdiction ids, names, and parent relationships that every regulator and profile jurisdiction reference must resolve against.

## How it works

The file is a flat YAML list of entries, each with an `id`, a `name`, and an optional `parents` list (a list, not a single parent, so the graph can be a DAG). An optional `type` marks non-country entries (`supranational` for `eu`, `international` for `intl`). An optional `note` explains modelling decisions.

EU member states list `parents: [eu]`. EEA-EFTA states Iceland (`is`) and Norway (`no`) also list `eu` directly as a parent (there is deliberately no separate "EEA" jurisdiction id), with a note that EU financial services law applies once incorporated into the EEA Agreement. `gb` (United Kingdom) and `sg` (Singapore) have no parents. `us` has no parent; `us-ny` and `us-ca` list `parents: [us]`.

The UAE (`ae`) models a federal/free-zone split: `ae` is the federal (onshore) jurisdiction; `ae-dubai` (Dubai emirate, VARA), `ae-difc` (DIFC free zone, DFSA), and `ae-adgm` (ADGM free zone, FSRA) all list `parents: [ae]` as flat siblings of each other, deliberately not parent/child of one another, because the free zones are carved out of both federal and emirate law.

`intl` (international standard-setting bodies, e.g. FATF) is a standalone root with no parent or child edges: its standards bind nobody directly, so it is not modelled as an ancestor of any jurisdiction. Profiles opt into it explicitly (e.g. `jurisdictions: [eu/*, intl]`).

## Inputs

Hand-edited YAML, grows by pull request.

## Outputs and side effects

Ancestors are computed as the transitive closure of `parents`. Every regulator's `jurisdiction` field in `config/regulators/*.yaml` must be an id defined here; the collector fails loudly at startup if it is not (`regradar/taxonomy.py`).

## How to run or invoke

Not directly invoked; loaded via `JURISDICTIONS_PATH` (see `.env.example`, default `/app/config/jurisdictions.yaml`).

## How to test

No dedicated test command in the file's own comments; validated indirectly by collector startup, which fails loudly on an unresolvable regulator jurisdiction.

## Boundaries

Must never introduce a jurisdiction id that a regulator file references without adding it here first, since the collector treats that as a fatal startup error. `intl` must never gain parent/child edges, per its note, since that would wrongly assert direct applicability.

## Related pages

[Taxonomy and Classification](concept-taxonomy-classification.md)
