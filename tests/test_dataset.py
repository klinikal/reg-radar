import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from collector.schema import Item, make_item_id
from collector.storage import get_sync_state, init_db, insert_item, upsert_regulator
from mcpserver.queries import get_changes, search_items
from regradar.changes import encode_cursor
from regradar.classification import write_classification
from regradar.cli import main
from regradar.dataset import DatasetError, import_dataset, load_exclusions, publish_dataset
from regradar.taxonomy import load_and_exit_on_error

TAXONOMY_PATH = "config/taxonomy.yaml"
JURISDICTIONS_PATH = "config/jurisdictions.yaml"


def _taxonomy():
    return load_and_exit_on_error(TAXONOMY_PATH, JURISDICTIONS_PATH)


def _setup_db(tmp_path, regulator_id="fca", n=1):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    upsert_regulator(engine, {
        "id": regulator_id, "name": "FCA", "full_name": "Financial Conduct Authority",
        "jurisdiction": "gb", "jurisdiction_type": "national", "website": "https://fca.org.uk",
        "mica_competent_authority": False, "enabled": True,
    })
    now = datetime.now(timezone.utc)
    ids = []
    for i in range(n):
        item_id = make_item_id(regulator_id, f"https://example.com/dataset-{i}", now)
        insert_item(engine, Item(
            id=item_id, regulator_id=regulator_id, source_label="news", title=f"Notice {i}",
            url=f"https://example.com/dataset-{i}", published_at=now, fetched_at=now,
            date_inferred=False, language="en", content_snippet="snip",
            source_type="scrape", raw_content="<p>hello</p>", body_md=f"body {i}", extractor="raw/1",
        ))
        ids.append(item_id)
    return db_path, engine, ids


def _export_and_publish(tmp_path, db_path, *, exclude_file=None, out_name="out"):
    out_dir = tmp_path / out_name
    # One call, both formats, one shared cursor - see regradar/exporter.py's
    # module docstring for why two separate --format calls into the same
    # --out would leave the second one reading an already-advanced cursor.
    args = ["export", "--format", "markdown", "--format", "jsonl", "--out", str(out_dir), "--db-path", db_path]
    if exclude_file:
        args += ["--exclude-regulator-file", str(exclude_file)]
    rc1 = main(args)
    assert rc1 == 0

    dist_dir = tmp_path / f"{out_name}-dist"
    manifest = publish_dataset(
        out_dir, dist_dir, tarball_name="regradar-dataset-test.tar.gz",
        taxonomy_version=1, excluded_regulators=load_exclusions(str(exclude_file)) if exclude_file else set(),
    )
    tarball_path = dist_dir / manifest["tarball"]
    return manifest, tarball_path, dist_dir


def test_manifest_counts_and_sha256_match_tarball(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, ids = _setup_db(tmp_path, n=3)

    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path)

    assert manifest["item_count"] == 3
    assert manifest["per_regulator_counts"] == {"fca": 3}
    assert manifest["excluded_regulators"] == []
    assert tarball_path.exists()

    import hashlib
    h = hashlib.sha256(tarball_path.read_bytes()).hexdigest()
    assert manifest["sha256"] == h
    assert (dist_dir / "manifest.json").exists()
    assert json.loads((dist_dir / "manifest.json").read_text()) == manifest


def test_excluded_regulator_absent_from_markdown_and_jsonl(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, fca_ids = _setup_db(tmp_path, regulator_id="fca", n=1)
    now = datetime.now(timezone.utc)
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    insert_item(engine, Item(
        id=make_item_id("esma", "https://esma.example/1", now), regulator_id="esma", source_label="news",
        title="ESMA item", url="https://esma.example/1", published_at=now, fetched_at=now,
        date_inferred=False, language="en", content_snippet="s", source_type="rss",
        raw_content="r", body_md="esma body", extractor="raw/1",
    ))

    exclude_file = tmp_path / "exclusions.yaml"
    exclude_file.write_text("- esma\n")

    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path, exclude_file=exclude_file)

    assert manifest["excluded_regulators"] == ["esma"]
    assert manifest["per_regulator_counts"] == {"fca": 1}
    assert manifest["item_count"] == 1

    out_dir = tmp_path / "out"
    md_files = list((out_dir / "export").rglob("*.md"))
    assert len(md_files) == 1
    assert "esma" not in str(md_files[0])
    lines = (out_dir / "export.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["regulator"] == "fca"


def test_tampered_tarball_rejected_on_sha256_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, ids = _setup_db(tmp_path, n=1)
    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path)

    # Corrupt the tarball after the manifest (with its sha256) was written.
    with tarball_path.open("ab") as f:
        f.write(b"tampered")

    empty_db_path = str(tmp_path / "empty.db")
    engine2 = init_db(empty_db_path)
    with pytest.raises(DatasetError, match="sha256 mismatch"):
        import_dataset(engine2, str(tarball_path), taxonomy=_taxonomy())


def test_import_refuses_non_empty_db_without_force(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, ids = _setup_db(tmp_path, n=1)
    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path)

    # Import into the SAME (non-empty) database it was exported from.
    with pytest.raises(DatasetError, match="already has"):
        import_dataset(engine, str(tarball_path), taxonomy=_taxonomy())

    # --force allows it (re-inserting is a no-op per item id, insert_item
    # just returns False for an id that already exists).
    summary = import_dataset(engine, str(tarball_path), taxonomy=_taxonomy(), force=True)
    assert summary["items_loaded"] == 0


def test_import_round_trips_items_classifications_and_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, ids = _setup_db(tmp_path, n=2)
    taxonomy = _taxonomy()
    write_classification(engine, taxonomy, ids[0], "document_type", "warning", by="rule", confidence=0.9)
    write_classification(engine, taxonomy, ids[0], "instrument", "mica", by="human:tester", confidence=1.0)

    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path)

    empty_db_path = str(tmp_path / "empty.db")
    engine2 = init_db(empty_db_path)
    summary = import_dataset(engine2, str(tarball_path), taxonomy=taxonomy)

    assert summary["items_loaded"] == 2
    assert summary["excluded_regulators"] == []
    assert summary["cursor"] is not None

    with engine2.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
        assert count == 2
        labels_raw = conn.execute(
            text("SELECT labels FROM items WHERE id = :id"), {"id": ids[0]},
        ).scalar()
        rows = conn.execute(
            text("SELECT axis, value, by, confidence FROM classifications WHERE item_id = :id ORDER BY axis"),
            {"id": ids[0]},
        ).fetchall()

    labels = json.loads(labels_raw)
    assert labels["document_type"] == "warning"
    assert labels["instrument"] == ["mica"]
    by_axis = {r[0]: r for r in rows}
    assert by_axis["document_type"][1:3] == ("warning", "rule")
    assert by_axis["instrument"][1:3] == ("mica", "human:tester")

    # The body comes back with the item, and is searchable: items_fts
    # indexes raw_content, never body_md, so an import that only filled
    # body_md would leave the whole corpus title-only searchable.
    with engine2.connect() as conn:
        body = conn.execute(text("SELECT body_md FROM items WHERE id = :id"), {"id": ids[0]}).scalar()
    assert body in ("body 0", "body 1")
    assert [r["id"] for r in search_items(engine2, "body")] != []

    # Stored sync cursor round-trips through collector.storage's generic
    # sync_state key/value store.
    stored = get_sync_state(engine2, "dataset_import_cursor")
    assert stored is not None
    assert stored.startswith(manifest["generated_at"][:19])  # same timestamp, ignoring the id tie-break


def test_get_changes_from_stored_cursor_empty_until_newer_item(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, ids = _setup_db(tmp_path, n=1)
    taxonomy = _taxonomy()

    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path)

    empty_db_path = str(tmp_path / "empty.db")
    engine2 = init_db(empty_db_path)
    summary = import_dataset(engine2, str(tarball_path), taxonomy=taxonomy)
    cursor = summary["cursor"]

    docs, next_cursor = get_changes(engine2, since=cursor)
    assert docs == []

    # ...and they ARE on the stream from before it. The imported updated_at
    # must be stored in the naive format get_changes() string-compares
    # against ("YYYY-MM-DD HH:MM:SS[.ffffff]"), not the "+00:00" repr a raw
    # SQL write of a tz-aware datetime leaves behind - see
    # collector.storage.db_now for why that silently breaks the cursor.
    earlier = encode_cursor(datetime(2000, 1, 1, tzinfo=timezone.utc), "0")
    docs_all, _ = get_changes(engine2, since=earlier)
    assert [d["id"] for d in docs_all] == ids
    with engine2.connect() as conn:
        stored_updated_at = conn.execute(
            text("SELECT updated_at FROM items WHERE id = :id"), {"id": ids[0]},
        ).scalar()
    assert "+" not in stored_updated_at, stored_updated_at

    # A newer item (fetched/updated well after generated_at) shows up.
    later = datetime.now(timezone.utc)
    insert_item(engine2, Item(
        id=make_item_id("fca", "https://example.com/new-after-import", later),
        regulator_id="fca", source_label="news", title="Newer notice",
        url="https://example.com/new-after-import", published_at=later, fetched_at=later,
        date_inferred=False, language="en", content_snippet="s", source_type="scrape",
        raw_content="r", body_md="new body", extractor="raw/1",
    ))
    docs2, _ = get_changes(engine2, since=cursor)
    assert len(docs2) == 1
    assert docs2[0]["title"] == "Newer notice"


def test_load_exclusions_missing_file_returns_empty_set(tmp_path):
    assert load_exclusions(str(tmp_path / "does-not-exist.yaml")) == set()


def test_import_dataset_cli_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ARTEFACTS_DIR", str(tmp_path / "originals"))
    db_path, engine, ids = _setup_db(tmp_path, n=1)
    manifest, tarball_path, dist_dir = _export_and_publish(tmp_path, db_path)

    empty_db_path = str(tmp_path / "empty.db")
    rc = main([
        "import-dataset", str(tarball_path), "--db-path", empty_db_path,
        "--taxonomy-path", TAXONOMY_PATH, "--jurisdictions-path", JURISDICTIONS_PATH,
    ])
    assert rc == 0

    from sqlalchemy import create_engine
    engine2 = create_engine(f"sqlite:///{empty_db_path}")
    with engine2.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
    assert count == 1
