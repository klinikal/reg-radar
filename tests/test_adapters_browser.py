"""Tests for collector.adapters.browser.BrowserAdapter.

Importable and testable without the playwright browser binary present -
parse() (used here) never launches a browser, it just runs the `scrape`
parser against already-rendered HTML. The fixture below reproduces AMLA's
document library DOM shape (../wiki/content/concept-source-adapter-framework.md:
"browser is proven against a fixture reproducing a real
JS-rendered target's DOM shape"). A real Playwright launch is only exercised
if REGRADAR_TEST_REAL_BROWSER=1 is set (never in CI).
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from collector.adapters.base import FetchContext
from collector.adapters.browser import BrowserAdapter, _ssrf_route_handler

# AMLA's document library renders its list client-side (Angular/ECL
# components) - same ecl-content-item card shape as the public-consultations
# page (verified via the scrape adapter's AMLA fixture), but only present in
# the DOM after JS execution, which is why it needs `browser` rather than
# `scrape`.
AMLA_LIBRARY_HTML = '''
<div id="document-library-app">
  <article class="ecl-content-item">
    <div class="ecl-content-block__title">
      <a class="ecl-link" href="/publications/aml-cft-risk-assessment-methodology">AML/CFT Risk Assessment Methodology</a>
    </div>
    <dl class="ecl-description-list">
      <dt>Publication date</dt>
      <dd class="ecl-description-list__definition"><time datetime="2026-06-18T09:00:00Z">18 June 2026</time></dd>
    </dl>
  </article>
  <article class="ecl-content-item">
    <div class="ecl-content-block__title">
      <a class="ecl-link" href="/publications/casp-supervision-guidelines">CASP Supervision Guidelines</a>
    </div>
    <dl class="ecl-description-list">
      <dt>Publication date</dt>
      <dd class="ecl-description-list__definition"><time datetime="2026-05-02T09:00:00Z">2 May 2026</time></dd>
    </dl>
  </article>
</div>
'''

AMLA_SOURCE = {
    "type": "browser",
    "url": "https://www.amla.europa.eu/publications/document-library_en",
    "label": "document_library",
    "preferred_language": "en",
    "tags": ["casp", "aml"],
    "browser": {
        "wait_selector": "article.ecl-content-item",
        "item_selector": "article.ecl-content-item",
        "title_selector": ".ecl-content-block__title a",
        "link_selector": ".ecl-content-block__title a",
        "date_selector": ".ecl-description-list__definition time",
        "date_attribute": "datetime",
    },
}


def test_parse_extracts_title_link_and_date_from_rendered_dom():
    docs = BrowserAdapter().parse(AMLA_LIBRARY_HTML, AMLA_SOURCE, "amla")
    assert len(docs) == 2
    assert docs[0].title == "AML/CFT Risk Assessment Methodology"
    assert docs[0].url == "https://www.amla.europa.eu/publications/aml-cft-risk-assessment-methodology"
    assert docs[0].date_inferred is False
    assert docs[0].published_at.month == 6
    assert docs[0].published_at.day == 18


def test_parse_reuses_scrape_selector_shape():
    docs = BrowserAdapter().parse(AMLA_LIBRARY_HTML, AMLA_SOURCE, "amla")
    assert docs[1].title == "CASP Supervision Guidelines"


def test_module_importable_without_browser_binary():
    # The whole point of this adapter guarding its own playwright import
    # inside fetch()/fetch_raw() rather than at module scope - the package
    # (collector/requirements.txt) is a hard dependency and is importable
    # here, but a browser binary is never required just to import this
    # module or call parse().
    import collector.adapters.browser  # noqa: F401


def test_ssrf_route_handler_aborts_request_to_private_address():
    route = MagicMock()
    request = MagicMock()
    request.url = "http://169.254.169.254/latest/meta-data/"
    _ssrf_route_handler(route, request)
    route.abort.assert_called_once()
    route.continue_.assert_not_called()


def test_ssrf_route_handler_continues_public_request():
    route = MagicMock()
    request = MagicMock()
    request.url = "https://example.com/asset.js"
    with patch("collector.adapters.browser._host_resolves_to_private", return_value=False):
        _ssrf_route_handler(route, request)
    route.continue_.assert_called_once()
    route.abort.assert_not_called()


def test_ssrf_route_handler_aborts_hostname_resolving_to_private_ip():
    # Same DNS-rebinding coverage as the httpx SSRF path - a redirect or XHR
    # target during rendering can be a plain hostname that resolves to an
    # internal address, not just a raw IP literal.
    route = MagicMock()
    request = MagicMock()
    request.url = "http://internal.example.com/data"
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("10.0.0.5", 0))]
        _ssrf_route_handler(route, request)
    route.abort.assert_called_once()


@pytest.mark.skipif(
    os.getenv("REGRADAR_TEST_REAL_BROWSER") != "1",
    reason="real Playwright browser launch - opt in with REGRADAR_TEST_REAL_BROWSER=1 (never run in CI)",
)
def test_fetch_launches_a_real_browser():
    ctx = FetchContext(user_agent="regradar-test/1.0", timeout=10, retries=1, flaresolverr_url=None, regulator_id="amla")
    docs = BrowserAdapter().fetch({**AMLA_SOURCE, "url": "https://example.com"}, ctx)
    assert isinstance(docs, list)
