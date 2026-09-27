"""Repairing items stored with a relative URL (#113).

The trap this guards: ids are never recomputed here (nl-dnb's fix keeps
hashing the link as emitted, see collector/adapters/rss.py and
collector/pipeline.py), so a repair that touched `id` would be wrong - only
`url`/`updated_at` should change, and only for rows that are still relative.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text as sa_text

from scripts.repair_relative_urls import repair

RELATIVE = "/en/general-news/enforcement-measures-2026/item1"
FEED_URL = "https://www.dnb.nl/en/rss/enforcement.xml"
RESOLVED = "https://www.dnb.nl/en/general-news/enforcement-measures-2026/item1"
ITEM_ID = "abc123"


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    with eng.begin() as c:
        c.execute(sa_text(
            "CREATE TABLE items (id TEXT PRIMARY KEY, regulator_id TEXT, source_label TEXT,"
            " url TEXT, updated_at DATETIME)"
        ))
        c.execute(sa_text("INSERT INTO items VALUES (:i,'nl-dnb','enforcement_measures',:u,NULL)"),
                  {"i": ITEM_ID, "u": RELATIVE})
    return eng


def _sources():
    return {("nl-dnb", "enforcement_measures"): FEED_URL}


def _run(engine, apply_changes):
    with patch("scripts.repair_relative_urls.feed_urls_by_source", return_value=_sources()):
        return repair(engine, "unused", apply_changes)


def test_dry_run_writes_nothing(engine):
    repaired, unresolved, unchanged = _run(engine, apply_changes=False)
    assert (repaired, unresolved, unchanged) == (1, 0, 0)
    with engine.connect() as c:
        row = c.execute(sa_text("SELECT id, url, updated_at FROM items")).one()
    assert row.id == ITEM_ID
    assert row.url == RELATIVE
    assert row.updated_at is None


def test_apply_resolves_url_bumps_updated_at_and_keeps_id(engine):
    _run(engine, apply_changes=True)
    with engine.connect() as c:
        row = c.execute(sa_text("SELECT id, url, updated_at FROM items")).one()
    assert row.id == ITEM_ID
    assert row.url == RESOLVED
    assert row.updated_at is not None


def test_already_absolute_row_is_untouched(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    with eng.begin() as c:
        c.execute(sa_text(
            "CREATE TABLE items (id TEXT PRIMARY KEY, regulator_id TEXT, source_label TEXT,"
            " url TEXT, updated_at DATETIME)"
        ))
        c.execute(sa_text("INSERT INTO items VALUES ('x','nl-dnb','enforcement_measures',:u,NULL)"),
                  {"u": RESOLVED})
    repaired, unresolved, unchanged = _run(eng, apply_changes=True)
    assert repaired == 0
    with eng.connect() as c:
        row = c.execute(sa_text("SELECT url, updated_at FROM items")).one()
    assert row.url == RESOLVED
    assert row.updated_at is None


def test_source_with_no_configured_url_is_reported_unresolved(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    with eng.begin() as c:
        c.execute(sa_text(
            "CREATE TABLE items (id TEXT PRIMARY KEY, regulator_id TEXT, source_label TEXT,"
            " url TEXT, updated_at DATETIME)"
        ))
        c.execute(sa_text("INSERT INTO items VALUES ('x','other-reg','unknown_source','/rel',NULL)"))
    with patch("scripts.repair_relative_urls.feed_urls_by_source", return_value=_sources()):
        repaired, unresolved, unchanged = repair(eng, "unused", apply_changes=True)
    assert (repaired, unresolved) == (0, 1)
