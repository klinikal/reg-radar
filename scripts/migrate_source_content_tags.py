"""One-off migration: split every existing item's blended `tags` (now
source_tags/content_tags-only going forward — see collector/schema.py) into
the two new buckets. source_tags = old tags minus the blanket
regulation-name denylist (collector/fetcher.py's _BLANKET_LABEL_DENYLIST).
content_tags is NOT guessed from the old blended list - it's freshly
recomputed by re-running the real keyword matcher against each item's
actual text, so it reflects genuine content signal rather than inherited
ambiguity.

Run inside the collector container:

    docker compose exec collector python -m scripts.migrate_source_content_tags --dry-run
    docker compose exec collector python -m scripts.migrate_source_content_tags

Always try --dry-run first - it computes and logs what would change without
writing anything.
"""
import argparse
import json
import logging
import os

from sqlalchemy import text

from collector.pipeline import filter_source_tags as _filter_source_tags
from collector.storage import init_db

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_alert_rules(path: str) -> list[dict]:
    """Reads the old alerts.yaml `keyword`/`tag` format - inlined here
    (rather than importing collector.keyword_alerts, which
    ../wiki/content/component-rules.md removes along with
    the feature it implemented) since this v1 migration is a completed,
    idempotent one-off that only matters for a pre-v2 database still
    carrying the legacy `tags` column, and needs the exact same matching
    behaviour it used when items were first migrated."""
    import yaml
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"keyword": entry["keyword"], "tag": entry["tag"]}
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("keyword"), str) and isinstance(entry.get("tag"), str)
    ]


def match_keywords(text_to_match: str, rules: list[dict]) -> list[str]:
    if not text_to_match or not rules:
        return []
    lowered = text_to_match.lower()
    matched = {rule["tag"] for rule in rules if rule["keyword"].lower() in lowered}
    return sorted(matched)


def migrate_item_tags(old_tags: list[str], text_for_matching: str, alert_rules: list[dict]) -> tuple[list[str], list[str]]:
    source_tags = _filter_source_tags(old_tags)
    content_tags = match_keywords(text_for_matching, alert_rules)
    return source_tags, content_tags


def run(db_path: str, alerts_path: str, dry_run: bool, batch_size: int = 500) -> None:
    engine = init_db(db_path)

    with engine.connect() as conn:
        cols = [row[1] for row in conn.execute(text("PRAGMA table_info(items)")).fetchall()]
    if "tags" not in cols:
        logger.info(
            "No legacy `tags` column found - nothing to migrate, this database "
            "was created after the schema split."
        )
        return

    alert_rules = load_alert_rules(alerts_path)
    logger.info("Loaded %d keyword rule(s) from %s", len(alert_rules), alerts_path)

    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM items WHERE tags IS NOT NULL")).scalar()
    logger.info("Migrating tags for %d legacy item(s)%s", total, " [DRY RUN]" if dry_run else "")

    migrated = 0
    offset = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, title, content_snippet, raw_content, tags FROM items "
                     "WHERE tags IS NOT NULL ORDER BY id LIMIT :limit OFFSET :offset"),
                {"limit": batch_size, "offset": offset},
            ).fetchall()
        if not rows:
            break

        for item_id, title, content_snippet, raw_content, old_tags_json in rows:
            old_tags = json.loads(old_tags_json) if old_tags_json else []
            combined_text = f"{title or ''} {content_snippet or ''} {raw_content or ''}"
            new_source_tags, new_content_tags = migrate_item_tags(old_tags, combined_text, alert_rules)

            if not dry_run:
                with engine.connect() as conn:
                    conn.execute(
                        text("UPDATE items SET source_tags = :s, content_tags = :c WHERE id = :id"),
                        {"s": json.dumps(new_source_tags), "c": json.dumps(new_content_tags), "id": item_id},
                    )
                    conn.commit()
            migrated += 1

        offset += batch_size
        logger.info("Processed %d/%d items", migrated, total)

    logger.info("Migration complete: %d items processed%s", migrated, " [DRY RUN - nothing written]" if dry_run else "")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Compute and log changes but write nothing")
    parser.add_argument("--db-path", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    parser.add_argument("--alerts-path", default=os.getenv("ALERTS_PATH", "/app/config/alerts.yaml"))
    args = parser.parse_args()
    run(args.db_path, args.alerts_path, args.dry_run)


if __name__ == "__main__":
    main()
