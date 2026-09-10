---
title: CLI Entrypoint
type: cli
sources: [regradar/cli.py]
last-verified: 2026-09-10
---

## Purpose

`regradar/cli.py` is the single console entrypoint for the `reg-radar` command. It builds the top-level argparse parser and dispatches to whichever subcommand the user picked.

## How it works

This file wires subcommands together but implements none of them itself. Each subcommand lives in its own module under `regradar/` and exposes two functions:

- `add_subparser(subparsers)`: registers that module's `argparse` subparser and calls `parser.set_defaults(func=<module>.run)`.
- `run(args) -> int`: executes the subcommand and returns a process exit code.

`cli.py` holds a `_SUBCOMMAND_MODULES` list of the subcommand modules. `build_parser()` creates the top-level parser (`prog="reg-radar"`) with a required `command` subparsers group, then loops over `_SUBCOMMAND_MODULES` calling each module's `add_subparser()`. `main()` parses `argv`, then calls `args.func(args)`, which resolves to whichever module's `run()` was registered for the chosen subcommand. Adding a new subcommand means importing the module and appending it to `_SUBCOMMAND_MODULES`; nothing else in this file changes.

`cli.py` also configures root logging (`logging.basicConfig`, level from the `LOG_LEVEL` env var, default `INFO`) before any subcommand runs.

## Inputs

`argv` (defaults to `sys.argv` via argparse when `None` is passed to `main()`), and the `LOG_LEVEL` environment variable for log verbosity.

## Outputs and side effects

Exit code returned by the chosen subcommand's `run()`. No direct side effects of its own beyond logging configuration; all actual side effects (database writes, file output, etc.) belong to the dispatched subcommand.

## How to run or invoke

The `reg-radar` console script is registered in `pyproject.toml` (`[project.scripts] reg-radar = "regradar.cli:main"`), so once the package is installed:

```
reg-radar --help
```

It is also runnable as a module:

```
python -m regradar.cli --help
```

`_SUBCOMMAND_MODULES = [render, reclassify, exporter, digest, classify, dataset, modules]` registers these subcommand modules. Note that `modules.py` registers two subcommands (`setup` and `modules`), so the seven modules yield eight subcommand verbs in total: `render`, `reclassify`, `export`, `digest`, `classify`, `import-dataset`, `setup`, `modules`.

## How to test

`reg-radar --help` lists all registered subcommands; `reg-radar <subcommand> --help` shows that subcommand's own flags, proving `add_subparser()` wired it correctly.

## Boundaries

Must never implement subcommand logic directly, that stays in the subcommand's own module. Must never add a subcommand by any means other than appending to `_SUBCOMMAND_MODULES`.

## Related pages

[reg-radar render](cli-render.md), [reg-radar reclassify](cli-reclassify.md), [reg-radar export](cli-exporter.md), [reg-radar digest](cli-digest.md), [reg-radar classify](cli-classify.md), [reg-radar import-dataset](cli-dataset.md), [reg-radar setup / modules](cli-modules.md)
