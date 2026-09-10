"""NYDFS's industry-letter and enforcement tables carry unambiguous ISO
dates as plain cell TEXT (`<td>2026-08-03</td>`, no <time datetime="...">).
collector/adapters/scrape.py:_parse_date applies dayfirst=True to any date
read as text, which does not leave ISO alone: dateutil reads "2026-08-03"
as 8 March 2026. Every NYDFS row whose day-of-month is <= 12 therefore lands
with its day and month swapped, silently and with date_inferred False - the
newest enforcement action reads as 2026-03-08 instead of 2026-08-03.

That was a bug in shared adapter code, not in this registry entry - no
selector avoids it, since the cell has no attribute to read. Fixed alongside
#44 by keying dayfirst off the string's shape, so every ISO-text source is
right by default rather than needing a per-source flag. These tests now guard
that fix against regression: NYDFS is the first source to render dates this
way, but it will not be the last.
"""
from pathlib import Path

import pytest

from scripts.source_health import fixture_path, load_regulators, parse_fixture

REGULATORS = load_regulators("config/regulators")

def _newest(label: str):
    regulator = next(r for r in REGULATORS if r["id"] == "us-ny-dfs")
    source = next(s for s in regulator["sources"] if s["label"] == label)
    content = Path(fixture_path("us-ny-dfs", source)).read_bytes()
    items = parse_fixture(source, "us-ny-dfs", content, fetch_pdf=False)
    return max(i.published_at for i in items if i.published_at).date().isoformat()


def test_enforcement_newest_date_matches_the_table():
    # Newest ACTION DATE in the captured table, found by scanning every row:
    # these rows are NOT date-sorted, so "the first row" is not "the newest".
    assert _newest("enforcement_actions") == "2026-08-03"


def test_industry_letters_newest_date_is_not_in_the_future():
    # 2026-08-11 currently misparses to 2026-11-08, i.e. a date later than
    # the day the fixture was captured - the loudest symptom of the swap.
    assert _newest("industry_letters") == "2026-09-10"
