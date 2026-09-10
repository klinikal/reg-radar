"""Unit tests for collector.pipeline - the shared post-fetch step (id
generation, tag filtering, language resolution, snippet truncation, body
extraction) that used to be duplicated across collector/fetcher.py's three
parsers (../wiki/content/concept-source-adapter-framework.md).
"""
from collector.adapters.base import RawDocument
from collector.adapters.page_watch import PageWatchAdapter
from collector.adapters.rss import RssAdapter
from collector.adapters.scrape import ScrapeAdapter
from collector.pipeline import (
    BLANKET_LABEL_DENYLIST,
    filter_source_tags,
    process_raw_documents,
    resolve_language,
    truncate_snippet,
)


def test_truncate_snippet():
    assert len(truncate_snippet("a" * 600)) == 500


def test_truncate_snippet_short():
    assert truncate_snippet("short") == "short"


def test_resolve_language_override_wins():
    assert resolve_language("de", "en", "", "") == "de"


def test_resolve_language_from_source_preferred():
    assert resolve_language(None, "fr", "", "") == "fr"


def test_resolve_language_from_detection():
    result = resolve_language(
        None, None,
        "The European Securities and Markets Authority",
        "published new guidance on crypto assets today",
    )
    assert result == "en"


def test_filter_source_tags_strips_blanket_regulation_labels():
    assert filter_source_tags(["casp", "mica", "warnings"]) == ["warnings"]


def test_filter_source_tags_keeps_notice_type_tags():
    assert filter_source_tags(["press_releases", "sanctions", "enforcement"]) == ["press_releases", "sanctions", "enforcement"]


def test_filter_source_tags_handles_empty_list():
    assert filter_source_tags([]) == []


def test_blanket_label_denylist_contains_expected_entries():
    assert BLANKET_LABEL_DENYLIST == frozenset({"casp", "mica", "aml", "art", "emt", "dora"})


# --- end-to-end: adapter.parse() -> process_raw_documents() ---

def test_rss_sets_source_tags_filtered_and_content_tags_empty():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Test item</title><link>https://example.com/a</link>
      <pubDate>Fri, 03 Jul 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    source = {"label": "all_news", "tags": ["casp", "mica", "news"], "type": "rss"}
    raw_docs = RssAdapter().parse(rss, source, "esma")
    items = process_raw_documents(raw_docs, source, "esma")
    assert items[0].source_tags == ["news"]
    assert items[0].content_tags == []


def test_rss_body_md_has_not_collected_note():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Test item</title><link>https://example.com/a</link>
      <description>&lt;p&gt;Short summary&lt;/p&gt;</description>
      <pubDate>Fri, 03 Jul 2026 10:00:00 GMT</pubDate></item>
    </channel></rss>"""
    source = {"label": "all_news", "tags": [], "type": "rss"}
    raw_docs = RssAdapter().parse(rss, source, "esma")
    items = process_raw_documents(raw_docs, source, "esma")
    assert "Short summary" in items[0].body_md
    assert items[0].body_md.endswith("> Full text not collected. See https://example.com/a.")
    assert items[0].extractor.startswith("markdownify/")
    assert items[0].raw_bytes is None


def test_scrape_pdf_sets_body_md_extractor_and_raw_bytes():
    from unittest.mock import patch

    html = """
    <html><body>
      <div class="item"><h2>Warning notice</h2><a href="https://example.com/notice.pdf">link</a></div>
    </body></html>
    """
    source = {
        "url": "https://example.com/warnings", "label": "test_feed", "type": "scrape",
        "scrape": {
            "item_selector": ".item", "title_selector": "h2", "link_selector": "a",
            "date_selector": None, "date_attribute": None, "content_selector": None,
            "extract_pdf_text": True,
        },
        "tags": [],
    }
    with patch("collector.adapters.scrape.fetch_bytes") as mock_fetch, \
         patch("collector.adapters.scrape.extract_pdf_text") as mock_extract:
        mock_fetch.return_value = b"%PDF-fake-bytes"
        mock_extract.return_value = "Entity warned: SOME UNAUTHORISED FIRM LTD"

        raw_docs = ScrapeAdapter().parse(html, source, "test_reg", {})
        items = process_raw_documents(raw_docs, source, "test_reg")

    assert items[0].body_md == "Entity warned: SOME UNAUTHORISED FIRM LTD"
    assert items[0].extractor.startswith("pypdf/")
    assert items[0].raw_bytes == b"%PDF-fake-bytes"
    assert items[0].mime == "application/pdf"


def test_scrape_content_selector_sets_body_md_extractor_and_raw_bytes():
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
    raw_docs = ScrapeAdapter().parse(html, source, "test_reg")
    items = process_raw_documents(raw_docs, source, "test_reg")
    assert items[0].body_md == "Hello **world**"
    assert items[0].extractor.startswith("markdownify/")
    assert items[0].mime == "text/html"
    assert b"Hello" in items[0].raw_bytes


def test_page_watch_body_md_is_flattened_text():
    html = '<html><body><div class="content">Real page text here.</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content", "type": "page_watch"}
    raw_docs = PageWatchAdapter().parse(html, source, "esma")
    items = process_raw_documents(raw_docs, source, "esma")
    assert items[0].body_md == "Real page text here."
    assert items[0].extractor == "raw/1"


def test_page_watch_stores_the_whole_page_as_the_artefact():
    # Section 3.3: the artefact is the whole fetched page (raw_bytes/mime),
    # distinct from body_md which comes from just the watch_selector
    # fragment - proves that split survives end to end through the pipeline.
    html = '<html><body><nav>ignore</nav><div class="content">Real page text here.</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content", "type": "page_watch"}
    raw_docs = PageWatchAdapter().parse(html, source, "esma")
    items = process_raw_documents(raw_docs, source, "esma")
    assert items[0].raw_bytes == html.encode("utf-8")
    assert items[0].mime == "text/html"


def test_page_watch_item_id_stable_across_runs_with_same_content():
    html = '<html><body><div class="content">Unchanged text.</div></body></html>'
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content", "type": "page_watch"}
    items1 = process_raw_documents(PageWatchAdapter().parse(html, source, "esma"), source, "esma")
    items2 = process_raw_documents(PageWatchAdapter().parse(html, source, "esma"), source, "esma")
    assert items1[0].id == items2[0].id


def test_page_watch_changed_content_produces_different_item_id():
    source = {"label": "test_page", "url": "https://example.com/page", "watch_selector": ".content", "type": "page_watch"}
    html1 = '<html><body><div class="content">Original text.</div></body></html>'
    html2 = '<html><body><div class="content">Updated text.</div></body></html>'
    items1 = process_raw_documents(PageWatchAdapter().parse(html1, source, "esma"), source, "esma")
    items2 = process_raw_documents(PageWatchAdapter().parse(html2, source, "esma"), source, "esma")
    assert items1[0].id != items2[0].id


def test_generic_raw_document_with_no_content_falls_back_to_snippet_note():
    raw = RawDocument(url="https://example.com/x", title="X", published_at=None, date_inferred=True)
    source = {"label": "s", "type": "sitemap", "tags": []}
    items = process_raw_documents([raw], source, "reg")
    assert items[0].body_md == "> Full text not collected. See https://example.com/x."
    assert items[0].raw_bytes is None
    assert items[0].mime is None


# --- classify() / rules_engine wiring (../wiki/content/component-rules.md:
# the pipeline's classify() hook is the
# only place rule scoring runs; collector.main writes the result) ---

def test_classify_is_a_noop_without_a_rules_engine():
    from collector.pipeline import classify
    from collector.schema import Item
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    item = Item(
        id="x", regulator_id="esma", source_label="s", title="MiCA update", url="https://x",
        published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="", source_type="rss",
    )
    raw = RawDocument(url="https://x", title="MiCA update", published_at=now)
    classify(item, raw)  # rules_engine=None default
    assert item.rule_labels == {}


def test_classify_scores_item_when_rules_engine_given(tmp_path):
    from collector.pipeline import classify
    from collector.schema import Item
    from regradar.rules import RulesEngine
    from regradar.taxonomy import build_taxonomy
    from datetime import datetime, timezone

    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules: []\n")  # alias-derived rules alone are enough for "MiCA"
    rules_engine = RulesEngine.load(str(rules_path), taxonomy)

    now = datetime.now(timezone.utc)
    item = Item(
        id="x", regulator_id="esma", source_label="s", title="New MiCA guidance", url="https://x",
        published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="", source_type="rss", raw_content="",
    )
    raw = RawDocument(url="https://x", title="New MiCA guidance", published_at=now)
    classify(item, raw, rules_engine=rules_engine)
    assert item.rule_labels == {("instrument", "mica"): 0.7}


def test_process_raw_documents_threads_rules_engine_into_classify(tmp_path):
    from regradar.rules import RulesEngine
    from regradar.taxonomy import build_taxonomy

    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text("rules: []\n")
    rules_engine = RulesEngine.load(str(rules_path), taxonomy)

    source = {"type": "rss", "label": "all_news"}
    raw_docs = [RawDocument(url="https://x/a", title="MiCA obligations", published_at=None, content_html="<p>MiCA text</p>")]
    items = process_raw_documents(raw_docs, source, "esma", rules_engine=rules_engine)
    # "MiCA" appears in both title and body_md, so the alias rule (fields:
    # [title, body] by default) scores both and caps at 1.0.
    assert items[0].rule_labels.get(("instrument", "mica")) == 1.0


def test_process_raw_documents_without_rules_engine_leaves_rule_labels_empty():
    source = {"type": "rss", "label": "all_news"}
    raw_docs = [RawDocument(url="https://x/a", title="MiCA obligations", published_at=None, content_html="<p>MiCA text</p>")]
    items = process_raw_documents(raw_docs, source, "esma")
    assert items[0].rule_labels == {}
