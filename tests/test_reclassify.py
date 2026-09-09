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
        "jurisdictions_path": "config/jurisdictions.yaml", "rules_path": "config/rules.yaml",
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


def _rules_path(tmp_path, content: str):
    path = tmp_path / "rules.yaml"
    path.write_text(content)
    return str(path)


def test_by_rule_choice_is_registered():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    add_subparser(subparsers)
    args = parser.parse_args(["reclassify", "--axis", "instrument", "--by", "rule"])
    assert args.by == "rule"


def _insert_item_with_title(engine, item_id, title, regulator_id="esma", source_label="all_news"):
    from datetime import datetime, timezone
    from collector.schema import Item
    from collector.storage import insert_item
    now = datetime.now(timezone.utc)
    insert_item(engine, Item(
        id=item_id, regulator_id=regulator_id, source_label=source_label, title=title,
        url=f"https://example.com/{item_id}", published_at=now, fetched_at=now, date_inferred=False,
        language="en", content_snippet=title, source_type="rss", raw_content="",
    ))


def test_by_rule_writes_rule_classification_for_matching_items(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item_with_title(engine, "i1", "New MiCA guidance published")
    rules_path = _rules_path(tmp_path, (
        "rules:\n  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
    ))
    args = _args(db_path=db_path, rules_path=rules_path, by="rule")

    run(args)

    with engine.connect() as conn:
        row = conn.execute(sa_text(
            "SELECT axis, value, by, confidence FROM classifications WHERE item_id='i1' AND superseded_at IS NULL"
        )).fetchone()
    assert tuple(row) == ("instrument", "mica", "rule", 1.0)  # explicit rule (0.9) + alias rule (0.7) sum, capped


def test_by_rule_writes_nothing_for_non_matching_items(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item_with_title(engine, "i1", "Annual staffing report published")
    rules_path = _rules_path(tmp_path, (
        "rules:\n  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
    ))
    args = _args(db_path=db_path, rules_path=rules_path, by="rule")

    run(args)

    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications WHERE item_id='i1'")).scalar()
    assert count == 0


def test_by_rule_supersedes_prior_rule_rows_but_never_human_rows(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item_with_title(engine, "i1", "New MiCA guidance published")
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    write_classification(engine, taxonomy, "i1", "instrument", "dora", by="rule", confidence=0.6, classifier_id="old")
    write_classification(engine, taxonomy, "i1", "document_type", "warning", by="human:alice", confidence=1.0)
    rules_path = _rules_path(tmp_path, (
        "rules:\n  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
    ))
    args = _args(db_path=db_path, rules_path=rules_path, by="rule")

    run(args)

    with engine.connect() as conn:
        live_instrument = conn.execute(
            sa_text("SELECT value FROM classifications WHERE item_id='i1' AND axis='instrument' AND superseded_at IS NULL")
        ).fetchall()
        human_row = conn.execute(
            sa_text("SELECT superseded_at FROM classifications WHERE item_id='i1' AND by='human:alice'")
        ).fetchone()
    assert [r[0] for r in live_instrument] == ["mica"]  # old dora rule row superseded, mica written
    assert human_row[0] is None  # untouched


def test_by_rule_dry_run_writes_nothing(tmp_path):
    db_path = _db_path()
    engine = init_db(db_path)
    _insert_item_with_title(engine, "i1", "New MiCA guidance published")
    rules_path = _rules_path(tmp_path, (
        "rules:\n  - axis: instrument\n    value: mica\n    match: [{pattern: MiCA, weight: 0.9}]\n    fields: [title]\n"
    ))
    args = _args(db_path=db_path, rules_path=rules_path, by="rule", dry_run=True)

    run(args)

    with engine.connect() as conn:
        count = conn.execute(sa_text("SELECT COUNT(*) FROM classifications")).scalar()
    assert count == 0


def test_by_rule_acceptance_mica_items_labelled_fca_consumer_duty_item_is_not(tmp_path):
    """Acceptance criterion (docs/issues/04-...): running `reclassify --by
    rule` writes instrument=mica rows (confidence > 0.5) for MiCA-related
    items, and none for an FCA item about a UK-only regime (Consumer Duty) -
    titles drawn from real regulator wording (see task report for the full
    10-vs-10 verification against captured source fixtures)."""
    db_path = _db_path()
    engine = init_db(db_path)
    mica_titles = [
        "Beware of unauthorized crypto-asset service providers under MiCA",
        "Crypto-assets service providers - End of the MiCA transitional period",
        "Only MiCA-licensed companies may offer crypto-assets within the EU from July 1",
        "MiCA: CNB grants crypto licences to 11 entities",
        "Announcement on the end of the MiCA Regulation transitional period",
        "Circular to the Industry on the Authorisation Process for MiCA Applicants",
        "VFA Licence and Transition to CASP under the Markets in Crypto-Assets Act (MiCA)",
        "FSC: MiCA licensing applications open for Crypto Companies",
        "Guidance on MiCA authorisation for crypto-asset service providers",
        "MiCA obligations for issuers of asset-referenced tokens",
    ]
    fca_titles = [
        "FCA finalises Consumer Duty portfolio review findings",
        "FCA decides to ban and fine Daniel Thomas over unauthorised pension transfer advice",
        "FCA urges young adults to check for unclaimed Child Trust Funds",
        "FCA bans trio behind a scheme designed to bypass visa rules",
        "Consumers warned to beware of risky mini-bonds and loan notes",
        "FCA fines and bans former SVS Securities CEO",
        "FCA bans senior manager for lack of honesty and integrity",
        "FCA expands global presence with new attaches in India and UAE",
        "Consumer Duty: good practice and areas for improvement",
        "FCA Primary Market Bulletin 65",
    ]
    for i, title in enumerate(mica_titles):
        _insert_item_with_title(engine, f"mica-{i}", title, regulator_id="mt-mfsa")
    for i, title in enumerate(fca_titles):
        _insert_item_with_title(engine, f"fca-{i}", title, regulator_id="uk-fca")

    args = _args(db_path=db_path, rules_path="config/rules.yaml", by="rule")
    run(args)

    with engine.connect() as conn:
        mica_rows = conn.execute(sa_text(
            "SELECT item_id, confidence FROM classifications WHERE axis='instrument' AND value='mica' "
            "AND superseded_at IS NULL"
        )).fetchall()
        fca_mica_count = conn.execute(sa_text(
            "SELECT COUNT(*) FROM classifications WHERE item_id LIKE 'fca-%' AND axis='instrument' "
            "AND value='mica' AND superseded_at IS NULL"
        )).scalar()

    mica_item_ids = {r[0] for r in mica_rows}
    assert all(item_id.startswith("mica-") for item_id in mica_item_ids)
    assert len(mica_item_ids) == 10
    assert all(confidence > 0.5 for _, confidence in mica_rows)
    assert fca_mica_count == 0
