import fcntl
import json
import sys
import tempfile
import os
import pytest
from unittest.mock import patch, MagicMock

from collector.main import load_regulators, run_collection, run_collection_locked, _acquire_lock, _release_lock
from collector.storage import init_db, record_run_start, RunRow, SourceErrorRow
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session


# ---------------------------------------------------------------------------
# load_regulators tests
# ---------------------------------------------------------------------------

def test_load_regulators_missing_file():
    with pytest.raises(SystemExit) as exc_info:
        load_regulators("/nonexistent/path/regulators.yaml")
    assert exc_info.value.code == 1


def test_load_regulators_empty_file():
    with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
        f.write(b"")
        path = f.name
    try:
        with pytest.raises(SystemExit) as exc_info:
            load_regulators(path)
        assert exc_info.value.code == 1
    finally:
        os.unlink(path)


def test_load_regulators_non_list():
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
        f.write("key: value\n")
        path = f.name
    try:
        with pytest.raises(SystemExit) as exc_info:
            load_regulators(path)
        assert exc_info.value.code == 1
    finally:
        os.unlink(path)


def _write_yaml(path, content):
    with open(path, "w") as f:
        f.write(content)


def test_load_regulators_directory_concatenates_every_yaml_file(tmp_path):
    _write_yaml(tmp_path / "at.yaml", "- id: at-fma\n  jurisdiction: at\n  sources: [{label: news, type: rss}]\n")
    _write_yaml(tmp_path / "eu.yaml", "- id: esma\n  jurisdiction: eu\n  sources: [{label: news, type: rss}]\n")
    regulators = load_regulators(str(tmp_path))
    assert {r["id"] for r in regulators} == {"at-fma", "esma"}


def test_load_regulators_directory_no_yaml_files_exits(tmp_path):
    with pytest.raises(SystemExit) as exc_info:
        load_regulators(str(tmp_path))
    assert exc_info.value.code == 1


def test_load_regulators_duplicate_id_across_files_exits(tmp_path):
    _write_yaml(tmp_path / "at.yaml", "- id: dupe\n  jurisdiction: at\n  sources: [{label: a, type: rss}]\n")
    _write_yaml(tmp_path / "be.yaml", "- id: dupe\n  jurisdiction: be\n  sources: [{label: b, type: rss}]\n")
    with pytest.raises(SystemExit) as exc_info:
        load_regulators(str(tmp_path))
    assert exc_info.value.code == 1


def test_load_regulators_duplicate_source_label_within_regulator_exits(tmp_path):
    _write_yaml(
        tmp_path / "at.yaml",
        "- id: at-fma\n  jurisdiction: at\n  sources:\n"
        "  - {label: news, type: rss}\n  - {label: news, type: scrape}\n",
    )
    with pytest.raises(SystemExit) as exc_info:
        load_regulators(str(tmp_path))
    assert exc_info.value.code == 1


def test_load_regulators_missing_regulator_id_exits(tmp_path):
    _write_yaml(tmp_path / "at.yaml", "- jurisdiction: at\n  sources: []\n")
    with pytest.raises(SystemExit) as exc_info:
        load_regulators(str(tmp_path))
    assert exc_info.value.code == 1


def test_load_regulators_single_file_still_works(tmp_path):
    path = tmp_path / "regulators.yaml"
    _write_yaml(path, "- id: at-fma\n  jurisdiction: at\n  sources: [{label: news, type: rss}]\n")
    regulators = load_regulators(str(path))
    assert len(regulators) == 1
    assert regulators[0]["id"] == "at-fma"


def test_load_regulators_split_registry_loads_cleanly():
    # The real, current config/regulators/ split - proves the migration
    # produced a directory that loads without duplicate id/label errors.
    regulators = load_regulators("config/regulators")
    assert len(regulators) == 62
    assert {r["id"] for r in regulators} >= {"esma", "amla", "eba", "eiopa", "mt-mfsa"}


# ---------------------------------------------------------------------------
# run_collection tests
# ---------------------------------------------------------------------------

def _make_engine():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    engine = init_db(db_path)
    return engine, db_path


def _base_cfg():
    return {
        "user_agent": "test/1.0",
        "timeout": 10,
        "retries": 1,
        "flaresolverr_url": None,
    }


def test_run_collection_bad_regulator_skipped():
    """A regulator dict missing 'id' should be skipped; subsequent regulators are processed."""
    engine, db_path = _make_engine()
    try:
        good_reg_processed = []

        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg, **kwargs):
            good_reg_processed.append(reg_id)
            return []

        bad_reg = {"name": "no-id", "sources": [{"label": "x", "url": "http://x"}]}
        good_reg = {
            "id": "good-reg",
            "name": "Good",
            "enabled": True,
            "sources": [{"label": "s1", "url": "http://good.example"}],
        }

        with patch("collector.main.fetch_source", side_effect=fake_fetch):
            run_collection(engine, [bad_reg, good_reg], _base_cfg())

        assert "good-reg" in good_reg_processed
    finally:
        os.unlink(db_path)


def test_run_collection_skips_disabled_source():
    """A source with enabled: false is skipped, but its regulator's other (enabled) sources still run."""
    engine, db_path = _make_engine()
    try:
        fetched_labels = []

        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg, **kwargs):
            fetched_labels.append(source["label"])
            return []

        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [
                {"label": "off", "url": "http://example.com/off", "enabled": False},
                {"label": "on", "url": "http://example.com/on"},
            ],
        }

        with patch("collector.main.fetch_source", side_effect=fake_fetch):
            run_collection(engine, [reg], _base_cfg())

        assert fetched_labels == ["on"]
    finally:
        os.unlink(db_path)


def test_run_collection_source_error_marks_run_partial_not_success():
    """A per-source error that doesn't escape the loop must show as 'partial',
    not 'success'/'ok' - the run status field exists specifically to surface
    this, and silently marking it success/ok masked exactly the failures
    it's meant to catch."""
    engine, db_path = _make_engine()
    try:
        def failing_fetch(source, reg_id, flaresolverr_url, fetch_cfg):
            raise RuntimeError("selector broke")

        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "broken", "url": "http://example.com/broken"}],
        }

        with patch("collector.main.fetch_source", side_effect=failing_fetch):
            run_collection(engine, [reg], _base_cfg())

        with Session(engine) as session:
            rows = session.query(RunRow).all()
            assert len(rows) == 1
            assert rows[0].status == "partial"
    finally:
        os.unlink(db_path)


def test_run_collection_clean_run_marks_success_not_ok():
    engine, db_path = _make_engine()
    try:
        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "fine", "url": "http://example.com/fine"}],
        }

        with patch("collector.main.fetch_source", return_value=[]):
            run_collection(engine, [reg], _base_cfg())

        with Session(engine) as session:
            rows = session.query(RunRow).all()
            assert len(rows) == 1
            assert rows[0].status == "success"
    finally:
        os.unlink(db_path)


def test_run_collection_records_db_integrity_ok_for_healthy_db():
    # A fresh init_db()'d temp file is a clean database - the integrity
    # check run at the start of every collection cycle should pass and be
    # recorded on the run row, not left null.
    engine, db_path = _make_engine()
    try:
        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "fine", "url": "http://example.com/fine"}],
        }

        with patch("collector.main.fetch_source", return_value=[]):
            run_collection(engine, [reg], _base_cfg())

        with Session(engine) as session:
            rows = session.query(RunRow).all()
            assert len(rows) == 1
            assert bool(rows[0].db_integrity_ok) is True
    finally:
        os.unlink(db_path)


def test_run_collection_logs_critical_when_db_integrity_check_fails():
    # A failing integrity check must be surfaced loudly (not just recorded
    # silently in a column nobody looks at until a search fails) - this is
    # the whole point of checking at collection time rather than waiting for
    # a read to trip over corruption first.
    engine, db_path = _make_engine()
    try:
        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "fine", "url": "http://example.com/fine"}],
        }

        with patch("collector.main.check_db_integrity", return_value=False), \
                patch("collector.main.fetch_source", return_value=[]), \
                patch("collector.main.logger") as mock_logger:
            run_collection(engine, [reg], _base_cfg())

        mock_logger.critical.assert_called_once()
        assert "INTEGRITY CHECK FAILED" in mock_logger.critical.call_args[0][0]

        with Session(engine) as session:
            rows = session.query(RunRow).all()
            assert bool(rows[0].db_integrity_ok) is False
    finally:
        os.unlink(db_path)


def test_run_collection_scrape_source_zero_items_recorded_as_error():
    # A scrape source returning [] with no exception (a broken selector, a
    # site redesign) used to look identical to "nothing new today" - it
    # called reset_source_errors and the run showed clean. Every scrape
    # source here only ever looks at page 1 of a listing, which in normal
    # operation always has *something* - 0 items is suspicious and must be
    # recorded as a source error, not silently reset.
    engine, db_path = _make_engine()
    try:
        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "broken_selector", "url": "http://example.com/x", "type": "scrape"}],
        }

        with patch("collector.main.fetch_source", return_value=[]):
            run_collection(engine, [reg], _base_cfg())

        with Session(engine) as session:
            rows = session.query(SourceErrorRow).filter_by(regulator_id="test-reg", source_label="broken_selector").all()
            assert len(rows) == 1
            assert rows[0].consecutive_failures >= 1

            # Recording the source error alone isn't enough - total_errors
            # and the run's status must reflect it too, or get_run_log_tool
            # still reports a clean "success" run despite a broken selector.
            run_rows = session.query(RunRow).all()
            assert len(run_rows) == 1
            assert run_rows[0].status != "success"
            assert run_rows[0].total_errors >= 1
    finally:
        os.unlink(db_path)


def test_run_collection_rss_source_zero_items_not_treated_as_error():
    # Unlike scrape sources, an RSS feed with genuinely nothing new is
    # completely normal (e.g. quiet news day) - must not be flagged.
    engine, db_path = _make_engine()
    try:
        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "quiet_feed", "url": "http://example.com/rss", "type": "rss"}],
        }

        with patch("collector.main.fetch_source", return_value=[]):
            run_collection(engine, [reg], _base_cfg())

        with Session(engine) as session:
            rows = session.query(SourceErrorRow).filter_by(regulator_id="test-reg", source_label="quiet_feed").all()
            # Not an error (consecutive_failures stays 0) - but the stall
            # detector still counts the empty run, independent of the
            # scrape-only error check above.
            assert len(rows) == 1
            assert rows[0].consecutive_failures == 0
            assert rows[0].consecutive_empty_runs == 1
    finally:
        os.unlink(db_path)


def test_run_collection_page_watch_zero_items_not_treated_as_error():
    # Unlike scrape sources, a page_watch source with genuinely nothing
    # changed since the last check is completely normal - must not be
    # flagged as a possible broken selector.
    engine, db_path = _make_engine()
    try:
        reg = {
            "id": "test-reg",
            "name": "Test",
            "enabled": True,
            "sources": [{"label": "watched_page", "url": "http://example.com/page", "type": "page_watch"}],
        }

        with patch("collector.main.fetch_source", return_value=[]):
            run_collection(engine, [reg], _base_cfg())

        with Session(engine) as session:
            rows = session.query(SourceErrorRow).filter_by(regulator_id="test-reg", source_label="watched_page").all()
            # Not an error (consecutive_failures stays 0) - but the stall
            # detector still counts the empty run, independent of the
            # scrape-only error check above.
            assert len(rows) == 1
            assert rows[0].consecutive_failures == 0
            assert rows[0].consecutive_empty_runs == 1
    finally:
        os.unlink(db_path)


def test_run_collection_completes_run_row_on_exception():
    """If fetch_source raises unexpectedly, the run row status must not remain 'running'."""
    engine, db_path = _make_engine()
    try:
        regulators = [
            {
                "id": "test-reg",
                "name": "Test",
                "enabled": True,
                "sources": [{"label": "s1", "url": "http://example.com"}],
            }
        ]

        def exploding_fetch(source, reg_id, flaresolverr_url, fetch_cfg):
            raise RuntimeError("network boom")

        with patch("collector.main.fetch_source", side_effect=exploding_fetch):
            run_collection(engine, regulators, _base_cfg())

        with Session(engine) as session:
            rows = session.query(RunRow).all()
            assert len(rows) == 1
            assert rows[0].status != "running"
    finally:
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# Concurrent-run lock tests
# ---------------------------------------------------------------------------

def test_acquire_lock_succeeds_when_unlocked():
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, ".collector.lock")
        lock_file = _acquire_lock(lock_path)
        try:
            assert lock_file is not None
        finally:
            _release_lock(lock_file)


def test_acquire_lock_fails_when_already_held():
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, ".collector.lock")
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = _acquire_lock(lock_path)
            assert result is None
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


def test_run_collection_locked_skips_when_another_run_holds_the_lock():
    # Simulates a manual --once run overlapping the scheduled cron job (two
    # separate processes, same shared lockfile on the data volume) - the
    # second one must skip entirely, not run concurrently against the same
    # SQLite file.
    engine, db_path = _make_engine()
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, ".collector.lock")
        holder = open(lock_path, "w")
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch("collector.main.run_collection") as mock_run:
                run_collection_locked(engine, [], _base_cfg(), lock_path)
            mock_run.assert_not_called()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
            os.unlink(db_path)


def test_run_collection_locked_runs_and_releases_when_lock_is_free():
    engine, db_path = _make_engine()
    with tempfile.TemporaryDirectory() as tmpdir:
        lock_path = os.path.join(tmpdir, ".collector.lock")
        try:
            with patch("collector.main.run_collection") as mock_run:
                run_collection_locked(engine, [], _base_cfg(), lock_path)
            mock_run.assert_called_once()

            # Lock must be released afterwards - a second call must also run.
            with patch("collector.main.run_collection") as mock_run_2:
                run_collection_locked(engine, [], _base_cfg(), lock_path)
            mock_run_2.assert_called_once()
        finally:
            os.unlink(db_path)


def test_run_collection_writes_rule_labels_for_matching_items(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from collector.storage import init_db
    from collector.main import run_collection
    from collector.schema import Item
    from regradar.rules import RulesEngine
    from regradar.taxonomy import build_taxonomy

    engine = init_db(str(tmp_path / "test.db"))
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    rules_path = tmp_path / "rules.yaml"
    rules_path.write_text(
        "rules:\n  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
    )
    rules_engine = RulesEngine.load(str(rules_path), taxonomy)

    now = datetime.now(timezone.utc)

    def fake_fetch_source(source, regulator_id, flaresolverr_url, config, rules_engine=None):
        item = Item(
            id="fixed-test-id", regulator_id="esma", source_label="all_news", title="New MiCA guidance",
            url="https://x", published_at=now, fetched_at=now, date_inferred=False, language="en",
            content_snippet="details on MiCA reporting", source_type="rss",
        )
        if rules_engine is not None:
            item.rule_labels = rules_engine.score(item.title, item.raw_content)
        return [item]

    monkeypatch.setattr("collector.main.fetch_source", fake_fetch_source)

    regulators = [{"id": "esma", "name": "ESMA", "enabled": True,
                   "sources": [{"type": "rss", "url": "https://x", "label": "all_news", "enabled": True}]}]
    cfg = {"user_agent": "t/1.0", "timeout": 5, "retries": 1, "flaresolverr_url": None}

    run_collection(engine, regulators, cfg, taxonomy=taxonomy, rules_engine=rules_engine)

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        row = conn.execute(sa_text(
            "SELECT axis, value, by, confidence FROM classifications WHERE item_id='fixed-test-id' AND by='rule'"
        )).fetchone()
    assert row == ("instrument", "mica", "rule", 1.0)  # explicit rule (0.9) + alias rule (0.7) sum, capped


def test_run_collection_without_rules_engine_writes_no_rule_labels(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from collector.storage import init_db
    from collector.main import run_collection
    from collector.schema import Item

    engine = init_db(str(tmp_path / "test.db"))
    now = datetime.now(timezone.utc)
    fake_item = Item(
        id="fixed-test-id-2", regulator_id="esma", source_label="all_news", title="New MiCA guidance",
        url="https://x", published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="details on MiCA reporting", source_type="rss",
    )
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [fake_item])
    regulators = [{"id": "esma", "name": "ESMA", "enabled": True,
                   "sources": [{"type": "rss", "url": "https://x", "label": "all_news", "enabled": True}]}]
    cfg = {"user_agent": "t/1.0", "timeout": 5, "retries": 1, "flaresolverr_url": None}

    run_collection(engine, regulators, cfg)  # rules_engine defaults to None

    from sqlalchemy import text as sa_text
    with engine.connect() as conn:
        count = conn.execute(sa_text(
            "SELECT COUNT(*) FROM classifications WHERE item_id='fixed-test-id-2' AND by='rule'"
        )).scalar()
    assert count == 0


# ---------------------------------------------------------------------------
# run_collection: detail-page fetch for new RSS items (issue #109)
# ---------------------------------------------------------------------------

def _rss_reg(detail_selector=None):
    reg = {"id": "hu-mnb", "name": "MNB", "enabled": True,
           "sources": [{"type": "rss", "url": "https://mnb.example/feed", "label": "news", "enabled": True}]}
    if detail_selector:
        reg["detail_selector"] = detail_selector
    return reg


def _rss_item(item_id, url="https://mnb.example/article/1"):
    from datetime import datetime, timezone
    from collector.schema import Item

    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id="hu-mnb", source_label="news", title="An item",
        url=url, published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="feed summary", source_type="rss",
        raw_content="<p>feed summary</p>", body_md="feed summary\n\n*Full text not collected.*",
    )


def test_run_collection_new_rss_item_gets_full_body_via_detail_selector(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _rss_item("new-item-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: {
            "status": "recovered", "body_md": "Full article body", "extractor": "markdownify/1.0",
            "content_html": "<div>Full article body</div>", "raw_content": "Full article body",
        },
    )

    run_collection(engine, [_rss_reg(".main-content")], _base_cfg())

    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT body_md, extractor FROM items WHERE id='new-item-1'")
        ).fetchone()
    assert row == ("Full article body", "markdownify/1.0")


def test_run_collection_known_rss_item_is_not_refetched(tmp_path, monkeypatch):
    from collector.storage import insert_item

    engine = init_db(str(tmp_path / "test.db"))
    item = _rss_item("existing-item-1")
    insert_item(engine, item)  # already in the DB, as if a prior run collected it

    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [_rss_item("existing-item-1")])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetch_detail_body must not be called")),
    )

    run_collection(engine, [_rss_reg(".main-content")], _base_cfg())  # must not raise

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='existing-item-1'")
        ).scalar()
    assert body_md == item.body_md  # unchanged


def test_run_collection_detail_fetch_failure_falls_back_to_summary_without_failing_source(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _rss_item("blocked-item-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: {"status": "blocked", "reason": "blocked: decoy page"},
    )

    run_collection(engine, [_rss_reg(".main-content")], _base_cfg())

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='blocked-item-1'")
        ).scalar()
    assert body_md == item.body_md  # kept the RSS summary

    with Session(engine) as session:
        run = session.query(RunRow).one()
        assert run.status == "success"  # not marked partial/failed
        assert session.query(SourceErrorRow).count() == 0  # source health untouched
        # Issue #109 follow-up: visible in the run log even though it never
        # touched status or source-health counters.
        assert json.loads(run.detail_fetch_failures) == ["hu-mnb: blocked x1"]


def test_run_collection_detail_fetch_exception_keeps_item_and_rest_of_feed(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    first, second = _rss_item("raises-item-1"), _rss_item("raises-item-2")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [first, second])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("malformed page")),
    )
    monkeypatch.setattr("collector.main._DETAIL_FETCH_DELAY_SECONDS", 0)

    run_collection(engine, [_rss_reg(".main-content")], _base_cfg())

    with engine.connect() as conn:
        assert conn.execute(sa_text("SELECT COUNT(*) FROM items")).scalar() == 2
    with Session(engine) as session:
        run = session.query(RunRow).one()
        assert run.status == "success"
        assert session.query(SourceErrorRow).count() == 0
        assert json.loads(run.detail_fetch_failures) == ["hu-mnb: error x2"]


def test_run_collection_rss_source_without_detail_selector_is_unchanged(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _rss_item("no-selector-item-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetch_detail_body must not be called")),
    )

    run_collection(engine, [_rss_reg(detail_selector=None)], _base_cfg())  # no detail_selector configured

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='no-selector-item-1'")
        ).scalar()
    assert body_md == item.body_md  # unchanged


def test_run_collection_detail_fetch_budget_exceeded_items_still_inserted(tmp_path, monkeypatch):
    # Issue #112: a per-run wall-clock budget for detail fetches. A fake
    # clock puts the deadline between the two items' checks, so the first
    # gets its detail fetch and the second is reported as budget_exceeded
    # but still inserted with its RSS summary intact.
    engine = init_db(str(tmp_path / "test.db"))
    first = _rss_item("budget-item-1", url="https://mnb.example/article/1")
    second = _rss_item("budget-item-2", url="https://mnb.example/article/2")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [first, second])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: {
            "status": "recovered", "body_md": "Full article body", "extractor": "markdownify/1.0",
            "content_html": "<div>Full article body</div>", "raw_content": "Full article body",
        },
    )
    monkeypatch.setattr("collector.main._DETAIL_FETCH_DELAY_SECONDS", 0)

    # clock_fn() brackets each detail fetch: item 1 runs from 0 to 20, so 20s
    # of the 10s budget is spent and item 2's fetch is skipped. Time outside
    # detail fetches is never sampled, so it can't use up the budget.
    clock_values = iter([0.0, 20.0])

    run_collection(
        engine, [_rss_reg(".main-content")], _base_cfg(),
        detail_fetch_budget_seconds=10, clock_fn=lambda: next(clock_values),
    )

    with engine.connect() as conn:
        rows = dict(conn.execute(sa_text("SELECT id, body_md FROM items")).fetchall())
    assert rows["budget-item-1"] == "Full article body"  # within budget - detail fetch ran
    assert rows["budget-item-2"] == second.body_md  # budget spent - RSS summary kept, still inserted

    with Session(engine) as session:
        run = session.query(RunRow).one()
        assert run.status == "success"  # budget_exceeded is not a run error
        assert json.loads(run.detail_fetch_failures) == ["hu-mnb: budget_exceeded x1"]


# ---------------------------------------------------------------------------
# Issue #115: scrape items from a detail_selector regulator whose listing
# produced no real body ("Full text not collected" / extractor raw/1)
# ---------------------------------------------------------------------------

def _scrape_reg(detail_selector=".article-body", extract_pdf_text=False):
    scrape_cfg = {"item_selector": ".item", "title_selector": "h2", "link_selector": "a"}
    if extract_pdf_text:
        scrape_cfg["extract_pdf_text"] = True
    reg = {"id": "mt-mfsa", "name": "MFSA", "enabled": True,
           "sources": [{"type": "scrape", "url": "https://mfsa.example/news", "label": "news",
                        "enabled": True, "scrape": scrape_cfg}]}
    if detail_selector:
        reg["detail_selector"] = detail_selector
    return reg


def _scrape_placeholder_item(item_id, url="https://mfsa.example/news/1"):
    from datetime import datetime, timezone
    from collector.schema import Item

    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id="mt-mfsa", source_label="news", title="Regulatory action",
        url=url, published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="", source_type="scrape",
        raw_content="", body_md=f"> Full text not collected. See {url}.", extractor="raw/1",
    )


def _scrape_real_body_item(item_id, url="https://mfsa.example/news/2"):
    from datetime import datetime, timezone
    from collector.schema import Item

    now = datetime.now(timezone.utc)
    return Item(
        id=item_id, regulator_id="mt-mfsa", source_label="news", title="Warning notice",
        url=url, published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="Full notice text", source_type="scrape",
        raw_content="Full notice text", body_md="Full notice text", extractor="markdownify/1.0",
    )


def test_run_collection_new_scrape_placeholder_item_gets_full_body(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _scrape_placeholder_item("mfsa-item-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: {
            "status": "recovered", "body_md": "Full article body", "extractor": "markdownify/1.0",
            "content_html": "<div>Full article body</div>", "raw_content": "Full article body",
        },
    )

    run_collection(engine, [_scrape_reg()], _base_cfg())

    with engine.connect() as conn:
        row = conn.execute(
            sa_text("SELECT body_md, extractor FROM items WHERE id='mfsa-item-1'")
        ).fetchone()
    assert row == ("Full article body", "markdownify/1.0")


def test_run_collection_scrape_item_with_real_listing_body_is_not_refetched(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _scrape_real_body_item("mfsa-item-2")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetch_detail_body must not be called")),
    )

    run_collection(engine, [_scrape_reg()], _base_cfg())  # must not raise

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='mfsa-item-2'")
        ).scalar()
    assert body_md == item.body_md  # unchanged


def test_run_collection_scrape_pdf_source_is_not_refetched(tmp_path, monkeypatch):
    # extract_pdf_text sources: the item's href IS the document, so a
    # detail_selector fetch of it would find nothing - skipped outright even
    # though the listing produced no real body (e.g. the PDF download failed).
    engine = init_db(str(tmp_path / "test.db"))
    item = _scrape_placeholder_item("mfsa-pdf-item-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetch_detail_body must not be called")),
    )

    run_collection(engine, [_scrape_reg(extract_pdf_text=True)], _base_cfg())  # must not raise

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='mfsa-pdf-item-1'")
        ).scalar()
    assert body_md == item.body_md  # unchanged


def test_run_collection_scrape_detail_fetch_failure_keeps_placeholder_and_is_reported(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _scrape_placeholder_item("mfsa-blocked-item-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: {"status": "blocked", "reason": "blocked: cloudflare challenge"},
    )

    run_collection(engine, [_scrape_reg()], _base_cfg())

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='mfsa-blocked-item-1'")
        ).scalar()
    assert body_md == item.body_md  # placeholder kept

    with Session(engine) as session:
        run = session.query(RunRow).one()
        assert run.status == "success"
        assert json.loads(run.detail_fetch_failures) == ["mt-mfsa: blocked x1"]


def test_run_collection_scrape_source_without_detail_selector_is_unchanged(tmp_path, monkeypatch):
    engine = init_db(str(tmp_path / "test.db"))
    item = _scrape_placeholder_item("mfsa-no-selector-1")
    monkeypatch.setattr("collector.main.fetch_source", lambda *a, **kw: [item])
    monkeypatch.setattr(
        "collector.main.fetch_detail_body",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("fetch_detail_body must not be called")),
    )

    run_collection(engine, [_scrape_reg(detail_selector=None)], _base_cfg())

    with engine.connect() as conn:
        body_md = conn.execute(
            sa_text("SELECT body_md FROM items WHERE id='mfsa-no-selector-1'")
        ).scalar()
    assert body_md == item.body_md  # unchanged
