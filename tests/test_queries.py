import json
import os
import tempfile
from datetime import datetime, timezone, timedelta
from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator, record_run_start, record_run_complete
from mcpserver.queries import (
    search_items,
    search_items_advanced,
    get_month_facets,
    get_recent,
    list_regulators,
    get_item,
    get_run_log,
    get_warnings,
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
