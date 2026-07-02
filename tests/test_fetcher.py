from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import httpx
import pytest
from collector.fetcher import (
    fetch_source,
    _resolve_language,
    _truncate_snippet,
    _parse_scrape,
    _validate_url_not_private,
    _extract_pdf_text,
    _fetch_html,
    _fetch_via_flaresolverr,
    _looks_like_block_page,
    _fetch_bytes,
    _has_expected_content,
)


SAMPLE_CONFIG = {"user_agent": "test-agent/1.0", "timeout": 10, "retries": 1}


def test_truncate_snippet():
    text = "a" * 600
    assert len(_truncate_snippet(text)) == 500


def test_truncate_snippet_short():
    text = "short"
    assert _truncate_snippet(text) == "short"


def test_resolve_language_from_feed_item():
    item = {"language": "de"}
    result = _resolve_language(item, source_preferred="en", title="", snippet="")
    assert result == "de"


def test_resolve_language_from_source_preferred():
    item = {}
    result = _resolve_language(item, source_preferred="fr", title="", snippet="")
    assert result == "fr"


def test_resolve_language_from_detection():
    item = {}
    result = _resolve_language(
        item,
        source_preferred=None,
        title="The European Securities and Markets Authority",
        snippet="published new guidance on crypto assets today",
    )
    assert result == "en"


def test_fetch_source_rss_returns_items():
    rss_xml = """<?xml version="1.0"?>
    <rss version="2.0">
      <channel>
        <title>Test Feed</title>
        <item>
          <title>New MiCA regulation</title>
          <link>https://example.com/item1</link>
          <description>Full description here with enough content to detect language.</description>
          <pubDate>Wed, 01 Jul 2026 10:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>"""

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.iter_bytes.return_value = [rss_xml.encode()]

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value.__enter__ = MagicMock(return_value=mock_response)
        mock_client.stream.return_value.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        source = {
            "type": "rss",
            "url": "https://example.com/feed.rss",
            "label": "all_news",
            "preferred_language": "en",
            "tags": ["casp"],
        }
        items = fetch_source(source, "esma", flaresolverr_url=None, config=SAMPLE_CONFIG)

    assert len(items) == 1
    assert items[0].title == "New MiCA regulation"
    assert items[0].regulator_id == "esma"
    assert items[0].source_type == "rss"
    assert items[0].date_inferred is False


def test_fetch_source_raises_on_http_error():
    # A genuine fetch failure must propagate as an exception, not silently
    # return [] - run_collection() relies on this to distinguish "fetch
    # failed" (record a source error) from "fetch succeeded, feed just had
    # nothing new" (a normal, healthy outcome for RSS).
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.side_effect = Exception("connection refused")
        mock_client_cls.return_value = mock_client

        source = {
            "type": "rss",
            "url": "https://example.com/feed.rss",
            "label": "all_news",
            "preferred_language": "en",
            "tags": [],
        }
        with pytest.raises(RuntimeError):
            fetch_source(source, "esma", flaresolverr_url=None, config=SAMPLE_CONFIG)


# --- scrape path tests ---

SCRAPE_SOURCE = {
    "url": "https://example.com/news",
    "label": "test_feed",
    "type": "scrape",
    "scrape": {
        "item_selector": ".item",
        "title_selector": "h2",
        "link_selector": "a",
        "date_selector": ".date",
        "date_attribute": None,
        "content_selector": None,
    },
    "tags": ["test"],
}


def test_parse_scrape_empty_href_skipped():
    html = """
    <html><body>
      <div class="item"><h2>Valid</h2><a href="https://example.com/valid">link</a></div>
      <div class="item"><h2>Empty</h2><a href="">link</a></div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 1
    assert items[0].url == "https://example.com/valid"


def test_parse_scrape_relative_href_resolved():
    html = """
    <html><body>
      <div class="item"><h2>Relative</h2><a href="/news/item-1">link</a></div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 1
    assert items[0].url == "https://example.com/news/item-1"


def test_parse_scrape_missing_title_skipped():
    html = """
    <html><body>
      <div class="item"><a href="https://example.com/page">link</a></div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 0


def test_parse_scrape_date_parsed():
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2>
        <a href="https://example.com/page">link</a>
        <span class="date">2024-01-15</span>
      </div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 1
    assert items[0].published_at is not None
    assert items[0].date_inferred is False


def test_parse_scrape_date_dayfirst_not_misread_as_month_first():
    # "02/07/2026" is ambiguous: DD/MM/YYYY (2 July) vs MM/DD/YYYY (7 Feb).
    # EEA/UK sources use DD/MM/YYYY - dateutil defaults to month-first without
    # dayfirst=True, which would silently misparse this as 7 February.
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2>
        <a href="https://example.com/page">link</a>
        <span class="date">02/07/2026</span>
      </div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 1
    assert items[0].date_inferred is False
    assert items[0].published_at.month == 7
    assert items[0].published_at.day == 2


def test_parse_scrape_date_fuzzy_strips_surrounding_text_when_opted_in():
    # FCA's search results render "Published: 02/07/2026" with no clean
    # sub-element isolating just the date - date_fuzzy: true must tolerate
    # the "Published: " prefix rather than failing to parse entirely.
    # fuzzy is opt-in per source (not a global default - see the next test)
    # because it silently extracts a date-shaped substring from anywhere in
    # the text and never fails loudly.
    fuzzy_source = {**SCRAPE_SOURCE, "scrape": {**SCRAPE_SOURCE["scrape"], "date_fuzzy": True}}
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2>
        <a href="https://example.com/page">link</a>
        <span class="date">Published: 02/07/2026</span>
      </div>
    </body></html>
    """
    items = _parse_scrape(html, fuzzy_source, "test_reg")
    assert len(items) == 1
    assert items[0].date_inferred is False
    assert items[0].published_at.month == 7
    assert items[0].published_at.day == 2


def test_parse_scrape_date_fuzzy_defaults_off_and_prefix_fails_to_parse():
    # Without date_fuzzy: true (the default), a prefixed date string like
    # "Published: 02/07/2026" fails to parse and falls back to date_inferred
    # - it must NOT silently apply fuzzy matching for sources that never
    # opted in.
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2>
        <a href="https://example.com/page">link</a>
        <span class="date">Published: 02/07/2026</span>
      </div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 1
    assert items[0].date_inferred is True
    assert items[0].published_at is None


def test_parse_scrape_no_date_inferred():
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2>
        <a href="https://example.com/page">link</a>
      </div>
    </body></html>
    """
    items = _parse_scrape(html, SCRAPE_SOURCE, "test_reg")
    assert len(items) == 1
    assert items[0].date_inferred is True
    assert items[0].published_at is None


def test_extract_pdf_text_returns_none_on_invalid_pdf():
    assert _extract_pdf_text(b"this is not a pdf") is None


def test_extract_pdf_text_caps_page_count():
    # A huge/hostile PDF shouldn't be able to stall a collection run by
    # forcing extraction of thousands of pages synchronously in the loop.
    mock_pages = [MagicMock(extract_text=MagicMock(return_value=f"page {i} content")) for i in range(100)]
    mock_reader = MagicMock()
    mock_reader.pages = mock_pages
    with patch("pypdf.PdfReader", return_value=mock_reader):
        text = _extract_pdf_text(b"%PDF-fake-bytes")
    assert text is not None
    assert "page 49 content" in text  # within the 50-page cap
    assert "page 50 content" not in text  # beyond the cap - not extracted


def test_fetch_bytes_discards_response_over_max_bytes():
    mock_response = MagicMock()
    mock_response.iter_bytes.return_value = [b"x" * 1000]
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value.__enter__ = MagicMock(return_value=mock_response)
        mock_client.stream.return_value.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _fetch_bytes("https://example.com/huge.pdf", "ua", 10, 1, max_bytes=500)

        assert result is None


def test_fetch_bytes_allows_response_under_max_bytes():
    mock_response = MagicMock()
    mock_response.iter_bytes.return_value = [b"x" * 100]
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value.__enter__ = MagicMock(return_value=mock_response)
        mock_client.stream.return_value.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _fetch_bytes("https://example.com/small.pdf", "ua", 10, 1, max_bytes=500)

        assert result == b"x" * 100


def test_fetch_bytes_stops_reading_once_cap_exceeded_without_downloading_rest():
    # The whole point of streaming is to abort mid-download rather than pull
    # the entire body into memory first and discard it after the fact - a
    # chunk generator that keeps yielding past the cap must never be fully
    # consumed once the running total exceeds max_bytes.
    chunks_yielded = []

    def chunk_generator():
        for _ in range(1000):
            chunks_yielded.append(1)
            yield b"x" * 100

    mock_response = MagicMock()
    mock_response.iter_bytes.return_value = chunk_generator()
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value.__enter__ = MagicMock(return_value=mock_response)
        mock_client.stream.return_value.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = _fetch_bytes("https://example.com/huge.pdf", "ua", 10, 1, max_bytes=500)

        assert result is None
        assert len(chunks_yielded) <= 6  # cap/chunk_size + 1, not all 1000


PDF_SCRAPE_SOURCE = {
    "url": "https://example.com/warnings",
    "label": "test_feed",
    "type": "scrape",
    "scrape": {
        "item_selector": ".item",
        "title_selector": "h2",
        "link_selector": "a",
        "date_selector": None,
        "date_attribute": None,
        "content_selector": None,
        "extract_pdf_text": True,
    },
    "tags": ["test"],
}


def test_parse_scrape_extract_pdf_text_fetches_and_uses_link_as_document():
    # CNMV-style source: the item's own link *is* the document (a PDF), not a
    # separate HTML landing page - raw_content should come from extracting
    # that PDF's text, not from content_selector (which is unset/irrelevant).
    html = """
    <html><body>
      <div class="item"><h2>Warning notice</h2><a href="https://example.com/notice.pdf">link</a></div>
    </body></html>
    """
    with patch("collector.fetcher._fetch_bytes") as mock_fetch, \
         patch("collector.fetcher._extract_pdf_text") as mock_extract:
        mock_fetch.return_value = b"%PDF-fake-bytes"
        mock_extract.return_value = "Entity warned: SOME UNAUTHORISED FIRM LTD"

        items = _parse_scrape(html, PDF_SCRAPE_SOURCE, "test_reg", SAMPLE_CONFIG)

        mock_fetch.assert_called_once_with("https://example.com/notice.pdf", "test-agent/1.0", 10, 1, max_bytes=25 * 1024 * 1024)
        assert len(items) == 1
        assert items[0].raw_content == "Entity warned: SOME UNAUTHORISED FIRM LTD"


def test_parse_scrape_extract_pdf_text_falls_back_to_content_selector_on_failure():
    html = """
    <html><body>
      <div class="item">
        <h2>Warning notice</h2>
        <a href="https://example.com/notice.pdf">link</a>
        <p class="summary">Fallback summary text</p>
      </div>
    </body></html>
    """
    source = {
        **PDF_SCRAPE_SOURCE,
        "scrape": {**PDF_SCRAPE_SOURCE["scrape"], "content_selector": ".summary"},
    }
    with patch("collector.fetcher._fetch_bytes") as mock_fetch:
        mock_fetch.return_value = None  # simulate the PDF fetch failing

        items = _parse_scrape(html, source, "test_reg", SAMPLE_CONFIG)

        assert len(items) == 1
        assert items[0].raw_content == "Fallback summary text"


def test_looks_like_block_page_detects_cloudfront_style_block():
    html = (
        '<html><head><title>ERROR: The request could not be satisfied</title></head>'
        '<body><h1>403 ERROR</h1>Request blocked. We can\'t connect to the server.</body></html>'
    )
    assert _looks_like_block_page(html) is True


def test_looks_like_block_page_does_not_flag_real_content():
    # A real page can legitimately be short (e.g. one result) and must not
    # be misclassified as a block page just because of its size.
    html = '<html><body><div class="single-publication">Real Circular Title</div></body></html>'
    assert _looks_like_block_page(html) is False


def test_looks_like_block_page_ignores_large_pages_even_with_marker_text():
    # A long real page happening to contain a phrase like "rate limit" in an
    # article body shouldn't be misclassified - block pages are always small.
    html = "<html><body>" + ("Discussion of rate limit policy. " * 500) + "</body></html>"
    assert _looks_like_block_page(html) is False


def test_fetch_html_skips_plain_http_when_flaresolverr_available():
    # A Cloudflare-protected site blocks plain HTTP too - retrying it first
    # only wastes requests that count toward that site's own rate limiting,
    # for no chance of success. Go straight to FlareSolverr instead.
    with patch("collector.fetcher._fetch_via_flaresolverr") as mock_fs, \
         patch("httpx.Client") as mock_client_cls:
        mock_fs.return_value = "<html>fs content</html>"

        result = _fetch_html("https://example.com", "ua", 10, 3, flaresolverr_url="http://flaresolverr:8191")

        assert result == "<html>fs content</html>"
        mock_client_cls.assert_not_called()
        mock_fs.assert_called_once_with("https://example.com", "http://flaresolverr:8191", 10, None)


def test_fetch_html_uses_plain_http_when_no_flaresolverr():
    with patch("collector.fetcher._fetch_via_flaresolverr") as mock_fs, \
         patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.text = "<html>direct content</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = _fetch_html("https://example.com", "ua", 10, 3, flaresolverr_url=None)

        assert result == "<html>direct content</html>"
        mock_fs.assert_not_called()


def test_fetch_via_flaresolverr_backs_off_and_gives_up_on_ban_message():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "error",
            "message": "Error solving the challenge. Cloudflare has blocked this request. Probably your IP is banned for this site.",
        }
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result is None
        assert mock_post.call_count == 3  # exhausts _FLARESOLVERR_MAX_RETRIES, doesn't hammer indefinitely


def test_fetch_via_flaresolverr_returns_content_on_success():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok", "solution": {"response": "<html>real content</html>"}}
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result == "<html>real content</html>"
        assert mock_post.call_count == 1  # no retry needed on first success


def test_fetch_via_flaresolverr_treats_block_page_as_failure_not_success():
    # FlareSolverr can report status "ok" while the page it actually got back
    # is a CDN-layer block/rate-limit page, not the real site - a successful
    # HTTP round-trip isn't the same as successful content. This must be
    # retried/backed off like any other failure, not returned as if valid.
    block_page = (
        '<html><head><title>ERROR: The request could not be satisfied</title></head>'
        '<body><h1>403 ERROR</h1>Request blocked.</body></html>'
    )
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok", "solution": {"response": block_page}}
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result is None
        assert mock_post.call_count == 3  # retried like any other failure, didn't accept it as real content


def test_fetch_via_flaresolverr_rejects_non_2xx_upstream_status():
    # solution.status is the real upstream HTTP status FlareSolverr's browser
    # received - distinct from the top-level "status": "ok" (which only
    # means FlareSolverr itself didn't error). A CDN 403 shows up here
    # directly and is a far more reliable signal than content-sniffing.
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html>short but real-looking</html>", "status": 403},
        }
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result is None
        assert mock_post.call_count == 3


def test_fetch_via_flaresolverr_accepts_2xx_upstream_status():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html>real content</html>", "status": 200},
        }
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result == "<html>real content</html>"


def test_fetch_via_flaresolverr_missing_solution_status_falls_back_to_content_checks():
    # Not every FlareSolverr version/response necessarily exposes
    # solution.status - must degrade gracefully to the content-based checks
    # rather than erroring or silently rejecting everything.
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html><div class='single-publication'>Real item</div></html>"},
        }
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30, expected_selector=".single-publication")

        assert result is not None


def test_fetch_via_flaresolverr_rejects_200_page_missing_expected_selector():
    # A genuine HTTP 200 that isn't an obvious block page either (a
    # maintenance notice, an unrelated redirect target) must still be
    # rejected if it doesn't contain what was actually asked for - the
    # positive-fingerprint check, not just the block-page denylist.
    unrelated_page = "<html><body>" + ("Some unrelated maintenance notice content. " * 50) + "</body></html>"
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": unrelated_page, "status": 200},
        }
        mock_post.return_value = mock_response

        result = _fetch_via_flaresolverr(
            "https://example.com", "http://flaresolverr:8191", 30, expected_selector=".single-publication",
        )

        assert result is None
        assert mock_post.call_count == 3


def test_has_expected_content_true_when_no_selector_given():
    assert _has_expected_content("<html>anything</html>", None) is True


def test_has_expected_content_false_when_selector_absent():
    assert _has_expected_content("<html><body>no match here</body></html>", ".single-publication") is False


def test_has_expected_content_true_when_selector_present():
    html = "<html><body><div class='single-publication'>item</div></body></html>"
    assert _has_expected_content(html, ".single-publication") is True


def test_ssrf_private_ip_blocked():
    with pytest.raises(ValueError, match="private"):
        _validate_url_not_private("http://192.168.1.1/path")


def test_ssrf_loopback_ip_blocked():
    with pytest.raises(ValueError, match="private"):
        _validate_url_not_private("http://127.0.0.1/path")


def test_ssrf_link_local_metadata_ip_blocked():
    # 169.254.169.254 is the cloud provider metadata endpoint (AWS/GCP/Azure)
    # - a classic SSRF target if a scraped page can redirect a fetch there.
    with pytest.raises(ValueError, match="private"):
        _validate_url_not_private("http://169.254.169.254/latest/meta-data/")


def test_ssrf_hostname_resolving_to_private_ip_blocked():
    # A bare-IP-literal check alone misses DNS rebinding: a hostname that
    # *resolves* to an internal address must be blocked too, not just a URL
    # that already contains a raw private IP.
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("10.0.0.5", 0))]
        with pytest.raises(ValueError, match="private"):
            _validate_url_not_private("http://internal.example.com/path")


def test_ssrf_hostname_resolving_to_public_ip_allowed():
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 0))]
        _validate_url_not_private("http://example.com/path")  # must not raise


def test_ssrf_event_hook_blocks_hostname_resolving_to_private_ip():
    from collector.fetcher import _make_ssrf_check

    hook = _make_ssrf_check()
    fake_request = MagicMock()
    fake_request.url.host = "internal.example.com"
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("172.16.0.1", 0))]
        with pytest.raises(httpx.InvalidURL, match="private"):
            hook(fake_request)
