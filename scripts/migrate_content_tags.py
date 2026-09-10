"""One-off migration: imports every existing item's `content_tags` (the old
JSON list, keyword-derived by collector/keyword_alerts.py from alerts.yaml)
into the `classifications` table as `by: import`, confidence 0.5 rows
(see ../wiki/content/concept-taxonomy-classification.md's migration-from-v1-tags
section) - so they survive the move to the new store, but
rank below anything better (see regradar/classification.py's tie-break
order).

# ASSUMPTION: every content_tags value seen in this codebase's example data
# and alerts.yaml (mica, casp, aml, dora, ...) is instrument-shaped, so
# imported rows are written on the `instrument` axis. Verify against the
# real alerts.yaml/content_tags before running against a production
# database with tags that don't fit that pattern.

import_classification() intentionally bypasses taxonomy validation so a tag
that no longer maps to a current instrument value is still preserved as
history rather than dropped or rejected - effective_labels() already
ignores axis/value pairs the current taxonomy doesn't recognise.

Re-running is safe: pairs already imported (by='import', same item/axis/
value) are skipped.

Run from the repository root:

    .venv/bin/python -m scripts.migrate_content_tags --dry-run
    .venv/bin/python -m scripts.migrate_content_tags
"""
import argparse
import json
import logging
import os

from sqlalchemy import text

from collector.storage import init_db
from regradar.classification import import_classification, materialize_labels
from regradar.taxonomy import build_taxonomy

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

AXIS = "instrument"


def run(db_path: str, taxonomy_path: str, jurisdictions_path: str, dry_run: bool, batch_size: int = 500) -> int:
    """Returns the number of (item, content_tag) pairs imported (or that
    would be, on a dry run)."""
    engine = init_db(db_path)
    taxonomy = build_taxonomy(taxonomy_path, jurisdictions_path)

    with engine.connect() as conn:
        already_imported = {
            (item_id, value)
            for item_id, value in conn.execute(
                text("SELECT item_id, value FROM classifications WHERE axis=:a AND by='import'"),
                {"a": AXIS},
            ).fetchall()
        }
        total = conn.execute(
            text("SELECT COUNT(*) FROM items WHERE content_tags IS NOT NULL AND content_tags != '[]'")
        ).scalar()
    logger.info("Importing content_tags for up to %d item(s)%s", total, " [DRY RUN]" if dry_run else "")

    imported_pairs = 0
    processed = 0
    offset = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, content_tags FROM items "
                    "WHERE content_tags IS NOT NULL AND content_tags != '[]' "
                    "ORDER BY id LIMIT :limit OFFSET :offset"
                ),
                {"limit": batch_size, "offset": offset},
            ).fetchall()
        if not rows:
            break

        for item_id, content_tags_json in rows:
            tags = json.loads(content_tags_json) if content_tags_json else []
            item_changed = False
            for tag in tags:
                if (item_id, tag) in already_imported:
                    continue
                imported_pairs += 1
                item_changed = True
                if not dry_run:
                    import_classification(engine, item_id, AXIS, tag, taxonomy.version(),
                                           confidence=0.5, classifier_id="migrate_content_tags")
            if item_changed and not dry_run:
                materialize_labels(engine, taxonomy, item_id)
            processed += 1

        offset += batch_size
        logger.info("Processed %d/%d item(s)", processed, total)

    logger.info(
        "Migration complete: %d (item, content_tag) pair(s) imported%s",
        imported_pairs, " [DRY RUN - nothing written]" if dry_run else "",
    )
    return imported_pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Compute and log what would be imported but write nothing")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    args = parser.parse_args()
    run(args.db_path, args.taxonomy_path, args.jurisdictions_path, args.dry_run)


if __name__ == "__main__":
    main()
