from datetime import datetime, timezone

from sqlalchemy import text

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from scripts.reprocess_bodies import _current_extractor, reprocess


def _make_engine(tmp_path):
    return init_db(str(tmp_path / "test.db"))


def _make_item(raw_bytes, mime, extractor, suffix=""):
    now = datetime.now(timezone.utc)
    url = f"https://example.com/{extractor}{suffix}"
    return Item(
        id=make_item_id("fca", url, now),
        regulator_id="fca", source_label="news", title="t",
        url=url, published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="s",
        source_type="scrape", raw_content="hello world",
        body_md="hello world", extractor=extractor,
        raw_bytes=raw_bytes, mime=mime,
    )


def test_dry_run_reports_count_without_writing(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    engine = _make_engine(tmp_path)
    stale = _make_item(b"<p>hello <b>world</b></p>", "text/html", "markdownify/0.0.1")
    insert_item(engine, stale)

    count = reprocess(engine, str(artefacts_dir), dry_run=True, limit=None)

    assert count == 1
    with engine.connect() as conn:
        body_md = conn.execute(text("SELECT body_md FROM items WHERE id=:id"), {"id": stale.id}).scalar()
    assert body_md == "hello world"  # unchanged - dry run wrote nothing


def test_reprocess_updates_stale_html_item(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    engine = _make_engine(tmp_path)
    stale = _make_item(b"<p>hello <b>world</b></p>", "text/html", "markdownify/0.0.1")
    insert_item(engine, stale)

    processed = reprocess(engine, str(artefacts_dir), dry_run=False, limit=None)

    assert processed == 1
    with engine.connect() as conn:
        row = conn.execute(text("SELECT body_md, extractor FROM items WHERE id=:id"), {"id": stale.id}).fetchone()
    assert row[0] == "hello **world**"
    assert row[1] == _current_extractor("text/html")


def test_reprocess_skips_items_already_current(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    engine = _make_engine(tmp_path)
    current = _make_item(b"<p>hello</p>", "text/html", _current_extractor("text/html"))
    insert_item(engine, current)

    processed = reprocess(engine, str(artefacts_dir), dry_run=False, limit=None)

    assert processed == 0


def test_reprocess_respects_limit(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    engine = _make_engine(tmp_path)
    for i in range(3):
        item = _make_item(f"<p>item {i}</p>".encode(), "text/html", "markdownify/0.0.1", suffix=str(i))
        insert_item(engine, item)

    processed = reprocess(engine, str(artefacts_dir), dry_run=False, limit=2)

    assert processed == 2
