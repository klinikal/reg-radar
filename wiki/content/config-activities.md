---
title: Activities Config
type: config
sources: [config/activities.yaml]
last-verified: 2026-09-10
---

## Purpose

The interpretive layer between "what a document is" (`config/taxonomy.yaml`) and "who should care" (a profile). Each activity maps to the set of instruments a firm doing that activity needs to watch.

## How it works

A flat YAML list; each entry has an `id`, a display `name`, and an `instruments` list, whose values must exactly match `instrument` axis ids in `config/taxonomy.yaml`.

Four activities are defined: `equities_dealing` (Dealing and broking in equities: mifid2, mar, csdr, emir, sftr, uk_fsma, uk_consumer_duty), `casp` (Crypto-asset service provider: mica, amld, dora, transfer_of_funds_regulation, uk_mlr, uk_financial_promotions), `emi` (Electronic money or payment institution: psd2, emd2, amld, dora, uk_psr), and `bank` (Credit institution: crr_crd, brrd, dgsd, amld, dora).

## Inputs

Hand-edited YAML, grows by pull request, no code change required.

## Outputs and side effects

Consumed by profiles (`config/profiles/*.yaml`), which select one or more `activities` ids; a profile's effective instrument set is derived from its activities' mapped instruments.

## How to run or invoke

Not directly invoked; loaded via `ACTIVITIES_PATH` (see `.env.example`, default `/app/config/activities.yaml`).

## How to test

No dedicated test command in the file's own comments.

## Boundaries

Instrument ids listed here must exactly match ids in `config/taxonomy.yaml`. The file's own comment notes that the architecture doc's illustrative example used non-matching ids (`travel_rule`, `uk_fin_prom`) that don't match what actually shipped (`transfer_of_funds_regulation`, `uk_financial_promotions`); this file uses the real, shipped ids.

## Related pages

[Profiles and Activities](concept-profiles-activities.md)
