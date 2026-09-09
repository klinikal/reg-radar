"""Rebuild item_chunks from items.raw_content/content_snippet.

Idempotent via content hash - unchanged items are skipped automatically by
run_chunking(). Use --item-id to test/re-chunk a single item, or run without
arguments for a full-corpus pass.

Run inside the collector container (has scripts/ and DB access):

    docker compose exec collector python -m scripts.rebuild_chunks --dry-run
    docker compose exec collector python -m scripts.rebuild_chunks
    docker compose exec collector python -m scripts.rebuild_chunks --item-id <id>
"""
import argparse
import logging
import os

from sqlalchemy import text

from collector.enrichment import run_chunking
from collector.storage import init_db

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report how many items would be (re)chunked without writing")
    parser.add_argument("--item-id", help="Only process this single item id")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "/app/data/reg_radar.db")
    engine = init_db(db_path)

    if args.dry_run:
        with engine.connect() as conn:
            if args.item_id:
                total = conn.execute(text("SELECT COUNT(*) FROM items WHERE id=:id"), {"id": args.item_id}).scalar()
            else:
                total = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
        logger.info("Dry run: %d item(s) would be evaluated for chunking (unchanged ones will be skipped)", total)
        return

    if args.item_id:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT id FROM items WHERE id=:id"), {"id": args.item_id}).fetchone()
        if not row:
            logger.error("No item with id %s", args.item_id)
            return
        # run_chunking() processes the whole corpus - for a single-item test
        # run, delete that item's existing chunks first so it's guaranteed
        # to be re-chunked rather than skipped as unchanged.
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM item_chunks WHERE item_id=:id"), {"id": args.item_id})
            conn.commit()

    chunked, skipped = run_chunking(engine)
    logger.info("Chunking complete: %d item(s) chunked, %d skipped (unchanged)", chunked, skipped)


if __name__ == "__main__":
    main()
