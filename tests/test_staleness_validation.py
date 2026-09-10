"""Freshness assertions must either fire or say they cannot.

FATF shipped a `staleness:` override on a page_watch source - warn 200 /
error 550, carefully sized against its three-plenary-a-year cadence - which
could never fire under any circumstances: a page_watch item carries a
fetch-time date, not the source's, so the age the check reads is always
zero. It read as considered configuration while asserting nothing, on the
one source whose whole purpose is catching a grey-list change.

These tests cover the two halves of that: the check no longer treats a
collector-invented date as evidence of freshness, and the registry refuses
the shape rather than accepting it and quietly ignoring it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from collector.main import _source_staleness_error, load_regulators
from scripts.source_health import check_staleness, newest_published_at


def _item(published_at, date_inferred):
    class I:
        pass
    i = I()
    i.published_at = published_at
    i.date_inferred = date_inferred
    return i


OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_newest_published_at_ignores_an_inferred_date():
    """An inferred date is the collector's fetch-time guess, not the source's
    claim - the docstring always said so, the filter did not."""
    assert newest_published_at([_item(datetime.now(timezone.utc), True)]) is None


def test_newest_published_at_uses_a_real_date():
    assert newest_published_at([_item(OLD, False)]) == OLD


def test_a_fetch_time_date_cannot_make_a_source_look_fresh():
    """The page_watch failure mode: every run stamps `now`, so an age-based
    check reads zero days and reports health it never verified."""
    error, warning = check_staleness([_item(datetime.now(timezone.utc), True)], {"type": "page_watch"})
    assert (error, warning) == (None, None)


def test_a_genuinely_stale_source_still_errors():
    error, _ = check_staleness([_item(OLD, False)], {"type": "rss"})
    assert error is not None and "stale" in error


# --- registry validation --------------------------------------------------

def test_staleness_on_a_page_watch_source_is_rejected():
    problem = _source_staleness_error(
        {"type": "page_watch", "staleness": {"warn_days": 200, "error_days": 550}}
    )
    assert problem is not None
    # The message has to say why, or the next person re-adds it.
    assert "page_watch" in problem and "never fire" in problem


def test_staleness_on_a_dated_source_is_accepted():
    assert _source_staleness_error(
        {"type": "rss", "staleness": {"warn_days": 200, "error_days": 550}}
    ) is None


def test_warn_above_error_is_rejected():
    problem = _source_staleness_error(
        {"type": "rss", "staleness": {"warn_days": 900, "error_days": 400}}
    )
    assert problem is not None and "never fire" in problem


def test_no_staleness_block_is_fine():
    assert _source_staleness_error({"type": "rss"}) is None


def test_the_real_registry_carries_no_unfireable_override():
    """Regression guard: an override that cannot fire is worse than none,
    because it looks like the question was considered and answered."""
    offenders = [
        f"{reg['id']}/{src['label']}"
        for reg in load_regulators("config/regulators")
        for src in (reg.get("sources") or [])
        if _source_staleness_error(src)
    ]
    assert offenders == []
