"""config/modules.yaml selection (issue #48).

The one that matters is test_migration_set_matches_collect_all_set: the whole
point of the migration writing `eu/*` + `gb/*` is that an upgraded install
collects exactly what it collected before, and that is asserted against the
real registry, not a fixture.
"""
import os
import pytest
from unittest.mock import patch

from collector.main import (
    ModulesConfigError,
    MODULES_MIGRATION_YAML,
    _load_modules_file,
    _migrate_modules_file,
    load_regulators,
)
from collector.storage import init_db
from regradar.taxonomy import Taxonomy, load_jurisdictions
from sqlalchemy import text as sa_text


REGISTRY = """\
- id: esma
  jurisdiction: eu
  sources:
  - {label: news, type: rss}
  - {label: warnings, type: rss}
- id: fr-amf
  jurisdiction: fr
  sources:
  - {label: news, type: rss}
- id: mt-mfsa
  jurisdiction: mt
  sources:
  - {label: news, type: rss}
  - {label: solvency, type: rss, enabled: false}
- id: uk-fca
  jurisdiction: gb
  sources:
  - {label: news, type: rss}
  - {label: warnings, type: rss}
- id: us-sec
  jurisdiction: us
  enabled: false
  sources:
  - {label: news, type: rss}
"""

JURISDICTIONS = """\
- {id: eu, name: European Union}
- {id: fr, name: France, parents: [eu]}
- {id: mt, name: Malta, parents: [eu]}
- {id: gb, name: United Kingdom}
- {id: us, name: United States}
"""


@pytest.fixture
def config(tmp_path):
    (tmp_path / "regulators").mkdir()
    (tmp_path / "regulators" / "all.yaml").write_text(REGISTRY)
    (tmp_path / "jurisdictions.yaml").write_text(JURISDICTIONS)
    return tmp_path


def _resolve(config, modules_yaml):
    (config / "modules.yaml").write_text(modules_yaml)
    return load_regulators(
        str(config / "regulators"),
        modules_path=str(config / "modules.yaml"),
        jurisdictions_path=str(config / "jurisdictions.yaml"),
    )


def _selected(regulators):
    return {r["id"]: [s["label"] for s in r["sources"]] for r in regulators}


# ---------------------------------------------------------------------------
# Loader modes
# ---------------------------------------------------------------------------

def test_modules_path_none_returns_whole_registry(config):
    """Every non-collector caller (backfill scripts, source_health) must keep
    seeing the whole registry regardless of what the operator collects."""
    regulators = load_regulators(str(config / "regulators"))
    assert len(regulators) == 5


def test_absent_with_collect_all_env_is_whole_registry(config):
    with patch.dict(os.environ, {"REGRADAR_COLLECT_ALL": "true"}):
        regulators = load_regulators(
            str(config / "regulators"),
            modules_path=str(config / "modules.yaml"),
            jurisdictions_path=str(config / "jurisdictions.yaml"),
        )
    assert len(regulators) == 5


def test_absent_without_env_collects_nothing(config, caplog):
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REGRADAR_COLLECT_ALL", None)
        with caplog.at_level("INFO"):
            regulators = load_regulators(
                str(config / "regulators"),
                modules_path=str(config / "modules.yaml"),
                jurisdictions_path=str(config / "jurisdictions.yaml"),
            )
    assert regulators == []
    assert "reg-radar setup" in caplog.text


def test_main_exits_cleanly_when_nothing_selected(config, tmp_path, caplog):
    """The new-install default must not look like a crash: no traceback, and
    main() returns rather than raising or exiting non-zero."""
    env = {
        "DB_PATH": str(tmp_path / "test.db"),
        "REGULATORS_PATH": str(config / "regulators"),
        "MODULES_PATH": str(config / "modules.yaml"),
        "JURISDICTIONS_PATH": str(config / "jurisdictions.yaml"),
    }
    from collector import main as main_mod
    with patch.dict(os.environ, env):
        os.environ.pop("REGRADAR_COLLECT_ALL", None)
        with patch.object(main_mod.sys, "argv", ["collector", "--once"]):
            with caplog.at_level("INFO"):
                assert main_mod.main() is None  # no SystemExit, no exception
    assert "reg-radar setup" in caplog.text


# ---------------------------------------------------------------------------
# Resolution order: defaults -> wildcard -> module id -> sources
# ---------------------------------------------------------------------------

def test_wildcard_expands_to_jurisdiction_and_descendants(config):
    selected = _selected(_resolve(config, "version: 1\nmodules:\n  - id: eu/*\n"))
    assert set(selected) == {"esma", "fr-amf", "mt-mfsa"}


def test_explicit_module_id_selects_one_regulator(config):
    selected = _selected(_resolve(config, "version: 1\nmodules:\n  - id: uk-fca\n"))
    assert set(selected) == {"uk-fca"}
    assert selected["uk-fca"] == ["news", "warnings"]


def test_sources_list_narrows_within_a_module(config):
    selected = _selected(_resolve(config, "version: 1\nmodules:\n  - id: uk-fca\n    sources: [warnings]\n"))
    assert selected["uk-fca"] == ["warnings"]


def test_defaults_are_inherited(config):
    regulators = _resolve(
        config,
        'version: 1\ndefaults:\n  schedule: "0 3 * * *"\n  on_enable: backfill_all\nmodules:\n  - id: eu/*\n',
    )
    assert regulators[0]["module"] == {"id": "eu/*", "schedule": "0 3 * * *", "on_enable": "backfill_all"}


def test_wildcard_overrides_defaults(config):
    regulators = _resolve(
        config,
        'version: 1\ndefaults:\n  schedule: "0 3 * * *"\nmodules:\n  - id: eu/*\n    schedule: "0 9 * * *"\n',
    )
    assert regulators[0]["module"]["schedule"] == "0 9 * * *"


def test_explicit_id_overrides_wildcard(config):
    regulators = _resolve(
        config,
        'version: 1\ndefaults:\n  schedule: "0 3 * * *"\n'
        'modules:\n  - id: eu/*\n    schedule: "0 9 * * *"\n  - id: fr-amf\n    schedule: "0 */4 * * *"\n',
    )
    by_id = {r["id"]: r for r in regulators}
    assert by_id["fr-amf"]["module"]["schedule"] == "0 */4 * * *"
    assert by_id["esma"]["module"]["schedule"] == "0 9 * * *"


def test_narrower_wildcard_beats_wider_one(config):
    regulators = _resolve(
        config,
        'version: 1\nmodules:\n  - id: eu/*\n    schedule: "0 9 * * *"\n  - id: fr/*\n    schedule: "0 1 * * *"\n',
    )
    by_id = {r["id"]: r for r in regulators}
    assert by_id["fr-amf"]["module"]["schedule"] == "0 1 * * *"
    assert by_id["esma"]["module"]["schedule"] == "0 9 * * *"


def test_explicit_id_without_sources_overrides_wildcard_sources(config):
    """Narrower always wins, including when the narrower rule is silent about
    sources - that means "all of them", not "inherit the wildcard's list"."""
    regulators = _resolve(
        config,
        "version: 1\nmodules:\n  - id: eu/*\n    sources: [warnings]\n  - id: esma\n",
    )
    assert _selected(regulators)["esma"] == ["news", "warnings"]


def test_resolution_does_not_mutate_the_registry_entry(config):
    regulators = _resolve(config, "version: 1\nmodules:\n  - id: uk-fca\n    sources: [news]\n")
    unfiltered = {r["id"]: r for r in load_regulators(str(config / "regulators"))}
    assert len(unfiltered["uk-fca"]["sources"]) == 2
    assert len(regulators[0]["sources"]) == 1


# ---------------------------------------------------------------------------
# Registry precedence: the registry wins on `false`, loudly
# ---------------------------------------------------------------------------

def test_registry_disabled_regulator_warns_when_selected_by_name(config, caplog):
    with caplog.at_level("WARNING"):
        _resolve(config, "version: 1\nmodules:\n  - id: us-sec\n")
    assert "us-sec" in caplog.text
    assert "enabled: false" in caplog.text


def test_registry_disabled_source_warns_when_selected_by_name(config, caplog):
    with caplog.at_level("WARNING"):
        _resolve(config, "version: 1\nmodules:\n  - id: mt-mfsa\n    sources: [news, solvency]\n")
    assert "mt-mfsa/solvency" in caplog.text


def test_wildcard_sweep_of_disabled_sources_is_not_a_warning(config, caplog):
    """A wildcard is not an overridden request. Warning per disabled source
    would put 50-odd MFSA circular lines in every run's log."""
    with caplog.at_level("WARNING"):
        _resolve(config, "version: 1\nmodules:\n  - id: eu/*\n")
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_unknown_module_id_is_rejected(config):
    with pytest.raises(SystemExit) as exc:
        _resolve(config, "version: 1\nmodules:\n  - id: xx-nope\n")
    assert exc.value.code == 1


def test_unknown_jurisdiction_wildcard_is_rejected(config):
    with pytest.raises(SystemExit):
        _resolve(config, "version: 1\nmodules:\n  - id: zz/*\n")


def test_unknown_source_label_is_rejected(config):
    with pytest.raises(SystemExit):
        _resolve(config, "version: 1\nmodules:\n  - id: uk-fca\n    sources: [news, nope]\n")


def test_bad_cron_is_rejected_naming_the_entry(tmp_path):
    path = tmp_path / "modules.yaml"
    path.write_text('version: 1\nmodules:\n  - id: uk-fca\n    schedule: "not a cron"\n')
    with pytest.raises(ModulesConfigError) as exc:
        _load_modules_file(str(path))
    assert "uk-fca" in str(exc.value)
    assert "not a cron" in str(exc.value)


@pytest.mark.parametrize("body", [
    "version: 2\nmodules: []\n",
    "modules: []\n",
    "version: 1\nmodules: {}\n",
    "version: 1\nmodules:\n  - {}\n",
    "version: 1\nmodules:\n  - id: uk-fca\n  - id: uk-fca\n",
    "version: 1\nmodules:\n  - id: uk-fca\n    sources: news\n",
    "version: 1\nmodules:\n  - id: uk-fca\n    on_enable: whenever\n",
    "version: 1\ndefaults:\n  schedule: nope\nmodules: []\n",
])
def test_malformed_files_raise_a_readable_error(tmp_path, body):
    path = tmp_path / "modules.yaml"
    path.write_text(body)
    with pytest.raises(ModulesConfigError) as exc:
        _load_modules_file(str(path))
    assert str(path) in str(exc.value)


def test_empty_modules_list_selects_nothing(config):
    assert _resolve(config, "version: 1\nmodules: []\n") == []


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def _db_with_item(tmp_path, with_item):
    engine = init_db(str(tmp_path / "m.db"))
    if with_item:
        with engine.begin() as conn:
            conn.execute(sa_text(
                "INSERT INTO items (id, regulator_id, source_label, title, url) "
                "VALUES ('x1', 'esma', 'news', 't', 'https://example.invalid/1')"
            ))
    return engine


def test_migration_writes_modules_file_for_an_existing_install(tmp_path):
    modules_path = tmp_path / "modules.yaml"
    engine = _db_with_item(tmp_path, with_item=True)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REGRADAR_COLLECT_ALL", None)
        _migrate_modules_file(engine, str(modules_path))
    assert modules_path.read_text() == MODULES_MIGRATION_YAML
    assert "eu/*" in modules_path.read_text() and "gb/*" in modules_path.read_text()


def test_migration_does_not_write_for_a_fresh_install(tmp_path):
    modules_path = tmp_path / "modules.yaml"
    engine = _db_with_item(tmp_path, with_item=False)
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("REGRADAR_COLLECT_ALL", None)
        _migrate_modules_file(engine, str(modules_path))
    assert not modules_path.exists()


def test_migration_never_overwrites_an_existing_file(tmp_path):
    modules_path = tmp_path / "modules.yaml"
    modules_path.write_text("version: 1\nmodules: []\n")
    engine = _db_with_item(tmp_path, with_item=True)
    _migrate_modules_file(engine, str(modules_path))
    assert modules_path.read_text() == "version: 1\nmodules: []\n"


def test_migration_set_matches_collect_all_set(tmp_path):
    """Acceptance criterion: no behaviour change for existing installs. The
    migration-written file must resolve to exactly the regulator/source set
    the pre-#48 collector walked - the real registry, not a fixture."""
    def enabled_pairs(regulators):
        return sorted(
            f"{r['id']}/{s['label']}"
            for r in regulators if r.get("enabled", True)
            for s in (r.get("sources") or []) if s.get("enabled", True)
        )

    everything = load_regulators("config/regulators")
    taxonomy = Taxonomy(0, {}, load_jurisdictions("config/jurisdictions.yaml"))

    def in_eea_or_uk(reg):
        return {"eu", "gb"} & {reg["jurisdiction"], *taxonomy.ancestors(reg["jurisdiction"])}

    # "No behaviour change" means the set an EEA+UK install collected before
    # the global coverage expansion - not the whole registry, which now also
    # holds the US and will grow further. A registry entry outside eu/gb is
    # deliberately NOT selected by migration: an existing install never
    # collected it, and that is precisely why #48 had to land first.
    before = enabled_pairs(r for r in everything if in_eea_or_uk(r))
    modules_path = tmp_path / "modules.yaml"
    modules_path.write_text(MODULES_MIGRATION_YAML)
    after = enabled_pairs(load_regulators(
        "config/regulators",
        modules_path=str(modules_path),
        jurisdictions_path="config/jurisdictions.yaml",
    ))
    assert before == after
    assert len(before) > 0

    # The teeth: every non-EEA/UK entry must be absent from an upgraded
    # install. Without this the test passes vacuously as the registry grows.
    outside = [r["id"] for r in everything if not in_eea_or_uk(r)]
    assert outside, "expected at least one non-EEA/UK regulator in the registry"
    assert not [p for p in after if p.split("/")[0] in outside]


def test_example_file_is_valid_and_resolves_against_the_real_registry():
    doc = _load_modules_file("config/modules.example.yaml")
    assert doc["version"] == 1
    load_regulators(
        "config/regulators",
        modules_path="config/modules.example.yaml",
        jurisdictions_path="config/jurisdictions.yaml",
    )
