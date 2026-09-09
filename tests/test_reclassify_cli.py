"""Confirms `regradar.reclassify` is wired into regradar/cli.py's
subcommand registry (Task 4's add_subparser()/run() convention) and
reachable as `regradar reclassify ...` end to end."""
import tempfile

import yaml
from sqlalchemy import text as sa_text

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from datetime import datetime, timezone
from regradar.cli import main


def test_reclassify_help_lists_reclassify_subcommand(capsys):
    exit_code = None
    try:
        main(["--help"])
    except SystemExit as e:
        exit_code = e.code
    assert exit_code == 0
    assert "reclassify" in capsys.readouterr().out


def test_reclassify_subcommand_runs_end_to_end_via_cli_main(tmp_path):
    db_path = str(tmp_path / "test.db")
    engine = init_db(db_path)
    now = datetime.now(timezone.utc)
    item = Item(
        id=make_item_id("esma", "https://example.com/a", now), regulator_id="esma",
        source_label="mica_hub_page", title="Notice", url="https://example.com/a",
        published_at=now, fetched_at=now, date_inferred=False, language="en",
        content_snippet="...", source_type="rss",
    )
    insert_item(engine, item)

    regulators_path = tmp_path / "regulators.yaml"
    regulators_path.write_text(yaml.dump([
        {"id": "esma", "sources": [{"label": "mica_hub_page", "labels": {"instrument": ["mica"]}}]}
    ]))

    exit_code = main([
        "reclassify", "--axis", "instrument",
        "--db-path", db_path,
        "--regulators-path", str(regulators_path),
        "--taxonomy-path", "config/taxonomy.yaml",
        "--jurisdictions-path", "config/jurisdictions.yaml",
    ])
    assert exit_code == 0

    with engine.connect() as conn:
        row = conn.execute(sa_text("SELECT axis, value, by FROM classifications WHERE item_id=:i"), {"i": item.id}).fetchone()
    assert tuple(row) == ("instrument", "mica", "source_default")
