"""One-off backfill for FCA publications (notices/decisions, policy/guidance).

The regular collector only scrapes page 1 of each category (default sort is
newest-first) on each scheduled run - that's enough to catch new publications
going forward, but the historic archive behind it (~5,980 notices/decisions +
~3,743 policy/guidance documents as of 2026-07-02) is never collected by the
normal daily run. This script walks a category's full paginated result set.

This is deliberately NOT run automatically - FCA is a much larger crawl than
the ESMA library backfill (see scripts/backfill_esma_library.py), and running
it is a real decision, not a side effect of adding the source. Always
--dry-run first.

Reads source URLs and CSS selectors directly from config/regulators/ (the
uk-fca entry) rather than duplicating them here, so the backfill can never
silently drift out of sync with the live scrape config.

Run inside the collector container:

    docker compose exec collector python -m scripts.backfill_fca_publications --category notices_and_decisions --dry-run --max-pages 2
    docker compose exec collector python -m scripts.backfill_fca_publications --category notices_and_decisions
    docker compose exec collector python -m scripts.backfill_fca_publications --category all
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
from collector.main import load_regulators
from collector.storage import init_db, insert_item, record_run_complete, record_run_start

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

REGULATOR_ID = "uk-fca"
PAGE_SIZE = 10
CATEGORIES = ["notices_and_decisions", "policy_and_guidance"]


def _load_source(label: str) -> dict:
    regulators_path = os.getenv("REGULATORS_PATH", "/app/config/regulators")
    regulators = load_regulators(regulators_path)
    reg = next(r for r in regulators if r["id"] == REGULATOR_ID)
    return next(s for s in reg["sources"] if s["label"] == label)


def _detect_total(html: str) -> int:
    match = re.search(r'of\s*<strong>([\d,]+)</strong>\s*search results', html)
    return int(match.group(1).replace(",", "")) if match else 0


def _page_url(base_url: str, start: int) -> str:
    parts = urlsplit(base_url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["start"] = str(start)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def run(label: str, delay: float, dry_run: bool, max_pages: int | None) -> None:
    source = _load_source(label)
    user_agent = os.getenv("USER_AGENT", "reg-radar/2.0 (+https://github.com/klinikal/reg-radar)")
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("REQUEST_RETRIES", "3"))
    db_path = os.getenv("DB_PATH", "/app/data/reg_radar.db")
    fetch_cfg = {"user_agent": user_agent, "timeout": timeout, "retries": retries}

    resp = httpx.get(source["url"], headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    total = _detect_total(resp.text)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) if total else 1
    if max_pages:
        total_pages = min(total_pages, max_pages)

    logger.info(
        "Backfilling FCA '%s': ~%d documents, %d pages%s",
        label, total, total_pages, " [DRY RUN - nothing will be written]" if dry_run else "",
    )

    engine = None if dry_run else init_db(db_path)
    run_id = None if dry_run else record_run_start(engine)
    total_fetched = 0
    total_new = 0
    total_errors = 0
    error_detail = []

    for page in range(total_pages):
        start = page * PAGE_SIZE + 1
        page_source = {**source, "url": _page_url(source["url"], start)}
        try:
            items = fetch_source(page_source, REGULATOR_ID, None, fetch_cfg)
            total_fetched += len(items)
            if dry_run:
                logger.info("%s start=%d: %d items (dry run, not inserted)", label, start, len(items))
            else:
                new_count = sum(1 for item in items if insert_item(engine, item))
                total_new += new_count
                logger.info("%s start=%d: %d items, %d new", label, start, len(items), new_count)
        except Exception as e:
            total_errors += 1
            msg = f"{label} start={start}: {e}"
            error_detail.append(msg)
            logger.error("Error fetching %s", msg)

        if page < total_pages - 1:
            time.sleep(delay)

    if not dry_run:
        record_run_complete(engine, run_id, total_fetched, total_new, total_errors, error_detail)

    logger.info(
        "Backfill complete (%s): fetched=%d new=%d errors=%d%s",
        label, total_fetched, total_new, total_errors, " [DRY RUN]" if dry_run else "",
    )
    if total_errors:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--category", choices=[*CATEGORIES, "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse every page but write nothing")
    parser.add_argument("--max-pages", type=int, default=None, help="Cap pages fetched per category (useful for testing)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds to wait between page requests (default 1.0)")
    args = parser.parse_args()

    labels = CATEGORIES if args.category == "all" else [args.category]
    for label in labels:
        run(label, args.delay, args.dry_run, args.max_pages)


if __name__ == "__main__":
    main()
