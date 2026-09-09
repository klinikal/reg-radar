import os
import tempfile
import time
from datetime import datetime, timezone

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator
from regradar.cli import main


def _setup_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    upsert_regulator(engine, {
        "id": "fca", "name": "FCA", "full_name": "Financial Conduct Authority",
        "jurisdiction": "GB", "jurisdiction_type": "national", "website": "https://fca.org.uk",
        "mica_competent_authority": False, "enabled": True,
    })
    now = datetime.now(timezone.utc)
    item = Item(
        id=make_item_id("fca", "https://example.com/render", now),
        regulator_id="fca", source_label="news", title="Renderable notice",
        url="https://example.com/render", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="snip",
        source_type="scrape", raw_content="<p>hello world</p>",
        body_md="hello world", extractor="raw/1",
    )
    insert_item(engine, item)
    return db_path, item.id


def test_render_single_item_writes_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["render", item_id, "--out", str(out_dir), "--db-path", db_path])

    assert rc == 0
    files = list(out_dir.rglob("*.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert content.startswith("---\n")
    assert "hello world" in content


def test_render_unknown_item_id_returns_error(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["render", "does-not-exist", "--out", str(out_dir), "--db-path", db_path])

    assert rc == 1


def test_render_all_is_idempotent_on_unchanged_items(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    assert main(["render", "--all", "--out", str(out_dir), "--db-path", db_path]) == 0
    files = list(out_dir.rglob("*.md"))
    assert len(files) == 1
    mtime_before = files[0].stat().st_mtime

    time.sleep(1.05)  # mtime resolution on some filesystems is ~1s
    assert main(["render", "--all", "--out", str(out_dir), "--db-path", db_path]) == 0

    mtime_after = files[0].stat().st_mtime
    assert mtime_before == mtime_after


def test_render_requires_item_id_or_all(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)

    rc = main(["render", "--out", str(tmp_path / "out"), "--db-path", db_path])

    assert rc == 2
