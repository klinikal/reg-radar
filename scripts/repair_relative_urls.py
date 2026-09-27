"""Repair items stored with a relative URL (issue #113).

Since about 2026-07-09 nl-dnb's RSS feeds (enforcement_measures,
crypto_service_providers) emit relative `<link>` values
(`/en/general-news/...`) instead of absolute ones. `collector/adapters/rss.py`
stored them as-is until it started resolving them against the feed's own URL
(same fix as this issue) - so every row collected before that fix has
`items.url` still relative, breaking links in the web UI, exports and
digests.

Ids are untouched: `make_item_id` was never given the relative link to hash
in the first place for these rows in a way this repair would change (it
hashes the link as collected, and this script never recomputes an id), so
there is nothing to migrate across `classifications`/`item_artefacts`/
`item_chunks`/`items_fts` - only `items.url` (and `items.updated_at`) change.

WHERE-guarded to touch only rows that are actually still relative
(`url NOT LIKE 'http%'`) - never every row in the source, matching the
lesson from the 2026-07-04 tags-column migration incident.

Dry run by default. Writing requires --apply.

    python -m scripts.repair_relative_urls            # report only
    python -m scripts.repair_relative_urls --apply     # write
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

from sqlalchemy import create_engine, text as sa_text

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
logger = logging.getLogger("repair")


def affected_items(conn):
    """(id, regulator_id, source_label, url) for every row whose url isn't
    absolute. LIKE is case-insensitive in SQLite for ASCII, so this also
    catches an unlikely `HTTP...` scheme."""
    return list(conn.execute(sa_text(
        "SELECT id, regulator_id, source_label, url FROM items WHERE url NOT LIKE 'http%' ORDER BY regulator_id, source_label"
    )))


def feed_urls_by_source(regulators_path: str) -> dict[tuple[str, str], str]:
    """{(regulator_id, source_label): feed_url} - the base every affected
    source's relative links must be resolved against, same as the RSS
    adapter resolves them at collection time."""
    from collector.main import load_regulators
    out = {}
    for reg in load_regulators(regulators_path):
        for src in reg.get("sources") or []:
            if src.get("label") and src.get("url"):
                out[(reg["id"], src["label"])] = src["url"]
    return out


def repair(engine, regulators_path: str, apply_changes: bool):
    with engine.connect() as conn:
        rows = affected_items(conn)
    feed_urls = feed_urls_by_source(regulators_path)

    repaired = unresolved = unchanged = 0
    for old_id, regulator_id, source_label, url in rows:
        feed_url = feed_urls.get((regulator_id, source_label))
        if not feed_url:
            logger.warning("  no configured source url for %s/%s, left alone: %s", regulator_id, source_label, url)
            unresolved += 1
            continue
        resolved = urljoin(feed_url, url)
        if resolved == url or not resolved.startswith("http"):
            # Already resolves to itself (e.g. no base to join against) -
            # nothing to fix.
            unchanged += 1
            continue
        logger.info("  %s/%s %s -> %s", regulator_id, source_label, url, resolved)
        repaired += 1
        if not apply_changes:
            continue
        with engine.begin() as conn:
            conn.execute(sa_text(
                "UPDATE items SET url = :new, updated_at = :now WHERE id = :id AND url NOT LIKE 'http%'"
            ), {"new": resolved, "now": datetime.now(timezone.utc), "id": old_id})

    verb = "repaired" if apply_changes else "would repair"
    logger.info("%s %d item(s); %d with no configured source to resolve against; %d already resolved to themselves",
                verb, repaired, unresolved, unchanged)
    if not apply_changes and repaired:
        logger.info("Dry run - nothing written. Re-run with --apply to commit.")
    return repaired, unresolved, unchanged


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    ap.add_argument("--db", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    ap.add_argument("--regulators-path", default=os.getenv("REGULATORS_PATH", "/app/config/regulators"))
    args = ap.parse_args()

    engine = create_engine(f"sqlite:///{args.db}")
    repaired, unresolved, _ = repair(engine, args.regulators_path, args.apply)
    sys.exit(1 if unresolved and not repaired else 0)


if __name__ == "__main__":
    main()
