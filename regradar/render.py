"""`regradar render` - writes items to the section 3.1 canonical document
layout. Plugs into regradar/cli.py's subcommand registry: add_subparser()
registers the argparse subcommand, run() executes it.

The pure rendering logic (frontmatter, Markdown body, the file layout) is
in regradar.document; this module is the DB-access and file-writing glue
around it. Document assembly (jurisdiction_ancestors/labels/
label_provenance) reuses mcpserver.queries._document_fields - the same
helper get_item()/_documents_for_items() use - so this writer can't drift
from what the MCP tools/exporter report for the same item.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from collector.storage import get_artefact
from mcpserver.queries import _document_fields
from regradar.document import document_path, render_item

logger = logging.getLogger(__name__)


def _load_item(engine, item_id: str) -> tuple[dict | None, dict]:
    """Returns (item, reg) - the raw items-table row and its regulators-row
    (empty dict if the regulator isn't found), for _document_fields() to
    join. (None, {}) if the item doesn't exist."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM items WHERE id = :id"), {"id": item_id}).fetchone()
        if row is None:
            return None, {}
        item = dict(row._mapping)
        reg_row = conn.execute(
            text("SELECT name, full_name, jurisdiction FROM regulators WHERE id = :id"),
            {"id": item["regulator_id"]},
        ).fetchone()
    reg = dict(reg_row._mapping) if reg_row is not None else {}
    return item, reg


def render_item_to_dir(engine, item_id: str, out_dir: Path, taxonomy=None) -> Path | None:
    """Renders one item under out_dir, skipping the write if the file
    already has identical content (so `--all` run twice doesn't touch
    mtimes of unchanged files). Returns the path written/checked, or None
    if the item doesn't exist.

    `taxonomy` is optional (as get_item()'s/get_changes()'s) - pass one to
    populate jurisdiction_ancestors/label_provenance in the rendered file.
    """
    item, reg = _load_item(engine, item_id)
    if item is None:
        return None
    artefact = get_artefact(engine, item_id)
    classification_rows = []
    if taxonomy is not None:
        with engine.connect() as conn:
            classification_rows = [
                dict(r._mapping) for r in conn.execute(
                    text("SELECT axis, value, by, confidence, taxonomy_version, superseded_at "
                         "FROM classifications WHERE item_id = :id"),
                    {"id": item_id},
                ).fetchall()
            ]
    doc_item, labels, label_provenance = _document_fields(item, reg, taxonomy, classification_rows)
    text_out = render_item(doc_item, artefact, labels, label_provenance)
    path = out_dir / document_path(doc_item)
    if path.exists() and path.read_text(encoding="utf-8") == text_out:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text_out, encoding="utf-8")
    return path


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("render", help="Render items to the canonical document format")
    parser.add_argument("item_id", nargs="?", help="Render a single item by id")
    parser.add_argument("--all", action="store_true", help="Render every item in the database")
    parser.add_argument("--out", default="export", help="Output directory (default: export)")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/regradar.db"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    if not args.all and not args.item_id:
        print("regradar render: pass an item_id or --all", file=sys.stderr)
        return 2
    engine = create_engine(f"sqlite:///{args.db_path}")
    out_dir = Path(args.out)

    from regradar.taxonomy import load_and_exit_on_error
    taxonomy = load_and_exit_on_error(args.taxonomy_path, args.jurisdictions_path)

    if args.all:
        with engine.connect() as conn:
            ids = [row[0] for row in conn.execute(text("SELECT id FROM items ORDER BY id")).fetchall()]
        written = 0
        for item_id in ids:
            if render_item_to_dir(engine, item_id, out_dir, taxonomy=taxonomy) is not None:
                written += 1
        print(f"Rendered {written} item(s) to {out_dir}")
        return 0

    path = render_item_to_dir(engine, args.item_id, out_dir, taxonomy=taxonomy)
    if path is None:
        print(f"regradar render: no item with id {args.item_id!r}", file=sys.stderr)
        return 1
    print(path)
    return 0
