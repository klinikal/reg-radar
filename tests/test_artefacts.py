import hashlib
import os
import tempfile
from datetime import datetime, timezone

import pytest

from collector.schema import Item, make_item_id
from collector.storage import get_artefact, init_db, insert_item, update_item_body


def _make_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _make_item(raw_bytes=b"<p>hello</p>", mime="text/html", **overrides):
    now = datetime.now(timezone.utc)
    fields = dict(
        id=make_item_id("fca", "https://example.com/artefact", now),
        regulator_id="fca",
        source_label="news",
        title="Artefact test",
        url="https://example.com/artefact",
        published_at=now,
        fetched_at=now,
        date_inferred=False,
        language="en",
        content_snippet="hello",
        source_type="scrape",
        raw_content="hello",
        body_md="hello",
        extractor="markdownify/1.2.3",
        raw_bytes=raw_bytes,
        mime=mime,
    )
    fields.update(overrides)
    return Item(**fields)


def test_insert_item_writes_artefact_and_row(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path))
    engine = _make_engine()
    item = _make_item(raw_bytes=b"pdf-bytes-here", mime="application/pdf")

    insert_item(engine, item)

    artefact = get_artefact(engine, item.id)
    assert artefact is not None
    assert artefact["mime"] == "application/pdf"
    assert artefact["bytes"] == len(b"pdf-bytes-here")
    assert artefact["sha256"] == hashlib.sha256(b"pdf-bytes-here").hexdigest()

    on_disk = tmp_path / artefact["path"]
    assert on_disk.exists()
    assert on_disk.read_bytes() == b"pdf-bytes-here"
    assert artefact["path"].startswith(item.id[:2] + "/")


def test_insert_item_without_raw_bytes_writes_no_artefact(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path))
    engine = _make_engine()
    item = _make_item(raw_bytes=None, mime=None)

    insert_item(engine, item)

    assert get_artefact(engine, item.id) is None
    assert list(tmp_path.iterdir()) == []


def test_store_artefacts_false_skips_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("STORE_ARTEFACTS", "false")
    engine = _make_engine()
    item = _make_item()

    insert_item(engine, item)

    assert get_artefact(engine, item.id) is None
    assert list(tmp_path.iterdir()) == []


def test_max_artefact_bytes_skips_oversized_artefact(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_ARTEFACT_BYTES", "5")
    engine = _make_engine()
    item = _make_item(raw_bytes=b"this is way more than five bytes")

    insert_item(engine, item)

    assert get_artefact(engine, item.id) is None


def test_update_item_body_bumps_updated_at(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path))
    engine = _make_engine()
    item = _make_item(raw_bytes=None, mime=None)
    insert_item(engine, item)

    from sqlalchemy import text
    with engine.connect() as conn:
        before = conn.execute(text("SELECT updated_at FROM items WHERE id=:id"), {"id": item.id}).scalar()

    update_item_body(engine, item.id, "new **body**", "markdownify/9.9.9")

    with engine.connect() as conn:
        row = conn.execute(text("SELECT body_md, extractor, updated_at FROM items WHERE id=:id"), {"id": item.id}).fetchone()
    assert row[0] == "new **body**"
    assert row[1] == "markdownify/9.9.9"
    assert row[2] != before


def test_update_item_body_on_missing_item_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path))
    engine = _make_engine()
    update_item_body(engine, "does-not-exist", "body", "markdownify/1.2.3")  # must not raise


def test_artefact_write_failure_still_inserts_the_item(tmp_path, monkeypatch):
    # ARTEFACTS_DIR points at a plain file, not a directory - the write's
    # mkdir(parents=True) then fails with NotADirectoryError. This must not
    # take the whole item down with it (a later item in the same source's
    # loop would otherwise be lost too - collector/main.py wraps the loop
    # per source, not per item).
    not_a_dir = tmp_path / "not_a_directory"
    not_a_dir.write_text("i'm a file")
    monkeypatch.setenv("ARTEFACTS_DIR", str(not_a_dir))
    engine = _make_engine()
    item = _make_item()

    result = insert_item(engine, item)

    assert result is True
    from sqlalchemy import text
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM items WHERE id=:id"), {"id": item.id}).fetchone()
    assert row is not None
    assert get_artefact(engine, item.id) is None
