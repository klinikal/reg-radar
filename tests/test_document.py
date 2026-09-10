import json
from datetime import date, datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from regradar import document as doc

FIXTURES = Path(__file__).parent / "fixtures" / "documents"
SCHEMA = json.loads((Path(__file__).parent.parent / "regradar" / "schema" / "document-v1.json").read_text())


def make_item(**overrides):
    now = datetime(2026, 8, 15, 6, 2, 11, tzinfo=timezone.utc)
    item = {
        "id": "9f2c" + "a" * 60,
        "regulator_id": "fca",
        "regulator_name": "Financial Conduct Authority",
        "jurisdiction": "GB",
        "title": "CP26/12: Proposed changes to the client assets sourcebook",
        "url": "https://example.org/cp26-12",
        "published_at": datetime(2026, 8, 14, tzinfo=timezone.utc),
        "fetched_at": now,
        "updated_at": datetime(2026, 8, 20, 6, 0, 3, tzinfo=timezone.utc),
        "date_inferred": False,
        "language": "en",
        "source_label": "policy_guidance",
        "source_type": "scrape",
        "body_md": "# Heading\n\nSome body text.",
    }
    item.update(overrides)
    return item


# --- slugify / document_path ---------------------------------------------

def test_slugify_basic():
    assert doc.slugify("CP26/12: Proposed changes!") == "cp26-12-proposed-changes"


def test_slugify_ascii_folds_accents():
    assert doc.slugify("Réglementation générale française") == "reglementation-generale-francaise"
    assert doc.slugify("Über Bankenaufsicht") == "uber-bankenaufsicht"


def test_slugify_non_latin_script_falls_back():
    # Greek and Bulgarian (Cyrillic) have no ASCII-compatibility
    # decomposition, so folding drops every character - must still return a
    # usable slug rather than an empty path segment.
    assert doc.slugify("Ανακοίνωση εποπτείας") == "untitled"
    assert doc.slugify("Съобщение за надзор") == "untitled"


def test_slugify_empty_title():
    assert doc.slugify("") == "untitled"
    assert doc.slugify(None) == "untitled"


def test_slugify_truncates_to_80_chars():
    long_title = "word " * 40
    assert len(doc.slugify(long_title)) <= 80


def test_document_path_layout():
    item = make_item()
    path = doc.document_path(item)
    assert path == Path("export/gb/fca/2026/2026-08-14-cp26-12-proposed-changes-to-the-client-assets-sourcebook-9f2caaaa.md")


def test_document_path_falls_back_to_fetched_at_when_no_published_at():
    item = make_item(published_at=None, date_inferred=True)
    path = doc.document_path(item)
    assert "2026-08-15" in path.name  # fetched_at's date, not published_at's


def test_document_path_accepts_iso_strings_from_raw_sql():
    item = make_item(published_at="2026-08-14 00:00:00.000000", fetched_at="2026-08-15 06:02:11.000000")
    path = doc.document_path(item)
    assert "2026-08-14" in path.name


# --- HTML/PDF body conversion ---------------------------------------------

def test_html_to_markdown_preserves_structure():
    html = (FIXTURES / "esma_news.html").read_text(encoding="utf-8")
    md = doc.html_to_markdown(html)
    assert md.startswith("## ESMA publishes guidelines")
    assert "[guidelines" in md and "](https://example.org/guidelines/mica-marketing)" in md
    assert "- Clarity and fairness" in md
    assert "### Next steps" in md


def test_html_to_markdown_renders_pipe_tables():
    html = (FIXTURES / "fca_consultation.html").read_text(encoding="utf-8")
    md = doc.html_to_markdown(html)
    assert "| Chapter | Change |" in md
    assert "| --- | --- |" in md
    assert "1. Crypto-asset service providers" in md  # ordered list preserved


def test_html_to_markdown_preserves_non_ascii():
    # The decisive case against html2text (see docs/schema evaluation in
    # the task report): it silently strips German umlauts to their bare
    # ASCII letters ("für" -> "fur"). markdownify does not.
    html = (FIXTURES / "bafin_press_release.html").read_text(encoding="utf-8")
    md = doc.html_to_markdown(html)
    assert "für" in md
    assert "Geschäften" in md
    assert "Österreich" in md


def test_html_to_markdown_empty_input():
    assert doc.html_to_markdown("") == ""
    assert doc.html_to_markdown(None) == ""


def test_normalize_pdf_text_joins_wrapped_lines_into_paragraphs():
    raw = (FIXTURES / "scraped_pdf.txt").read_text(encoding="utf-8")
    normalized = doc.normalize_pdf_text(raw)
    paragraphs = normalized.split("\n\n")
    assert len(paragraphs) >= 3
    assert "\n" not in paragraphs[0]  # wrapped lines joined onto one line
    assert "Introduction" in normalized


def test_rss_body_markdown_appends_not_collected_note():
    snippet = (FIXTURES / "rss_snippet.txt").read_text(encoding="utf-8").strip()
    body, extractor = doc.rss_body_markdown(None, snippet, "https://example.org/warnings")
    assert body.endswith("> Full text not collected. See https://example.org/warnings.")
    assert snippet.splitlines()[0] in body
    assert extractor.startswith("markdownify/")


def test_convert_body_prefers_html_over_pdf_over_plain():
    body, extractor = doc.convert_body(content_html="<p>hi</p>", pdf_text="ignored", plain_text="ignored")
    assert body == "hi"
    assert extractor.startswith("markdownify/")

    body, extractor = doc.convert_body(pdf_text="line one\n\nline two")
    assert body == "line one\n\nline two"
    assert extractor.startswith("pypdf/")

    body, extractor = doc.convert_body(plain_text="  flat text  ")
    assert body == "flat text"
    assert extractor == "raw/1"


def test_convert_body_falls_back_when_nothing_collected():
    body, extractor = doc.convert_body(snippet="a short snippet", source_url="https://example.org/x")
    assert "a short snippet" in body
    assert body.endswith("> Full text not collected. See https://example.org/x.")
    assert extractor == "raw/1"


# --- effective_body_md (pre-v2 raw_content fallback) ----------------------

def test_effective_body_md_returns_body_md_when_present():
    item = make_item(body_md="already have a body")
    assert doc.effective_body_md(item) == "already have a body"


def test_effective_body_md_returns_empty_when_neither_present():
    item = make_item(body_md="", raw_content=None)
    assert doc.effective_body_md(item) == ""


def test_effective_body_md_derives_rss_body_with_not_collected_note():
    # A pre-v2 RSS item: body_md is NULL, raw_content is the feed's own
    # unconverted HTML summary (collector.pipeline._extract's is_partial
    # branch) - the derived body must look like a fresh RSS item's own
    # rss_body_markdown() output, note included.
    item = make_item(
        body_md="", raw_content="<p>Warning about unregistered firms.</p>",
        source_type="rss", content_snippet="Warning about unregistered firms.",
        url="https://example.org/warnings/123",
    )
    body = doc.effective_body_md(item)
    assert "Warning about unregistered firms." in body
    assert body.endswith("> Full text not collected. See https://example.org/warnings/123.")


def test_effective_body_md_derives_scrape_body_via_paragraph_normalisation():
    # Pre-v2 scrape/page_watch raw_content is already plain extracted text
    # (collector.pipeline._extract's non-RSS branches) - only paragraph
    # joining applies, no "not collected" note (a real body was collected).
    raw = "This is a\nwrapped line.\n\nSecond paragraph\nhere."
    item = make_item(body_md="", raw_content=raw, source_type="scrape")
    body = doc.effective_body_md(item)
    assert body == "This is a wrapped line.\n\nSecond paragraph here."


def test_build_frontmatter_content_hash_covers_derived_body():
    item = make_item(body_md="", raw_content="hello", source_type="scrape")
    fm = doc.build_frontmatter(item)
    assert fm["content_hash"] == doc._content_hash(doc.effective_body_md(item))
    assert fm["content_hash"] != doc._content_hash("")


def test_render_item_renders_derived_body_for_pre_v2_item():
    item = make_item(body_md="", raw_content="hello world", source_type="scrape")
    text = doc.render_item(item, None, None, None)
    assert "hello world" in text.split("---\n\n", 1)[1]


# --- frontmatter build / render / parse round trip ------------------------

def test_build_frontmatter_omits_labels_when_absent():
    fm = doc.build_frontmatter(make_item())
    assert "labels" not in fm
    assert "label_provenance" not in fm
    assert "original" not in fm


def test_build_frontmatter_includes_labels_when_present():
    fm = doc.build_frontmatter(make_item(), labels={"document_type": "consultation"}, label_provenance=[{"axis": "document_type", "value": "consultation", "by": "source_default"}])
    assert fm["labels"] == {"document_type": "consultation"}
    assert fm["label_provenance"][0]["axis"] == "document_type"


def test_build_frontmatter_includes_original_when_artefact_present():
    artefact = {"path": "originals/9f/9f2c.pdf", "mime": "application/pdf", "sha256": "77aa", "bytes": 418233}
    fm = doc.build_frontmatter(make_item(), artefact=artefact)
    assert fm["original"] == artefact


def test_render_item_then_parse_round_trips_all_frontmatter_fields():
    item = make_item()
    artefact = {"path": "originals/9f/9f2c.pdf", "mime": "application/pdf", "sha256": "77aa", "bytes": 418233}
    labels = {"document_type": "consultation", "instrument": ["cass", "mifid2"]}
    provenance = [{"axis": "document_type", "value": "consultation", "by": "source_default", "confidence": 1.0, "taxonomy_version": 3}]

    fm_expected = doc.build_frontmatter(item, artefact=artefact, labels=labels, label_provenance=provenance)
    text = doc.render_item(item, artefact, labels, provenance)
    fm_actual, body = doc.parse(text)

    assert fm_actual == fm_expected
    assert body.strip() == item["body_md"].strip()


def test_parse_rejects_text_with_no_frontmatter():
    with pytest.raises(ValueError):
        doc.parse("just a body, no frontmatter block")


def test_rendered_frontmatter_is_valid_yaml_and_matches_json_schema():
    item = make_item()
    text = doc.render_item(item, None, None, None)
    fm, _body = doc.parse(text)  # raises if not valid YAML
    jsonschema.validate(doc.frontmatter_to_json_safe(fm), SCHEMA)


def test_frontmatter_top_level_values_are_obsidian_safe_types():
    # Obsidian's Properties panel renders scalars and lists cleanly.
    # `labels` and `original` are the only top-level values allowed to be a
    # mapping (a single level: axis/value pairs, or path/mime/sha256/bytes);
    # `label_provenance` is a list whose entries are mappings. Anything else
    # nested this deep would not render as a Properties panel field.
    item = make_item()
    labels = {"document_type": "consultation", "instrument": ["cass", "mifid2"]}
    provenance = [{"axis": "document_type", "value": "consultation", "by": "source_default"}]
    artefact = {"path": "originals/9f/9f2c.pdf", "mime": "application/pdf", "sha256": "77aa", "bytes": 1}
    fm = doc.build_frontmatter(item, artefact=artefact, labels=labels, label_provenance=provenance)

    mapping_keys = {"labels", "original"}
    for key, value in fm.items():
        if key in mapping_keys:
            assert isinstance(value, dict), (key, type(value))
        elif key == "label_provenance":
            assert isinstance(value, list) and all(isinstance(entry, dict) for entry in value)
        else:
            assert isinstance(value, (str, int, float, bool, type(None), list, date, datetime)), (key, type(value))
