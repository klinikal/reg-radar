import sys
import tempfile
import os
import pytest
from unittest.mock import patch, MagicMock

from collector.main import load_regulators, run_collection
from collector.storage import init_db, record_run_start, RunRow
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
