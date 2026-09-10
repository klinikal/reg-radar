"""`reg-radar setup` and `reg-radar modules` (issue #52).

The prompting layer is deliberately thin, so what is tested here is the
file-writing core underneath it plus the three non-interactive commands end
to end, against a fixture registry that carries the two facts the commands
have to respect: a source that declares a volume estimate and one that
declares `backfill.supported: false` with a retention reason.

test_enable_backfill_all_against_unsupported_source_writes_nothing is the
one that matters most: #50's refusal has to land before the file changes,
because a config the collector would reject at startup is worse than no
change at all.
"""
import argparse
import os
import pytest

import yaml

from regradar import modules as m
from regradar.cli import build_parser


REGISTRY = """\
- id: esma
  name: ESMA
  jurisdiction: eu
  sources:
  - label: news
    type: rss
    backfill: {supported: true, method: paged_api}
    volume_estimate: {first_run: 1200, as_of: 2026-09-10}
  - label: library
    type: scrape
    backfill: {supported: true, method: paged_api}
- id: fr-amf
  name: AMF
  jurisdiction: fr
  sources:
  - label: news
    type: rss
    backfill: {supported: true, method: paged_api}
- id: uk-fca
  name: FCA
  jurisdiction: gb
  sources:
  - label: news
    type: rss
  - label: warnings
    type: rss
- id: us-occ
  name: OCC
  jurisdiction: us
  sources:
  - label: news
    type: rss
    backfill:
      supported: false
      method: none
      note: "the feed retains only the 10 most recent items, so there is no history to fetch"
    volume_estimate: {first_run: 10, as_of: 2026-09-10}
- id: us-sec
  name: SEC
  jurisdiction: us
  enabled: false
  notes: Blocked by the site's WAF.
  sources:
  - label: news
    type: rss
- id: sg-sso
  name: Singapore Statutes Online
  jurisdiction: sg
  sources:
  - label: psa_amendments
    type: page_watch
"""

JURISDICTIONS = """\
- {id: eu, name: European Union}
- {id: fr, name: France, parents: [eu]}
- {id: gb, name: United Kingdom}
- {id: us, name: United States}
- {id: sg, name: Singapore}
"""


@pytest.fixture
def env(tmp_path):
    (tmp_path / "regulators").mkdir()
    (tmp_path / "regulators" / "all.yaml").write_text(REGISTRY)
    (tmp_path / "jurisdictions.yaml").write_text(JURISDICTIONS)
    return argparse.Namespace(
        regulators_path=str(tmp_path / "regulators"),
        jurisdictions_path=str(tmp_path / "jurisdictions.yaml"),
        modules_path=str(tmp_path / "modules.yaml"),
        all=False,
        on_enable=None,
        module_id=None,
        module_ids=[],
    )


def run(env, command, **kwargs):
    args = argparse.Namespace(**{**vars(env), **kwargs, "modules_command": command})
    return m.run(args)


def written(env) -> dict:
    with open(env.modules_path) as f:
        return yaml.safe_load(f)


def reg(env, reg_id):
    return next(r for r in m.load_context(env)[0] if r["id"] == reg_id)


# --- the pure core ---------------------------------------------------------

def test_volume_estimate_reports_unknown_sources_rather_than_counting_them_as_zero(env):
    """The whole reason the figure is shown before writing: "1,210 items"
    over a selection where most sources declare nothing is a number that
    reads as complete and is not."""
    regulators, _ = m.load_context(env)
    estimate = m.volume_estimate([(r, None) for r in regulators if r["id"] in ("esma", "uk-fca", "us-occ")])
    assert estimate == {
        "items": 1210, "sources": 5, "known_sources": 2, "unknown_sources": 3,
        "as_of": estimate["as_of"],
    }
    lines = " ".join(m.describe_volume(estimate, "new_only"))
    assert "approximately 1,210 items from 2 source(s)" in lines
    assert "plus 3 source(s) of unknown volume" in lines


def test_volume_estimate_with_no_declared_figures_says_unknown_not_zero(env):
    estimate = m.volume_estimate([(reg(env, "uk-fca"), None)])
    assert estimate["items"] == 0 and estimate["known_sources"] == 0
    assert "unknown - none of the 2 selected source(s)" in " ".join(m.describe_volume(estimate, "new_only"))


def test_cadence_defaults_come_from_what_the_registry_declares(env):
    # Tiny retention with no backfill route - polled slower, it loses documents.
    assert m.cadence_for(reg(env, "us-occ")) == m.CADENCE_FAST
    # Statute page_watch: changes a few times a year.
    assert m.cadence_for(reg(env, "sg-sso")) == m.CADENCE_WEEKLY
    assert m.cadence_for(reg(env, "esma")) == m.CADENCE_DAILY
    # Narrowing away the tiny feed takes the exception with it.
    assert m.cadence_for(reg(env, "esma"), ["library"]) == m.CADENCE_DAILY


def test_entry_writes_a_schedule_only_when_it_is_an_exception(env):
    assert m.entry_for("us-occ", None, reg(env, "us-occ")) == {"id": "us-occ", "schedule": m.CADENCE_FAST}
    assert m.entry_for("esma", None, reg(env, "esma")) == {"id": "esma"}
    assert m.entry_for("eu/*") == {"id": "eu/*"}


def test_unbackfillable_names_the_registrys_own_reason(env):
    assert m.unbackfillable(reg(env, "us-occ")) == [
        ("news", "the feed retains only the 10 most recent items, so there is no history to fetch")
    ]
    assert m.unbackfillable(reg(env, "esma")) == []


def test_parse_on_enable():
    assert m.parse_on_enable("new_only") == ("new_only", None)
    assert m.parse_on_enable("backfill_all") == ("backfill_all", None)
    assert m.parse_on_enable("backfill_from=2026-01-01") == ("backfill_from", "2026-01-01")
    for bad in ("backfill_from", "new_only=2026-01-01", "everything"):
        with pytest.raises(ValueError):
            m.parse_on_enable(bad)


def test_setup_groups_cover_every_regulator_in_the_registry(env):
    regulators, taxonomy = m.load_context(env)
    groups = m.group_jurisdictions(regulators, taxonomy)
    grouped = [r["id"] for g in groups for r in g["regulators"]]
    assert sorted(grouped) == sorted(r["id"] for r in regulators)
    assert len(grouped) == len(set(grouped))          # no regulator offered twice
    assert [g["id"] for g in groups] == ["eu", "gb", "us", "sg"]   # GROUP_ROOTS order, empties dropped


# --- the commands ----------------------------------------------------------

def test_enable_then_disable_round_trip(env, capsys):
    assert run(env, "enable", module_ids=["gb/*"]) == 0
    assert run(env, "enable", module_ids=["us-occ"]) == 0
    doc = written(env)
    assert doc["modules"] == [{"id": "gb/*"}, {"id": "us-occ", "schedule": m.CADENCE_FAST}]
    assert doc["defaults"]["on_enable"] == "new_only"

    assert run(env, "disable", module_id="us-occ") == 0
    assert written(env)["modules"] == [{"id": "gb/*"}]
    # The file it replaced is still there - PyYAML cannot round-trip
    # comments, so the backup is the only thing that makes a hand-edited
    # file recoverable.
    assert os.path.exists(env.modules_path + ".bak")


def test_enable_produces_a_file_the_collector_accepts(env):
    """`modules enable` output has to satisfy #48's own loader, not merely
    parse - the failure this closes is a CLI that writes something the
    collector then refuses at startup."""
    from collector.main import load_regulators
    assert run(env, "enable", module_ids=["eu/*"]) == 0
    selected = load_regulators(env.regulators_path, modules_path=env.modules_path,
                               jurisdictions_path=env.jurisdictions_path)
    assert sorted(r["id"] for r in selected) == ["esma", "fr-amf"]


def test_enable_backfill_all_against_unsupported_source_writes_nothing(env, capsys):
    assert run(env, "enable", module_ids=["us-occ"], on_enable="backfill_all") == 1
    err = capsys.readouterr().err
    assert "us-occ/news does not support backfill" in err
    assert "the feed retains only the 10 most recent items" in err
    assert "Use on_enable: new_only" in err
    assert not os.path.exists(env.modules_path)


def test_enable_backfill_all_leaves_an_existing_selection_untouched(env):
    assert run(env, "enable", module_ids=["eu/*"]) == 0
    before = open(env.modules_path).read()
    assert run(env, "enable", module_ids=["us-occ"], on_enable="backfill_all") == 1
    assert open(env.modules_path).read() == before


def test_enable_backfill_from_records_the_date(env):
    assert run(env, "enable", module_ids=["esma"], on_enable="backfill_from=2026-01-01") == 0
    assert written(env)["modules"] == [
        {"id": "esma", "on_enable": "backfill_from", "backfill_from": "2026-01-01"}
    ]


def test_enable_accepts_the_comma_separated_list_the_web_ui_prints(env):
    assert run(env, "enable", module_ids=["esma,", "fr-amf"]) == 0
    assert written(env)["modules"] == [{"id": "esma"}, {"id": "fr-amf"}]


def test_enable_shows_the_volume_before_it_is_collected(env, capsys):
    run(env, "enable", module_ids=["esma"])
    out = capsys.readouterr().out
    assert "approximately 1,200 items from 1 source(s)" in out
    assert "plus 1 source(s) of unknown volume" in out


def test_disable_warns_that_the_gap_may_be_unrecoverable_and_names_the_limit(env, capsys):
    run(env, "enable", module_ids=["us-occ"])
    capsys.readouterr()
    assert run(env, "disable", module_id="us-occ") == 0
    out = capsys.readouterr().out
    assert "us-occ/news cannot be backfilled: the feed retains only the 10 most recent items" in out
    assert "the hole is permanent" in out


def test_disable_expands_the_wildcard_that_was_selecting_it(env, capsys):
    """Dropping one regulator out of `eu/*` is only expressible by naming
    the rest - refusing instead would be refusing something the schema can
    express."""
    run(env, "enable", module_ids=["eu/*"])
    capsys.readouterr()
    assert run(env, "disable", module_id="fr-amf") == 0
    assert written(env)["modules"] == [{"id": "esma"}]
    assert "was selected through the wildcard eu/*" in capsys.readouterr().out


def test_disable_of_something_not_selected_changes_nothing(env, capsys):
    run(env, "enable", module_ids=["gb/*"])
    before = open(env.modules_path).read()
    assert run(env, "disable", module_id="esma") == 0
    assert "not selected" in capsys.readouterr().out
    assert open(env.modules_path).read() == before


def test_list_reports_cadence_on_enable_and_the_registry_override(env, capsys):
    run(env, "enable", module_ids=["us-occ"])
    run(env, "enable", module_ids=["us-sec"])
    capsys.readouterr()
    assert run(env, "list") == 0
    out = capsys.readouterr().out
    assert "us-occ" in out and m.CADENCE_FAST in out and "new_only" in out
    # #MOD1's precedence rule, with the registry's own reason: the operator
    # selected this and cannot fix it from their own file.
    assert "us-sec is selected but the registry marks it `enabled: false`" in out
    assert "Blocked by the site's WAF" in out


def test_list_without_a_selection_points_at_setup(env, capsys):
    assert run(env, "list") == 0
    out = capsys.readouterr().out
    assert "collects nothing" in out and "reg-radar setup" in out


def test_list_all_shows_what_is_not_selected(env, capsys):
    run(env, "enable", module_ids=["gb/*"])
    capsys.readouterr()
    run(env, "list", all=True)
    out = capsys.readouterr().out
    assert "Not selected (5)" in out and "fr-amf" in out


def test_broken_modules_file_is_named_not_silently_replaced(env, capsys):
    with open(env.modules_path, "w") as f:
        f.write("version: 1\nmodules:\n  - id: nope\n")
    assert run(env, "enable", module_ids=["gb/*"]) == 1
    assert "unknown module id 'nope'" in capsys.readouterr().err


def test_setup_refuses_to_prompt_when_there_is_no_terminal(env, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert run(env, "setup") == 1
    assert "reg-radar modules enable" in capsys.readouterr().err


def test_cli_wires_both_commands(env):
    parser = build_parser()
    assert parser.parse_args(["setup"]).modules_command == "setup"
    args = parser.parse_args(["modules", "enable", "us/*", "--on-enable=backfill_all"])
    assert (args.modules_command, args.module_ids, args.on_enable) == ("enable", ["us/*"], "backfill_all")
    # The web UI's empty state hands the user a comma-separated list of the
    # regulators that are missing (webui.modules.enable_hint) - pasting it
    # has to work, commas and all.
    assert parser.parse_args(["modules", "enable", "esma,", "fr-amf"]).module_ids == ["esma,", "fr-amf"]
    assert parser.parse_args(["modules", "disable", "sg-mas"]).module_id == "sg-mas"
