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
                    with_originals: bool = False, export_format: str = "markdown") -> dict:
    """Writes every item matching `profile` changed after `since` (an
    opaque cursor, or None for everything) to out_dir: the section 3.1
    layout for "markdown" (idempotent per render_item_to_dir - unchanged
    items are not rewritten), one JSON object per line appended to
    export.jsonl for "jsonl". Returns {"written": int, "cursor": str |
    None} - `cursor` is the new head of the stream (or `since` again if
    nothing changed), for the caller to persist.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    cursor = since
    jsonl_lines: list[str] = []

    while True:
        items, next_cursor = get_changes(engine, since=cursor, profile=profile, limit=_PAGE_SIZE, include_body=True)
        if not items:
            break
        for doc in items:
            item_id = doc["id"]
            if export_format == "markdown":
                if render_item_to_dir(engine, item_id, out_dir) is not None:
                    written += 1
            else:
                jsonl_lines.append(json.dumps(doc, sort_keys=False))
                written += 1
            if with_originals:
                _copy_original(engine, item_id, out_dir)
        cursor = next_cursor

    if jsonl_lines:
        with open(out_dir / JSONL_FILENAME, "a", encoding="utf-8") as f:
            for line in jsonl_lines:
                f.write(line + "\n")

    return {"written": written, "cursor": cursor}


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "export", help="Export items to the canonical document layout or JSONL, cursor-tracked",
    )
    parser.add_argument("--profile", help="Shipped profile template name or path (default: all items)")
    parser.add_argument("--format", choices=["markdown", "jsonl"], default="markdown")
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument(
        "--since", help="Cursor to export from (default: the persisted .regradar-cursor in --out, or a full export if none)",
    )
    parser.add_argument("--with-originals", action="store_true", help="Also copy artefacts into <out>/originals/")
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

    expanded_profile = None
    if args.profile:
        taxonomy = load_and_exit_on_error(args.taxonomy_path, args.jurisdictions_path)
        activities = load_activities(args.activities_path, taxonomy)
        try:
            expanded_profile = get_expanded_profile(
                args.profile, taxonomy, activities, profiles_dir=args.profiles_dir,
            )
        except Exception as e:
            print(f"regradar export: {e}", file=sys.stderr)
            return 1

    since = resolve_since(out_dir, args.since)
    result = export_profile(
        engine, out_dir, profile=expanded_profile, since=since,
        with_originals=args.with_originals, export_format=args.format,
    )
    if result["cursor"] and result["cursor"] != since:
        write_cursor(out_dir, result["cursor"])
    print(f"Exported {result['written']} item(s) to {out_dir}")
    return 0
