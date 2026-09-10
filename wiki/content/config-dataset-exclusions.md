---
title: Dataset Exclusions Config
type: config
sources: [config/dataset_exclusions.yaml]
last-verified: 2026-09-10
---

## Purpose

Lists regulator ids dropped from the published dataset export. Never affects the live collector or a local `reg-radar export` run unless the exclusion flag is passed.

## How it works

A YAML list of regulator ids (currently empty: `[]`). The file's comment explains why: nothing was found while compiling `NOTICE` (repository root) that explicitly states terms forbidding reuse. Most rows in `NOTICE` are "unclear" rather than "known permitted", but are shipped anyway pending a real legal review (see `NOTICE` and the README's "Published dataset" section), rather than defaulting a useful corpus down to nothing. A regulator id should be added here the moment its terms are found to forbid redistribution.

## Inputs

Hand-edited YAML list of regulator ids.

## Outputs and side effects

Consumed by `reg-radar export --exclude-regulator-file` and by `.github/workflows/publish-dataset.yml`, which excludes any regulator id listed here from the published dataset.

## How to run or invoke

Referenced via the `--exclude-regulator-file` flag on `reg-radar export`; not loaded by default.

## How to test

No dedicated test command in the file's own comments.

## Boundaries

Must never affect the live collector or a plain `reg-radar export` run; it only applies when `--exclude-regulator-file` is explicitly passed.

## Related pages

[reg-radar import-dataset](cli-dataset.md)
