"""Unit tests for collector.adapters.http - shared HTTP plumbing (SSRF
guards, FlareSolverr throttle/backoff/block-page detection, bounded PDF
fetch+extract). Moved out of collector/fetcher.py unchanged
(docs/issues/08-source-adapter-framework-registry-split.md).
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from collector.adapters.http import (
    BlockedFetchError,
    extract_pdf_text,
    fetch_bytes,
    fetch_html,
    fetch_via_flaresolverr,
    has_expected_content,
    looks_like_block_page,
    make_ssrf_check,
    validate_url_not_private,
)


def test_extract_pdf_text_returns_none_on_invalid_pdf():
    assert extract_pdf_text(b"this is not a pdf") is None


def test_extract_pdf_text_caps_page_count():
    # A huge/hostile PDF shouldn't be able to stall a collection run by
    # forcing extraction of thousands of pages synchronously in the loop.
    mock_pages = [MagicMock(extract_text=MagicMock(return_value=f"page {i} content")) for i in range(100)]
    mock_reader = MagicMock()
    mock_reader.pages = mock_pages
    with patch("pypdf.PdfReader", return_value=mock_reader):
        text = extract_pdf_text(b"%PDF-fake-bytes")
    assert text is not None
    assert "page 49 content" in text  # within the 50-page cap
    assert "page 50 content" not in text  # beyond the cap - not extracted


def test_fetch_bytes_routes_through_flaresolverr_when_url_given():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs:
        mock_fs.return_value = "<rss>feed content</rss>"
        result = fetch_bytes("https://example.com/feed.rss", "ua", 10, 1, flaresolverr_url="http://flaresolverr:8191")
        assert result == b"<rss>feed content</rss>"
        mock_fs.assert_called_once_with("https://example.com/feed.rss", "http://flaresolverr:8191", 10)


def test_fetch_bytes_flaresolverr_failure_returns_none():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs:
        mock_fs.return_value = None
        result = fetch_bytes("https://example.com/feed.rss", "ua", 10, 1, flaresolverr_url="http://flaresolverr:8191")
        assert result is None


def test_fetch_bytes_flaresolverr_response_respects_max_bytes():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs:
        mock_fs.return_value = "x" * 1000
        result = fetch_bytes("https://example.com/feed.rss", "ua", 10, 1, max_bytes=500, flaresolverr_url="http://flaresolverr:8191")
        assert result is None


def test_fetch_bytes_no_flaresolverr_url_uses_plain_http():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs, patch("httpx.Client") as mock_client_cls:
        mock_response = MagicMock()
        mock_response.iter_bytes.return_value = [b"plain content"]
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value.__enter__ = MagicMock(return_value=mock_response)
        mock_client.stream.return_value.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = fetch_bytes("https://example.com/doc.pdf", "ua", 10, 1)

        assert result == b"plain content"
        mock_fs.assert_not_called()


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

        result = fetch_bytes("https://example.com/huge.pdf", "ua", 10, 1, max_bytes=500)

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

        result = fetch_bytes("https://example.com/small.pdf", "ua", 10, 1, max_bytes=500)

        assert result == b"x" * 100


def test_fetch_bytes_stops_reading_once_cap_exceeded_without_downloading_rest():
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

        result = fetch_bytes("https://example.com/huge.pdf", "ua", 10, 1, max_bytes=500)

        assert result is None
        assert len(chunks_yielded) <= 6  # cap/chunk_size + 1, not all 1000


def test_looks_like_block_page_detects_cloudfront_style_block():
    html = (
        '<html><head><title>ERROR: The request could not be satisfied</title></head>'
        '<body><h1>403 ERROR</h1>Request blocked. We can\'t connect to the server.</body></html>'
    )
    assert looks_like_block_page(html) is True


def test_looks_like_block_page_does_not_flag_real_content():
    html = '<html><body><div class="single-publication">Real Circular Title</div></body></html>'
    assert looks_like_block_page(html) is False


def test_looks_like_block_page_ignores_large_pages_even_with_marker_text():
    html = "<html><body>" + ("Discussion of rate limit policy. " * 500) + "</body></html>"
    assert looks_like_block_page(html) is False


def test_fetch_html_skips_plain_http_when_flaresolverr_available():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs, \
         patch("httpx.Client") as mock_client_cls:
        mock_fs.return_value = "<html>fs content</html>"

        result = fetch_html("https://example.com", "ua", 10, 3, flaresolverr_url="http://flaresolverr:8191")

        assert result == "<html>fs content</html>"
        mock_client_cls.assert_not_called()
        mock_fs.assert_called_once_with("https://example.com", "http://flaresolverr:8191", 10, None)


def test_fetch_html_uses_plain_http_when_no_flaresolverr():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs, \
         patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.text = "<html>direct content</html>"
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = fetch_html("https://example.com", "ua", 10, 3, flaresolverr_url=None)

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

        # Since #39 this raises rather than returning None: a ban is a
        # block, and blocked is its own source state, not "failed to fetch".
        with pytest.raises(BlockedFetchError):
            fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert mock_post.call_count == 3  # exhausts _FLARESOLVERR_MAX_RETRIES, doesn't hammer indefinitely


def test_fetch_via_flaresolverr_returns_content_on_success():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok", "solution": {"response": "<html>real content</html>"}}
        mock_post.return_value = mock_response

        result = fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result == "<html>real content</html>"
        assert mock_post.call_count == 1  # no retry needed on first success


def test_fetch_via_flaresolverr_treats_block_page_as_failure_not_success():
    block_page = (
        '<html><head><title>ERROR: The request could not be satisfied</title></head>'
        '<body><h1>403 ERROR</h1>Request blocked.</body></html>'
    )
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok", "solution": {"response": block_page}}
        mock_post.return_value = mock_response

        # #39: an unsolved challenge surfaces as blocked, not as a generic
        # fetch failure - the FATF case, where FlareSolverr quietly stops
        # working and the source would otherwise just look quiet.
        with pytest.raises(BlockedFetchError):
            fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert mock_post.call_count == 3


def test_fetch_via_flaresolverr_rejects_non_2xx_upstream_status():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html>short but real-looking</html>", "status": 403},
        }
        mock_post.return_value = mock_response

        with pytest.raises(BlockedFetchError):  # #39: blocked, not merely unfetchable
            fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert mock_post.call_count == 3


def test_fetch_via_flaresolverr_accepts_2xx_upstream_status():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html>real content</html>", "status": 200},
        }
        mock_post.return_value = mock_response

        result = fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30)

        assert result == "<html>real content</html>"


def test_fetch_via_flaresolverr_missing_solution_status_falls_back_to_content_checks():
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": "<html><div class='single-publication'>Real item</div></html>"},
        }
        mock_post.return_value = mock_response

        result = fetch_via_flaresolverr("https://example.com", "http://flaresolverr:8191", 30, expected_selector=".single-publication")

        assert result is not None


def test_fetch_via_flaresolverr_rejects_200_page_missing_expected_selector():
    unrelated_page = "<html><body>" + ("Some unrelated maintenance notice content. " * 50) + "</body></html>"
    with patch("httpx.post") as mock_post, patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "solution": {"response": unrelated_page, "status": 200},
        }
        mock_post.return_value = mock_response

        result = fetch_via_flaresolverr(
            "https://example.com", "http://flaresolverr:8191", 30, expected_selector=".single-publication",
        )

        assert result is None
        assert mock_post.call_count == 3


def test_has_expected_content_true_when_no_selector_given():
    assert has_expected_content("<html>anything</html>", None) is True


def test_has_expected_content_false_when_selector_absent():
    assert has_expected_content("<html><body>no match here</body></html>", ".single-publication") is False


def test_has_expected_content_true_when_selector_present():
    html = "<html><body><div class='single-publication'>item</div></body></html>"
    assert has_expected_content(html, ".single-publication") is True


def test_ssrf_private_ip_blocked():
    with pytest.raises(ValueError, match="private"):
        validate_url_not_private("http://192.168.1.1/path")


def test_ssrf_loopback_ip_blocked():
    with pytest.raises(ValueError, match="private"):
        validate_url_not_private("http://127.0.0.1/path")


def test_ssrf_link_local_metadata_ip_blocked():
    # 169.254.169.254 is the cloud provider metadata endpoint (AWS/GCP/Azure)
    # - a classic SSRF target if a scraped page can redirect a fetch there.
    with pytest.raises(ValueError, match="private"):
        validate_url_not_private("http://169.254.169.254/latest/meta-data/")


def test_ssrf_hostname_resolving_to_private_ip_blocked():
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("10.0.0.5", 0))]
        with pytest.raises(ValueError, match="private"):
            validate_url_not_private("http://internal.example.com/path")


def test_ssrf_hostname_resolving_to_public_ip_allowed():
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("93.184.216.34", 0))]
        validate_url_not_private("http://example.com/path")  # must not raise


def test_ssrf_event_hook_blocks_hostname_resolving_to_private_ip():
    hook = make_ssrf_check()
    fake_request = MagicMock()
    fake_request.url.host = "internal.example.com"
    with patch("socket.getaddrinfo") as mock_resolve:
        mock_resolve.return_value = [(2, 1, 6, "", ("172.16.0.1", 0))]
        with pytest.raises(httpx.InvalidURL, match="private"):
            hook(fake_request)


def test_sec_gov_requests_are_spaced_at_least_one_second_apart(monkeypatch):
    """Observed sec.gov behaviour blocks bursts, so consecutive requests to
    any sec.gov host must be spaced by the configured minimum."""
    import collector.adapters.http as http_mod

    clock = [1000.0]
    slept = []
    monkeypatch.setattr(http_mod.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
    monkeypatch.setattr(http_mod, "_last_sec_request_at", float("-inf"))

    http_mod.throttle_sec_gov("https://www.sec.gov/news/pressreleases")
    assert slept == []            # first request is not delayed
    http_mod.throttle_sec_gov("https://www.sec.gov/cgi-bin/browse-edgar")
    assert slept == [1.0]         # immediate second request waits the full interval

    clock[0] += 5.0
    http_mod.throttle_sec_gov("https://sec.gov/robots.txt")
    assert slept == [1.0]         # apex domain also throttled, but enough time had passed


def test_throttle_is_a_noop_for_non_sec_hosts(monkeypatch):
    import collector.adapters.http as http_mod

    slept = []
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(http_mod, "_last_sec_request_at", float("-inf"))

    for url in ("https://www.fca.org.uk/news", "https://notsec.gov.example.com/x", "https://www.mas.gov.sg/api"):
        http_mod.throttle_sec_gov(url)
        http_mod.throttle_sec_gov(url)
    assert slept == []
