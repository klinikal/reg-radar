"""Playwright-driven adapter for JS-rendered listings (e.g. AMLA's document
library, DFSA Denmark's Blazor news list) - targets that need JS execution,
not a Cloudflare challenge, so they can skip the FlareSolverr dependency.

Reuses `scrape`'s selector shape (item_selector/title_selector/
link_selector/date_selector/date_attribute/content_selector) plus a
`wait_selector` the page must render before the DOM is read:

    sources:
      - type: browser
        url: https://example.com/library
        label: documents
        browser:
          wait_selector: ".doc-list-item"
          item_selector: ".doc-list-item"
          title_selector: ".doc-title"
          link_selector: "a"
          date_selector: ".doc-date"

The `playwright` package must be importable for this module to load (it is
pure Python; declared in collector/requirements.txt), but launching a real
browser needs its downloaded binary, installed in the collector Docker image
only - `parse()` (used by fixture-based tests and shared by `fetch()`) never
needs a browser at all, it just runs `scrape`'s parser against already-
rendered HTML.
"""
import logging
from urllib.parse import urlparse

from collector.adapters.base import FetchContext, RawDocument, SourceAdapter
from collector.adapters.http import _host_resolves_to_private, throttle_flaresolverr, validate_url_not_private
from collector.adapters.scrape import ScrapeAdapter

logger = logging.getLogger(__name__)


def _ssrf_route_handler(route, request) -> None:
    """Aborts any request (navigation, redirect, XHR/fetch, asset load - a
    JS-rendered page can trigger all of these after the initial goto()) whose
    URL is, or resolves to, a private/reserved address - the same check
    make_ssrf_check() applies to the httpx path. validate_url_not_private()
    alone only guards the top-level navigation URL, not a redirect chain or
    same-page requests the rendered JS makes afterwards.
    """
    host = urlparse(request.url).hostname or ""
    if host and _host_resolves_to_private(host):
        logger.warning("Blocked request to private/reserved address: %s", request.url)
        route.abort()
    else:
        route.continue_()


class BrowserAdapter(SourceAdapter):
    type_id = "browser"

    def fetch(self, source_cfg: dict, ctx: FetchContext) -> list[RawDocument]:
        html = self.fetch_raw(source_cfg, ctx)
        return self.parse(html, source_cfg, ctx.regulator_id)

    def fetch_raw(self, source_cfg: dict, ctx: FetchContext) -> str:
        """The rendered DOM, as a string - unlike most adapters' fetch_raw,
        this genuinely is a "raw" response worth capturing/diffing as a
        fixture: a browser-type source has no other network artifact, and
        without this override scripts/capture_fixture.py and the nightly
        source-health check (both call adapter.fetch_raw(), never fetch())
        could not touch a browser source at all - every one would raise
        NotImplementedError from SourceAdapter's base default, forever,
        regardless of environment (issue surfaced adding the first real
        browser sources, ie-cbi/se-fi)."""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "browser adapter requires the playwright package (see collector/requirements.txt)"
            ) from e

        url = source_cfg["url"]
        browser_cfg = source_cfg.get("browser", {})
        wait_selector = browser_cfg.get("wait_selector")

        # SSRF guard applies to this adapter's navigation too (issue #8) -
        # a Playwright page.goto() is just as capable of reaching an
        # internal address as httpx is.
        validate_url_not_private(url)
        # Shares the FlareSolverr per-domain throttle (issue #8 ruling): a
        # real browser launch/navigation carries a similar per-request cost
        # against the target site as a FlareSolverr request does.
        throttle_flaresolverr(urlparse(url).hostname or url)

        with sync_playwright() as p:
            # ponytail: --no-sandbox - the collector container runs as a
            # non-root, non-setuid user (Dockerfile.collector), so
            # Chromium's own sandbox can't initialise; the container's own
            # process/network isolation is the sandbox boundary here.
            browser = p.chromium.launch(args=["--no-sandbox"])
            try:
                page = browser.new_page(user_agent=ctx.user_agent)
                # Every request this page makes (redirects, XHR/fetch, asset
                # loads) is SSRF-checked, not just the initial goto() URL.
                page.route("**/*", _ssrf_route_handler)
                page.goto(url, timeout=ctx.timeout * 1000)
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=ctx.timeout * 1000)
                return page.content()
            finally:
                browser.close()

    def parse(self, html: str, source_cfg: dict, regulator_id: str) -> list[RawDocument]:
        browser_cfg = source_cfg.get("browser", {})
        scrape_shaped = {**source_cfg, "scrape": browser_cfg}
        return ScrapeAdapter().parse(html, scrape_shaped, regulator_id)
