---
title: reg-radar setup / modules
type: cli
sources: [regradar/modules.py]
last-verified: 2026-09-10
---

## Purpose

Writes and edits `config/modules.yaml`, the operator's own selection over
the shipped regulator registry: `reg-radar setup` does it interactively,
`reg-radar modules list|enable|disable` do it non-interactively. `setup`
is a subcommand registered alongside `modules`, not a separate module.

## How it works

Both commands build a `modules.yaml` document (`build_doc()`/`entry_for()`)
and validate it through the collector's own loader before writing
(`write_doc()` runs `_load_modules_file()` and `_resolve_modules()` from
`collector.main` against a temp copy first), so anything the collector
would refuse at startup is refused here, with nothing written to disk. An
existing file is always backed up to `.bak` before being overwritten,
since PyYAML has no round-trip mode and hand-edited comments would
otherwise be silently lost.

`reg-radar modules list` reads the current file (`read_doc()`), resolves
it against the registry (`resolve()`), and prints each selected
regulator's jurisdiction, cadence, `on_enable` mode, how it was selected
(direct id or wildcard), and whether the registry itself has since marked
it `enabled: false` (which overrides the selection). `--all` also lists
unselected registry regulators.

`reg-radar modules enable <id|jurisdiction/*> [...]` adds one or more ids
or jurisdiction wildcards to the document, giving a newly-added regulator
its §4.4 default cadence (`entry_for()`/`cadence_for()`, daily unless the
registry marks it as a tiny-retention feed with no backfill support, or
an all-`page_watch` regulator, which get a faster or slower cadence).
`--on-enable` can set `new_only`, `backfill_all`, or
`backfill_from=<date>` on the newly-touched entries. Prints the estimated
first-run volume for what was just enabled.

`reg-radar modules disable <id>` removes an id from the document. If it
was only selected through a wildcard, the wildcard is expanded into
explicit entries so the one id can be dropped, and any registry-disabled
regulators swept up by that expansion are named explicitly (so a later
registry re-enable can't silently reintroduce them). It prints which of
the removed regulator's sources have no backfill route (`unbackfillable()`),
i.e. the gap this creates is permanent.

`reg-radar setup` is the interactive prompting layer over the same
building blocks. It refuses to run if stdin isn't a TTY (pointing at
`modules enable` for scripted installs instead). It groups the registry
by jurisdiction (`group_jurisdictions()`, using `GROUP_ROOTS`: eu/gb/us/
sg/ae/intl, plus anything else as its own group), lets the operator
multi-select groups then regulators then, optionally, individual sources
per regulator (`_pick()`, blank/`all` selects everything), asks for an
`on_enable` mode, shows the estimated volume and any un-backfillable
sources for that choice, and, if re-run over an existing file, shows
which currently-collected modules this run would drop before asking for
final confirmation. Nothing is written until that confirmation.

## Inputs

- `setup`: `--modules-path`, `--regulators-path`, `--jurisdictions-path`
  (all env-backed: `MODULES_PATH`, `REGULATORS_PATH`,
  `JURISDICTIONS_PATH`), plus interactive stdin answers.
- `modules list`: the above, plus `--all` (also list unselected
  regulators).
- `modules enable`: the above, plus `module_ids` (one or more regulator
  ids or `jurisdiction/*` wildcards, positional, trailing commas
  ignored), `--on-enable` (`new_only|backfill_all|backfill_from=<date>`).
- `modules disable`: the above, plus `module_id` (single, positional).
- Reads `config/modules.yaml` (if present) and the full regulator
  registry (never filtered) plus `config/jurisdictions.yaml`.

## Outputs and side effects

Writes `config/modules.yaml` (backing up any existing file to
`config/modules.yaml.bak` first) on `setup` confirmation, `enable`, or
`disable`, unless collector-side validation rejects the document, in
which case nothing is written and the error is printed. Prints selection
summaries, volume estimates, and backfill-gap warnings to stdout; prints
errors to stderr and returns 1 on validation failure or cancellation
(Ctrl-C/EOF during `setup`).

## How to run or invoke

```
reg-radar setup
reg-radar modules list [--all]
reg-radar modules enable <id|jurisdiction/*> [<id> ...] [--on-enable new_only|backfill_all|backfill_from=<YYYY-MM-DD>]
reg-radar modules disable <id>
```

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_modules.py` and run it via pytest from `code/`.

## Boundaries

`setup` must never overwrite an existing `config/modules.yaml` without
showing the current selection and asking first, and must never write
without a final confirmation. Must never write a document that fails the
collector's own `_load_modules_file`/`_resolve_modules` validation, an
invalid selection leaves the existing file untouched. Must never silently
drop a wildcard-covered, registry-disabled regulator when expanding a
wildcard for `disable`, it must be named explicitly instead.

## Related pages

[CLI Entrypoint](cli-entrypoint.md), [Taxonomy and Classification](concept-taxonomy-classification.md)
