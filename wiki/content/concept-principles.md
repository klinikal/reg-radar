---
title: Principles
type: concept
sources: [docs/design/regradar-architecture.md]
last-verified: 2026-09-10
---

## Purpose

The five principles v2 is built against. Every other contract in the
architecture doc (document format, taxonomy, profiles, export) exists to
uphold these.

## How it works

1. **Config over code.** Vocabularies, hierarchies, rules and activity
   mappings live in YAML under `config/`. Adding a value, a jurisdiction or
   an activity must never need a code change or a migration.
2. **Objective labels, interpretive mappings.** The classifier assigns only
   matters of fact: which regulator, which jurisdiction, which legal
   instrument, what kind of document. "What kind of business cares about
   this" is a mapping in config that people can argue about in pull
   requests, never a label the classifier writes.
3. **Provenance on everything.** Every label records who assigned it, how
   confident they were, and which taxonomy version was in force.
   Reclassifying must be cheap and must never destroy human corrections.
4. **Interfaces before implementations.** The write interface for
   classifications exists before any classifier. The export contract exists
   before any adapter. The source adapter interface exists before the next
   regulator is added.
5. **Nothing silently disappears.** Unclassified items are visible by
   default in every profile. A stalled source is an alert, not an empty
   list.

## Inputs

None, this is a design constraint, not a runnable component.

## Outputs and side effects

None directly. These principles constrain the design of every other
contract described elsewhere in the architecture doc.

## How to run or invoke

Not applicable, this page describes a design principle set, not a runnable
component.

## How to test

Not applicable. Conformance to these principles is checked by review, not
by a command.

## Boundaries

A change that violates one of these principles (for example, hardcoding a
new jurisdiction in code instead of `config/`, or a classifier silently
overwriting a human correction) should not be merged without updating this
doc first, per the architecture doc's own rule that issues change the doc
before deviating from it.

## Related pages

[Canonical Document Format](concept-canonical-document-format.md)
[Taxonomy and Classification](concept-taxonomy-classification.md)
[Profiles and Activities](concept-profiles-activities.md)
[Export and Sync Contract](concept-export-sync-contract.md)
[Source Adapter Framework](concept-source-adapter-framework.md)
