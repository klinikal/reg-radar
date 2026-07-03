import fcntl
import sys
import tempfile
import os
import pytest
from unittest.mock import patch, MagicMock

from collector.main import load_regulators, run_collection, run_collection_locked, _acquire_lock, _release_lock
from collector.storage import init_db, record_run_start, RunRow, SourceErrorRow
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

        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg):
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

        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg):
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
            assert len(rows) == 0
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
            assert len(rows) == 0
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
