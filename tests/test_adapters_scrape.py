"""Unit tests for collector.adapters.scrape.ScrapeAdapter.parse() - v1's
_parse_scrape, unchanged behaviour, now returning RawDocument instead of
Item.
"""
from unittest.mock import patch

from collector.adapters.scrape import ScrapeAdapter

SAMPLE_CONFIG = {"user_agent": "test-agent/1.0", "timeout": 10, "retries": 1}

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


def test_parse_empty_href_skipped():
    html = """
    <html><body>
      <div class="item"><h2>Valid</h2><a href="https://example.com/valid">link</a></div>
      <div class="item"><h2>Empty</h2><a href="">link</a></div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert len(docs) == 1
    assert docs[0].url == "https://example.com/valid"


def test_parse_relative_href_resolved():
    html = """
    <html><body>
      <div class="item"><h2>Relative</h2><a href="/news/item-1">link</a></div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert docs[0].url == "https://example.com/news/item-1"


def test_parse_missing_title_skipped():
    html = """
    <html><body>
      <div class="item"><a href="https://example.com/page">link</a></div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert docs == []


def test_parse_date_parsed():
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2><a href="https://example.com/page">link</a>
        <span class="date">2024-01-15</span>
      </div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert docs[0].published_at is not None
    assert docs[0].date_inferred is False


def test_parse_date_dayfirst_not_misread_as_month_first():
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2><a href="https://example.com/page">link</a>
        <span class="date">02/07/2026</span>
      </div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert docs[0].date_inferred is False
    assert docs[0].published_at.month == 7
    assert docs[0].published_at.day == 2


def test_parse_date_fuzzy_strips_surrounding_text_when_opted_in():
    fuzzy_source = {**SCRAPE_SOURCE, "scrape": {**SCRAPE_SOURCE["scrape"], "date_fuzzy": True}}
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2><a href="https://example.com/page">link</a>
        <span class="date">Published: 02/07/2026</span>
      </div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, fuzzy_source, "test_reg")
    assert docs[0].date_inferred is False
    assert docs[0].published_at.month == 7
    assert docs[0].published_at.day == 2


def test_parse_date_fuzzy_defaults_off_and_prefix_fails_to_parse():
    html = """
    <html><body>
      <div class="item">
        <h2>Title</h2><a href="https://example.com/page">link</a>
        <span class="date">Published: 02/07/2026</span>
      </div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert docs[0].date_inferred is True
    assert docs[0].published_at is None


def test_parse_no_date_selector_match_inferred():
    html = """
    <html><body>
      <div class="item"><h2>Title</h2><a href="https://example.com/page">link</a></div>
    </body></html>
    """
    docs = ScrapeAdapter().parse(html, SCRAPE_SOURCE, "test_reg")
    assert docs[0].date_inferred is True
    assert docs[0].published_at is None


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


def test_parse_extract_pdf_text_fetches_and_sets_pdf_text_and_artefact():
    html = """
    <html><body>
      <div class="item"><h2>Warning notice</h2><a href="https://example.com/notice.pdf">link</a></div>
    </body></html>
    """
    with patch("collector.adapters.scrape.fetch_bytes") as mock_fetch, \
         patch("collector.adapters.scrape.extract_pdf_text") as mock_extract:
        mock_fetch.return_value = b"%PDF-fake-bytes"
        mock_extract.return_value = "Entity warned: SOME UNAUTHORISED FIRM LTD"

        docs = ScrapeAdapter().parse(html, PDF_SCRAPE_SOURCE, "test_reg", SAMPLE_CONFIG)

        mock_fetch.assert_called_once_with("https://example.com/notice.pdf", "test-agent/1.0", 10, 1, max_bytes=25 * 1024 * 1024)
        assert docs[0].pdf_text == "Entity warned: SOME UNAUTHORISED FIRM LTD"
        assert docs[0].body == b"%PDF-fake-bytes"
        assert docs[0].mime == "application/pdf"


def test_parse_extract_pdf_text_falls_back_to_content_selector_on_failure():
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
    with patch("collector.adapters.scrape.fetch_bytes") as mock_fetch:
        mock_fetch.return_value = None  # simulate the PDF fetch failing

        docs = ScrapeAdapter().parse(html, source, "test_reg", SAMPLE_CONFIG)

        assert docs[0].pdf_text is None
        assert "Fallback summary text" in docs[0].content_html


def test_parse_content_selector_sets_content_html_and_artefact():
    html = """
    <html><body>
      <div class="item">
        <h2>Notice</h2><a href="https://example.com/n">link</a>
        <div class="summary"><p>Hello <b>world</b></p></div>
      </div>
    </body></html>
    """
    source = {
        "url": "https://example.com/warnings", "label": "test_feed", "type": "scrape",
        "scrape": {
            "item_selector": ".item", "title_selector": "h2", "link_selector": "a",
            "date_selector": None, "date_attribute": None,
            "content_selector": ".summary", "extract_pdf_text": False,
        },
        "tags": [],
    }
    docs = ScrapeAdapter().parse(html, source, "test_reg")
    assert "Hello" in docs[0].content_html
    assert docs[0].mime == "text/html"
    assert b"Hello" in docs[0].body


def test_parse_amla_consultations_extracts_title_link_and_opening_date():
    # Fixture mirrors the live page's real DOM (verified 2026-07-03): each
    # item has two <time datetime="..."> elements (Opening date, Deadline) -
    # date_selector must resolve to the FIRST one, not both.
    html = '''
    <article class="ecl-content-item">
      <div class="ecl-content-block__title">
        <a class="ecl-link" href="/policy/public-consultations/test-consultation_en">Test AMLA consultation</a>
      </div>
      <dl class="ecl-description-list">
        <dt>Opening date</dt>
        <dd class="ecl-description-list__definition"><time datetime="2026-07-02T12:00:00Z">2 July 2026</time></dd>
        <dt>Deadline</dt>
        <dd class="ecl-description-list__definition"><time datetime="2026-09-20T21:59:59Z">20 September 2026</time></dd>
      </dl>
    </article>
    '''
    source = {
        "type": "scrape",
        "url": "https://www.amla.europa.eu/policy/public-consultations_en",
        "label": "public_consultations",
        "preferred_language": "en",
        "tags": ["casp", "aml"],
        "scrape": {
            "item_selector": "article.ecl-content-item",
            "title_selector": ".ecl-content-block__title a",
            "link_selector": ".ecl-content-block__title a",
            "date_selector": ".ecl-description-list__definition time",
            "date_attribute": "datetime",
            "content_selector": None,
        },
    }
    docs = ScrapeAdapter().parse(html, source, "amla", SAMPLE_CONFIG)
    assert len(docs) == 1
    assert "Test AMLA consultation" in docs[0].title
    assert docs[0].published_at is not None
    assert docs[0].published_at.day == 2
    assert docs[0].published_at.month == 7
    assert docs[0].date_inferred is False


def test_parse_esma_news_extracts_title_link_and_date():
    # .search-title/.search-date sit *inside* the real title <a>
    # (rel="bookmark"), and a second, empty <a> lives elsewhere in the card
    # - link_selector must key off rel="bookmark", not a bare "a".
    html = '''
    <article class="node node--type-news">
      <a href="/press-news/esma-news/test-headline" rel="bookmark">
        <div class="search-title"><span class="field--name-title">Test ESMA headline</span></div>
        <div class="search-date">03/07/2026</div>
      </a>
      <a href="" aria-label="See Responses"></a>
    </article>
    '''
    source = {
        "type": "scrape",
        "url": "https://www.esma.europa.eu/press-news/esma-news",
        "label": "all_news",
        "preferred_language": "en",
        "tags": ["casp"],
        "scrape": {
            "item_selector": "article.node--type-news",
            "title_selector": ".search-title",
            "link_selector": 'a[rel="bookmark"]',
            "date_selector": ".search-date",
            "date_attribute": None,
            "content_selector": None,
        },
    }
    docs = ScrapeAdapter().parse(html, source, "esma", SAMPLE_CONFIG)
    assert len(docs) == 1
    assert "Test ESMA headline" in docs[0].title
    assert docs[0].published_at is not None
    assert docs[0].date_inferred is False
