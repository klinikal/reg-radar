import argparse
import tempfile
from datetime import datetime, timezone

import yaml
from sqlalchemy import text as sa_text

from collector.schema import Item, make_item_id
from collector.storage import init_db, insert_item
from regradar.classification import write_classification
from regradar.reclassify import add_subparser, run, source_defaults
from regradar.taxonomy import build_taxonomy


def _db_path():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return f.name


def _regulators_path(tmp_path, regulators):
    path = tmp_path / "regulators.yaml"
    path.write_text(yaml.dump(regulators))
    return str(path)


def _insert_item(engine, item_id, regulator_id="esma", source_label="mica_hub_page"):
    now = datetime.now(timezone.utc)
    item = Item(
        id=item_id, regulator_id=regulator_id, source_label=source_label, title="Notice",
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet="...", source_type="rss",
    )
    insert_item(engine, item)


def _args(**overrides):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_subparser(subparsers)
    defaults = {
        "axis": "instrument", "by": "source_default", "dry_run": False,
        "db_path": "", "regulators_path": "", "taxonomy_path": "config/taxonomy.yaml",
        "jurisdictions_path": "config/jurisdictions.yaml",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_source_defaults_maps_regulator_and_source_label_to_labels():
    regulators = [{
        "id": "esma", "sources": [{"label": "mica_hub_page", "labels": {"instrument": ["mica"]}}],
    }]
    defaults = source_defaults(regulators)
    assert defaults[("esma", "mica_hub_page")] == {"instrument": ["mica"]}


def test_add_subparser_registers_reclassify_command():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_subparser(subparsers)
    args = parser.parse_args(["reclassify", "--axis", "instrument"])
    assert args.command == "reclassify"
    assert args.axis == "instrument"
    assert args.by == "source_default"  # default


def test_run_writes_source_default_classification_for_matching_items(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item(engine, "i1")
    regulators = [{
        "id": "esma", "sources": [{"label": "mica_hub_page", "labels": {"instrument": ["mica"]}}],
    }]
    args = _args(db_path=db_path, regulators_path=_regulators_path(tmp_path, regulators))

    run(args)

    with engine.connect() as conn:
        rows = conn.execute(sa_text("SELECT axis, value, by FROM classifications WHERE item_id='i1'")).fetchall()
    assert [tuple(r) for r in rows] == [("instrument", "mica", "source_default")]


def test_run_dry_run_writes_nothing(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item(engine, "i1")
    regulators = [{
        "id": "esma", "sources": [{"label": "mica_hub_page", "labels": {"instrument": ["mica"]}}],
    }]
    args = _args(db_path=db_path, regulators_path=_regulators_path(tmp_path, regulators), dry_run=True)

    run(args)

    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications")).scalar()
    assert count == 0


def test_run_never_touches_human_rows(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item(engine, "i1")
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    write_classification(engine, taxonomy, "i1", "instrument", "dora", by="human:alice", confidence=1.0)

    regulators = [{
        "id": "esma", "sources": [{"label": "mica_hub_page", "labels": {"instrument": ["mica"]}}],
    }]
    args = _args(db_path=db_path, regulators_path=_regulators_path(tmp_path, regulators))
    run(args)

    with engine.connect() as conn:
        human_row = conn.execute(
            sa_text("SELECT superseded_at FROM classifications WHERE item_id='i1' AND by='human:alice'")
        ).fetchone()
    assert human_row[0] is None  # still live


def test_run_supersedes_prior_source_default_rows_before_rewriting(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item(engine, "i1")
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    write_classification(engine, taxonomy, "i1", "instrument", "dora", by="source_default", confidence=0.6)

    regulators = [{
        "id": "esma", "sources": [{"label": "mica_hub_page", "labels": {"instrument": ["mica"]}}],
    }]
    args = _args(db_path=db_path, regulators_path=_regulators_path(tmp_path, regulators))
    run(args)

    with engine.connect() as conn:
        live = conn.execute(
            sa_text("SELECT value FROM classifications WHERE item_id='i1' AND superseded_at IS NULL")
        ).fetchall()
    assert [r[0] for r in live] == ["mica"]


def test_run_uses_taxonomy_source_default_confidence_not_a_hardcoded_axis_branch(tmp_path):
    """Confidence must come from the axis's configured
    source_default_confidence (config/taxonomy.yaml), not a branch on axis
    name in reclassify.py."""
    taxonomy_path = tmp_path / "taxonomy.yaml"
    taxonomy_path.write_text("""\
version: 1
axes:
  - id: topic
    cardinality: many
    source_default_confidence: 0.42
    values:
      - id: staking
        name: Staking
""")
    jurisdictions_path = tmp_path / "jurisdictions.yaml"
    jurisdictions_path.write_text("- id: eu\n  name: European Union\n")

    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item(engine, "i1", source_label="s")
    regulators = [{"id": "esma", "sources": [{"label": "s", "labels": {"topic": ["staking"]}}]}]
    args = _args(
        axis="topic", db_path=db_path, regulators_path=_regulators_path(tmp_path, regulators),
        taxonomy_path=str(taxonomy_path), jurisdictions_path=str(jurisdictions_path),
    )
    run(args)

    with engine.connect() as conn:
        confidence = conn.execute(sa_text("SELECT confidence FROM classifications WHERE item_id='i1'")).scalar()
    assert confidence == 0.42
