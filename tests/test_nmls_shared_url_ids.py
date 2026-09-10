"""NMLS (CSBS) publishes its `State Licensing` news through a SharePoint list
that carries NO per-item URL field of any kind (verified live 2026-09-10:
the item shape is Id/Title/NewsItemCategory/NewsItemDescription/Created/
Modified/GUID and nothing url-shaped), so every item is configured with the
same landing-page url. Issue #44's definition of done requires the resulting
ids to be *demonstrated* distinct, not assumed: make_item_id hashes
regulator_id|url|published_at, so distinctness rests entirely on the
`Created` timestamps being unique across the window.

Deliberately fixture-driven rather than registry-parametrised: the NMLS
entry belongs in config/regulators/us.yaml, which this change does not own,
and the json_api adapter cannot yet emit these items at all (it skips any
item whose url_field resolves to nothing - see the reported adapter delta).
The fixture is the exact response the configured source fetches.
"""
import json
from pathlib import Path

from dateutil.parser import parse as parse_date

from collector.schema import make_item_id

LANDING_PAGE = (
    "https://mortgage.nationwidelicensingsystem.org/knowledge/news/SitePages/"
    "NMLS-State-%26-Agency-News.aspx"
)

FIXTURE = Path("tests/fixtures/sources/us-nmls/state_licensing.json")


def _items():
    return json.loads(FIXTURE.read_bytes())["value"]


def test_shared_landing_page_url_still_yields_distinct_item_ids():
    items = _items()
    assert len(items) >= 20
    ids = [
        make_item_id("us-nmls", LANDING_PAGE, parse_date(item["Created"]))
        for item in items
    ]
    assert len(set(ids)) == len(ids)


def test_every_item_is_state_licensing_with_a_title_and_a_real_date():
    for item in _items():
        assert item["NewsItemCategory"] == "State Licensing"
        assert item["Title"].strip()
        assert parse_date(item["Created"])
