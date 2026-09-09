"""`regradar` console entrypoint.

Subcommand modules plug into this registry by exposing two functions:

    add_subparser(subparsers: argparse._SubParsersAction) -> None
        Adds the module's subparser to `subparsers` and calls
        parser.set_defaults(func=<module>.run).

    run(args: argparse.Namespace) -> int
        Executes the subcommand and returns a process exit code.

To add a subcommand, import the module below and append it to
_SUBCOMMAND_MODULES - nothing else in this file changes.
"""
import argparse
import logging
import os
import sys

from regradar import render

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

_SUBCOMMAND_MODULES = [render]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="regradar")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for module in _SUBCOMMAND_MODULES:
        module.add_subparser(subparsers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
