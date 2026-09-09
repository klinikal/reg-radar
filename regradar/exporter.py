"""`regradar export --profile <p> --format markdown|jsonl --out <dir>
[--since <cursor>] [--with-originals]` (architecture doc section 6).

Wired into regradar/cli.py's subcommand registry (add_subparser()/run() -
see that module's docstring for the convention). Reuses
regradar.render.render_item_to_dir for the markdown format - the section
3.1 layout is not reimplemented here.

`--out` gets its own `.regradar-cursor` file, so a scheduled export doesn't
need external state tracking: the next run without `--since` picks up
exactly where the last one left off. `--since` on the command line always
overrides the persisted cursor for that one run.

`--format jsonl` writes export.jsonl as a full current-state snapshot (one
line per item id, latest wins) rather than an append-only log - a
downstream consumer (e.g. issue #12's published dataset) reads it as "the
whole corpus right now", not "everything that has ever changed". It is
still rebuilt cheaply on each run by reading the existing snapshot, folding
in only what get_changes() reports as new since the cursor, and skipping
the rewrite entirely when the resulting content is byte-identical.

`--format` is repeatable (`--format markdown --format jsonl`, default just
`markdown`) and all requested formats share the ONE `.regradar-cursor` in
`--out` and the one pass over get_changes(). Running markdown and jsonl as
two separate invocations into the same `--out` is a trap: the first
invocation's cursor advances past everything, so the second sees nothing
left and writes an empty (or truncated) snapshot for whichever format it
covers. Pass both formats in one `--format markdown --format jsonl` call
instead. As a safety net, export_profile() also detects a persisted cursor
paired with a format that has no prior output in `--out` (no export.jsonl
for jsonl, no *.md under export/ for markdown) and falls back to a full
export rather than trusting a cursor that was never advanced for that
format.
"""
import argparse
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy import create_engine

from collector.storage import get_artefact
from mcpserver.queries import get_changes
from regradar.changes import CursorError, decode_cursor
from regradar.dataset import load_exclusions
from regradar.profiles import get_expanded_profile, load_activities
from regradar.render import render_item_to_dir
from regradar.taxonomy import load_and_exit_on_error

logger = logging.getLogger(__name__)

CURSOR_FILENAME = ".regradar-cursor"
JSONL_FILENAME = "export.jsonl"
# Items are fetched from get_changes() in pages of this size regardless of
# how many actually match - keeps memory bounded on a large catch-up
# export without needing the caller to think about pagination.
_PAGE_SIZE = 200


def _cursor_path(out_dir: Path) -> Path:
    return out_dir / CURSOR_FILENAME


def read_cursor(out_dir: Path) -> str | None:
    path = _cursor_path(out_dir)
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    return raw or None


def write_cursor(out_dir: Path, cursor: str) -> None:
    _cursor_path(out_dir).write_text(cursor, encoding="utf-8")


def resolve_since(out_dir: Path, cli_since: str | None) -> str | None:
    """`--since` on the command line wins outright. Otherwise, read the
    persisted cursor; a missing one means "first export, do a full one".
    An unparseable one is treated the same way (logged, not raised) - the
    issue's requirement that a stale/missing cursor falls back to a full
    export rather than corrupting the target directory.
    """
    if cli_since:
        return cli_since
    persisted = read_cursor(out_dir)
    if persisted is None:
        return None
    try:
        decode_cursor(persisted)
    except CursorError as e:
        logger.warning(
            "Persisted cursor at %s is unparseable (%s) - falling back to a full export",
            _cursor_path(out_dir), e,
        )
        return None
    return persisted


def _jsonl_path(out_dir: Path) -> Path:
    return out_dir / JSONL_FILENAME


def _read_jsonl_snapshot(out_dir: Path) -> dict[str, str]:
    """Loads the current export.jsonl into {item_id: line} - the snapshot
    export_profile() updates in place (latest wins) and rewrites from."""
    path = _jsonl_path(out_dir)
    if not path.exists():
        return {}
    snapshot: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc_id = json.loads(line)["id"]
        except (json.JSONDecodeError, KeyError):
            continue
        snapshot[doc_id] = line
    return snapshot


def _write_jsonl_snapshot(out_dir: Path, snapshot: dict[str, str]) -> None:
    """Rewrites export.jsonl as the full current-state snapshot (one line
    per item id, sorted for a deterministic byte-for-byte diff). Skips the
    write entirely when the content is unchanged, so an unchanged export
    doesn't touch the file's mtime - the same idempotency contract as the
    markdown format's per-item files.
    """
    lines = [snapshot[item_id] for item_id in sorted(snapshot)]
    content = "".join(line + "\n" for line in lines)
    path = _jsonl_path(out_dir)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    path.write_text(content, encoding="utf-8")


def _markdown_tree_empty(out_dir: Path) -> bool:
    export_dir = out_dir / "export"
    if not export_dir.exists():
        return True
    return next(export_dir.rglob("*.md"), None) is None


def _copy_original(engine, item_id: str, out_dir: Path) -> None:
    artefact = get_artefact(engine, item_id)
    if not artefact:
        return
    src = Path(os.getenv("ARTEFACTS_DIR", "/app/data/originals")) / artefact["path"]
    if not src.exists():
        logger.warning("Artefact for item %s not found at %s - skipping --with-originals copy", item_id, src)
        return
    dest = out_dir / "originals" / artefact["path"]
    if dest.exists() and dest.stat().st_size == src.stat().st_size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)


def export_profile(engine, out_dir: Path, *, profile=None, since: str | None = None,
                    with_originals: bool = False, export_formats=("markdown",),
                    taxonomy=None, exclude_regulators: set | None = None) -> dict:
    """Writes every item matching `profile` changed after `since` (an
    opaque cursor, or None for everything) to out_dir, in every format
    listed in `export_formats` ("markdown", "jsonl", or both) from a
    single pass over get_changes() and a single shared cursor - see the
    module docstring for why the formats must not be exported in separate
    invocations into the same `--out`. "markdown" is the section 3.1
    layout (idempotent per render_item_to_dir - unchanged items are not
    rewritten). "jsonl" is export.jsonl, a full current-state snapshot -
    one line per item id, latest wins - rebuilt from the existing snapshot
    plus whatever changed since `since`, and only rewritten if its content
    actually changed (so an unchanged run doesn't touch the file's mtime
    either). Returns {"written": int, "cursor": str | None} - `cursor` is
    the new head of the stream (or `since` again if nothing changed), for
    the caller to persist.

    `exclude_regulators` (a set of regulator ids, see regradar.dataset.
    load_exclusions - issue #12) drops matching items from every format
    entirely, so a published dataset built from this same command never
    contains them regardless of `--profile`. `taxonomy`, when given, is
    passed straight through to get_changes(), which populates each
    document's jurisdiction_ancestors/label_provenance.
    """
    formats = set(export_formats)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    cursor = since

    # A persisted cursor with no prior output for a requested format means
    # some earlier run in this --out only covered a *different* format -
    # trusting the cursor would silently truncate this format's output to
    # "whatever changed since a run that never wrote it". Fall back to a
    # full export instead.
    if cursor is not None:
        if ("jsonl" in formats and not _jsonl_path(out_dir).exists()) or \
                ("markdown" in formats and _markdown_tree_empty(out_dir)):
            logger.warning(
                "Persisted cursor at %s has no prior output for a requested format - "
                "falling back to a full export", _cursor_path(out_dir),
            )
            cursor = None

    snapshot = _read_jsonl_snapshot(out_dir) if "jsonl" in formats else None

    while True:
        items, next_cursor = get_changes(engine, since=cursor, profile=profile, limit=_PAGE_SIZE,
                                          include_body=True, taxonomy=taxonomy)
        if not items:
            break
        for doc in items:
            item_id = doc["id"]
            if exclude_regulators and doc.get("regulator") in exclude_regulators:
                continue  # cursor still advances past it (below) - it's just never written
            item_written = False
            if "markdown" in formats and render_item_to_dir(engine, item_id, out_dir, taxonomy=taxonomy) is not None:
                item_written = True
            if "jsonl" in formats:
                snapshot[item_id] = json.dumps(doc, sort_keys=False)
                item_written = True
            if item_written:
                written += 1
            if with_originals:
                _copy_original(engine, item_id, out_dir)
        cursor = next_cursor

    if snapshot is not None:
        _write_jsonl_snapshot(out_dir, snapshot)

    return {"written": written, "cursor": cursor}


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "export", help="Export items to the canonical document layout or JSONL, cursor-tracked",
    )
    parser.add_argument("--profile", help="Shipped profile template name or path (default: all items)")
    parser.add_argument(
        "--format", choices=["markdown", "jsonl"], action="append", dest="formats",
        help="Repeatable - e.g. --format markdown --format jsonl to write both from one change set "
             "(default: markdown)",
    )
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument(
        "--since", help="Cursor to export from (default: the persisted .regradar-cursor in --out, or a full export if none)",
    )
    parser.add_argument("--with-originals", action="store_true", help="Also copy artefacts into <out>/originals/")
    parser.add_argument(
        "--exclude-regulator-file",
        help="Path to a YAML file listing regulator ids (flat list) to drop from this export entirely",
    )
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/regradar.db"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.add_argument("--activities-path", default=os.getenv("ACTIVITIES_PATH", "config/activities.yaml"))
    parser.add_argument("--profiles-dir", default=os.getenv("PROFILES_DIR", "config/profiles"))
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    engine = create_engine(f"sqlite:///{args.db_path}")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    taxonomy = load_and_exit_on_error(args.taxonomy_path, args.jurisdictions_path)
    expanded_profile = None
    if args.profile:
        activities = load_activities(args.activities_path, taxonomy)
        try:
            expanded_profile = get_expanded_profile(
                args.profile, taxonomy, activities, profiles_dir=args.profiles_dir,
            )
        except Exception as e:
            print(f"regradar export: {e}", file=sys.stderr)
            return 1

    exclude_regulators = None
    if args.exclude_regulator_file:
        try:
            exclude_regulators = load_exclusions(args.exclude_regulator_file)
        except Exception as e:
            print(f"regradar export: {e}", file=sys.stderr)
            return 1

    export_formats = list(dict.fromkeys(args.formats or ["markdown"]))

    since = resolve_since(out_dir, args.since)
    result = export_profile(
        engine, out_dir, profile=expanded_profile, since=since,
        with_originals=args.with_originals, export_formats=export_formats,
        taxonomy=taxonomy, exclude_regulators=exclude_regulators,
    )
    if result["cursor"] and result["cursor"] != since:
        write_cursor(out_dir, result["cursor"])
    print(f"Exported {result['written']} item(s) to {out_dir}")
    return 0
