import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item, upsert_regulator
from regradar.changes import encode_cursor
from regradar.cli import main

SCHEMA = json.loads((Path(__file__).parent.parent / "regradar" / "schema" / "document-v1.json").read_text())


def _setup_db(tmp_path, with_artefact=False):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    upsert_regulator(engine, {
        "id": "fca", "name": "FCA", "full_name": "Financial Conduct Authority",
        "jurisdiction": "GB", "jurisdiction_type": "national", "website": "https://fca.org.uk",
        "mica_competent_authority": False, "enabled": True,
    })
    now = datetime.now(timezone.utc)
    kwargs = dict(
        id=make_item_id("fca", "https://example.com/export", now),
        regulator_id="fca", source_label="news", title="Exportable notice",
        url="https://example.com/export", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="snip",
        source_type="scrape", raw_content="<p>hello world</p>",
        body_md="hello world", extractor="raw/1",
    )
    if with_artefact:
        kwargs["raw_bytes"] = b"<p>hello world</p>"
        kwargs["mime"] = "text/html"
    item = Item(**kwargs)
    insert_item(engine, item)
    return db_path, item.id


def test_export_markdown_produces_section3_layout(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])

    assert rc == 0
    files = list((out_dir / "export").rglob("*.md"))
    assert len(files) == 1
    assert "fca" in str(files[0])
    content = files[0].read_text()
    assert content.startswith("---\n")
    assert "hello world" in content


def test_export_markdown_second_run_touches_no_files_third_run_writes_only_new_item(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc1 = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])
    assert rc1 == 0
    files1 = sorted((out_dir / "export").rglob("*.md"))
    assert len(files1) == 1
    mtimes1 = {p: p.stat().st_mtime_ns for p in files1}

    # Second run: no new data.
    rc2 = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])
    assert rc2 == 0
    files2 = sorted((out_dir / "export").rglob("*.md"))
    mtimes2 = {p: p.stat().st_mtime_ns for p in files2}
    assert mtimes1 == mtimes2

    # Third run: one new item.
    from collector.storage import init_db as _init_db
    from collector.storage import insert_item as _insert_item
    engine = _init_db(db_path)
    now = datetime.now(timezone.utc)
    new_item = Item(
        id=make_item_id("fca", "https://example.com/export-2", now),
        regulator_id="fca", source_label="news", title="Second notice",
        url="https://example.com/export-2", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="snip2",
        source_type="scrape", raw_content="<p>second</p>", body_md="second", extractor="raw/1",
    )
    _insert_item(engine, new_item)

    rc3 = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])
    assert rc3 == 0
    files3 = sorted((out_dir / "export").rglob("*.md"))
    assert len(files3) == 2
    # Every file present before the third run is untouched.
    for p in files1:
        assert p.stat().st_mtime_ns == mtimes1[p]
    new_files = set(files3) - set(files1)
    assert len(new_files) == 1


def test_export_jsonl_matches_document_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])

    assert rc == 0
    jsonl_path = out_dir / "export.jsonl"
    assert jsonl_path.exists()
    lines = jsonl_path.read_text().strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    jsonschema.validate(doc, SCHEMA)


def test_export_jsonl_second_run_with_no_changes_leaves_mtime_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc1 = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])
    assert rc1 == 0
    jsonl_path = out_dir / "export.jsonl"
    mtime1 = jsonl_path.stat().st_mtime_ns

    rc2 = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])
    assert rc2 == 0
    assert jsonl_path.stat().st_mtime_ns == mtime1


def test_export_jsonl_is_a_snapshot_one_line_per_id_latest_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc1 = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])
    assert rc1 == 0

    # A second item, plus an update to the first item's title (bumps
    # updated_at) - the snapshot should end up with exactly one line per
    # id: two ids total, the first one carrying the new title.
    from collector.storage import init_db as _init_db
    from collector.storage import insert_item as _insert_item
    engine = _init_db(db_path)
    now = datetime.now(timezone.utc)
    second = Item(
        id=make_item_id("fca", "https://example.com/export-2", now),
        regulator_id="fca", source_label="news", title="Second notice",
        url="https://example.com/export-2", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="snip2",
        source_type="scrape", raw_content="<p>second</p>", body_md="second", extractor="raw/1",
    )
    _insert_item(engine, second)
    from collector.storage import update_item_body
    update_item_body(engine, item_id, "hello world, updated", "raw/1")

    rc2 = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])
    assert rc2 == 0

    lines = (out_dir / "export.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    docs_by_id = {json.loads(line)["id"]: json.loads(line) for line in lines}
    assert set(docs_by_id) == {item_id, second.id}
    assert docs_by_id[item_id]["body_md"] == "hello world, updated"


def test_export_with_originals_copies_artefact(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    db_path, item_id = _setup_db(tmp_path, with_artefact=True)
    out_dir = tmp_path / "out"

    rc = main(["export", "--format", "markdown", "--out", str(out_dir), "--with-originals", "--db-path", db_path])

    assert rc == 0
    copied = list((out_dir / "originals").rglob("*.html"))
    assert len(copied) == 1


def test_export_without_originals_flag_does_not_copy(tmp_path, monkeypatch):
    artefacts_dir = tmp_path / "originals"
    monkeypatch.setenv("ARTEFACTS_DIR", str(artefacts_dir))
    db_path, item_id = _setup_db(tmp_path, with_artefact=True)
    out_dir = tmp_path / "out"

    rc = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])

    assert rc == 0
    assert not (out_dir / "originals").exists()


def test_export_persists_cursor_and_since_overrides_it(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc1 = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])
    assert rc1 == 0
    cursor_path = out_dir / ".reg-radar-cursor"
    assert cursor_path.exists()
    persisted = cursor_path.read_text().strip()
    assert "|" in persisted

    # --since overrides the persisted cursor - passing the persisted one
    # back in as --since (a full catch-up from "the beginning") re-exports
    # nothing new since it's identical to the current head.
    rc2 = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path, "--since", persisted])
    assert rc2 == 0


def test_export_unparseable_persisted_cursor_falls_back_to_full_export(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    (out_dir / ".reg-radar-cursor").write_text("not-a-valid-cursor")

    with caplog.at_level("WARNING"):
        rc = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])

    assert rc == 0
    assert "unparseable" in caplog.text
    files = list((out_dir / "export").rglob("*.md"))
    assert len(files) == 1


def test_export_profile_narrows_items(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "uk-fca", "name": "FCA", "full_name": "FCA", "jurisdiction": "gb"})
    now = datetime.now(timezone.utc)
    insert_item(engine, Item(
        id=make_item_id("esma", "https://x/1", now), regulator_id="esma", source_label="news",
        title="EU item", url="https://x/1", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="s", source_type="rss", raw_content="r", body_md="b", extractor="raw/1",
    ))
    insert_item(engine, Item(
        id=make_item_id("uk-fca", "https://x/2", now), regulator_id="uk-fca", source_label="news",
        title="UK item", url="https://x/2", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="s", source_type="rss", raw_content="r", body_md="b", extractor="raw/1",
    ))
    out_dir = tmp_path / "out"

    rc = main([
        "export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path,
        "--profile", "eu-casp", "--profiles-dir", "config/profiles",
        "--taxonomy-path", "config/taxonomy.yaml", "--jurisdictions-path", "config/jurisdictions.yaml",
        "--activities-path", "config/activities.yaml",
    ])
    assert rc == 0
    lines = (out_dir / "export.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["regulator"] == "esma"


def test_export_repeatable_format_writes_both_from_one_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc = main([
        "export", "--format", "markdown", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path,
    ])

    assert rc == 0
    md_files = list((out_dir / "export").rglob("*.md"))
    assert len(md_files) == 1
    lines = (out_dir / "export.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    # One shared cursor for both formats, not one each.
    assert (out_dir / ".reg-radar-cursor").exists()


def test_export_second_format_with_no_prior_output_falls_back_to_full_export(tmp_path, monkeypatch, caplog):
    """Running --format markdown, then a later --format jsonl call into the
    SAME --out, must not silently produce an empty export.jsonl just
    because markdown's earlier run already advanced the persisted cursor
    past everything. (regradar/exporter.py's module docstring recommends
    --format markdown --format jsonl in one call instead - this is the
    safety net for when that isn't followed.)
    """
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    rc1 = main(["export", "--format", "markdown", "--out", str(out_dir), "--db-path", db_path])
    assert rc1 == 0
    assert not (out_dir / "export.jsonl").exists()

    with caplog.at_level("WARNING"):
        rc2 = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])

    assert rc2 == 0
    assert "falling back to a full export" in caplog.text
    lines = (out_dir / "export.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_explicit_since_into_an_empty_out_dir_is_not_second_guessed(tmp_path, monkeypatch, caplog):
    """The no-prior-output safety net exists for a cursor persisted in THIS
    --out that a different format advanced past. An explicit --since makes
    no claim about the directory, so an empty --out is expected, not a
    symptom - silently widening it to a full export would ignore what the
    caller asked for."""
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    # A cursor far in the future: nothing is newer, so a respected --since
    # exports nothing, while a discarded one would export the whole corpus.
    future = encode_cursor(datetime(2999, 1, 1, tzinfo=timezone.utc), "z")
    with caplog.at_level("WARNING"):
        rc = main([
            "export", "--format", "jsonl", "--out", str(out_dir),
            "--db-path", db_path, "--since", future,
        ])

    assert rc == 0
    assert "falling back to a full export" not in caplog.text
    assert (out_dir / "export.jsonl").read_text() == ""


def test_export_jsonl_deleted_with_persisted_cursor_rebuilds_the_full_snapshot(tmp_path, monkeypatch, caplog):
    """The exact reported repro: a completed jsonl export, then export.jsonl
    removed (a botched copy, a cleanup script) while .reg-radar-cursor
    survives. Trusting that cursor wrote an EMPTY snapshot - the whole
    corpus silently gone from the export."""
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    assert main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path]) == 0
    assert len((out_dir / "export.jsonl").read_text().strip().splitlines()) == 1
    cursor_before = (out_dir / ".reg-radar-cursor").read_text()

    (out_dir / "export.jsonl").unlink()
    with caplog.at_level("WARNING"):
        rc = main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path])

    assert rc == 0
    assert "falling back to a full export" in caplog.text
    assert len((out_dir / "export.jsonl").read_text().strip().splitlines()) == 1
    assert (out_dir / ".reg-radar-cursor").read_text() == cursor_before


def test_export_reports_only_items_it_actually_wrote(tmp_path, monkeypatch, capsys):
    """An unchanged re-export rewrites nothing, so it must not claim to
    have exported the item again."""
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, _item_id = _setup_db(tmp_path)
    out_dir = tmp_path / "out"

    assert main(["export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path]) == 0
    assert "Exported 1 item(s)" in capsys.readouterr().out

    # An old --since walks the whole change set again, so the item IS
    # re-checked - but its snapshot line is identical, so nothing is written.
    past = encode_cursor(datetime(2000, 1, 1, tzinfo=timezone.utc), "a")
    assert main([
        "export", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path, "--since", past,
    ]) == 0
    assert "Exported 0 item(s)" in capsys.readouterr().out
