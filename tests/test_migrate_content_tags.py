import json
import tempfile
from datetime import datetime, timezone

from sqlalchemy import text as sa_text

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from scripts.migrate_content_tags import run


def _make_engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name), f.name


def _insert_item(engine, item_id, content_tags):
    now = datetime.now(timezone.utc)
    item = Item(
        id=item_id, regulator_id="esma", source_label="all_news", title="MiCA update",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="MiCA update", source_type="rss", content_tags=content_tags,
    )
    insert_item(engine, item)


def test_dry_run_imports_nothing(tmp_path):
    engine, db_path = _make_engine()
    _insert_item(engine, "i1", ["mica", "dora"])

    imported = run(db_path, "config/taxonomy.yaml", "config/jurisdictions.yaml", dry_run=True)

    assert imported == 2
    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications")).scalar()
    assert count == 0


def test_run_imports_one_classification_row_per_item_content_tag_pair(tmp_path):
    engine, db_path = _make_engine()
    _insert_item(engine, "i1", ["mica", "dora"])
    _insert_item(engine, "i2", ["mica"])
    _insert_item(engine, "i3", [])  # no content_tags - nothing to import

    imported = run(db_path, "config/taxonomy.yaml", "config/jurisdictions.yaml", dry_run=False)

    assert imported == 3  # (i1, mica), (i1, dora), (i2, mica)
    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications WHERE by='import'")).scalar()
        labels = conn.execute(sa_text("SELECT labels FROM items WHERE id='i1'")).scalar()
    assert count == 3
    assert json.loads(labels) == {"instrument": ["dora", "mica"]}


def test_run_is_idempotent_on_second_pass(tmp_path):
    engine, db_path = _make_engine()
    _insert_item(engine, "i1", ["mica"])

    run(db_path, "config/taxonomy.yaml", "config/jurisdictions.yaml", dry_run=False)
    imported_second_pass = run(db_path, "config/taxonomy.yaml", "config/jurisdictions.yaml", dry_run=False)

    assert imported_second_pass == 0
    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications WHERE by='import'")).scalar()
    assert count == 1


def test_run_sets_confidence_0_5_and_by_import():
    engine, db_path = _make_engine()
    _insert_item(engine, "i1", ["mica"])
    run(db_path, "config/taxonomy.yaml", "config/jurisdictions.yaml", dry_run=False)
    with engine.connect() as conn:
        by, confidence = conn.execute(
            sa_text("SELECT by, confidence FROM classifications WHERE item_id='i1'")
        ).fetchone()
    assert by == "import"
    assert confidence == 0.5
