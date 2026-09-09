"""`regradar reclassify --axis <a> [--by <provenance>] [--dry-run]`
(architecture doc §4.4).

This issue re-applies source defaults only; a later issue adds the rules
engine and the LLM classifier skill to the same command.

Runnable standalone via `python -m regradar.reclassify reclassify ...` for
this branch's own tests. `regradar/cli.py` (a parallel branch) wires this
module's add_subparser()/run() into the integrated CLI at merge - it
attaches by calling `add_subparser(subparsers)` on its own subparsers
object and dispatches to `run(args)`; see that module's docstring for the
full convention.
"""
import argparse
import logging
import os
import sys

import yaml

from collector.storage import init_db
from regradar.classification import supersede_axis, write_classification
from regradar.taxonomy import build_taxonomy
from sqlalchemy import text

logger = logging.getLogger(__name__)


def source_defaults(regulators: list) -> dict:
    """Maps (regulator_id, source_label) -> that source's `labels` block
    from regulators.yaml (post-migration shape - see
    scripts/migrate_regulator_labels.py)."""
    defaults = {}
    for reg in regulators:
        for source in reg.get("sources", []):
            labels = source.get("labels")
            if labels:
                defaults[(reg["id"], source.get("label"))] = labels
    return defaults


def run(args) -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = init_db(args.db_path)
    with open(args.regulators_path) as f:
        regulators = yaml.safe_load(f)
    taxonomy = build_taxonomy(args.taxonomy_path, args.jurisdictions_path)

    if args.axis not in taxonomy.axes():
        logger.error("Unknown axis %r - valid axes: %s", args.axis, sorted(taxonomy.axes()))
        sys.exit(1)

    defaults = source_defaults(regulators)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, regulator_id, source_label FROM items")).fetchall()

    stale_version_count = 0
    changed = 0
    for item_id, regulator_id, source_label in rows:
        with engine.connect() as conn:
            live_versions = conn.execute(
                text(
                    "SELECT taxonomy_version FROM classifications "
                    "WHERE item_id=:i AND axis=:a AND superseded_at IS NULL"
                ),
                {"i": item_id, "a": args.axis},
            ).fetchall()
        if any(v[0] != taxonomy.version() for v in live_versions):
            stale_version_count += 1

        source_labels = defaults.get((regulator_id, source_label), {})
        axis_value = source_labels.get(args.axis)
        if axis_value is None:
            continue
        values = axis_value if isinstance(axis_value, list) else [axis_value]

        if args.dry_run:
            changed += 1
            continue

        supersede_axis(engine, item_id, args.axis, by=args.by)
        for value in values:
            write_classification(
                engine, taxonomy, item_id, args.axis, value,
                by="source_default", confidence=taxonomy.source_default_confidence(args.axis),
                classifier_id="reclassify",
            )
        changed += 1

    if args.dry_run:
        logger.info(
            "[DRY RUN] %d item(s) would be reclassified on axis %r; %d live row(s) on this "
            "axis carry a taxonomy_version older than the current one (%d)",
            changed, args.axis, stale_version_count, taxonomy.version(),
        )
    else:
        logger.info("Reclassified %d item(s) on axis %r", changed, args.axis)


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser("reclassify", help="Re-apply classification defaults to existing items")
    parser.add_argument("--axis", required=True, help="Taxonomy axis to reclassify")
    parser.add_argument("--by", default="source_default", choices=["source_default"],
                         help="Provenance to supersede and rewrite (default and only choice for now: "
                              "source_default - this issue re-applies source defaults only; rule/llm:* "
                              "are added by a follow-on issue)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/regradar.db"))
    parser.add_argument("--regulators-path", default=os.getenv("REGULATORS_PATH", "config/regulators.yaml"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.set_defaults(func=run)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_subparser(subparsers)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
