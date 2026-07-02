"""Rebuild items_fts from the items table - backfill for existing rows and
disaster recovery if items_fts ever drifts from items.

Run inside any container that has DB access:

    docker compose exec mcp python -m scripts.rebuild_fts_index --dry-run
    docker compose exec mcp python -m scripts.rebuild_fts_index
"""
import argparse
import logging
import os
import sys

from sqlalchemy import text

from collector.storage import init_db

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def rebuild(engine, dry_run: bool) -> int:
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
        if dry_run:
            logger.info("Dry run: %d items would be (re)indexed into items_fts", total)
            return total
        conn.execute(text("DELETE FROM items_fts"))
        conn.execute(text(
            "INSERT INTO items_fts (item_id, title, content_snippet, raw_content) "
            "SELECT id, COALESCE(title, ''), COALESCE(content_snippet, ''), COALESCE(raw_content, '') "
            "FROM items"
        ))
        conn.commit()
        indexed = conn.execute(text("SELECT COUNT(*) FROM items_fts")).scalar()
        logger.info("Rebuilt items_fts: %d rows indexed", indexed)
        return indexed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report the row count without writing")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "/app/data/casp_radar.db")
    engine = init_db(db_path)
    rebuild(engine, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
