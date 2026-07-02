"""One-off backfill for the ESMA document library (~7,260 historic documents).

The regular collector only scrapes page 0 of the library (default sort is
newest-first) on each scheduled run - that's enough to catch new publications
going forward, but it means the ~363 pages of history behind it are never
collected by the normal daily run. This script walks the full paginated
archive once to backfill that history.

Run inside the collector container, which already has the needed deps and the
mounted data volume:

    docker compose exec collector python -m scripts.backfill_esma_library --dry-run
    docker compose exec collector python -m scripts.backfill_esma_library

Always try --dry-run first - it fetches and parses every page exactly as the
real run would, but does not write to the database, so you can sanity-check
counts before committing to the full crawl.
"""
import argparse
import logging
import os
import re
import sys
import time

import httpx

from collector.fetcher import fetch_source
from collector.storage import init_db, insert_item, record_run_complete, record_run_start

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

BASE_URL = "https://www.esma.europa.eu/databases-library/esma-library"
REGULATOR_ID = "esma"

SOURCE_TEMPLATE = {
    "type": "scrape",
    "label": "library",
    "preferred_language": "en",
    "tags": ["casp", "mica", "publications"],
    "scrape": {
        "item_selector": "table.views-table tbody tr",
        "title_selector": "td.views-field-title a",
        "link_selector": "td.views-field-title a",
        "date_selector": "td.views-field-created time",
        "date_attribute": "datetime",
        "content_selector": "td.views-field-field-document-type",
    },
}


def _detect_last_page(user_agent: str, timeout: int) -> int:
    resp = httpx.get(BASE_URL, headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    match = re.search(r'href="\?page=(\d+)"[^>]*title="Go to last page"', resp.text)
    if match:
        return int(match.group(1))
    logger.warning("Could not auto-detect last page from the pager - defaulting to 0 (page 0 only)")
    return 0


def run(start_page: int, end_page: int | None, delay: float, dry_run: bool) -> None:
    user_agent = os.getenv("USER_AGENT", "casp-radar/1.0")
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("REQUEST_RETRIES", "3"))
    db_path = os.getenv("DB_PATH", "/app/data/casp_radar.db")
    fetch_cfg = {"user_agent": user_agent, "timeout": timeout, "retries": retries}

    if end_page is None:
        end_page = _detect_last_page(user_agent, timeout)

    page_count = end_page - start_page + 1
    logger.info(
        "Backfilling ESMA library pages %d..%d (%d pages, up to ~%d documents)%s",
        start_page, end_page, page_count, page_count * 20,
        " [DRY RUN - nothing will be written]" if dry_run else "",
    )

    engine = None if dry_run else init_db(db_path)
    run_id = None if dry_run else record_run_start(engine)
    total_fetched = 0
    total_new = 0
    total_errors = 0
    error_detail = []

    for page in range(start_page, end_page + 1):
        page_url = BASE_URL if page == 0 else f"{BASE_URL}?page={page}"
        source = {**SOURCE_TEMPLATE, "url": page_url}
        try:
            items = fetch_source(source, REGULATOR_ID, None, fetch_cfg)
            total_fetched += len(items)
            if dry_run:
                logger.info("page %d: %d items (dry run, not inserted)", page, len(items))
            else:
                new_count = sum(1 for item in items if insert_item(engine, item))
                total_new += new_count
                logger.info("page %d: %d items, %d new", page, len(items), new_count)
        except Exception as e:
            total_errors += 1
            msg = f"page {page}: {e}"
            error_detail.append(msg)
            logger.error("Error fetching %s", msg)

        if page < end_page:
            time.sleep(delay)

    if not dry_run:
        record_run_complete(engine, run_id, total_fetched, total_new, total_errors, error_detail)

    logger.info(
        "Backfill complete: fetched=%d new=%d errors=%d%s",
        total_fetched, total_new, total_errors, " [DRY RUN]" if dry_run else "",
    )
    if total_errors:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse every page but write nothing")
    parser.add_argument("--start-page", type=int, default=0)
    parser.add_argument("--end-page", type=int, default=None, help="Default: auto-detected from the site's pager")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between page requests (default 1.0)")
    args = parser.parse_args()
    run(args.start_page, args.end_page, args.delay, args.dry_run)


if __name__ == "__main__":
    main()
