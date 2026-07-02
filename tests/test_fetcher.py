from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
from collector.fetcher import fetch_source, _resolve_language, _truncate_snippet


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
    mock_response.text = rss_xml
    mock_response.content = rss_xml.encode()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
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


def test_fetch_source_returns_empty_on_http_error():
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("connection refused")
        mock_client_cls.return_value = mock_client

        source = {
            "type": "rss",
            "url": "https://example.com/feed.rss",
            "label": "all_news",
            "preferred_language": "en",
            "tags": [],
        }
        items = fetch_source(source, "esma", flaresolverr_url=None, config=SAMPLE_CONFIG)

    assert items == []
