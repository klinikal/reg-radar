"""FATF black-and-grey-lists page_watch: the watched fragment must carry the
actual country lists, not a pointer to them.

The `watch_selector` is the whole source. `body > div.container.responsivegrid`
was chosen because it is the narrowest fragment that still contains both
statements' country lists - a narrower selector matches only the intro blurb
(page_watch uses select_one, and div.aem-Grid--10 first-matches it), which
would hash to a constant and never report a grey-list change at all. That
failure is silent: the source keeps fetching cleanly forever. Hence this
test rather than the generic fixture contract, which only asserts one item
with a title, a url and a resolved date - all true of an empty blurb.
"""
from pathlib import Path

from bs4 import BeautifulSoup

from collector.adapters.page_watch import PageWatchAdapter
from collector.main import load_regulators

FIXTURE = Path(__file__).parent / "fixtures" / "sources" / "intl-fatf" / "black_and_grey_lists.html"


def _source() -> dict:
    reg = next(r for r in load_regulators("config/regulators") if r["id"] == "intl-fatf")
    return next(s for s in reg["sources"] if s["label"] == "black_and_grey_lists")


def test_watched_fragment_carries_both_country_lists():
    source = _source()
    docs = PageWatchAdapter().parse(FIXTURE.read_text(encoding="utf-8"), source, "intl-fatf")
    assert len(docs) == 1
    text = docs[0].content_text

    # Call-for-action list ("black list") - all three, as at 19 June 2026.
    assert "Democratic People's Republic of Korea Iran Myanmar" in text

    # Increased-monitoring list ("grey list") - spot-checked rather than
    # asserted whole, because the whole point of the source is that this
    # membership changes ~3x a year. Angola opens the list and Yemen closes
    # it; a fragment holding both is holding the list.
    assert "Jurisdictions under Increased Monitoring" in text
    assert "Angola" in text and "Yemen" in text


def test_narrower_selector_would_lose_the_lists():
    # Documents *why* watch_selector must stay as configured: the obvious
    # narrowing loses the substance while still parsing cleanly.
    soup = BeautifulSoup(FIXTURE.read_text(encoding="utf-8"), "lxml")
    narrower = soup.select_one("div.aem-Grid--10")
    assert narrower is not None, "selector premise changed - re-verify watch_selector"
    assert "Myanmar" not in narrower.get_text(" ", strip=True)
