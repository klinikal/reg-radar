---
title: Registry
type: component
sources: [regradar/registry.py]
last-verified: 2026-09-10
---

## Purpose

Loads `config/regulators` (the registry) and `config/modules.yaml` (the operator's
selection over it), and validates both. Split out of `collector/main.py` so that callers
needing only registry/module logic (`reg-radar reclassify`, the backfill scripts,
`source_health`, `webui/modules.py`) never pull in `apscheduler`, which the collector's
own scheduling needs at import time.

## How it works

`load_regulators(config_path, modules_path=None, jurisdictions_path=...)` loads every
`*.yaml` under a directory (skipping `_`-prefixed files like `_template.yaml`) or a
single file, checks for duplicate regulator ids and duplicate source labels within a
regulator, and validates each source's `backfill`/`staleness` blocks
(`_source_backfill_error`, `_source_staleness_error`). With no `modules_path`, it returns
the whole registry.

With `modules_path` set (only the collector daemon does this), three states: a present
`config/modules.yaml` resolves exactly what it selects (`_resolve_modules`); an absent
file with `REGRADAR_COLLECT_ALL=true` collects the whole registry; otherwise nothing is
collected, and a line points the operator at `reg-radar setup`.

`_resolve_modules` applies the resolution order defaults -> jurisdiction wildcard (e.g.
`fr/*`, narrowest match wins) -> explicit module id -> `sources:` list, attaches a
resolved `module` block to each selected regulator, and refuses (raises
`BackfillNotSupportedError`) if `on_enable: backfill_all`/`backfill_from` is requested
against a source the registry declares cannot be backfilled. A registry `enabled: false`
always wins over an operator selection.

`_validate_module_keys` structurally validates `schedule` (a cron string, checked via a
lazily-imported `apscheduler.triggers.cron.CronTrigger` so importing this module never
pulls in apscheduler unless a `schedule:` is actually present), `on_enable` (one of
`new_only`/`backfill_from`/`backfill_all`), and `backfill_from`'s date.

`collector/main.py` re-imports everything from this module so existing callers of
`from collector.main import load_regulators` keep working.

## Inputs

`config/regulators/` (directory or single file), `config/modules.yaml` (optional),
`config/jurisdictions.yaml`, and the `REGRADAR_COLLECT_ALL` env var.

## Outputs and side effects

Returns a list of regulator dicts (each carrying a resolved `sources` list and, when
modules are resolved, a `module` block). Exits the process (`sys.exit(1)`) on a
structurally invalid registry file, or raises `ModulesConfigError` /
`BackfillNotSupportedError` on an invalid `modules.yaml` or a refused backfill request.

## How to run or invoke

Not run directly; called by `collector/main.py`, `reg-radar reclassify`, the backfill
scripts, `scripts/source_health.py` and `webui/modules.py`.

## How to test

[UNVERIFIED], no test command is stated in this file's own comments; see `tests/` for
`test_modules.py`/`test_modules_cli.py` coverage.

## Boundaries

Never imports `apscheduler` at module level. A source with no `backfill:` block is always
`supported: false`, never inferred as supported. A registry `enabled: false` is never
overridden by an operator's module selection.

## Related pages

[Regulators Registry](config-regulators.md), [Modules Example Config](config-modules-example.md), [reg-radar setup / modules](cli-modules.md), [Collector Pipeline](component-collector-pipeline.md)
