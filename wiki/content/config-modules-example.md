---
title: Modules Example Config
type: config
sources: [config/modules.example.yaml]
last-verified: 2026-09-10
---

## Purpose

Template and worked example for `config/modules.yaml`, the per-install selection of which regulators an install actually collects, as opposed to `config/regulators/`, which stays a complete community-maintained catalogue.

## How it works

Top-level `version: 1`. A `defaults` block sets a `schedule` (cron expression) and `on_enable` policy (`new_only` | `backfill_from` | `backfill_all`) inherited by any module without its own. Under `modules`, each entry has an `id`, either an explicit regulator id from `config/regulators/*.yaml` (e.g. `uk-fca`) or a jurisdiction wildcard (`eu/*`) that expands to every regulator in that jurisdiction and its descendants per `config/jurisdictions.yaml`.

Resolution order, narrower always winning: (1) `defaults`, (2) jurisdiction wildcard, (3) explicit module id, (4) `sources:` within a module (narrows to specific source labels, e.g. `sources: [news, warnings]`).

The registry always wins on `false`: any regulator `config/regulators/` marks `enabled: false` is never collected however explicitly selected here, and the collector logs a WARNING when this override applies.

`schedule:` is a per-module cron cadence; the collector adds one job per distinct resolved cron expression, not per regulator. Anything without a `schedule:` anywhere falls back to `SCHEDULE` in `.env`. `--once` ignores cadence and runs everything in one pass. All cadences share one collection lock and queue rather than overlap; a job that finds the lock held is skipped with a WARNING naming its cron expression.

`on_enable` is consulted once, at the moment a module is switched on, and never again: `new_only` (default) records the enable moment and collects only what's published after; `backfill_from` uses a given `backfill_from:` date; `backfill_all` has no date bound. Both backfill values are refused outright for a source `config/regulators/` declares un-backfillable. They only stop the collector filtering old items out of the current feed page; walking an archive is a separate `scripts/backfill_*.py` run, which the enable logs name.

Switching a module OFF is non-destructive: already-collected data (including `page_watch` baselines) stays in the database.

With no `config/modules.yaml` at all, the collector collects nothing and says so; set `REGRADAR_COLLECT_ALL=true` to collect the whole registry instead.

## Inputs

Copied to `config/modules.yaml` (gitignored, never touched by `git pull`) and hand-edited, or generated/adjusted via `reg-radar setup`, `reg-radar modules list`, `reg-radar modules enable/disable`.

## Outputs and side effects

Drives which regulators and sources the collector actually schedules and fetches on each run.

## How to run or invoke

```
reg-radar setup                      # pick jurisdictions and regulators
reg-radar modules list               # what this install collects
reg-radar modules enable eu/*        # adjust it later
```

Or hand-edit: `cp config/modules.example.yaml config/modules.yaml`. Note that `reg-radar modules enable/disable` rewrites the file through a YAML dump, so hand-written comments are not preserved; the previous version is kept as `modules.yaml.bak`.

## How to test

No dedicated test command in the file's own comments; `reg-radar modules list` shows the resolved selection before committing to it.

## Boundaries

Collection-time filtering is not free to reverse: turning a module off for a period leaves a hole in collected history, and many sources cannot be backfilled (some feeds only ever expose the last handful of items). Turn something off because it's never wanted, not because of uncertainty. A registry-level `enabled: false` can never be overridden from this file.

## Related pages

[reg-radar setup / modules](cli-modules.md)
