"""Tests for the collector's taxonomy integration: failing loudly at
startup on bad config, and writing source-default classifications on
insert (../wiki/content/concept-taxonomy-classification.md scope)."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest
from sqlalchemy import text as sa_text
from datetime import datetime, timezone

from collector.main import run_collection
from collector.storage import init_db
from regradar.taxonomy import build_taxonomy, load_and_exit_on_error


def _make_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name), f.name


def _base_cfg():
    return {"user_agent": "test/1.0", "timeout": 10, "retries": 1, "flaresolverr_url": None}


def test_load_and_exit_on_error_exits_on_invalid_taxonomy_yaml(tmp_path):
    bad_taxonomy = tmp_path / "taxonomy.yaml"
    bad_taxonomy.write_text("version: 1\naxes: []\n")  # empty axes list is invalid
    jurisdictions = tmp_path / "jurisdictions.yaml"
    jurisdictions.write_text("- id: eu\n  name: European Union\n")
    with pytest.raises(SystemExit) as exc_info:
        load_and_exit_on_error(str(bad_taxonomy), str(jurisdictions))
    assert exc_info.value.code == 1


def test_load_and_exit_on_error_exits_on_unknown_regulator_jurisdiction():
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    assert taxonomy is not None  # sanity: real config is valid
    with pytest.raises(SystemExit) as exc_info:
        load_and_exit_on_error(
            "config/taxonomy.yaml", "config/jurisdictions.yaml",
            regulators=[{"id": "xx-reg", "jurisdiction": "atlantis"}],
            regulators_path="config/regulators.yaml",
        )
    assert exc_info.value.code == 1


def test_run_collection_writes_source_default_classifications_on_insert():
    engine, db_path = _make_engine()
    try:
        taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")

        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg, **kwargs):
            from collector.schema import Item, make_item_id
            now = datetime.now(timezone.utc)
            return [Item(
                id=make_item_id(reg_id, "https://example.com/a", now), regulator_id=reg_id,
                source_label=source["label"], title="A warning notice", url="https://example.com/a",
                published_at=now, fetched_at=now, date_inferred=False, language="en",
                content_snippet="...", source_type="rss",
            )]

        reg = {
            "id": "test-reg", "name": "Test", "enabled": True,
            "sources": [{
                "label": "warnings", "url": "http://example.com/warnings",
                "labels": {"document_type": "warning", "instrument": ["mica"]},
            }],
        }

        with patch("collector.main.fetch_source", side_effect=fake_fetch):
            run_collection(engine, [reg], _base_cfg(), taxonomy=taxonomy)

        with engine.connect() as conn:
            rows = conn.execute(
                sa_text("SELECT axis, value, by, confidence FROM classifications ORDER BY axis, value")
            ).fetchall()
            labels = conn.execute(sa_text("SELECT labels FROM items")).scalar()

        assert [tuple(r) for r in rows] == [
            ("document_type", "warning", "source_default", 1.0),
            ("instrument", "mica", "source_default", 0.6),
        ]
        assert json.loads(labels) == {"document_type": "warning", "instrument": ["mica"]}
    finally:
        os.unlink(db_path)


def test_run_collection_without_taxonomy_skips_classification_silently():
    """taxonomy=None (e.g. an older caller) must not write classifications
    or blow up - source defaults are simply not applied."""
    engine, db_path = _make_engine()
    try:
        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg, **kwargs):
            from collector.schema import Item, make_item_id
            now = datetime.now(timezone.utc)
            return [Item(
                id=make_item_id(reg_id, "https://example.com/a", now), regulator_id=reg_id,
                source_label=source["label"], title="A", url="https://example.com/a",
                published_at=now, fetched_at=now, date_inferred=False, language="en",
                content_snippet="...", source_type="rss",
            )]

        reg = {
            "id": "test-reg", "name": "Test", "enabled": True,
            "sources": [{"label": "warnings", "url": "http://example.com/warnings",
                         "labels": {"document_type": "warning"}}],
        }
        with patch("collector.main.fetch_source", side_effect=fake_fetch):
            run_collection(engine, [reg], _base_cfg())  # no taxonomy kwarg

        with engine.connect() as conn:
            count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications")).scalar()
        assert count == 0
    finally:
        os.unlink(db_path)


def test_run_collection_uses_a_new_axis_configured_confidence_with_no_code_change(tmp_path):
    """Source-default confidence must come from taxonomy.yaml, not a branch
    on axis name in collector code - a brand new axis with its own
    source_default_confidence is applied with zero code changes."""
    taxonomy_path = tmp_path / "taxonomy.yaml"
    taxonomy_path.write_text("""\
version: 1
axes:
  - id: topic
    cardinality: one
    source_default_confidence: 0.42
    values:
      - id: staking
        name: Staking
""")
    jurisdictions_path = tmp_path / "jurisdictions.yaml"
    jurisdictions_path.write_text("- id: eu\n  name: European Union\n")
    taxonomy = build_taxonomy(str(taxonomy_path), str(jurisdictions_path))

    engine, db_path = _make_engine()
    try:
        def fake_fetch(source, reg_id, flaresolverr_url, fetch_cfg, **kwargs):
            from collector.schema import Item, make_item_id
            now = datetime.now(timezone.utc)
            return [Item(
                id=make_item_id(reg_id, "https://example.com/a", now), regulator_id=reg_id,
                source_label=source["label"], title="A", url="https://example.com/a",
                published_at=now, fetched_at=now, date_inferred=False, language="en",
                content_snippet="...", source_type="rss",
            )]

        reg = {
            "id": "test-reg", "name": "Test", "enabled": True,
            "sources": [{"label": "s", "url": "http://example.com/s", "labels": {"topic": "staking"}}],
        }
        with patch("collector.main.fetch_source", side_effect=fake_fetch):
            run_collection(engine, [reg], _base_cfg(), taxonomy=taxonomy)

        with engine.connect() as conn:
            confidence = conn.execute(sa_text("SELECT confidence FROM classifications WHERE axis='topic'")).scalar()
        assert confidence == 0.42
    finally:
        os.unlink(db_path)
