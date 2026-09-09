"""Offline contract test: every enabled source's existing parser is run
against its captured fixture (tests/fixtures/sources/<regulator_id>/<label>)
and must yield at least one item with a non-empty title, non-empty url, and
a resolved date state (published_at set, or date_inferred True).

Fixtures are captured from the live page/feed via `python -m
scripts.capture_fixture` - see that module's docstring to (re)capture one.
Regenerating a fixture is a deliberate, manual act; this test never fetches
anything itself.
"""
import pytest

from scripts.source_health import assert_contract, fixture_path, iter_enabled_sources, load_regulators, parse_fixture

REGULATORS = load_regulators("config/regulators")

# Sources whose fixture could not be captured in this environment - each
# entry names the reason (never a silent skip). All four regulators here are
# Cloudflare-protected (lt-lb, mt-mfsa) or otherwise bot-blocked (nl-dnb),
# and FlareSolverr is not running for fixture capture, so a live fetch
# returns 403 rather than the actual page content.
CAPTURE_FAILURES = {
    ("lt-lb", "publications_en"):
        "403 Forbidden - lt-lb is cloudflare_protected and FlareSolverr was not running during fixture capture",
    ("mt-mfsa", "circulars_anti_money_laundering"):
        "403 Forbidden - mt-mfsa is cloudflare_protected and FlareSolverr was not running during fixture capture",
    ("mt-mfsa", "circulars_authorisations"):
        "403 Forbidden - mt-mfsa is cloudflare_protected and FlareSolverr was not running during fixture capture",
    ("mt-mfsa", "circulars_crypto_assets_circulars"):
        "403 Forbidden - mt-mfsa is cloudflare_protected and FlareSolverr was not running during fixture capture",
    ("mt-mfsa", "policy_and_guidelines_consultations"):
        "403 Forbidden - mt-mfsa is cloudflare_protected and FlareSolverr was not running during fixture capture",
    ("nl-dnb", "crypto_service_providers"):
        "403 Forbidden from dnb.nl - not marked cloudflare_protected in config, but blocked without a "
        "browser-context fetch (FlareSolverr) in this environment",
    ("nl-dnb", "enforcement_measures"):
        "403 Forbidden from dnb.nl - not marked cloudflare_protected in config, but blocked without a "
        "browser-context fetch (FlareSolverr) in this environment",
    ("ro-asf", "all_news_en"):
        "WAF returns HTTP 200 with a 'Request Rejected' block page body instead of the feed - not "
        "marked cloudflare_protected in config, but blocked without a browser-context fetch in this environment",
    ("es-cnmv", "sector_information"):
        "feed fetched successfully but currently has zero <item> entries - no live content available to "
        "build a meaningful fixture from at capture time",
}


def _params():
    params = []
    for regulator, source in iter_enabled_sources(REGULATORS):
        key = (regulator["id"], source["label"])
        marks = [pytest.mark.xfail(reason=CAPTURE_FAILURES[key], strict=False)] if key in CAPTURE_FAILURES else []
        params.append(pytest.param(regulator, source, marks=marks, id=f"{regulator['id']}/{source['label']}"))
    return params


@pytest.mark.parametrize("regulator,source", _params())
# Real RSS description text can happen to look like a bare filename (no
# HTML tags) - bs4's heuristic warning about that isn't a bug in the parser
# under test here (collector.fetcher, out of scope for this issue), it's
# noise inherent to running many real feeds' actual content through
# get_text(). Scoped to this test only, not suppressed suite-wide.
@pytest.mark.filterwarnings("ignore::bs4.MarkupResemblesLocatorWarning")
def test_source_fixture_contract(regulator, source):
    path = fixture_path(regulator["id"], source)
    assert path.exists(), f"missing fixture: {path}"
    content = path.read_bytes()
    # fetch_pdf=False: this test is offline by contract (see module
    # docstring) - a scrape source with extract_pdf_text: true (e.g.
    # es-cnmv/warnings_unregistered) must not make a live network call here.
    items = parse_fixture(source, regulator["id"], content, fetch_pdf=False)
    assert_contract(items)
