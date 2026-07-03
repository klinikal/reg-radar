from scripts.migrate_source_content_tags import migrate_item_tags


def test_migrate_item_tags_strips_denylist_and_recomputes_content_tags():
    old_tags = ["casp", "mica", "warnings"]
    alert_rules = [{"keyword": "MiCA", "tag": "mica"}]
    source_tags, content_tags = migrate_item_tags(old_tags, "This notice concerns MiCA compliance.", alert_rules)
    assert source_tags == ["warnings"]
    assert content_tags == ["mica"]


def test_migrate_item_tags_no_keyword_match_gives_empty_content_tags():
    old_tags = ["press_releases"]
    alert_rules = [{"keyword": "MiCA", "tag": "mica"}]
    source_tags, content_tags = migrate_item_tags(old_tags, "Unrelated staffing announcement.", alert_rules)
    assert source_tags == ["press_releases"]
    assert content_tags == []


def test_run_dry_run_does_not_modify_database(tmp_path):
    from datetime import datetime, timezone
    from collector.storage import init_db, insert_item
    from collector.schema import Item, make_item_id
    from scripts.migrate_source_content_tags import run
    from sqlalchemy import text as sa_text
    import json

    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    now = datetime.now(timezone.utc)
    item = Item(
        id=make_item_id("esma", "https://x/a", now), regulator_id="esma", source_label="all_news",
        title="MiCA update", url="https://x/a", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="MiCA update", source_type="rss",
        source_tags=["casp", "mica", "news"], content_tags=[],
    )
    insert_item(engine, item)

    alerts_path = str(tmp_path / "alerts.yaml")
    with open(alerts_path, "w") as f:
        f.write('- keyword: "MiCA"\n  tag: mica\n')

    run(db_path, alerts_path, dry_run=True)

    with engine.connect() as conn:
        row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = :id"), {"id": item.id}).fetchone()
    assert json.loads(row[0]) == ["casp", "mica", "news"]  # unchanged - dry run
    assert json.loads(row[1]) == []


def test_run_writes_migrated_tags_when_not_dry_run(tmp_path):
    from datetime import datetime, timezone
    from collector.storage import init_db, insert_item
    from collector.schema import Item, make_item_id
    from scripts.migrate_source_content_tags import run
    from sqlalchemy import text as sa_text
    import json

    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    now = datetime.now(timezone.utc)
    item = Item(
        id=make_item_id("esma", "https://x/a", now), regulator_id="esma", source_label="all_news",
        title="MiCA update", url="https://x/a", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="MiCA update", source_type="rss",
        source_tags=["casp", "mica", "news"], content_tags=[],
    )
    insert_item(engine, item)

    alerts_path = str(tmp_path / "alerts.yaml")
    with open(alerts_path, "w") as f:
        f.write('- keyword: "MiCA"\n  tag: mica\n')

    run(db_path, alerts_path, dry_run=False)

    with engine.connect() as conn:
        row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = :id"), {"id": item.id}).fetchone()
    assert json.loads(row[0]) == ["news"]  # casp/mica stripped
    assert json.loads(row[1]) == ["mica"]  # recomputed from real text via keyword match
