"""`regradar reclassify --axis <a> [--by <provenance>] [--dry-run]`
(../wiki/content/component-classification.md).

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

from regradar.registry import load_regulators
from collector.storage import init_db
from regradar.classification import materialize_labels, supersede_axis, write_classification
from regradar.rules import RulesEngine
from regradar.taxonomy import build_taxonomy
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Page size for keyset-paginated item scans (see _iter_items_by_id below).
# Bounds peak memory regardless of table size; each page is fetched on its
# own short-lived connection, closed before any writes for that page happen.
ITEM_BATCH_SIZE = 500


def _iter_items_by_id(engine, columns: str):
    """Yields rows from `items` in bounded pages, ordered by `id` (keyset
    pagination), instead of one `fetchall()` of the whole table. Each page
    is read on its own connection that is closed before the caller does any
    writes - so a write from a previous row is always visible to the next
    page's read, and no cursor is held open across the classification
    writes (supersede_axis()/write_classification()/materialize_labels()
    each open their own connection against the same SQLite file)."""
    last_id = ""
    while True:
        with engine.connect() as conn:
            page = conn.execute(
                text(f"SELECT {columns} FROM items WHERE id > :last_id ORDER BY id LIMIT :limit"),
                {"last_id": last_id, "limit": ITEM_BATCH_SIZE},
            ).fetchall()
        if not page:
            return
        yield from page
        last_id = page[-1][0]


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


def _run_rule_reclassify(engine, taxonomy, args) -> int:
    """`reclassify --axis <a> --by rule`: rescoring every item against the
    current config/rules.yaml (see ../wiki/content/component-rules.md),
    superseding and rewriting only this axis's
    prior `by: rule` rows - human rows are untouched (supersede_axis()'s own
    NOT LIKE 'human:%' guard)."""
    rules_engine = RulesEngine.load(args.rules_path, taxonomy)
    with engine.connect() as conn:
        live_rule_item_ids = {
            r[0] for r in conn.execute(
                text(
                    "SELECT DISTINCT item_id FROM classifications "
                    "WHERE axis=:a AND by='rule' AND superseded_at IS NULL"
                ),
                {"a": args.axis},
            ).fetchall()
        }

    to_write = 0
    to_supersede_only = 0
    for item_id, title, raw_content in _iter_items_by_id(engine, "id, title, raw_content"):
        scores = rules_engine.score(title or "", raw_content or "")
        axis_values = {value: confidence for (axis, value), confidence in scores.items() if axis == args.axis}

        if not axis_values:
            # Rules no longer match this item, but a prior `by: rule` row may
            # still be live - supersede it so a rule that stopped matching
            # doesn't leave a stale classification winning forever (same
            # supersede-then-rewrite property as the source_default path,
            # ../wiki/content/component-classification.md).
            if item_id not in live_rule_item_ids:
                continue
            to_supersede_only += 1
            if args.dry_run:
                continue
            superseded = supersede_axis(engine, item_id, args.axis, by="rule")
            if superseded:
                materialize_labels(engine, taxonomy, item_id)
            continue

        to_write += 1
        if args.dry_run:
            continue
        supersede_axis(engine, item_id, args.axis, by="rule")
        for value, confidence in axis_values.items():
            write_classification(
                engine, taxonomy, item_id, args.axis, value,
                by="rule", confidence=confidence, classifier_id=rules_engine.classifier_id,
            )

    verb = "[DRY RUN] Would reclassify" if args.dry_run else "Reclassified"
    logger.info(
        "%s %d item(s) on axis %r via rule scoring; %d additional item(s) %s superseded "
        "(no longer match any rule)",
        verb, to_write, args.axis, to_supersede_only, "would be" if args.dry_run else "were",
    )
    return 0


def run(args) -> int:
    engine = init_db(args.db_path)
    taxonomy = build_taxonomy(args.taxonomy_path, args.jurisdictions_path)

    if args.axis not in taxonomy.axes():
        logger.error("Unknown axis %r - valid axes: %s", args.axis, sorted(taxonomy.axes()))
        return 1

    if args.by == "rule":
        # No regulators.yaml needed for rule-based rescoring - only
        # config/rules.yaml and each item's own title/body.
        return _run_rule_reclassify(engine, taxonomy, args)

    regulators = load_regulators(args.regulators_path)
    defaults = source_defaults(regulators)

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, regulator_id, source_label FROM items")).fetchall()
        live_item_ids = {
            r[0] for r in conn.execute(
                text(
                    "SELECT DISTINCT item_id FROM classifications "
                    "WHERE axis=:a AND by=:b AND superseded_at IS NULL"
                ),
                {"a": args.axis, "b": args.by},
            ).fetchall()
        }

    stale_version_count = 0
    to_write = 0
    to_supersede_only = 0
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
            # No current config default for this item's source, but a prior
            # source_default row may still be live (e.g. the default was
            # narrowed/removed from regulators.yaml) - supersede it so it
            # doesn't keep winning effective_labels() forever (architecture
            # doc §4.4).
            if item_id not in live_item_ids:
                continue
            to_supersede_only += 1
            if args.dry_run:
                continue
            superseded = supersede_axis(engine, item_id, args.axis, by=args.by)
            if superseded:
                materialize_labels(engine, taxonomy, item_id)
            continue

        values = axis_value if isinstance(axis_value, list) else [axis_value]
        to_write += 1
        if args.dry_run:
            continue

        supersede_axis(engine, item_id, args.axis, by=args.by)
        for value in values:
            write_classification(
                engine, taxonomy, item_id, args.axis, value,
                by="source_default", confidence=taxonomy.source_default_confidence(args.axis),
                classifier_id="reclassify",
            )

    if args.dry_run:
        logger.info(
            "[DRY RUN] %d item(s) would be reclassified on axis %r; %d additional item(s) would be "
            "superseded (no longer match a source default); %d live row(s) on this axis carry a "
            "taxonomy_version older than the current one (%d)",
            to_write, args.axis, to_supersede_only, stale_version_count, taxonomy.version(),
        )
    else:
        logger.info(
            "Reclassified %d item(s) on axis %r; superseded %d additional item(s) that no longer "
            "match a source default",
            to_write, args.axis, to_supersede_only,
        )
    return 0


def add_subparser(subparsers) -> None:
    parser = subparsers.add_parser("reclassify", help="Re-apply classification defaults to existing items")
    parser.add_argument("--axis", required=True, help="Taxonomy axis to reclassify")
    parser.add_argument("--by", default="source_default", choices=["source_default", "rule"],
                         help="Provenance to supersede and rewrite: source_default re-applies "
                              "regulators.yaml sources[].labels; rule rescores every item against "
                              "config/rules.yaml (llm:* is agent-driven via the classifier skill, not "
                              "this command). Only supersedes existing rows whose provenance already "
                              "matches --by (e.g. --by rule only touches prior 'rule' rows, leaving "
                              "source_default/llm:* rows live); human:* rows are never touched. "
                              "Superseded rows are kept as history, not deleted.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    parser.add_argument("--regulators-path", default=os.getenv("REGULATORS_PATH", "config/regulators"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.add_argument("--rules-path", default=os.getenv("RULES_PATH", "config/rules.yaml"))
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
