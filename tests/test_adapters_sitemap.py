"""Fixture-based unit tests for collector.adapters.sitemap.SitemapAdapter."""
from collector.adapters.sitemap import SitemapAdapter

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://example.com/publications/mica-guidance-note</loc>
    <lastmod>2026-07-02</lastmod>
  </url>
  <url>
    <loc>https://example.com/publications/travel-rule-update</loc>
    <lastmod>2026-06-15T10:30:00Z</lastmod>
  </url>
  <url>
    <loc>https://example.com/about-us</loc>
  </url>
</urlset>
"""


def test_parse_extracts_loc_and_lastmod():
    source = {"label": "publications", "sitemap": {"url_pattern": "/publications/"}}
    docs = SitemapAdapter().parse(SITEMAP_XML, source, "reg")
    assert len(docs) == 2
    assert docs[0].url == "https://example.com/publications/mica-guidance-note"
    assert docs[0].date_inferred is False
    assert docs[0].published_at.year == 2026 and docs[0].published_at.month == 7 and docs[0].published_at.day == 2


def test_parse_url_pattern_filters_unrelated_entries():
    source = {"label": "publications", "sitemap": {"url_pattern": "/publications/"}}
    docs = SitemapAdapter().parse(SITEMAP_XML, source, "reg")
    assert all("/publications/" in d.url for d in docs)


def test_parse_no_url_pattern_returns_every_entry():
    source = {"label": "everything", "sitemap": {}}
    docs = SitemapAdapter().parse(SITEMAP_XML, source, "reg")
    assert len(docs) == 3


def test_parse_title_from_last_segment_humanised():
    source = {"label": "publications", "sitemap": {"url_pattern": "/publications/"}}
    docs = SitemapAdapter().parse(SITEMAP_XML, source, "reg")
    assert docs[0].title == "Mica guidance note"


def test_parse_missing_lastmod_marks_date_inferred():
    source = {"label": "everything", "sitemap": {}}
    docs = SitemapAdapter().parse(SITEMAP_XML, source, "reg")
    about = next(d for d in docs if d.url.endswith("about-us"))
    assert about.date_inferred is True
    assert about.published_at is None


def test_parse_invalid_xml_returns_empty_list():
    docs = SitemapAdapter().parse("not xml at all <<<", {"label": "x", "sitemap": {}}, "reg")
    assert docs == []
