"""Integration tests for collector.fetcher.fetch_source(): registry lookup
+ adapter fetch + shared pipeline, end to end. Adapter-internal parsing and
HTTP-plumbing unit tests live under tests/test_adapters_*.py and
tests/test_pipeline.py (../wiki/content/concept-source-adapter-framework.md
moved this logic out of collector/fetcher.py).
"""
from unittest.mock import patch, MagicMock

import pytest

from collector.fetcher import fetch_source


SAMPLE_CONFIG = {"user_agent": "test-agent/1.0", "timeout": 10, "retries": 1}


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


def test_fetch_source_rss_routes_through_flaresolverr_when_cloudflare_protected():
    with patch("collector.adapters.http.fetch_via_flaresolverr") as mock_fs:
        mock_fs.return_value = """<?xml version="1.0"?>
        <rss version="2.0"><channel><title>Test</title>
        <item><title>Item</title><link>https://example.com/1</link>
        <pubDate>Wed, 01 Jul 2026 10:00:00 +0000</pubDate></item>
        </channel></rss>"""
        source = {
            "type": "rss",
            "url": "https://example.com/feed.rss",
            "label": "all_news",
            "preferred_language": "en",
            "tags": ["casp"],
        }
        items = fetch_source(source, "lt-lb", flaresolverr_url="http://flaresolverr:8191", config=SAMPLE_CONFIG)
        assert len(items) == 1
        mock_fs.assert_called_once()


def test_fetch_source_page_watch_raises_when_expected_content_missing():
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.text = '<html><body><div class="wrong-page">Not what we expected.</div></body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        source = {
            "type": "page_watch",
            "url": "https://example.com/page",
            "label": "test_page",
            "watch_selector": ".content",
        }
        with pytest.raises(RuntimeError):
            fetch_source(source, "esma", flaresolverr_url=None, config=SAMPLE_CONFIG)


def test_fetch_source_page_watch_returns_item_when_content_present():
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.text = '<html><body><div class="content">Page text.</div></body></html>'
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        source = {
            "type": "page_watch",
            "url": "https://example.com/page",
            "label": "test_page",
            "watch_selector": ".content",
        }
        items = fetch_source(source, "esma", flaresolverr_url=None, config=SAMPLE_CONFIG)
        assert len(items) == 1
        assert "Page text." in items[0].raw_content


def test_fetch_source_unknown_type_returns_empty_list():
    source = {"type": "carrier-pigeon", "url": "https://example.com/x", "label": "nope"}
    assert fetch_source(source, "esma", flaresolverr_url=None, config=SAMPLE_CONFIG) == []
