"""Repair items stored with a fetch-time date because their feed's date was
unparseable (issue #67).

The FCA's news and warnings feeds render their date for humans
("Thursday, September 3, 2026 - 10:35"), which feedparser cannot read as
RFC 822, so every item from them was stored with `published_at IS NULL` and
`date_inferred = 1`. `collector/adapters/rss.py` now falls back to the raw
string, but that only helps items collected from here on: the id of an
already-stored item was computed WITHOUT a date, and `make_item_id` hashes
the date when there is one. Re-collecting would therefore insert a second
copy under a new id rather than updating the existing row.

Deleting the affected rows and re-collecting is not an option either - each
feed carries only 20 items, so most affected rows are long out of the feed
window and would be lost rather than refreshed.

So this repairs them in place: fetch each item's own page, take the real
date from its `<time datetime>`, recompute the id, and move the row and
everything referencing it. `items_fts` has no triggers - it is maintained by
hand in `storage.insert_item` - so it is updated explicitly here too.

Dry run by default. Writing requires --apply.

    python -m scripts.repair_undated_rss_items            # report only
    python -m scripts.repair_undated_rss_items --apply    # write
"""
import argparse
import logging
import os
import sys
import time
from datetime import timezone

from bs4 import BeautifulSoup
from sqlalchemy import create_engine, text as sa_text

from collector.adapters.base import parse_text_date
from collector.adapters.http import DEFAULT_USER_AGENT, fetch_html
from collector.schema import make_item_id

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
logger = logging.getLogger("repair")

# Tables holding a foreign key to items.id. items_fts is included because it
# is a plain FTS5 table with an UNINDEXED item_id column and no triggers.
REFERRING = [("classifications", "item_id"), ("item_artefacts", "item_id"),
             ("item_chunks", "item_id"), ("items_fts", "item_id")]

DEFAULT_REGULATOR = "uk-fca"
DEFAULT_SOURCES = ("news", "warnings")
# The date the page itself states, not the feed's. Verified on fca.org.uk:
# <time datetime="2026-07-02T11:11:14Z">02/07/2026</time>
DATE_SELECTOR = "time[datetime]"
POLITE_DELAY_SECONDS = 1.0


def affected_items(conn, regulator_id, sources):
    placeholders = ",".join(f":s{i}" for i in range(len(sources)))
    params = {"reg": regulator_id, **{f"s{i}": s for i, s in enumerate(sources)}}
    return list(conn.execute(sa_text(
        f"SELECT id, url, source_label, fetched_at FROM items "
        f"WHERE regulator_id = :reg AND source_label IN ({placeholders}) "
        f"AND published_at IS NULL ORDER BY fetched_at"
    ), params))


def feed_dates(source_url, user_agent, timeout, retries):
    """{item url: date} exactly as the RSS adapter would derive it.

    An item still inside the feed's window will be re-collected, and the
    adapter computes its id from the date IT derives. If the repair used a
    different derivation the two disagree and the next run inserts a second
    copy rather than deduping - which is precisely what happened the first
    time this ran: the page states `2026-08-06T10:25:16Z` while the feed says
    "Thursday, August 6, 2026 - 11:25", the same instant to a different
    precision and timezone, so the ids differed and 32 items duplicated.

    So for anything the feed still carries, the feed wins - not because it is
    the better date (it is not; it lacks seconds and a timezone) but because
    it is the one the collector will keep re-deriving. Items outside the
    window are never re-fetched, so the page's better date is safe there.
    """
    import feedparser
    try:
        raw = fetch_html(source_url, user_agent, timeout, retries)
    except Exception as e:
        logger.warning("could not read feed %s: %s - falling back to page dates", source_url, e)
        return {}
    out = {}
    for entry in feedparser.parse(raw).entries:
        link = (entry.get("link") or "").strip()
        if not link:
            continue
        if entry.get("published_parsed"):
            continue  # adapter takes this path; nothing was ever undated here
        for key in ("published", "updated", "created"):
            if entry.get(key):
                parsed = parse_text_date(str(entry[key]))
                if parsed is not None:
                    out[link] = parsed
                break
    return out


def real_published_at(url, user_agent, timeout, retries):
    """The publication date the item's own page states, or None."""
    try:
        html = fetch_html(url, user_agent, timeout, retries)
    except Exception as e:
        logger.warning("  fetch failed %s: %s", url, e)
        return None
    if not html:
        return None
    el = BeautifulSoup(html, "lxml").select_one(DATE_SELECTOR)
    if el is None:
        return None
    # datetime="..." is machine-readable, so dayfirst must not be applied.
    return parse_text_date(el.get("datetime"), date_attribute="datetime")


def repair(engine, regulator_id, sources, apply_changes, user_agent, timeout, retries, limit=None, source_urls=None):
    with engine.connect() as conn:
        rows = affected_items(conn, regulator_id, sources)

    # Prefer the feed's own derivation for anything still in its window.
    from_feed = {}
    for src in source_urls or []:
        from_feed.update(feed_dates(src, user_agent, timeout, retries))
    if from_feed:
        logger.info("%d item(s) still in a feed window will take the feed's date", len(from_feed))
    if limit:
        rows = rows[:limit]
    logger.info("%d item(s) with no publication date in %s/%s",
                len(rows), regulator_id, ",".join(sources))

    repaired = unresolved = collided = 0
    for i, (old_id, url, label, fetched_at) in enumerate(rows, 1):
        if i > 1:
            time.sleep(POLITE_DELAY_SECONDS)
        published = from_feed.get(url) or real_published_at(url, user_agent, timeout, retries)
        if published is None:
            logger.warning("  [%d/%d] no date found, left alone: %s", i, len(rows), url)
            unresolved += 1
            continue
        new_id = make_item_id(regulator_id, url, published)
        if new_id == old_id:
            continue
        with engine.connect() as conn:
            clash = conn.execute(sa_text("SELECT 1 FROM items WHERE id = :i"), {"i": new_id}).fetchone()
        if clash:
            # Already re-collected under the correct id - repairing would
            # collide on the primary key. Report; never clobber.
            logger.warning("  [%d/%d] target id already exists, skipped: %s", i, len(rows), url)
            collided += 1
            continue
        logger.info("  [%d/%d] %s -> %s (%s, was fetched %s)",
                    i, len(rows), label, published.date().isoformat(), old_id[:8], str(fetched_at)[:10])
        repaired += 1
        if not apply_changes:
            continue
        iso = published.astimezone(timezone.utc).replace(tzinfo=None)
        with engine.begin() as conn:
            for table, col in REFERRING:
                try:
                    conn.execute(sa_text(f"UPDATE {table} SET {col} = :new WHERE {col} = :old"),
                                 {"new": new_id, "old": old_id})
                except Exception as e:
                    logger.warning("    %s not updated: %s", table, e)
            conn.execute(sa_text(
                "UPDATE items SET id = :new, published_at = :pub, date_inferred = 0 WHERE id = :old"
            ), {"new": new_id, "pub": iso, "old": old_id})

    verb = "repaired" if apply_changes else "would repair"
    logger.info("%s %d item(s); %d with no recoverable date; %d skipped as already present",
                verb, repaired, unresolved, collided)
    if not apply_changes and repaired:
        logger.info("Dry run - nothing written. Re-run with --apply to commit.")
    return repaired, unresolved, collided


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--regulator", default=DEFAULT_REGULATOR)
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help="comma-separated source labels")
    ap.add_argument("--apply", action="store_true",
                    help="write the changes (default is a dry run)")
    ap.add_argument("--limit", type=int, default=None, help="only handle the first N (for a trial)")
    ap.add_argument("--db", default=os.getenv("DB_PATH", "/app/data/reg_radar.db"))
    args = ap.parse_args()

    engine = create_engine(f"sqlite:///{args.db}")
    sources = tuple(s.strip() for s in args.sources.split(",") if s.strip())
    # The feed URLs for those source labels, read from the registry.
    from collector.main import load_regulators
    source_urls = [
        src["url"]
        for reg in load_regulators(os.getenv("REGULATORS_PATH", "/app/config/regulators"))
        if reg["id"] == args.regulator
        for src in (reg.get("sources") or [])
        if src.get("label") in sources and src.get("url")
    ]
    repaired, unresolved, _ = repair(
        engine, args.regulator, sources,
        args.apply, os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
        int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")), int(os.getenv("REQUEST_RETRIES", "3")),
        args.limit, source_urls,
    )
    sys.exit(1 if unresolved and not repaired else 0)


if __name__ == "__main__":
    main()
