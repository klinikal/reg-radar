---
title: Profiles and Activities
type: concept
sources: [docs/design/regradar-architecture.md]
last-verified: 2026-09-10
---

## Purpose

User profiles filter the whole corpus down to what a given regulated
entity needs, via an interpretive `activities.yaml` layer that maps
business activities to taxonomy instruments.

## How it works

`config/activities.yaml` is the interpretive layer, versioned by PR. Each
entry has an `id`, a `name`, and a list of `instruments` (taxonomy
instrument ids) it maps to. For example `casp` maps to
`[mica, amld, dora, travel_rule, uk_mlr, uk_fin_prom]`.

A profile file has: `name`, `jurisdictions` (a list, where `<id>/*` also
includes every descendant, so `eu/*` is ESMA-level plus every member state
regulator, while plain `eu` is ESMA-level only), `activities`,
`instruments` (additional explicit instruments), `exclude_instruments`
(hard exclude, subtracted last), `document_types` (empty means all),
`regulators` (empty means all in matched jurisdictions), `languages`, and
`include_unclassified` (default true).

Expansion:

1. Jurisdiction set = union of each selected jurisdiction and its
   ancestors, expanded with descendants where `<id>/*` syntax is used.
2. Instrument set = union of instruments from selected activities, plus
   explicit `instruments`, minus `exclude_instruments`.
3. An item matches if its jurisdiction is in the jurisdiction set, and
   (its instrument labels intersect the instrument set, or it has no
   instrument label and `include_unclassified` is true), and its
   document_type is in `document_types` when that list is non-empty, and
   the same for `regulators` and `languages`.

Profiles apply identically in the web UI (a profile picker), every MCP
query tool (a `profile` parameter), the JSON API, and the exporter.
Shipped templates live in `config/profiles/`. A user's own profile lives
wherever they like and is passed by path.

## Inputs

`config/activities.yaml`, a profile YAML file, and each item's effective
labels (jurisdiction, instrument, document_type, regulator, language) from
[Taxonomy and Classification](concept-taxonomy-classification.md).

## Outputs and side effects

A filtered view of the corpus, used by the web UI, MCP query tools, the
JSON API, and the exporter. Profiles do not write anything back to items.

## How to run or invoke

Not applicable at this level. See [Profiles](component-profiles.md) for the
runnable component.

## How to test

Not applicable at this level. See [Profiles](component-profiles.md) for the
command that proves profile expansion and matching work.

## Boundaries

Activities are an interpretive mapping and must never be written to item
frontmatter, they are derived at filter time only. `exclude_instruments`
is always applied last, after the union of activity and explicit
instruments.

## Related pages

[Principles](concept-principles.md)
[Taxonomy and Classification](concept-taxonomy-classification.md)
[Export and Sync Contract](concept-export-sync-contract.md)
[Profiles](component-profiles.md)
