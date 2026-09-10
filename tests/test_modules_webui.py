"""Issue #53: an empty result caused by module state is an explained
state, never an empty list - and the four reasons that produce one are
kept apart, because they look identical to a user and blaming the wrong
one is worse than saying nothing.

Everything here goes through the rendered HTML rather than the query
layer: the whole point of the issue is what the page says.
"""
import textwrap

import pytest
from fastapi.testclient import TestClient

from datetime import datetime, timezone

from collector.schema import Item
from collector.storage import insert_item, upsert_regulator


REGISTRY = {
    "eu.yaml": """
        - id: esma
          name: ESMA
          full_name: European Securities and Markets Authority
          jurisdiction: eu
          sources:
            - label: news
              type: rss
              url: https://example.com/esma.xml
    """,
    "sg.yaml": """
        - id: sg-mas
          name: MAS
          full_name: Monetary Authority of Singapore
          jurisdiction: sg
          sources:
            - label: news
              type: rss
              url: https://example.com/mas.xml
    """,
    "dk.yaml": """
        - id: dk-dfsa
          name: Danish FSA
          full_name: Finanstilsynet
          jurisdiction: dk
          enabled: false
          sources:
            - label: news
              type: rss
              url: https://example.com/dfsa.xml
    """,
}


@pytest.fixture
def regulators_dir(tmp_path):
    d = tmp_path / "regulators"
    d.mkdir()
    for name, body in REGISTRY.items():
        (d / name).write_text(textwrap.dedent(body))
    return d


def _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=None, collect_all=False):
    import webui.app as app_module

    modules_path = tmp_path / "modules.yaml"
    if modules_yaml is not None:
        modules_path.write_text(textwrap.dedent(modules_yaml))

    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("TAXONOMY_PATH", "config/taxonomy.yaml")
    monkeypatch.setenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml")
    monkeypatch.setenv("ACTIVITIES_PATH", "config/activities.yaml")
    monkeypatch.setenv("REGRADAR_COLLECT_ALL", "true" if collect_all else "")
    monkeypatch.setattr(app_module, "REGULATORS_PATH", str(regulators_dir))
    monkeypatch.setattr(app_module, "MODULES_PATH", str(modules_path))
    monkeypatch.setattr(app_module, "PROFILES_DIR", "config/profiles")
    app_module._engine = None
    app_module._taxonomy = None
    app_module._activities = None
    app_module._module_state = None
    app_module._module_state_mtime = None
    engine = app_module.get_engine()
    return TestClient(app_module.app), engine, app_module


def _seed(engine, regulator_id="esma", jurisdiction="eu", n=1):
    upsert_regulator(engine, {"id": regulator_id, "name": regulator_id.upper(),
                              "full_name": regulator_id.upper(), "jurisdiction": jurisdiction})
    now = datetime.now(timezone.utc)
    for i in range(n):
        insert_item(engine, Item(
            id=f"{regulator_id}-{i}", regulator_id=regulator_id, source_label="news",
            title="Notice", url=f"https://example.com/{regulator_id}/{i}",
            published_at=now, fetched_at=now, date_inferred=False, language="en",
            content_snippet="...", source_type="rss",
        ))


EU_ONLY = """
    version: 1
    defaults:
      on_enable: new_only
    modules:
      - id: eu/*
"""


# --- reason 1: no modules.yaml at all (what a new install sees first) -------

def test_no_modules_file_explains_that_nothing_is_collected(tmp_path, monkeypatch, regulators_dir):
    client, _, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=None)

    body = client.get("/").text
    assert "This install is not collecting anything yet" in body
    assert "modules.yaml" in body
    assert "REGRADAR_COLLECT_ALL" in body
    # The state it replaces must be gone.
    assert "No items match these filters." not in body


def test_no_modules_file_wins_over_a_jurisdiction_filter(tmp_path, monkeypatch, regulators_dir):
    # Nothing is collected for anything, so a per-jurisdiction story would
    # not be true - the install-wide reason takes precedence.
    client, _, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=None)

    body = client.get("/", params={"jurisdiction": "sg"}).text
    assert "This install is not collecting anything yet" in body
    assert "No module is enabled for" not in body


# --- reason 2: the regulator is not in the operator's selection ------------

def test_unselected_jurisdiction_names_it_and_gives_the_enable_command(tmp_path, monkeypatch, regulators_dir):
    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=EU_ONLY)
    _seed(engine)

    body = client.get("/", params={"jurisdiction": "sg"}).text
    assert "No module is enabled for" in body
    assert "Singapore" in body                       # names the jurisdiction
    assert "MAS" in body                             # names the regulator
    assert "nothing has ever been collected from" in body
    assert "reg-radar modules enable sg/*" in body   # the enable path
    assert "cannot be backfilled" in body or "most recent items" in body


def test_unselected_regulator_filter_suggests_the_regulator_not_the_wildcard(
        tmp_path, monkeypatch, regulators_dir):
    # Only part of a jurisdiction missing must not produce an instruction
    # wider than what is actually missing. Here sg has exactly one
    # regulator, so filtering on it is the whole jurisdiction: assert the
    # narrower path directly on the helper instead.
    from webui.modules import enable_hint

    assert enable_hint("us", {"us-sec"}, {"us-sec", "us-cftc"}) == "us-sec"
    assert enable_hint("us", {"us-sec", "us-cftc"}, {"us-sec", "us-cftc"}) == "us/*"


def test_a_profile_scoped_to_an_uncollected_jurisdiction_is_explained(
        tmp_path, monkeypatch, regulators_dir):
    # The issue's named walkthrough: install collecting only eu/*, profile
    # scoped to Singapore. No Singapore profile ships, so one is written
    # into a test-local profiles dir.
    import webui.app as app_module

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "sg-casp.yaml").write_text("name: sg-casp\njurisdictions: [sg]\n")

    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=EU_ONLY)
    monkeypatch.setattr(app_module, "PROFILES_DIR", str(profiles))
    _seed(engine)

    body = client.get("/", params={"profile": "sg-casp"}).text
    assert "No module is enabled for" in body
    assert "Singapore" in body
    assert "reg-radar modules enable sg/*" in body


# --- reason 3: selected, but the registry force-disabled it ----------------

def test_registry_disabled_does_not_blame_the_user(tmp_path, monkeypatch, regulators_dir):
    modules = """
        version: 1
        modules:
          - id: dk-dfsa
    """
    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=modules)
    _seed(engine)  # eu only - dk must come back empty
    # Filtering by regulator, not jurisdiction: a jurisdiction filter also
    # matches that jurisdiction's ancestors (a "dk" filter picks up EU-wide
    # regulators too), which would widen the scope past the broken source.
    body = client.get("/", params={"regulator_id": "dk-dfsa"}).text
    assert "disabled in the registry" in body
    assert "You <em>did</em> select" in body
    assert "Nothing to change on your side" in body
    # Never the not-selected wording: the user's configuration is correct.
    assert "No module is enabled for" not in body
    assert "reg-radar modules enable" not in body


# --- reason 4: collecting fine, the filter just matches nothing ------------

def test_collecting_but_no_match_is_the_ordinary_empty_state(tmp_path, monkeypatch, regulators_dir):
    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=EU_ONLY)
    _seed(engine)

    body = client.get("/", params={"q": "nothingmatchesthis"}).text
    assert "Nothing matches yet" in body
    assert "search terms or date range" in body
    assert "No module is enabled for" not in body
    assert "This install is not collecting anything yet" not in body


def test_new_only_module_says_where_its_history_starts(tmp_path, monkeypatch, regulators_dir):
    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=EU_ONLY)
    _seed(engine)

    body = client.get("/", params={"q": "nothingmatchesthis"}).text
    assert "on_enable: new_only" in body
    assert "History for this install starts" in body
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in body


def test_broken_modules_file_is_reported_not_fatal(tmp_path, monkeypatch, regulators_dir):
    # The collector exits on this file; a web server must not.
    client, _, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml="version: 9\n")

    resp = client.get("/")
    assert resp.status_code == 200
    assert "could not be read" in resp.text


# --- module state view ----------------------------------------------------

def test_modules_view_distinguishes_the_three_module_states(tmp_path, monkeypatch, regulators_dir):
    modules = """
        version: 1
        defaults:
          on_enable: new_only
        modules:
          - id: eu/*
            schedule: "0 6 * * *"
          - id: dk-dfsa
    """
    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=modules)
    _seed(engine, n=3)

    body = client.get("/modules").text
    assert ">collecting<" in body          # esma, selected and registry-enabled
    assert ">registry-disabled<" in body   # dk-dfsa, selected but broken upstream
    assert ">not selected<" in body        # sg-mas, in the catalogue only
    # Cadence, item count, on_enable mode and history-start date.
    assert "0 6 * * *" in body
    assert "new_only" in body
    assert ">3<" in body
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in body
    # The two-filter model, stated once and plainly.
    assert "Modules decide what is fetched; profiles decide what is shown" in body
    # Irreversibility named where module state is shown.
    assert "not freely reversible" in body


def test_modules_view_survives_a_missing_modules_file(tmp_path, monkeypatch, regulators_dir):
    client, _, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=None)

    resp = client.get("/modules")
    assert resp.status_code == 200
    assert "Nothing is being collected" in resp.text
    assert ">collecting<" not in resp.text


def test_collect_all_env_var_collects_the_whole_registry(tmp_path, monkeypatch, regulators_dir):
    client, _, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=None, collect_all=True)

    body = client.get("/modules").text
    assert "REGRADAR_COLLECT_ALL" in body
    # sg-mas is collected here even though no modules.yaml selects it...
    assert ">collecting<" in body
    # ...but the registry still wins on `false` for dk-dfsa.
    assert ">registry-disabled<" in body


# --- the registry must be visible, not just the database ------------------

def test_uncollected_regulators_are_offered_as_filters_and_flagged(tmp_path, monkeypatch, regulators_dir):
    # sg-mas has never been collected, so it is not in the database. If the
    # picker were built from the database it could not be selected at all,
    # and the explained state would be unreachable.
    client, engine, _ = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=EU_ONLY)
    _seed(engine)

    body = client.get("/").text
    assert 'value="sg-mas"' in body
    assert 'value="sg"' in body
    assert "not collected" in body


def test_module_state_is_reread_when_modules_yaml_changes(tmp_path, monkeypatch, regulators_dir):
    # The empty state tells the operator to go and enable a module; showing
    # them the same complaint afterwards would make the instruction a lie.
    client, engine, app_module = _client(tmp_path, monkeypatch, regulators_dir, modules_yaml=EU_ONLY)
    _seed(engine)
    assert "No module is enabled for" in client.get("/", params={"jurisdiction": "sg"}).text

    modules_path = tmp_path / "modules.yaml"
    modules_path.write_text(textwrap.dedent(EU_ONLY) + "  - id: sg/*\n")
    os_mtime_bump(modules_path)

    body = client.get("/", params={"jurisdiction": "sg"}).text
    assert "No module is enabled for" not in body
    assert "Nothing matches yet" in body


def os_mtime_bump(path):
    """Second-resolution mtimes on some filesystems can collide with the
    read that just happened, which would hide a genuine cache bug behind a
    stale timestamp rather than exposing it."""
    import os

    st = os.stat(path)
    os.utime(path, (st.st_atime + 10, st.st_mtime + 10))
