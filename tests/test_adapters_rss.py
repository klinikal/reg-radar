"""Unit tests for collector.adapters.rss.RssAdapter.parse() - v1's
_parse_rss, unchanged behaviour, now returning RawDocument instead of Item.
"""
from collector.adapters.rss import RssAdapter


def test_parse_extracts_title_url_and_date():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>New MiCA regulation</title><link>https://example.com/item1</link>
      <description>Full description here.</description>
      <pubDate>Wed, 01 Jul 2026 10:00:00 +0000</pubDate></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news"}, "esma")
    assert len(docs) == 1
    assert docs[0].title == "New MiCA regulation"
    assert docs[0].url == "https://example.com/item1"
    assert docs[0].date_inferred is False
    assert docs[0].published_at is not None


def test_parse_skips_entries_missing_title_or_link():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>No link</title></item>
      <item><link>https://example.com/no-title</link></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news"}, "esma")
    assert docs == []


def test_parse_no_pubdate_marks_date_inferred():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Item</title><link>https://example.com/a</link></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news"}, "esma")
    assert docs[0].date_inferred is True
    assert docs[0].published_at is None


def test_parse_sets_is_partial_and_carries_summary_as_content_html():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Item</title><link>https://example.com/a</link>
      <description>&lt;p&gt;Short summary&lt;/p&gt;</description></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news"}, "esma")
    assert docs[0].is_partial is True
    assert docs[0].content_html == "<p>Short summary</p>"


def test_parse_no_artefact_body():
    # RSS never fetches a full document - no artefact to store (matches v1's
    # Item.raw_bytes always None for rss).
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Item</title><link>https://example.com/a</link></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news"}, "esma")
    assert docs[0].body == b""
    assert docs[0].mime == ""


def test_parse_resolves_relative_link_against_feed_url():
    # nl-dnb (#113): the feed itself emits a relative <link>.
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Item</title><link>/en/general-news/enforcement-measures-2026/item1</link></item>
    </channel></rss>"""
    docs = RssAdapter().parse(
        rss, {"label": "enforcement_measures", "url": "https://www.dnb.nl/en/rss/enforcement.xml"}, "nl-dnb",
    )
    assert docs[0].url == "https://www.dnb.nl/en/general-news/enforcement-measures-2026/item1"


def test_parse_keeps_id_url_as_originally_emitted_link():
    # id_url must stay the relative form so make_item_id keeps hashing what
    # the feed emits - resolving before hashing would change the id of an
    # item already stored, and a re-listed item would duplicate (#113).
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>Item</title><link>/en/news/item1</link></item>
    </channel></rss>"""
    docs = RssAdapter().parse(
        rss, {"label": "news", "url": "https://www.dnb.nl/en/rss.xml"}, "nl-dnb",
    )
    assert docs[0].id_url == "/en/news/item1"
    assert docs[0].url == "https://www.dnb.nl/en/news/item1"


def test_parse_absolute_link_leaves_id_url_matching_url():
    # Every other regulator's feed already emits absolute links - urljoin
    # must be a no-op for them, and id_url must equal url so their item ids
    # are unaffected.
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <item><title>New MiCA regulation</title><link>https://example.com/item1</link></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news", "url": "https://example.com/feed.xml"}, "esma")
    assert docs[0].url == "https://example.com/item1"
    assert docs[0].id_url == "https://example.com/item1"


def test_parse_entry_language_tag_overrides():
    rss = """<?xml version="1.0"?>
    <rss xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0"><channel>
      <item><title>Item</title><link>https://example.com/a</link>
      <dc:language>de</dc:language></item>
    </channel></rss>"""
    docs = RssAdapter().parse(rss, {"label": "all_news"}, "esma")
    assert docs[0].language == "de"
