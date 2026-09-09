"""One-off backfill for MFSA publication categories.

The regular collector only scrapes page 1 of each *enabled* category
(newest-first) on every run - enough to catch new publications going
forward for the categories currently enabled in config/regulators/mt.yaml.
This script walks a category's full paginated history, and - unlike the
ESMA/FCA backfills - can also target categories that aren't enabled for
daily collection at all, since config/regulators/mt.yaml defines MFSA's full
~65-category taxonomy with only the CASP-relevant subset enabled.

MFSA is Cloudflare-protected and requires FlareSolverr for every request
(confirmed - plain HTTP is blocked regardless of User-Agent). The shared
throttle/backoff logic in collector/adapters/http.py paces every
FlareSolverr call automatically (default: minimum 8s between requests,
longer backoff on ban-like errors) - this script does not need its own
pacing logic, but inherits FlareSolverr's real per-request cost (~10-15s),
so a full run across many categories and pages is slow by design, not a
bug. Always --dry-run first, and prefer running overnight / off-hours given
the cumulative time.

Parses each page via the registered `scrape` adapter's own parser
(collector/adapters/scrape.py: ScrapeAdapter().parse()) instead of
duplicating its selector logic - the only thing this script adds on top is
walking the pagination and fetching each page's raw HTML (via the same
shared collector.adapters.http.fetch_html() the adapter itself uses).

Run inside the collector container:

    docker compose exec collector python -m scripts.backfill_mfsa_publications --category circulars_crypto_assets_circulars --dry-run --max-pages 2
    docker compose exec collector python -m scripts.backfill_mfsa_publications --category circulars_crypto_assets_circulars
    docker compose exec collector python -m scripts.backfill_mfsa_publications --all --dry-run --max-pages 1   # sanity-check every category cheaply
    docker compose exec collector python -m scripts.backfill_mfsa_publications --all                            # full ~65-category historic crawl - hours, run deliberately
"""
import argparse
import logging
import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit

from collector.adapters.http import fetch_html
from collector.adapters.scrape import ScrapeAdapter
from collector.main import load_regulators
from collector.pipeline import process_raw_documents
from collector.storage import init_db, insert_item, record_run_complete, record_run_start

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

REGULATOR_ID = "mt-mfsa"


def _load_scrape_sources() -> list[dict]:
    regulators_path = os.getenv("REGULATORS_PATH", "/app/config/regulators")
    regulators = load_regulators(regulators_path)
    reg = next(r for r in regulators if r["id"] == REGULATOR_ID)
    return [s for s in reg["sources"] if s.get("type") == "scrape"]


def _page_url(base_url: str, page: int) -> str:
    if page == 1:
        return base_url
    parts = urlsplit(base_url)
    path = parts.path.rstrip("/") + f"/page/{page}/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _has_next_page(html: str) -> bool:
    return bool(re.search(r'class="next page-numbers"', html))


def _backfill_category(source: dict, flaresolverr_url: str, fetch_cfg: dict, engine, dry_run: bool, max_pages: int | None):
    label = source["label"]
    page = 1
    total_fetched = 0
    total_new = 0
    total_errors = 0
    error_detail = []
    consecutive_page_failures = 0
    max_consecutive_failures = 3

    while True:
        page_url = _page_url(source["url"], page)
        page_source = {**source, "url": page_url}
        try:
            # fetch_source() doesn't expose the raw HTML (only parsed Items),
            # but the next-page link check below needs it - fetch directly
            # the same way fetch_source() would internally, including the
            # expected_selector fingerprint check (without it, a 200
            # maintenance/captcha page that lacks the real listing markup
            # parses to 0 items with no error raised).
            expected_selector = source.get("scrape", {}).get("item_selector")
            html = fetch_html(page_url, fetch_cfg["user_agent"], fetch_cfg["timeout"], fetch_cfg["retries"], flaresolverr_url, expected_selector)
            if html is None:
                raise RuntimeError("fetch failed (see FlareSolverr warnings above)")

            raw_docs = ScrapeAdapter().parse(html, page_source, REGULATOR_ID, fetch_cfg)
            items = process_raw_documents(raw_docs, page_source, REGULATOR_ID)
            total_fetched += len(items)

            if dry_run:
                logger.info("%s page %d: %d items (dry run, not inserted)", label, page, len(items))
            else:
                new_count = sum(1 for item in items if insert_item(engine, item))
                total_new += new_count
                logger.info("%s page %d: %d items, %d new", label, page, len(items), new_count)

            has_next = _has_next_page(html)
            consecutive_page_failures = 0
        except Exception as e:
            total_errors += 1
            consecutive_page_failures += 1
            msg = f"{label} page {page}: {e}"
            error_detail.append(msg)
            logger.error("Error fetching %s (consecutive failures: %d/%d)", msg, consecutive_page_failures, max_consecutive_failures)
            if consecutive_page_failures >= max_consecutive_failures:
                # A run of failures in a row suggests something's genuinely
                # broken for this category (blocked, category gone, etc.) -
                # give up on it, but this only ends *this* category's crawl,
                # not the whole backfill (the caller moves on to the next one).
                logger.error("%s: %d consecutive page failures - giving up on this category", label, consecutive_page_failures)
                has_next = False
            else:
                # A single transient failure (network blip, one bad page)
                # shouldn't silently end the rest of a category's history -
                # skip this page and keep going.
                has_next = True

        if not has_next or (max_pages and page >= max_pages):
            break
        page += 1

    return total_fetched, total_new, total_errors, error_detail


def run(category: str | None, run_all: bool, dry_run: bool, max_pages: int | None) -> None:
    user_agent = os.getenv("USER_AGENT", "regradar/2.0")
    timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("REQUEST_RETRIES", "3"))
    flaresolverr_url = os.getenv("FLARESOLVERR_URL", "").strip()
    db_path = os.getenv("DB_PATH", "/app/data/regradar.db")
    fetch_cfg = {"user_agent": user_agent, "timeout": timeout, "retries": retries}

    if not flaresolverr_url:
        logger.error("FLARESOLVERR_URL is not set - MFSA requires it for every request. Aborting.")
        sys.exit(1)

    all_sources = _load_scrape_sources()
    if run_all:
        targets = all_sources
    else:
        targets = [s for s in all_sources if s["label"] == category]
        if not targets:
            logger.error("No MFSA scrape source with label '%s' - available: %s", category, [s["label"] for s in all_sources])
            sys.exit(1)

    logger.info(
        "Backfilling %d MFSA categor%s%s",
        len(targets), "y" if len(targets) == 1 else "ies",
        " [DRY RUN - nothing will be written]" if dry_run else "",
    )

    engine = None if dry_run else init_db(db_path)
    run_id = None if dry_run else record_run_start(engine)
    grand_fetched = grand_new = grand_errors = 0
    grand_error_detail = []

    for source in targets:
        fetched, new, errors, error_detail = _backfill_category(source, flaresolverr_url, fetch_cfg, engine, dry_run, max_pages)
        grand_fetched += fetched
        grand_new += new
        grand_errors += errors
        grand_error_detail.extend(error_detail)

    if not dry_run:
        record_run_complete(engine, run_id, grand_fetched, grand_new, grand_errors, grand_error_detail)

    logger.info(
        "Backfill complete: categories=%d fetched=%d new=%d errors=%d%s",
        len(targets), grand_fetched, grand_new, grand_errors, " [DRY RUN]" if dry_run else "",
    )
    if grand_errors:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--category", help="Single source label to backfill (see config/regulators/mt.yaml mt-mfsa sources)")
    group.add_argument("--all", action="store_true", help="Backfill every MFSA category, enabled or not")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and parse every page but write nothing")
    parser.add_argument("--max-pages", type=int, default=None, help="Cap pages fetched per category (useful for testing)")
    args = parser.parse_args()
    run(args.category, args.all, args.dry_run, args.max_pages)


if __name__ == "__main__":
    main()
