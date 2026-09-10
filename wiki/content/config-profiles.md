---
title: Profiles Config
type: config
sources: [config/profiles/]
last-verified: 2026-09-10
---

## Purpose

Per-firm-type filter definitions that select which jurisdictions, activities, instruments, document types, regulators, and languages a firm's view of the collected data should include.

## How it works

Each file in `config/profiles/` is a single YAML mapping with fields: `name`, `jurisdictions` (list, supports wildcards like `eu/*` to mean a jurisdiction and its descendants), `activities` (list of ids from `config/activities.yaml`), `instruments` (explicit instrument allow-list, empty means derive from activities), `exclude_instruments` (instrument ids to drop even if included via activities), `document_types` (empty means all), `regulators` (empty means all, unfiltered by regulator), `languages` (empty means all languages), and `include_unclassified` (boolean, whether unclassified items are still shown).

Four example profiles exist:

- `eu-casp.yaml`: `jurisdictions: [eu/*, intl]`, `activities: [casp]`, `languages: []` (all languages, since a pan-EU firm needs member-state-language output, not just English).
- `eu-bank.yaml`: same jurisdiction/language shape, `activities: [bank]`.
- `eu-emi.yaml`: same jurisdiction/language shape, `activities: [emi]`.
- `uk-equities-dealer.yaml`: `jurisdictions: [gb, intl]`, `activities: [equities_dealing]`, `exclude_instruments: [mica]`, `languages: [en]` (a single English-language jurisdiction, unlike the EU profiles).

All four set `regulators: []` and `instruments: []` (deriving instruments from activities), and `include_unclassified: true`.

## Inputs

Hand-edited YAML, one file per profile.

## Outputs and side effects

Read to filter query results, exports, digests, and feeds to a firm-specific view of the collected data.

## How to run or invoke

Not directly invoked; loaded via `PROFILES_DIR` (see `.env.example`, default `/app/config/profiles`).

## How to test

No dedicated test command in these files' own comments.

## Boundaries

`instruments` and `exclude_instruments` values must be ids that exist in `config/taxonomy.yaml`; `activities` values must exist in `config/activities.yaml`; `jurisdictions` values (including the base of a wildcard) must exist in `config/jurisdictions.yaml`.

## Related pages

[Profiles](component-profiles.md)
