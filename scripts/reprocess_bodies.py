"""Re-runs Markdown body conversion from stored artefacts for items whose
`extractor` is older than the currently installed conversion library - e.g.
after a markdownify or pypdf version bump. Items with no stored artefact
(RSS entries, or scrape items collected before STORE_ARTEFACTS existed) are
left untouched; nothing to reprocess them from.

Run inside the collector container, which has both DB and artefact access:

    docker compose exec collector python -m scripts.reprocess_bodies --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies
    docker compose exec collector python -m scripts.reprocess_bodies --limit 50

`--from-raw-content` is a separate, one-off mode: pre-v2 installs have
items.body_md NULL (it didn't exist yet) but items.raw_content populated -
the exporter/get_item/etc already fall back to deriving a body from
raw_content on the fly (regradar.document.effective_body_md()), but that's
a per-read fallback; this backfills items.body_md itself (once) so the
stored row matches what every reader already renders, and it's picked up
by cursor-based consumers (get_changes()/webhooks/digests) as a real
change. Only touches items with NO artefact (those are `reprocess()`'s
job) and an empty body_md:

    docker compose exec collector python -m scripts.reprocess_bodies --from-raw-content --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies --from-raw-content

`--refetch` is a third, separate mode (issue #17): items collected from an
`is_partial` RSS source have no fetched article body at all (only the
feed's own summary, or nothing) - `body_md`, `raw_content` and
`content_snippet` all empty. This re-fetches each such item's own
`source_url` and extracts the body via the regulator's `detail_selector`
(config/regulators/*.yaml) - a regulator with no `detail_selector`
configured is skipped and reported, never guessed at. The fetch/select/
convert step itself lives in collector/detail.py, shared with
collector.main.run_collection's own detail fetch for brand-new RSS items
at collection time (issue #109) - this backfills what that collection-time
fetch didn't cover (items collected before this landed, or whose detail
fetch failed on the day). It also selects items whose body_md is only the
"not collected" placeholder even when raw_content/content_snippet aren't
literally empty (issue #115 - a scrape item from a `detail_selector`
regulator whose listing produced no real body carries this placeholder,
which the original all-empty check misses since the placeholder text is a
non-empty body_md); items with a real body are never selected either way:

    docker compose exec collector python -m scripts.reprocess_bodies --refetch --dry-run
    docker compose exec collector python -m scripts.reprocess_bodies --refetch --regulator hu-mnb --limit 50
"""
import argparse
import importlib.metadata
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from collector.adapters.http import (
    DEFAULT_USER_AGENT,
    extract_pdf_text as _extract_pdf_text,
    fetch_html,
    observe_fetch_statuses,
)
from collector.detail import fetch_detail_body
from collector.storage import add_item_artefact, init_db, update_item_body
from regradar.document import PLACEHOLDER_BODY_PREFIX, convert_body, effective_body_md

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

_DEFAULT_ARTEFACTS_DIR = "/app/data/originals"


def _library_version(package: str) -> str:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _current_extractor(mime: str) -> str:
    package = "pypdf" if mime == "application/pdf" else "markdownify"
    return f"{package}/{_library_version(package)}"


def _reconvert(content: bytes, mime: str) -> tuple[str, str]:
    if mime == "application/pdf":
        return convert_body(pdf_text=_extract_pdf_text(content) or "")
    return convert_body(content_html=content.decode("utf-8", errors="replace"))


def reprocess(engine, artefacts_dir: str, dry_run: bool, limit: int | None) -> int:
    sql = """
        SELECT i.id, i.extractor, a.path, a.mime
        FROM items i JOIN item_artefacts a ON a.item_id = i.id
        ORDER BY i.id
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()

    stale = [r for r in rows if r[1] != _current_extractor(r[3])]
    if limit is not None:
        stale = stale[:limit]

    if dry_run:
        logger.info("Dry run: %d item(s) would be reprocessed (of %d with an artefact)", len(stale), len(rows))
        return len(stale)

    processed = 0
    for item_id, _old_extractor, rel_path, mime in stale:
        full_path = Path(artefacts_dir) / rel_path
        if not full_path.exists():
            logger.warning("Artefact missing on disk for %s: %s", item_id, full_path)
            continue
        body_md, extractor = _reconvert(full_path.read_bytes(), mime)
        update_item_body(engine, item_id, body_md, extractor)
        processed += 1
    logger.info("Reprocessed %d item(s)", processed)
    return processed


_RAW_CONTENT_EXTRACTOR = "raw_content-fallback/1"


def backfill_from_raw_content(engine, dry_run: bool, limit: int | None) -> int:
    """Backfills items.body_md (and extractor) for pre-v2 items that have
    raw_content but no body_md and no stored artefact - see module
    docstring. Uses the same derivation effective_body_md() applies at
    read time, so the persisted body matches it exactly.
    """
    sql = """
        SELECT i.id, i.raw_content, i.source_type, i.content_snippet, i.url
        FROM items i
        LEFT JOIN item_artefacts a ON a.item_id = i.id
        WHERE a.item_id IS NULL
          AND (i.body_md IS NULL OR i.body_md = '')
          AND i.raw_content IS NOT NULL AND i.raw_content != ''
        ORDER BY i.id
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).fetchall()
    if limit is not None:
        rows = rows[:limit]

    if dry_run:
        logger.info("Dry run: %d item(s) would be backfilled from raw_content", len(rows))
        return len(rows)

    processed = 0
    for item_id, raw_content, source_type, snippet, url in rows:
        body_md = effective_body_md({
            "body_md": None, "raw_content": raw_content,
            "source_type": source_type, "content_snippet": snippet, "url": url,
        })
        if not body_md:
            continue
        update_item_body(engine, item_id, body_md, _RAW_CONTENT_EXTRACTOR)
        processed += 1
    logger.info("Backfilled %d item(s) from raw_content", processed)
    return processed


def _candidates_sql(regulator_id: str | None) -> str:
    # The WHERE-guard (tasks/lessons.md): every row selected is either
    # genuinely empty on all three body fields, or carries the exact
    # placeholder body (issue #115 - a scrape/RSS item whose listing/feed
    # produced no real content still has a non-empty body_md, since
    # convert_body/rss_body_markdown always write the "not collected" note,
    # so the original all-empty check alone misses it). Both branches are
    # narrow enough that a real body (any other extractor, or body_md not
    # starting with the placeholder text) is never touched.
    where_empty = (
        "(body_md IS NULL OR body_md = '')"
        " AND (raw_content IS NULL OR raw_content = '')"
        " AND (content_snippet IS NULL OR content_snippet = '')"
    )
    where_placeholder = "extractor = 'raw/1' AND body_md LIKE :placeholder_pattern"
    where = [f"(({where_empty}) OR ({where_placeholder}))"]
    if regulator_id:
        where.append("regulator_id = :regulator_id")
    return (
        "SELECT id, regulator_id, url, "
        f"CASE WHEN ({where_empty}) THEN 0 ELSE 1 END AS is_placeholder_only "
        f"FROM items WHERE {' AND '.join(where)} ORDER BY id"
    )


def refetch(engine, regulators: list[dict], dry_run: bool, limit: int | None, regulator_id: str | None,
            delay: float, user_agent: str, timeout: int, retries: int) -> dict:
    """Refetches source_url for every item with no body_md, raw_content or
    content_snippet, plus every item whose body_md is only the "not
    collected" placeholder (issue #115), using each regulator's configured
    `detail_selector` - see the module docstring and `_candidates_sql`.
    Returns a report dict: counts under "recovered"/"not_found"/"blocked"/
    "selector_miss"/"skipped_no_selector"/"fetch_failed", "placeholder_only"
    (how many selected candidates were the placeholder-body case rather than
    all-empty), plus "remaining_empty" (every item still without a body,
    each with its own reason).
    """
    reg_by_id = {r["id"]: r for r in regulators}
    params = {"placeholder_pattern": f"{PLACEHOLDER_BODY_PREFIX}%"}
    if regulator_id:
        params["regulator_id"] = regulator_id
    with engine.connect() as conn:
        rows = conn.execute(text(_candidates_sql(regulator_id)), params).fetchall()
    if limit is not None:
        rows = rows[:limit]

    report = {
        "recovered": 0, "not_found": 0, "blocked": 0, "selector_miss": 0,
        "skipped_no_selector": 0, "fetch_failed": 0, "placeholder_only": 0, "remaining_empty": [],
    }
    fetched = 0
    for item_id, item_regulator_id, url, is_placeholder_only in rows:
        if is_placeholder_only:
            report["placeholder_only"] += 1
        reg = reg_by_id.get(item_regulator_id)
        detail_selector = reg.get("detail_selector") if reg else None
        if not detail_selector:
            report["skipped_no_selector"] += 1
            report["remaining_empty"].append({"id": item_id, "reason": f"no detail_selector configured for {item_regulator_id}"})
            continue

        if fetched > 0:
            # Be polite to the source - throttle_flaresolverr already paces
            # FlareSolverr requests on its own; this covers the plain-HTTP path.
            time.sleep(delay)
        fetched += 1

        flaresolverr_url = None
        if reg.get("cloudflare_protected"):
            flaresolverr_url = os.getenv("FLARESOLVERR_URL", "").strip() or None

        result = fetch_detail_body(
            url, detail_selector, reg.get("detail_exclude"), user_agent, timeout, retries, flaresolverr_url,
            fetch_html=fetch_html, observe_fetch_statuses=observe_fetch_statuses,
        )
        if result["status"] != "recovered":
            report[result["status"]] += 1
            report["remaining_empty"].append({"id": item_id, "reason": result["reason"]})
            continue

        if not dry_run:
            update_item_body(engine, item_id, result["body_md"], result["extractor"])
            add_item_artefact(
                engine, item_id, result["content_html"].encode("utf-8"), "text/html", datetime.now(timezone.utc),
            )
        report["recovered"] += 1

    logger.info(
        "Refetch: %d recovered, %d 404, %d blocked, %d selector-miss, %d skipped (no detail_selector), "
        "%d other failures (%d of the selected candidates were placeholder-only)",
        report["recovered"], report["not_found"], report["blocked"],
        report["selector_miss"], report["skipped_no_selector"], report["fetch_failed"],
        report["placeholder_only"],
    )
    return report


def print_refetch_report(report: dict) -> None:
    print(f"recovered:            {report['recovered']}")
    print(f"404:                  {report['not_found']}")
    print(f"blocked:              {report['blocked']}")
    print(f"selector-miss:        {report['selector_miss']}")
    print(f"skipped-no-selector:  {report['skipped_no_selector']}")
    if report["fetch_failed"]:
        print(f"fetch-failed (other): {report['fetch_failed']}")
    if report["placeholder_only"]:
        print(f"placeholder-only candidates selected: {report['placeholder_only']}")
    if report["remaining_empty"]:
        print("\nItems still with no body:")
        for entry in report["remaining_empty"]:
            print(f"  {entry['id']}: {entry['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report the count without writing")
    parser.add_argument("--limit", type=int, default=None, help="Reprocess at most this many items")
    parser.add_argument("--from-raw-content", action="store_true",
                         help="Backfill body_md from raw_content for artefact-less pre-v2 items instead")
    parser.add_argument("--refetch", action="store_true",
                         help="Refetch source_url for items with no body_md/raw_content/content_snippet instead")
    parser.add_argument("--regulator", default=None, help="--refetch only: restrict to one regulator id (e.g. hu-mnb)")
    parser.add_argument("--delay", type=float, default=2.0, help="--refetch only: seconds between requests (default 2.0)")
    args = parser.parse_args()

    db_path = os.getenv("DB_PATH", "/app/data/reg_radar.db")
    artefacts_dir = os.getenv("ARTEFACTS_DIR", _DEFAULT_ARTEFACTS_DIR)
    engine = init_db(db_path)
    if args.refetch:
        from collector.main import load_regulators  # lazy: pulls in apscheduler, only needed here
        regulators_path = os.getenv("REGULATORS_PATH", "/app/config/regulators")
        regulators = load_regulators(regulators_path)
        user_agent = os.getenv("USER_AGENT", DEFAULT_USER_AGENT)
        timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
        retries = int(os.getenv("REQUEST_RETRIES", "3"))
        report = refetch(
            engine, regulators, dry_run=args.dry_run, limit=args.limit, regulator_id=args.regulator,
            delay=args.delay, user_agent=user_agent, timeout=timeout, retries=retries,
        )
        print_refetch_report(report)
    elif args.from_raw_content:
        backfill_from_raw_content(engine, dry_run=args.dry_run, limit=args.limit)
    else:
        reprocess(engine, artefacts_dir, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
