"""Repairing items whose feed date was unparseable (#67).

The trap this guards: `make_item_id` hashes the publication date, so giving
a stored item a real date changes its primary key. A repair that only
updated `items` would strand every classification pointing at the old id,
and one that re-collected instead of repairing would lose the rows already
out of the feed's 20-item window.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text as sa_text

from collector.schema import make_item_id
from scripts.repair_undated_rss_items import repair

PUBLISHED = datetime(2026, 7, 2, 11, 11, 14, tzinfo=timezone.utc)
URL = "https://www.fca.org.uk/news/press-releases/example"


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    old_id = make_item_id("uk-fca", URL, None)
    with eng.begin() as c:
        c.execute(sa_text("CREATE TABLE items (id TEXT PRIMARY KEY, regulator_id TEXT, source_label TEXT,"
                          " url TEXT, published_at DATETIME, date_inferred INT, fetched_at DATETIME)"))
        c.execute(sa_text("CREATE TABLE classifications (item_id TEXT, axis TEXT, value TEXT)"))
        c.execute(sa_text("CREATE VIRTUAL TABLE items_fts USING fts5(item_id UNINDEXED, title)"))
        c.execute(sa_text("INSERT INTO items VALUES (:i,'uk-fca','news',:u,NULL,1,'2026-07-04 06:00:00')"),
                  {"i": old_id, "u": URL})
        c.execute(sa_text("INSERT INTO classifications VALUES (:i,'document_type','press_release')"), {"i": old_id})
        c.execute(sa_text("INSERT INTO items_fts VALUES (:i,'Example')"), {"i": old_id})
    return eng


def _run(engine, apply_changes, date=PUBLISHED):
    with patch("scripts.repair_undated_rss_items.real_published_at", return_value=date):
        with patch("scripts.repair_undated_rss_items.time.sleep"):
            return repair(engine, "uk-fca", ("news", "warnings"), apply_changes, "ua", 5, 1)


def test_dry_run_writes_nothing(engine):
    repaired, _, _ = _run(engine, apply_changes=False)
    assert repaired == 1
    with engine.connect() as c:
        assert c.execute(sa_text("SELECT published_at FROM items")).scalar() is None


def test_apply_sets_the_real_date_and_clears_the_inferred_flag(engine):
    _run(engine, apply_changes=True)
    with engine.connect() as c:
        pub, inferred = c.execute(sa_text("SELECT published_at, date_inferred FROM items")).one()
    assert str(pub).startswith("2026-07-02")
    assert inferred == 0


def test_apply_moves_the_id_and_everything_referencing_it(engine):
    """The whole point: a classification left pointing at the old id would be
    orphaned, and the item would lose its labels."""
    _run(engine, apply_changes=True)
    new_id = make_item_id("uk-fca", URL, PUBLISHED)
    with engine.connect() as c:
        assert c.execute(sa_text("SELECT id FROM items")).scalar() == new_id
        assert c.execute(sa_text("SELECT item_id FROM classifications")).scalar() == new_id
        assert c.execute(sa_text("SELECT item_id FROM items_fts")).scalar() == new_id
        assert c.execute(sa_text("SELECT COUNT(*) FROM items")).scalar() == 1  # moved, not copied


def test_an_unrecoverable_date_leaves_the_row_untouched(engine):
    repaired, unresolved, _ = _run(engine, apply_changes=True, date=None)
    assert (repaired, unresolved) == (0, 1)
    with engine.connect() as c:
        assert c.execute(sa_text("SELECT published_at FROM items")).scalar() is None


def test_an_existing_target_id_is_skipped_not_clobbered(engine):
    """If the adapter fix ran first, the correct row may already exist. The
    repair must report that, never overwrite it."""
    new_id = make_item_id("uk-fca", URL, PUBLISHED)
    with engine.begin() as c:
        c.execute(sa_text("INSERT INTO items VALUES (:i,'uk-fca','news',:u,'2026-07-02 11:11:14',0,'2026-09-10')"),
                  {"i": new_id, "u": URL})
    repaired, _, collided = _run(engine, apply_changes=True)
    assert (repaired, collided) == (0, 1)
    with engine.connect() as c:
        assert c.execute(sa_text("SELECT COUNT(*) FROM items")).scalar() == 2


def test_repair_is_idempotent(engine):
    _run(engine, apply_changes=True)
    repaired, _, _ = _run(engine, apply_changes=True)
    assert repaired == 0
