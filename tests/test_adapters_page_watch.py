"""Unit tests for collector.adapters.page_watch.PageWatchAdapter.parse() -
v1's _parse_page_watch, unchanged behaviour, now returning RawDocument
instead of Item.
"""
from collector.adapters.page_watch import PageWatchAdapter


def test_parse_extracts_content_and_hashes_it():
    html = '<html><body><nav>ignore this</nav><div class="content">Real page text here.</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content"}
    docs = PageWatchAdapter().parse(html, source, "esma")
    assert len(docs) == 1
    assert "Real page text here." in docs[0].content_text
    assert docs[0].date_inferred is True
    assert docs[0].content_hash


def test_parse_same_content_produces_same_hash():
    html = '<html><body><div class="content">Unchanged text.</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content"}
    docs1 = PageWatchAdapter().parse(html, source, "esma")
    docs2 = PageWatchAdapter().parse(html, source, "esma")
    assert docs1[0].content_hash == docs2[0].content_hash


def test_parse_changed_content_produces_different_hash():
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content"}
    html1 = '<html><body><div class="content">Original text.</div></body></html>'
    html2 = '<html><body><div class="content">Updated text.</div></body></html>'
    docs1 = PageWatchAdapter().parse(html1, source, "esma")
    docs2 = PageWatchAdapter().parse(html2, source, "esma")
    assert docs1[0].content_hash != docs2[0].content_hash


def test_parse_no_selector_match_returns_empty():
    html = '<html><body><div class="other">nothing relevant</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content"}
    assert PageWatchAdapter().parse(html, source, "esma") == []


def test_parse_stores_the_fetched_page_as_an_artefact():
    # Section 3.3: the original response is stored for every item that has
    # a body - page_watch fetches a page body, so it's not exempt (unlike
    # RSS, which never fetches a full body at all).
    html = '<html><body><nav>ignore</nav><div class="content">Real page text here.</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content"}
    docs = PageWatchAdapter().parse(html, source, "esma")
    assert docs[0].body == html.encode("utf-8")
    assert docs[0].mime == "text/html"
