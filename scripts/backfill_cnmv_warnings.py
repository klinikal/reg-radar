"""One-off backfill for CNMV's unregistered-firm warnings (tipoNP=5).

The regular collector only scrapes page 0 (newest-first) on each scheduled
run - enough to catch new warnings going forward, but not the small historic
archive behind it (~5 pages / ~77 documents as of 2026-07-02 - much smaller
than the ESMA/FCA backfills, safe to just run directly rather than treating
it as a big separate decision).

Reads the source URL/selectors from config/regulators/ (the es-cnmv entry,
warnings_unregistered source) rather than duplicating them here.

Run inside the collector container:

    docker compose exec collector python -m scripts.backfill_cnmv_warnings --dry-run
    docker compose exec collector python -m scripts.backfill_cnmv_warnings
"""
import argparse
import logging
import os
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from collector.fetcher import fetch_source
from collector.main import load_regulators, BackfillNotSupportedError, require_backfill_supported
from collector.storage import init_db, insert_item, record_run_complete, record_run_start
from collector.adapters.http import DEFAULT_USER_AGENT

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

REGULATOR_ID = "es-cnmv"
SOURCE_LABEL = "warnings_unregistered"


def _load_source() -> dict:
    regulators_path = os.getenv("REGULATORS_PATH", "/app/config/regulators")
    regulators = load_regulators(regulators_path)
    reg = next(r for r in regulators if r["id"] == REGULATOR_ID)
    source = next(s for s in reg["sources"] if s["label"] == SOURCE_LABEL)
    require_backfill_supported(REGULATOR_ID, SOURCE_LABEL, source)
    return source


def _detect_total_pages(html: str) -> int:
    match = re.search(r"Page \d+ out of (\d+)", html)
    return int(match.group(1)) if match else 1


def _page_url(base_url: str, page: int) -> str:
    parts = urlsplit(base_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def run(delay: float, dry_run: bool) -> None:
    source = _load_source()
    user_agent = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("REQUEST_RETRIES", "3"))
    db_path = os.getenv("DB_PATH", "/app/data/reg_radar.db")
    fetch_cfg = {"user_agent": user_agent, "timeout": timeout, "retries": retries}

    resp = httpx.get(source["url"], headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    total_pages = _detect_total_pages(resp.text)

    logger.info(
        "Backfilling CNMV warnings: %d pages%s",
        total_pages, " [DRY RUN - nothing will be written]" if dry_run else "",
    )

    engine = None if dry_run else init_db(db_path)
    run_id = None if dry_run else record_run_start(engine)
    total_fetched = 0
    total_new = 0
    total_errors = 0
    error_detail = []

    for page in range(total_pages):
        page_source = {**source, "url": _page_url(source["url"], page)}
        try:
            items = fetch_source(page_source, REGULATOR_ID, None, fetch_cfg)
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

        if page < total_pages - 1:
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
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between page requests (default 1.0)")
    args = parser.parse_args()
    try:
        run(args.delay, args.dry_run)
    except BackfillNotSupportedError as e:
        logger.error("%s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
