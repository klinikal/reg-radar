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

    # `tags` (the OLD, retired column the migration actually reads from) isn't
    # on the ORM model, so a fresh test DB never gets it via init_db(). Add it
    # and seed it here to match the shape of a real legacy production DB.
    with engine.connect() as conn:
        conn.execute(sa_text("ALTER TABLE items ADD COLUMN tags TEXT"))
        conn.execute(sa_text("UPDATE items SET tags = :old_tags WHERE id = :id"),
                     {"old_tags": json.dumps(["casp", "mica", "news"]), "id": item.id})
        conn.commit()

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

    # `tags` (the OLD, retired column the migration actually reads from) isn't
    # on the ORM model, so a fresh test DB never gets it via init_db(). Add it
    # and seed it here to match the shape of a real legacy production DB.
    with engine.connect() as conn:
        conn.execute(sa_text("ALTER TABLE items ADD COLUMN tags TEXT"))
        conn.execute(sa_text("UPDATE items SET tags = :old_tags WHERE id = :id"),
                     {"old_tags": json.dumps(["casp", "mica", "news"]), "id": item.id})
        conn.commit()

    alerts_path = str(tmp_path / "alerts.yaml")
    with open(alerts_path, "w") as f:
        f.write('- keyword: "MiCA"\n  tag: mica\n')

    run(db_path, alerts_path, dry_run=False)

    with engine.connect() as conn:
        row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = :id"), {"id": item.id}).fetchone()
    assert json.loads(row[0]) == ["news"]  # casp/mica stripped
    assert json.loads(row[1]) == ["mica"]  # recomputed from real text via keyword match


def test_run_migrates_legacy_row_with_data_only_in_old_tags_column(tmp_path):
    from collector.storage import init_db
    from scripts.migrate_source_content_tags import run
    from sqlalchemy import text as sa_text
    from datetime import datetime, timezone
    import json

    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    now = datetime.now(timezone.utc)

    # The `tags` column was fully removed from the ItemRow ORM model (it's
    # only ever added to real production DBs via their pre-branch history,
    # not via init_db()/create_all() on a fresh DB). Add it here to make this
    # fresh test DB match the shape of a real legacy production DB, which
    # already has `tags` from before this branch.
    with engine.connect() as conn:
        conn.execute(sa_text("ALTER TABLE items ADD COLUMN tags TEXT"))
        conn.commit()

    # Simulate a genuinely pre-existing row: real data in the OLD `tags`
    # column, NULL in the new source_tags/content_tags columns — exactly
    # what every real production row looks like before this migration runs.
    # Inserted via raw SQL, not insert_item()/Item(), since the normal path
    # can no longer write to the retired `tags` column at all.
    with engine.connect() as conn:
        conn.execute(sa_text(
            "INSERT INTO items (id, regulator_id, source_label, title, url, "
            "published_at, fetched_at, date_inferred, language, content_snippet, "
            "source_type, tags, source_tags, content_tags, raw_content) "
            "VALUES ('legacy-1', 'esma', 'all_news', 'MiCA update', 'https://x/legacy', "
            ":now, :now, 0, 'en', 'MiCA update', 'rss', :old_tags, NULL, NULL, 'MiCA update details')"
        ), {"now": now.isoformat(), "old_tags": json.dumps(["casp", "mica", "warnings"])})
        conn.commit()

    alerts_path = str(tmp_path / "alerts.yaml")
    with open(alerts_path, "w") as f:
        f.write('- keyword: "MiCA"\n  tag: mica\n')

    run(db_path, alerts_path, dry_run=False)

    with engine.connect() as conn:
        row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = 'legacy-1'")).fetchone()
    assert json.loads(row[0]) == ["warnings"]  # casp/mica denylisted, warnings kept - read from the OLD tags column
    assert json.loads(row[1]) == ["mica"]  # freshly computed from real text via keyword match


def test_run_skips_new_style_rows_with_null_tags_column(tmp_path):
    from collector.storage import init_db
    from scripts.migrate_source_content_tags import run
    from sqlalchemy import text as sa_text
    from datetime import datetime, timezone
    import json

    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    now = datetime.now(timezone.utc)

    # Legacy `tags` column added to match a real production DB's shape.
    with engine.connect() as conn:
        conn.execute(sa_text("ALTER TABLE items ADD COLUMN tags TEXT"))
        conn.commit()

    # New-style row: written by post-merge collector code, which never
    # touches `tags` (NULL) and already has correct source_tags/content_tags.
    # The migration must leave this row completely alone.
    with engine.connect() as conn:
        conn.execute(sa_text(
            "INSERT INTO items (id, regulator_id, source_label, title, url, "
            "published_at, fetched_at, date_inferred, language, content_snippet, "
            "source_type, tags, source_tags, content_tags, raw_content) "
            "VALUES ('new-1', 'esma', 'all_news', 'MiCA update', 'https://x/new', "
            ":now, :now, 0, 'en', 'MiCA update', 'rss', NULL, :source_tags, :content_tags, "
            "'MiCA update details')"
        ), {
            "now": now.isoformat(),
            "source_tags": json.dumps(["warnings"]),
            "content_tags": json.dumps(["mica"]),
        })
        conn.commit()

    # Also seed a legacy row alongside it, to confirm the fix still migrates
    # legacy rows correctly while leaving the new-style row untouched.
    with engine.connect() as conn:
        conn.execute(sa_text(
            "INSERT INTO items (id, regulator_id, source_label, title, url, "
            "published_at, fetched_at, date_inferred, language, content_snippet, "
            "source_type, tags, source_tags, content_tags, raw_content) "
            "VALUES ('legacy-1', 'esma', 'all_news', 'MiCA update', 'https://x/legacy', "
            ":now, :now, 0, 'en', 'MiCA update', 'rss', :old_tags, NULL, NULL, 'MiCA update details')"
        ), {"now": now.isoformat(), "old_tags": json.dumps(["casp", "mica", "warnings"])})
        conn.commit()

    alerts_path = str(tmp_path / "alerts.yaml")
    with open(alerts_path, "w") as f:
        f.write('- keyword: "MiCA"\n  tag: mica\n')

    run(db_path, alerts_path, dry_run=False)

    with engine.connect() as conn:
        new_row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = 'new-1'")).fetchone()
        legacy_row = conn.execute(sa_text("SELECT source_tags, content_tags FROM items WHERE id = 'legacy-1'")).fetchone()

    # New-style row must be untouched - NOT overwritten with [].
    assert json.loads(new_row[0]) == ["warnings"]
    assert json.loads(new_row[1]) == ["mica"]

    # Legacy row still migrates correctly.
    assert json.loads(legacy_row[0]) == ["warnings"]
    assert json.loads(legacy_row[1]) == ["mica"]


def test_run_on_db_with_no_legacy_tags_column_completes_without_error(tmp_path):
    from collector.storage import init_db
    from scripts.migrate_source_content_tags import run

    db_path = str(tmp_path / "test.db")
    # init_db() never creates the retired `tags` column - simulates a DB
    # created fresh by the current, post-split schema.
    init_db(db_path)

    alerts_path = str(tmp_path / "alerts.yaml")
    with open(alerts_path, "w") as f:
        f.write('- keyword: "MiCA"\n  tag: mica\n')

    # Should return cleanly, not raise sqlite3.OperationalError: no such column: tags
    run(db_path, alerts_path, dry_run=False)
