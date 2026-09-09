"""`regradar reclassify --axis <a> [--by <provenance>] [--dry-run]`
(architecture doc §4.4).

This issue re-applies source defaults only; a later issue adds the rules
engine and the LLM classifier skill to the same command.

Wired into `regradar/cli.py`'s subcommand registry (add_subparser()/run(),
returning an exit code - see that module's docstring for the convention).
Also runnable standalone via `python -m regradar.reclassify reclassify ...`.
"""
import argparse
import logging
import os
import sys

from collector.main import load_regulators
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


def run(args) -> int:
    engine = init_db(args.db_path)
    regulators = load_regulators(args.regulators_path)
    taxonomy = build_taxonomy(args.taxonomy_path, args.jurisdictions_path)

    if args.axis not in taxonomy.axes():
        logger.error("Unknown axis %r - valid axes: %s", args.axis, sorted(taxonomy.axes()))
        return 1

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
    return 0


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser("reclassify", help="Re-apply classification defaults to existing items")
    parser.add_argument("--axis", required=True, help="Taxonomy axis to reclassify")
    parser.add_argument("--by", default="source_default", choices=["source_default"],
                         help="Provenance to supersede and rewrite (default and only choice for now: "
                              "source_default - this issue re-applies source defaults only; rule/llm:* "
                              "are added by a follow-on issue)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/regradar.db"))
    parser.add_argument("--regulators-path", default=os.getenv("REGULATORS_PATH", "config/regulators"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.set_defaults(func=run)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_subparser(subparsers)
    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
