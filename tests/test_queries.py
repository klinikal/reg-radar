import json
import os
import tempfile
import pytest
from datetime import datetime, timezone, timedelta
from collector.schema import Item, make_item_id
from collector.storage import (
    init_db, insert_item, upsert_regulator, record_run_start, record_run_complete, record_source_empty_run,
)
from mcpserver.queries import (
    search_items,
    search_items_advanced,
    get_month_facets,
    get_recent,
    list_regulators,
    get_item,
    get_run_log,
    get_warnings,
    list_tags,
    get_items_since,
    get_health,
    _sanitize_fts_terms,
)


def setup_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    engine = init_db(f.name)
    return engine, f.name


def make_item(regulator_id="esma", title="MiCA warning issued", url=None, tags=None, language="en", days_ago=0, raw_content="raw"):
    now = datetime.now(timezone.utc) - timedelta(days=days_ago)
    url = url or f"https://example.com/{regulator_id}/{now.timestamp()}"
    return Item(
        id=make_item_id(regulator_id, url, now),
        regulator_id=regulator_id,
        source_label="all_news",
        title=title,
        url=url,
        published_at=now,
        fetched_at=now,
        date_inferred=False,
        language=language,
        content_snippet="Content about crypto assets and MiCA.",
        source_type="rss",
        source_tags=tags or ["casp"],
        content_tags=[],
        raw_content=raw_content,
    )


def test_search_items_finds_by_title():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        insert_item(engine, make_item(title="Press release on interest rates", url="https://example.com/other"))
        results = search_items(engine, "MiCA warning")
        assert len(results) == 1
        assert "MiCA warning" in results[0]["title"]
    finally:
        os.unlink(db_path)


def test_search_items_finds_by_raw_content_not_just_snippet():
    # A batch document (e.g. a regulator's PDF warning naming several firms)
    # can easily exceed the 500-char content_snippet truncation - a firm
    # named later in the full text must still be findable via raw_content.
    engine, db_path = setup_engine()
    try:
        long_raw_content = ("Entity warned Observations " * 30) + "ZAP SOLUTIONS EUROPE SP. Z O. O."
        insert_item(engine, make_item(title="Warnings from CNMV on unregistered institutions", raw_content=long_raw_content))
        results = search_items(engine, "ZAP SOLUTIONS EUROPE")
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_search_items_multi_regulator_ids_is_or():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(regulator_id="esma", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="eba", url="https://example.com/2"))
        insert_item(engine, make_item(regulator_id="eiopa", url="https://example.com/3"))
        results = search_items(engine, "", regulator_ids=["esma", "eba"])
        assert {r["regulator_id"] for r in results} == {"esma", "eba"}
    finally:
        os.unlink(db_path)


def test_search_items_regulator_id_and_regulator_ids_combine():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(regulator_id="esma", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="eba", url="https://example.com/2"))
        insert_item(engine, make_item(regulator_id="eiopa", url="https://example.com/3"))
        results = search_items(engine, "", regulator_id="eiopa", regulator_ids=["esma"])
        assert {r["regulator_id"] for r in results} == {"esma", "eiopa"}
    finally:
        os.unlink(db_path)


def test_search_items_fts_finds_by_title():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        insert_item(engine, make_item(title="Press release on interest rates", url="https://example.com/other"))
        results = search_items(engine, "MiCA warning")
        assert len(results) == 1
        assert "MiCA warning" in results[0]["title"]
    finally:
        os.unlink(db_path)


def test_search_items_fts_finds_by_raw_content():
    engine, db_path = setup_engine()
    try:
        long_raw = "Firm A breach. " * 40 + "Firm Zeta breach of custody rules."
        insert_item(engine, make_item(title="Batch warning notice", raw_content=long_raw))
        results = search_items(engine, "Firm Zeta")
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_search_items_handles_fts_breaking_query_without_raising():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="crypto-assets circular"))
        results = search_items(engine, 'crypto-assets "unterminated')
        assert isinstance(results, list)
    finally:
        os.unlink(db_path)


def test_search_items_advanced_finds_by_raw_content_not_just_snippet():
    engine, db_path = setup_engine()
    try:
        long_raw_content = ("Entity warned Observations " * 30) + "ZAP SOLUTIONS EUROPE SP. Z O. O."
        insert_item(engine, make_item(title="Warnings from CNMV on unregistered institutions", raw_content=long_raw_content))
        items, total = search_items_advanced(engine, query="zap solutions europe")
        assert total == 1
    finally:
        os.unlink(db_path)


def test_search_items_advanced_multi_term_and():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="Bafin imposes administrative fines", url="https://example.com/1"))
        insert_item(engine, make_item(title="Bafin issues guidance note", url="https://example.com/2"))
        insert_item(engine, make_item(title="ESMA imposes administrative fines", url="https://example.com/3"))
        items, total = search_items_advanced(engine, query="Bafin fines")
        assert total == 1
        assert "administrative fines" in items[0]["title"]
    finally:
        os.unlink(db_path)


def test_search_items_advanced_plus_separator_same_as_space():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="Bafin imposes administrative fines", url="https://example.com/1"))
        plus_result, plus_total = search_items_advanced(engine, query="Bafin+fines")
        space_result, space_total = search_items_advanced(engine, query="Bafin fines")
        assert plus_total == space_total == 1
    finally:
        os.unlink(db_path)


def test_search_items_advanced_quoted_phrase_is_atomic():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="fines administrative in nature", url="https://example.com/1"))
        insert_item(engine, make_item(title="administrative fines imposed", url="https://example.com/2"))
        # unquoted: both terms required anywhere -> both items match
        _, total_and = search_items_advanced(engine, query="administrative fines")
        assert total_and == 2
        # quoted: only the item with the exact adjacent phrase matches
        _, total_phrase = search_items_advanced(engine, query='"administrative fines"')
        assert total_phrase == 1
    finally:
        os.unlink(db_path)


def test_search_items_advanced_exact_mode_treats_whole_query_as_phrase():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="fines administrative in nature", url="https://example.com/1"))
        insert_item(engine, make_item(title="administrative fines imposed", url="https://example.com/2"))
        items, total = search_items_advanced(engine, query="administrative fines", exact=True)
        assert total == 1
        assert items[0]["title"] == "administrative fines imposed"
    finally:
        os.unlink(db_path)


def test_search_items_advanced_date_to_includes_same_day():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="Today's warning", days_ago=0))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        items, total = search_items_advanced(engine, date_to=today)
        assert total == 1, "a plain YYYY-MM-DD date_to should include same-day items, not exclude them"
    finally:
        os.unlink(db_path)


def test_search_items_mcp_tool_date_to_includes_same_day():
    # search_items() (the MCP tool) had its own separate date_to handling
    # that never got the same fix as search_items_advanced() - regression
    # test for that specifically, not just the webui-facing function.
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="Today's MCP warning", days_ago=0))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        results = search_items(engine, "warning", date_to=today)
        assert len(results) == 1, "a plain YYYY-MM-DD date_to should include same-day items via the MCP tool too"
    finally:
        os.unlink(db_path)


def test_search_items_advanced_pagination_and_total_count():
    engine, db_path = setup_engine()
    try:
        for i in range(5):
            insert_item(engine, make_item(url=f"https://example.com/{i}"))
        page1, total = search_items_advanced(engine, limit=2, offset=0)
        page2, _ = search_items_advanced(engine, limit=2, offset=2)
        assert total == 5
        assert len(page1) == 2
        assert len(page2) == 2
        assert {i["id"] for i in page1}.isdisjoint({i["id"] for i in page2})
    finally:
        os.unlink(db_path)


def test_search_items_advanced_multi_regulator_is_or():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(regulator_id="esma", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="eba", url="https://example.com/2"))
        insert_item(engine, make_item(regulator_id="eiopa", url="https://example.com/3"))
        items, total = search_items_advanced(engine, regulator_ids=["esma", "eba"])
        assert total == 2
        assert {i["regulator_id"] for i in items} == {"esma", "eba"}
    finally:
        os.unlink(db_path)


def test_get_month_facets_counts_and_ignores_date_filter():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(url="https://example.com/1", days_ago=0))
        insert_item(engine, make_item(url="https://example.com/2", days_ago=0))
        insert_item(engine, make_item(url="https://example.com/3", days_ago=40))
        facets = get_month_facets(engine)
        assert len(facets) == 2
        this_month = datetime.now(timezone.utc).strftime("%Y-%m")
        counts = {f["month"]: f["count"] for f in facets}
        assert counts[this_month] == 2
    finally:
        os.unlink(db_path)


def test_get_month_facets_respects_query_and_regulator_filters():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(regulator_id="esma", title="MiCA warning issued", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="eba", title="Something else entirely", url="https://example.com/2"))
        facets = get_month_facets(engine, query="MiCA warning")
        assert sum(f["count"] for f in facets) == 1
        facets_by_reg = get_month_facets(engine, regulator_ids=["eba"])
        assert sum(f["count"] for f in facets_by_reg) == 1
    finally:
        os.unlink(db_path)


def test_get_recent_returns_latest():
    engine, db_path = setup_engine()
    try:
        for i in range(5):
            insert_item(engine, make_item(url=f"https://example.com/{i}"))
        results = get_recent(engine, limit=3)
        assert len(results) == 3
    finally:
        os.unlink(db_path)


def test_list_regulators_includes_stats():
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU", "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu", "mica_competent_authority": True, "enabled": True,
        })
        insert_item(engine, make_item(regulator_id="esma"))
        results = list_regulators(engine)
        esma = next(r for r in results if r["id"] == "esma")
        assert esma["total_items"] == 1
        assert "consecutive_failures" in esma
    finally:
        os.unlink(db_path)


def test_get_item_by_id():
    engine, db_path = setup_engine()
    try:
        item = make_item()
        insert_item(engine, item)
        result = get_item(engine, item.id)
        assert result is not None
        assert result["id"] == item.id
        assert "raw_content" in result
    finally:
        os.unlink(db_path)


def test_get_item_missing_returns_none():
    engine, db_path = setup_engine()
    try:
        result = get_item(engine, "nonexistent-id")
        assert result is None
    finally:
        os.unlink(db_path)


def test_get_item_includes_body_md_and_document_frontmatter():
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU", "jurisdiction_type": "supranational", "website": "https://esma.europa.eu",
            "mica_competent_authority": True, "enabled": True,
        })
        item = make_item(regulator_id="esma", raw_content="")
        insert_item(engine, item)

        result = get_item(engine, item.id)

        assert result["body_md"] == ""  # no body_md and no raw_content to derive one from
        assert "document" in result
        document = result["document"]
        assert document["id"] == item.id
        assert document["regulator"] == "esma"
        assert document["regulator_name"] == "European Securities and Markets Authority"
        assert document["jurisdiction"] == "eu"
        assert document["regradar_schema"] == 1
    finally:
        os.unlink(db_path)


def test_get_item_document_frontmatter_includes_labels_provenance_and_jurisdiction_ancestors():
    """A classified item's frontmatter carries effective labels, one
    label_provenance entry per effective value, and jurisdiction_ancestors
    from the real jurisdiction hierarchy (../wiki/content/concept-canonical-document-format.md,
    ../wiki/content/component-classification.md) -
    e.g. a French regulator's ancestors are [eu]."""
    from regradar.classification import write_classification
    from regradar.taxonomy import build_taxonomy

    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "fr-amf", "name": "AMF", "full_name": "Autorite des Marches Financiers",
            "jurisdiction": "fr", "jurisdiction_type": "member_state", "website": "https://www.amf-france.org",
            "mica_competent_authority": True, "enabled": True,
        })
        item = make_item(regulator_id="fr-amf")
        insert_item(engine, item)

        taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
        write_classification(engine, taxonomy, item.id, "document_type", "warning",
                              by="source_default", confidence=1.0)
        write_classification(engine, taxonomy, item.id, "instrument", "mica",
                              by="rule", confidence=0.9)

        result = get_item(engine, item.id, taxonomy=taxonomy)
        document = result["document"]

        assert document["jurisdiction"] == "fr"
        assert document["jurisdiction_ancestors"] == ["eu"]
        assert document["labels"] == {"document_type": "warning", "instrument": ["mica"]}
        provenance_by_axis = {p["axis"]: p for p in document["label_provenance"]}
        assert provenance_by_axis["document_type"] == {
            "axis": "document_type", "value": "warning", "by": "source_default",
            "confidence": 1.0, "taxonomy_version": taxonomy.version(),
        }
        assert provenance_by_axis["instrument"] == {
            "axis": "instrument", "value": "mica", "by": "rule",
            "confidence": 0.9, "taxonomy_version": taxonomy.version(),
        }
    finally:
        os.unlink(db_path)


def test_get_item_without_taxonomy_omits_provenance_and_ancestors():
    """Backward compatibility: a caller with no taxonomy handy still gets a
    valid document, just without the taxonomy-derived fields."""
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "fr-amf", "name": "AMF", "full_name": "Autorite des Marches Financiers",
            "jurisdiction": "fr", "jurisdiction_type": "member_state", "website": "https://www.amf-france.org",
            "mica_competent_authority": True, "enabled": True,
        })
        item = make_item(regulator_id="fr-amf")
        insert_item(engine, item)

        result = get_item(engine, item.id)
        document = result["document"]
        assert document["jurisdiction_ancestors"] == []
        assert "labels" not in document
        assert "label_provenance" not in document
    finally:
        os.unlink(db_path)


def test_get_item_derives_body_md_from_raw_content_for_pre_v2_items():
    """A pre-v2 row (items.body_md NULL/empty, raw_content populated) gets
    a real body_md back from get_item(), and document.content_hash covers
    that derived text, not the empty string - see
    regradar.document.effective_body_md()."""
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu",
            "jurisdiction_type": "supranational", "website": "https://esma.europa.eu",
            "mica_competent_authority": True, "enabled": True,
        })
        item = make_item(regulator_id="esma", raw_content="<p>Some notice text.</p>")
        insert_item(engine, item)

        result = get_item(engine, item.id)

        assert result["body_md"] != ""
        assert "Some notice text." in result["body_md"]
        document = result["document"]
        from regradar.document import _content_hash
        assert document["content_hash"] == _content_hash(result["body_md"])
        assert document["content_hash"] != _content_hash("")
    finally:
        os.unlink(db_path)


def test_get_run_log():
    engine, db_path = setup_engine()
    try:
        run_id = record_run_start(engine)
        record_run_complete(engine, run_id, 10, 5, 0, [])
        results = get_run_log(engine, limit=5)
        assert len(results) == 1
        assert results[0]["total_fetched"] == 10
    finally:
        os.unlink(db_path)


def test_get_warnings_filters_by_tag():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings", "casp"]))
        insert_item(engine, make_item(tags=["news"], url="https://example.com/news"))
        results = get_warnings(engine, days=30)
        assert len(results) == 1
        assert "warnings" in json.loads(results[0]["source_tags"])
    finally:
        os.unlink(db_path)


def test_get_recent_filters_by_tag():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["casp", "mica"]))
        insert_item(engine, make_item(tags=["news"], url="https://example.com/news"))
        results = get_recent(engine, tags=["casp"])
        assert len(results) == 1
        assert "casp" in json.loads(results[0]["source_tags"])
    finally:
        os.unlink(db_path)


def test_get_recent_multi_regulator_ids_is_or():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(regulator_id="esma", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="eba", url="https://example.com/2"))
        insert_item(engine, make_item(regulator_id="eiopa", url="https://example.com/3"))
        results = get_recent(engine, regulator_ids=["esma", "eba"])
        assert {r["regulator_id"] for r in results} == {"esma", "eba"}
    finally:
        os.unlink(db_path)


def test_get_recent_regulator_id_and_regulator_ids_combine():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(regulator_id="esma", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="eba", url="https://example.com/2"))
        insert_item(engine, make_item(regulator_id="eiopa", url="https://example.com/3"))
        results = get_recent(engine, regulator_id="eiopa", regulator_ids=["esma"])
        assert {r["regulator_id"] for r in results} == {"esma", "eiopa"}
    finally:
        os.unlink(db_path)


def test_get_recent_filters_by_jurisdiction():
    """jurisdictions matches the regulator's jurisdiction id OR any ancestor
    of it - e.g. filtering on "eu" also picks up a member-state regulator."""
    from regradar.taxonomy import build_taxonomy

    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "fr-amf", "name": "AMF", "full_name": "Autorite des Marches Financiers",
            "jurisdiction": "fr", "jurisdiction_type": "member_state",
        })
        upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
        upsert_regulator(engine, {"id": "us-sec", "name": "SEC", "full_name": "SEC", "jurisdiction": "us"})
        insert_item(engine, make_item(regulator_id="fr-amf", url="https://example.com/1"))
        insert_item(engine, make_item(regulator_id="esma", url="https://example.com/2"))
        insert_item(engine, make_item(regulator_id="us-sec", url="https://example.com/3"))

        taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
        results = get_recent(engine, jurisdictions=["fr"], taxonomy=taxonomy)

        assert {r["regulator_id"] for r in results} == {"fr-amf", "esma"}
    finally:
        os.unlink(db_path)


def test_get_recent_jurisdictions_without_taxonomy_raises():
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
        insert_item(engine, make_item(regulator_id="esma"))

        with pytest.raises(ValueError, match="jurisdictions filter requires a taxonomy"):
            get_recent(engine, jurisdictions=["fr"], taxonomy=None)
    finally:
        os.unlink(db_path)


def test_get_recent_filters_by_labels():
    from regradar.classification import write_classification
    from regradar.taxonomy import build_taxonomy

    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
        classified = make_item(regulator_id="esma", url="https://example.com/1")
        unclassified = make_item(regulator_id="esma", url="https://example.com/2")
        insert_item(engine, classified)
        insert_item(engine, unclassified)

        taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
        write_classification(engine, taxonomy, classified.id, "instrument", "mica",
                              by="rule", confidence=0.9)

        results = get_recent(engine, labels={"instrument": ["mica"]})

        assert [r["id"] for r in results] == [classified.id]
    finally:
        os.unlink(db_path)


def test_search_items_filters_by_tag():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings"], title="MiCA enforcement action"))
        insert_item(engine, make_item(tags=["news"], title="MiCA press release", url="https://example.com/news"))
        results = search_items(engine, query="MiCA", tags=["warnings"])
        assert len(results) == 1
        assert "warnings" in json.loads(results[0]["source_tags"])
    finally:
        os.unlink(db_path)


def test_get_warnings_days_zero_returns_all():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings"], days_ago=100))
        results = get_warnings(engine, days=0)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_search_items_non_numeric_limit():
    engine, db_path = setup_engine()
    try:
        results = search_items(engine, query="MiCA", limit="abc")
        assert isinstance(results, list)
    finally:
        os.unlink(db_path)


def test_get_recent_non_numeric_limit():
    engine, db_path = setup_engine()
    try:
        results = get_recent(engine, limit="abc")
        assert isinstance(results, list)
    finally:
        os.unlink(db_path)


def test_get_warnings_negative_days():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings"], days_ago=100))
        results_negative = get_warnings(engine, days=-5)
        results_zero = get_warnings(engine, days=0)
        assert results_negative == results_zero
    finally:
        os.unlink(db_path)


def test_get_warnings_non_numeric_days():
    engine, db_path = setup_engine()
    try:
        results = get_warnings(engine, days="abc")
        assert isinstance(results, list)
    finally:
        os.unlink(db_path)


def test_search_items_percent_wildcard():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        results = search_items(engine, query="%")
        assert len(results) == 0
    finally:
        os.unlink(db_path)


def test_get_warnings_days_clamp():
    engine, db_path = setup_engine()
    try:
        results = get_warnings(engine, days=9999)
        assert isinstance(results, list)
    finally:
        os.unlink(db_path)


def test_sanitize_fts_terms_quotes_each_term():
    assert _sanitize_fts_terms(["MiCA", "warning"]) == '"MiCA"* "warning"*'


def test_sanitize_fts_terms_escapes_embedded_quotes():
    assert _sanitize_fts_terms(['firm"s license']) == '"firm""s license"*'


def test_sanitize_fts_terms_handles_hyphenated_words():
    assert _sanitize_fts_terms(["crypto-assets"]) == '"crypto-assets"*'


def test_sanitize_fts_terms_empty_list_returns_empty_string():
    assert _sanitize_fts_terms([]) == ""


def test_sanitize_fts_terms_skips_empty_strings():
    assert _sanitize_fts_terms(["MiCA", "", "warning"]) == '"MiCA"* "warning"*'


def test_sanitize_fts_terms_single_empty_string_returns_empty():
    assert _sanitize_fts_terms([""]) == ""


def test_search_items_advanced_fts_finds_exact_phrase():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        insert_item(engine, make_item(title="Press release on interest rates", url="https://example.com/other"))
        items, total = search_items_advanced(engine, query="MiCA warning")
        assert total == 1
        assert "MiCA warning" in items[0]["title"]
    finally:
        os.unlink(db_path)


def test_search_items_advanced_fts_multi_term_and():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA custody warning"))
        insert_item(engine, make_item(title="MiCA passporting notice", url="https://example.com/other"))
        items, total = search_items_advanced(engine, query="MiCA custody")
        assert total == 1
        assert "custody" in items[0]["title"]
    finally:
        os.unlink(db_path)


def test_search_items_advanced_handles_fts_breaking_characters_without_raising():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="crypto-assets circular", raw_content="crypto-assets"))
        items, total = search_items_advanced(engine, query='crypto-assets "unterminated')
        assert isinstance(total, int)
    finally:
        os.unlink(db_path)


def test_search_items_advanced_falls_back_to_like_on_fts_error(monkeypatch, caplog):
    import mcpserver.queries as queries_module
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        monkeypatch.setattr(queries_module, "_sanitize_fts_terms", lambda terms: '"unterminated')
        with caplog.at_level("WARNING"):
            items, total = search_items_advanced(engine, query="MiCA warning")
        assert total == 1
        assert "MiCA warning" in items[0]["title"]
        assert any("falling back to LIKE" in r.message for r in caplog.records)
    finally:
        os.unlink(db_path)


def test_get_month_facets_fts_matches_search_items_advanced_filtering():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued", days_ago=0))
        insert_item(engine, make_item(title="Unrelated press release", url="https://example.com/other", days_ago=0))
        facets = get_month_facets(engine, query="MiCA warning")
        assert sum(f["count"] for f in facets) == 1
    finally:
        os.unlink(db_path)


def test_get_recent_filters_by_keyword_derived_tag(tmp_path):
    from collector.storage import init_db, insert_item
    from collector.schema import Item, make_item_id
    from mcpserver.queries import get_recent
    from datetime import datetime, timezone

    engine = init_db(str(tmp_path / "test.db"))
    now = datetime.now(timezone.utc)
    # tags list as it would look AFTER Task 3's keyword-tagging merge ran
    insert_item(engine, Item(
        id=make_item_id("esma", "https://x/a", now), regulator_id="esma", source_label="all_news",
        title="New MiCA guidance", url="https://x/a", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="details on MiCA", source_type="rss", source_tags=["casp", "mica"],
    ))
    insert_item(engine, Item(
        id=make_item_id("esma", "https://x/b", now), regulator_id="esma", source_label="all_news",
        title="Unrelated notice", url="https://x/b", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="staffing", source_type="rss", source_tags=["casp"],
    ))

    results = get_recent(engine, tags=["mica"])
    assert len(results) == 1
    assert results[0]["title"] == "New MiCA guidance"


def _tagged_item(regulator_id, title, source_tags, content_tags, when=None):
    when = when or datetime.now(timezone.utc)
    return Item(
        id=make_item_id(regulator_id, f"https://x/{title}", when), regulator_id=regulator_id,
        source_label="all_news", title=title, url=f"https://x/{title}", published_at=when,
        fetched_at=when, date_inferred=False, language="en", content_snippet=title,
        source_type="rss", source_tags=source_tags, content_tags=content_tags,
    )


def test_get_recent_tags_param_searches_both_buckets_combined(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    insert_item(engine, _tagged_item("esma", "a", ["warnings"], []))
    insert_item(engine, _tagged_item("esma", "b", [], ["mica"]))
    insert_item(engine, _tagged_item("esma", "c", ["news"], ["mifid"]))

    results = get_recent(engine, tags=["warnings"])
    assert {r["title"] for r in results} == {"a"}

    results = get_recent(engine, tags=["mica"])
    assert {r["title"] for r in results} == {"b"}


def test_get_recent_source_tags_param_filters_source_bucket_only(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    insert_item(engine, _tagged_item("esma", "a", ["warnings"], ["mica"]))
    insert_item(engine, _tagged_item("esma", "b", [], ["warnings"]))  # "warnings" only in content_tags

    results = get_recent(engine, source_tags=["warnings"])
    assert {r["title"] for r in results} == {"a"}


def test_get_recent_content_tags_param_filters_content_bucket_only(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    insert_item(engine, _tagged_item("esma", "a", ["mica"], []))  # "mica" only in source_tags
    insert_item(engine, _tagged_item("esma", "b", [], ["mica"]))

    results = get_recent(engine, content_tags=["mica"])
    assert {r["title"] for r in results} == {"b"}


def test_get_warnings_filters_source_tags_not_content_tags(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    insert_item(engine, _tagged_item("esma", "a", ["warnings"], []))
    insert_item(engine, _tagged_item("esma", "b", [], ["warnings"]))  # would have matched under old combined tags column

    results = get_warnings(engine, days=0)
    assert {r["title"] for r in results} == {"a"}


def test_get_item_returns_source_tags_and_content_tags(tmp_path):
    engine = init_db(str(tmp_path / "test.db"))
    item = _tagged_item("esma", "a", ["warnings"], ["mica"])
    insert_item(engine, item)
    result = get_item(engine, item.id)
    assert result["source_tags"] == '["warnings"]'
    assert result["content_tags"] == '["mica"]'


def test_get_warnings_includes_items_on_cutoff_boundary_day():
    # Regression test: the old cutoff string used isoformat() ("T" separator
    # + "+00:00" offset) while stored published_at/fetched_at use a space
    # separator with no offset. A lexicographic string compare hits that
    # mismatch at position 10 (' ' < 'T'), so every row from the cutoff day
    # itself compared as "before" the cutoff and was silently excluded,
    # regardless of time-of-day. days=N effectively returned ~N-1 days.
    # days_ago=6.99 (not exactly 7) deliberately gives the item a ~14-minute
    # margin over the naive "days ago" cutoff point, so real wall-clock time
    # elapsed between this insert and get_warnings()'s own datetime.now()
    # call can never flip the comparison - while still landing on the same
    # calendar day as the cutoff, which is what triggers the bug.
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings"], days_ago=6.99))
        results = get_warnings(engine, days=7)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_search_items_negative_limit_is_clamped():
    # SQLite treats a negative LIMIT as "no limit" - a common "give me
    # everything" convention an agent might pass, but here it means the
    # entire matching result set (potentially tens of MB with raw_content
    # included in the LIKE-fallback path) goes into one MCP response.
    engine, db_path = setup_engine()
    try:
        for i in range(3):
            insert_item(engine, make_item(title="MiCA warning issued", url=f"https://example.com/{i}"))
        results = search_items(engine, query="MiCA", limit=-1)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_get_recent_negative_limit_is_clamped():
    engine, db_path = setup_engine()
    try:
        for i in range(3):
            insert_item(engine, make_item(url=f"https://example.com/{i}"))
        results = get_recent(engine, limit=-1)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_get_run_log_negative_limit_is_clamped():
    engine, db_path = setup_engine()
    try:
        for _ in range(3):
            run_id = record_run_start(engine)
            record_run_complete(engine, run_id, 1, 1, 0, [])
        results = get_run_log(engine, limit=-1)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_get_warnings_respects_limit():
    # No LIMIT clause at all previously - days=0 (per the tool's own
    # docstring, "returns all") could return every warnings-tagged item ever
    # collected in one response.
    engine, db_path = setup_engine()
    try:
        for i in range(5):
            insert_item(engine, make_item(tags=["warnings"], url=f"https://example.com/{i}"))
        results = get_warnings(engine, days=0, limit=2)
        assert len(results) == 2
    finally:
        os.unlink(db_path)


def test_get_warnings_limit_clamps_and_defaults():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings"]))
        assert len(get_warnings(engine, days=0, limit=-5)) == 1
        assert isinstance(get_warnings(engine, days=0, limit=9999), list)
        assert isinstance(get_warnings(engine, days=0, limit="abc"), list)
    finally:
        os.unlink(db_path)


def test_search_items_multi_word_query_uses_and_semantics():
    # Regression: search_items() used to treat "mica travel rule" as one
    # exact adjacent FTS phrase via _sanitize_fts_terms([query]) - an agent
    # expecting AND semantics (words present anywhere, any order) got
    # near-zero results with no signal why. _search_conditions() already had
    # the correct term-splitting logic three functions away.
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA travel rule guidance issued", url="https://example.com/1"))
        insert_item(engine, make_item(title="travel rule under the MiCA framework", url="https://example.com/2"))
        results = search_items(engine, "mica travel rule")
        assert len(results) == 2
    finally:
        os.unlink(db_path)


def test_search_items_like_fallback_multi_term_and(monkeypatch, caplog):
    import mcpserver.queries as queries_module
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="Bafin imposes administrative fines", url="https://example.com/1"))
        insert_item(engine, make_item(title="Bafin issues guidance note", url="https://example.com/2"))
        insert_item(engine, make_item(title="ESMA imposes administrative fines", url="https://example.com/3"))
        monkeypatch.setattr(queries_module, "_sanitize_fts_terms", lambda terms: '"unterminated')
        with caplog.at_level("WARNING"):
            results = queries_module.search_items(engine, "Bafin fines")
        assert len(results) == 1
        assert "administrative fines" in results[0]["title"]
    finally:
        os.unlink(db_path)


def test_search_items_non_operational_error_propagates(monkeypatch):
    # Regression: `except Exception` around the FTS branch used to swallow
    # genuine bugs (not just FTS5 syntax errors) and silently re-run the
    # LIKE fallback, which would fail identically or mask the real problem.
    import mcpserver.queries as queries_module
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        monkeypatch.setattr(queries_module, "_sanitize_fts_terms", lambda terms: (_ for _ in ()).throw(ValueError("not a DB error")))
        with pytest.raises(ValueError):
            queries_module.search_items(engine, "MiCA")
    finally:
        os.unlink(db_path)


def test_search_items_advanced_non_operational_error_propagates(monkeypatch):
    import mcpserver.queries as queries_module
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        monkeypatch.setattr(queries_module, "_sanitize_fts_terms", lambda terms: (_ for _ in ()).throw(ValueError("not a DB error")))
        with pytest.raises(ValueError):
            queries_module.search_items_advanced(engine, query="MiCA")
    finally:
        os.unlink(db_path)


def test_get_month_facets_non_operational_error_propagates(monkeypatch):
    import mcpserver.queries as queries_module
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        monkeypatch.setattr(queries_module, "_sanitize_fts_terms", lambda terms: (_ for _ in ()).throw(ValueError("not a DB error")))
        with pytest.raises(ValueError):
            queries_module.get_month_facets(engine, query="MiCA")
    finally:
        os.unlink(db_path)


def test_search_items_offset_pagination():
    engine, db_path = setup_engine()
    try:
        for i in range(5):
            insert_item(engine, make_item(title="MiCA warning issued", url=f"https://example.com/{i}"))
        page1 = search_items(engine, "MiCA", limit=2, offset=0)
        page2 = search_items(engine, "MiCA", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        assert {i["id"] for i in page1}.isdisjoint({i["id"] for i in page2})
    finally:
        os.unlink(db_path)


def test_search_items_negative_offset_is_clamped():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(title="MiCA warning issued"))
        results = search_items(engine, "MiCA", offset=-5)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_list_tags_counts_and_separates_buckets():
    engine, db_path = setup_engine()
    try:
        insert_item(engine, make_item(tags=["warnings", "casp"]))
        insert_item(engine, make_item(tags=["warnings"], url="https://example.com/2"))
        result = list_tags(engine)
        source_by_tag = {e["tag"]: e["count"] for e in result["source_tags"]}
        assert source_by_tag["warnings"] == 2
        assert source_by_tag["casp"] == 1
        assert result["content_tags"] == []
    finally:
        os.unlink(db_path)


def test_get_items_since_returns_only_newer_items():
    engine, db_path = setup_engine()
    try:
        old_item = make_item(url="https://example.com/old")
        insert_item(engine, old_item)
        cutoff = old_item.fetched_at.strftime("%Y-%m-%d %H:%M:%S.%f")
        new_item = make_item(url="https://example.com/new")
        new_item.fetched_at = old_item.fetched_at + timedelta(seconds=5)
        insert_item(engine, new_item)
        results = get_items_since(engine, cutoff)
        assert len(results) == 1
        assert results[0]["url"] == "https://example.com/new"
    finally:
        os.unlink(db_path)


def test_get_items_since_accepts_iso_t_separator_and_z_suffix():
    engine, db_path = setup_engine()
    try:
        item = make_item(url="https://example.com/1")
        insert_item(engine, item)
        since = (item.fetched_at - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        results = get_items_since(engine, since)
        assert len(results) == 1
    finally:
        os.unlink(db_path)


def test_get_items_since_invalid_timestamp_raises():
    engine, db_path = setup_engine()
    try:
        with pytest.raises(ValueError):
            get_items_since(engine, "not-a-timestamp")
    finally:
        os.unlink(db_path)


def test_get_health_no_runs_returns_no_runs_status():
    engine, db_path = setup_engine()
    try:
        result = get_health(engine)
        assert result["status"] == "no_runs"
        assert result["latest_run"] is None
        assert result["db_integrity"] == "unknown"
    finally:
        os.unlink(db_path)


def test_get_health_returns_latest_run_status_and_age():
    engine, db_path = setup_engine()
    try:
        run_id = record_run_start(engine)
        record_run_complete(engine, run_id, 10, 5, 0, [])
        result = get_health(engine)
        assert result["status"] == "success"
        assert result["latest_run"]["run_id"] == run_id
        assert result["age_seconds"] >= 0
    finally:
        os.unlink(db_path)


def test_get_health_reports_db_integrity_ok():
    engine, db_path = setup_engine()
    try:
        run_id = record_run_start(engine)
        record_run_complete(engine, run_id, 10, 5, 0, [], db_integrity_ok=True)
        result = get_health(engine)
        assert result["db_integrity"] == "ok"
        assert "db_integrity_ok" not in result["latest_run"]
    finally:
        os.unlink(db_path)


def test_get_health_reports_db_integrity_corrupted():
    engine, db_path = setup_engine()
    try:
        run_id = record_run_start(engine)
        record_run_complete(engine, run_id, 10, 5, 0, [], db_integrity_ok=False)
        result = get_health(engine)
        assert result["db_integrity"] == "corrupted"
    finally:
        os.unlink(db_path)


def test_get_health_reports_db_integrity_unknown_when_never_checked():
    # A run recorded without db_integrity_ok specified (the pre-Task
    # default) must read as "unknown", not silently look "ok" or "corrupted".
    engine, db_path = setup_engine()
    try:
        run_id = record_run_start(engine)
        record_run_complete(engine, run_id, 10, 5, 0, [])
        result = get_health(engine)
        assert result["db_integrity"] == "unknown"
    finally:
        os.unlink(db_path)


def test_list_regulators_flags_stalled_source_at_threshold(monkeypatch):
    monkeypatch.setenv("STALL_THRESHOLD_RUNS", "3")
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU", "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu", "mica_competent_authority": True, "enabled": True,
        })
        for _ in range(3):
            record_source_empty_run(engine, "esma", "all_news")
        results = list_regulators(engine)
        esma = next(r for r in results if r["id"] == "esma")
        assert esma["stalled"] is True
    finally:
        os.unlink(db_path)


def test_list_regulators_not_stalled_below_threshold(monkeypatch):
    monkeypatch.setenv("STALL_THRESHOLD_RUNS", "3")
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU", "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu", "mica_competent_authority": True, "enabled": True,
        })
        record_source_empty_run(engine, "esma", "all_news")
        record_source_empty_run(engine, "esma", "all_news")
        results = list_regulators(engine)
        esma = next(r for r in results if r["id"] == "esma")
        assert esma["stalled"] is False
    finally:
        os.unlink(db_path)


def test_list_regulators_healthy_regulator_not_stalled():
    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {
            "id": "esma", "name": "ESMA", "full_name": "European Securities and Markets Authority",
            "jurisdiction": "EU", "jurisdiction_type": "supranational",
            "website": "https://www.esma.europa.eu", "mica_competent_authority": True, "enabled": True,
        })
        results = list_regulators(engine)
        esma = next(r for r in results if r["id"] == "esma")
        assert esma["stalled"] is False
    finally:
        os.unlink(db_path)


def test_get_health_lists_stalled_sources_at_threshold(monkeypatch):
    monkeypatch.setenv("STALL_THRESHOLD_RUNS", "2")
    engine, db_path = setup_engine()
    try:
        record_source_empty_run(engine, "esma", "all_news")
        record_source_empty_run(engine, "esma", "all_news")
        record_source_empty_run(engine, "fr-amf", "warnings")  # below threshold
        result = get_health(engine)
        stalled_keys = {(s["regulator_id"], s["source_label"]) for s in result["stalled_sources"]}
        assert ("esma", "all_news") in stalled_keys
        assert ("fr-amf", "warnings") not in stalled_keys
    finally:
        os.unlink(db_path)


def test_get_health_stalled_sources_empty_when_none_stalled():
    engine, db_path = setup_engine()
    try:
        result = get_health(engine)
        assert result["stalled_sources"] == []
    finally:
        os.unlink(db_path)


def test_search_items_jurisdictions_without_taxonomy_raises():
    """A jurisdictions filter cannot be expanded without a taxonomy.
    Dropping it silently used to widen the query to the whole corpus - the
    opposite of what the caller asked for."""
    from mcpserver.queries import _jurisdiction_filter_conditions

    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
        insert_item(engine, make_item(regulator_id="esma"))

        with pytest.raises(ValueError, match="jurisdictions filter requires a taxonomy"):
            search_items(engine, "", jurisdictions=["fr"], taxonomy=None)
        with pytest.raises(ValueError, match="jurisdictions filter requires a taxonomy"):
            _jurisdiction_filter_conditions(["fr"], None)
    finally:
        os.unlink(db_path)


def test_get_unclassified_derives_body_md_from_raw_content_for_pre_v2_items():
    """A v1-upgraded row has items.body_md NULL but raw_content populated.
    Returning the raw column left the classifier skill and `reg-radar
    classify` with an empty body to classify."""
    from mcpserver.queries import get_unclassified

    engine, db_path = setup_engine()
    try:
        upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
        item = make_item(regulator_id="esma", raw_content="<p>Some notice text.</p>")
        insert_item(engine, item)

        results = get_unclassified(engine, axis="document_type")

        assert [r["id"] for r in results] == [item.id]
        assert "Some notice text." in results[0]["body_md"]
        # Shape unchanged: the extra columns are only read to derive the body.
        assert "raw_content" not in results[0]
        assert "content_snippet" not in results[0]
    finally:
        os.unlink(db_path)
