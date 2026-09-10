"""Fixture-based unit tests for collector.adapters.json_api.JsonApiAdapter."""
import json

from collector.adapters.json_api import JsonApiAdapter, resolve_path

PAYLOAD = json.dumps({
    "data": {
        "items": [
            {"title": "MiCA Q&A round 3", "link": "https://example.com/docs/1", "date": "2026-07-01"},
            {"title": "Travel rule update", "link": "https://example.com/docs/2", "date": "2026-06-20"},
            {"title": "", "link": "https://example.com/docs/3", "date": "2026-06-19"},
        ]
    }
})


def test_resolve_path_nested_dict():
    assert resolve_path({"a": {"b": {"c": 1}}}, "a.b.c") == 1


def test_resolve_path_missing_key_returns_none():
    assert resolve_path({"a": {}}, "a.b.c") is None


def test_resolve_path_empty_path_returns_input():
    data = {"a": 1}
    assert resolve_path(data, "") is data


def test_parse_extracts_items_via_items_path():
    source = {
        "label": "docs",
        "json_api": {"items_path": "data.items", "title_field": "title", "url_field": "link", "published_field": "date"},
    }
    docs = JsonApiAdapter().parse(PAYLOAD, source, "reg")
    assert len(docs) == 2  # the blank-title entry is skipped
    assert docs[0].title == "MiCA Q&A round 3"
    assert docs[0].url == "https://example.com/docs/1"
    assert docs[0].date_inferred is False
    assert docs[0].published_at.month == 7


def test_parse_missing_published_field_marks_date_inferred():
    source = {"label": "docs", "json_api": {"items_path": "data.items", "title_field": "title", "url_field": "link"}}
    docs = JsonApiAdapter().parse(PAYLOAD, source, "reg")
    assert all(d.date_inferred for d in docs)


def test_parse_items_path_not_a_list_returns_empty():
    source = {"label": "docs", "json_api": {"items_path": "data", "title_field": "title", "url_field": "link"}}
    docs = JsonApiAdapter().parse(PAYLOAD, source, "reg")
    assert docs == []


def test_parse_invalid_json_returns_empty_list():
    source = {"label": "docs", "json_api": {"items_path": ""}}
    docs = JsonApiAdapter().parse("not json", source, "reg")
    assert docs == []


# A json_api source's `url` is an API endpoint, not the site root (issue #36).
MAS_SHAPED_SOURCE = {
    "label": "media_releases",
    "url": "https://www.mas.gov.sg/api/v1/search?fq=site_s:MAS&q=*:*&rows=100",
    "json_api": {
        "items_path": "response.docs",
        "title_field": "document_title_string_s",
        "url_field": "page_url_s",
    },
}


def _payload(page_url):
    return json.dumps({"response": {"docs": [
        {"document_title_string_s": "MAS issues notice", "page_url_s": page_url},
    ]}})


def test_parse_resolves_relative_url_against_the_source_endpoint():
    docs = JsonApiAdapter().parse(
        _payload("/news/media-releases/2026/mas-issues-notice"), MAS_SHAPED_SOURCE, "sg-mas"
    )
    assert docs[0].url == "https://www.mas.gov.sg/news/media-releases/2026/mas-issues-notice"


def test_parse_leaves_an_absolute_url_byte_identical():
    absolute = "https://www.federalregister.gov/documents/2026/09/10/2026-1234/rule"
    docs = JsonApiAdapter().parse(_payload(absolute), MAS_SHAPED_SOURCE, "us-fr")
    assert docs[0].url == absolute


def test_parse_without_a_source_url_leaves_the_value_untouched():
    source = {k: v for k, v in MAS_SHAPED_SOURCE.items() if k != "url"}
    docs = JsonApiAdapter().parse(_payload("/relative/only"), source, "reg")
    assert docs[0].url == "/relative/only"
