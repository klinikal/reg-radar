from datetime import datetime, timezone

from sqlalchemy import text

from collector.adapters.http import _note_fetch_status
from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
import scripts.reprocess_bodies as reprocess_bodies_module
from scripts.reprocess_bodies import (
    _current_extractor,
    _RAW_CONTENT_EXTRACTOR,
    backfill_from_raw_content,
    refetch,
    reprocess,
)


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


def _make_pre_v2_item(item_id_suffix, raw_content="hello world", source_type="scrape", body_md=""):
    now = datetime.now(timezone.utc)
    url = f"https://example.com/pre-v2-{item_id_suffix}"
    return Item(
        id=make_item_id("fca", url, now),
        regulator_id="fca", source_label="news", title="t",
        url=url, published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="s",
        source_type=source_type, raw_content=raw_content, body_md=body_md, extractor="",
    )


def test_backfill_from_raw_content_dry_run_reports_count_without_writing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = _make_engine(tmp_path)
    item = _make_pre_v2_item("1")
    insert_item(engine, item)

    count = backfill_from_raw_content(engine, dry_run=True, limit=None)

    assert count == 1
    with engine.connect() as conn:
        body_md = conn.execute(text("SELECT body_md FROM items WHERE id=:id"), {"id": item.id}).scalar()
    assert body_md == ""  # unchanged - dry run wrote nothing


def test_backfill_from_raw_content_derives_body_and_marks_extractor(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = _make_engine(tmp_path)
    item = _make_pre_v2_item("1", raw_content="line one\nwrapped\n\nsecond para", source_type="scrape")
    insert_item(engine, item)

    processed = backfill_from_raw_content(engine, dry_run=False, limit=None)

    assert processed == 1
    with engine.connect() as conn:
        row = conn.execute(text("SELECT body_md, extractor FROM items WHERE id=:id"), {"id": item.id}).fetchone()
    assert row[0] == "line one wrapped\n\nsecond para"
    assert row[1] == _RAW_CONTENT_EXTRACTOR


def test_backfill_from_raw_content_skips_items_with_an_artefact(tmp_path, monkeypatch):
    # Artefact-bearing items are reprocess()'s job, not this backfill's -
    # skip them even if body_md happens to be empty.
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = _make_engine(tmp_path)
    item = _make_item(b"<p>hello</p>", "text/html", "")
    insert_item(engine, item)
    with engine.connect() as conn:
        conn.execute(text("UPDATE items SET body_md = '' WHERE id = :id"), {"id": item.id})
        conn.commit()

    processed = backfill_from_raw_content(engine, dry_run=False, limit=None)

    assert processed == 0


def test_backfill_from_raw_content_skips_items_that_already_have_a_body(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = _make_engine(tmp_path)
    item = _make_pre_v2_item("1", body_md="already has one")
    insert_item(engine, item)

    processed = backfill_from_raw_content(engine, dry_run=False, limit=None)

    assert processed == 0


def test_backfill_from_raw_content_respects_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = _make_engine(tmp_path)
    for i in range(3):
        insert_item(engine, _make_pre_v2_item(str(i)))

    processed = backfill_from_raw_content(engine, dry_run=False, limit=2)

    assert processed == 2


def test_reprocess_respects_limit(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    engine = _make_engine(tmp_path)
    for i in range(3):
        item = _make_item(f"<p>item {i}</p>".encode(), "text/html", "markdownify/0.0.1", suffix=str(i))
        insert_item(engine, item)

    processed = reprocess(engine, str(artefacts_dir), dry_run=False, limit=2)

    assert processed == 2


# --- --refetch (issue #17) -------------------------------------------------

_HU_MNB = {"id": "hu-mnb", "detail_selector": ".main-content"}


def _make_empty_item(item_id_suffix, regulator_id="hu-mnb", url=None):
    """An item with body_md, raw_content AND content_snippet all empty - the
    only shape --refetch's WHERE-guard should ever pick up."""
    now = datetime.now(timezone.utc)
    url = url or f"https://example.com/{regulator_id}-{item_id_suffix}"
    return Item(
        id=make_item_id(regulator_id, url, now),
        regulator_id=regulator_id, source_label="news", title="t",
        url=url, published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="",
        source_type="rss", raw_content="", body_md="", extractor="",
    )


def test_refetch_recovers_body_via_detail_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = init_db(str(tmp_path / "test.db"))
    item = _make_empty_item("1")
    insert_item(engine, item)
    monkeypatch.setattr(
        reprocess_bodies_module, "fetch_html",
        lambda *a, **k: "<html><div class='main-content'><p>Recovered body</p></div></html>",
    )

    report = refetch(engine, [_HU_MNB], dry_run=False, limit=None, regulator_id=None,
                      delay=0, user_agent="ua", timeout=5, retries=1)

    assert report["recovered"] == 1
    with engine.connect() as conn:
        body_md = conn.execute(text("SELECT body_md FROM items WHERE id=:id"), {"id": item.id}).scalar()
    assert "Recovered body" in body_md


def test_refetch_never_touches_an_item_that_already_has_a_body(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = init_db(str(tmp_path / "test.db"))
    item = _make_empty_item("1")
    item.body_md = "already has one"
    insert_item(engine, item)
    monkeypatch.setattr(
        reprocess_bodies_module, "fetch_html",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch_html must not be called")),
    )

    report = refetch(engine, [_HU_MNB], dry_run=False, limit=None, regulator_id=None,
                      delay=0, user_agent="ua", timeout=5, retries=1)

    assert report["recovered"] == 0
    with engine.connect() as conn:
        body_md = conn.execute(text("SELECT body_md FROM items WHERE id=:id"), {"id": item.id}).scalar()
    assert body_md == "already has one"


def test_refetch_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = init_db(str(tmp_path / "test.db"))
    item = _make_empty_item("1")
    insert_item(engine, item)
    monkeypatch.setattr(
        reprocess_bodies_module, "fetch_html",
        lambda *a, **k: "<html><div class='main-content'><p>Recovered body</p></div></html>",
    )

    report = refetch(engine, [_HU_MNB], dry_run=True, limit=None, regulator_id=None,
                      delay=0, user_agent="ua", timeout=5, retries=1)

    assert report["recovered"] == 1
    with engine.connect() as conn:
        body_md = conn.execute(text("SELECT body_md FROM items WHERE id=:id"), {"id": item.id}).scalar()
    assert body_md == ""  # unchanged - dry run wrote nothing


def test_refetch_reports_404(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = init_db(str(tmp_path / "test.db"))
    item = _make_empty_item("1")
    insert_item(engine, item)

    def fake_fetch_html(*a, **k):
        _note_fetch_status(404)
        return None

    monkeypatch.setattr(reprocess_bodies_module, "fetch_html", fake_fetch_html)

    report = refetch(engine, [_HU_MNB], dry_run=False, limit=None, regulator_id=None,
                      delay=0, user_agent="ua", timeout=5, retries=1)

    assert report["recovered"] == 0
    assert report["not_found"] == 1
    assert report["remaining_empty"] == [{"id": item.id, "reason": "404 not found"}]


def test_refetch_reports_missing_detail_selector(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    engine = init_db(str(tmp_path / "test.db"))
    item = _make_empty_item("1", regulator_id="es-bde")
    insert_item(engine, item)
    monkeypatch.setattr(
        reprocess_bodies_module, "fetch_html",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fetch_html must not be called")),
    )

    report = refetch(engine, [{"id": "es-bde"}], dry_run=False, limit=None, regulator_id=None,
                      delay=0, user_agent="ua", timeout=5, retries=1)

    assert report["recovered"] == 0
    assert report["skipped_no_selector"] == 1
    assert report["remaining_empty"] == [
        {"id": item.id, "reason": "no detail_selector configured for es-bde"}
    ]
