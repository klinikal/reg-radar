"""Declared backfill capability and the refusal it enforces (issue #50).

The two that matter are test_backfill_request_against_an_unsupported_source_is_a_hard_error
and test_refused_backfill_writes_nothing: a source whose feed
retains ten items has no history to fetch at any price, and the failure this
file exists to prevent is the user asking for full history, being told it
worked, and having ten items.
"""
import os
import pytest

from collector.main import (
    BackfillNotSupportedError,
    _source_backfill_error,
    load_regulators,
    require_backfill_supported,
    source_backfill,
)
from collector.storage import init_db
from sqlalchemy import text as sa_text


REGISTRY = """\
- id: xx-occ
  jurisdiction: xx
  sources:
  - label: news
    type: rss
    backfill:
      supported: false
      method: none
      note: "the feed retains only the 10 most recent items, so there is no history to fetch"
  - label: undeclared
    type: rss
- id: xx-deep
  jurisdiction: xx
  sources:
  - label: api
    type: json_api
    backfill: {supported: true, method: paged_api}
"""

JURISDICTIONS = "- {id: xx, name: Example}\n"


@pytest.fixture
def config(tmp_path):
    (tmp_path / "regulators").mkdir()
    (tmp_path / "regulators" / "all.yaml").write_text(REGISTRY)
    (tmp_path / "jurisdictions.yaml").write_text(JURISDICTIONS)
    return tmp_path


def _load(config, modules_yaml):
    (config / "modules.yaml").write_text(modules_yaml)
    return load_regulators(
        str(config / "regulators"),
        modules_path=str(config / "modules.yaml"),
        jurisdictions_path=str(config / "jurisdictions.yaml"),
    )


# ---------------------------------------------------------------------------
# The default
# ---------------------------------------------------------------------------

def test_no_backfill_block_resolves_to_unsupported():
    """The safe default: asserting backfill nobody verified is the failure
    this field closes."""
    assert source_backfill({"label": "news"}) == {
        "supported": False, "method": "none", "script": None, "note": None,
    }


def test_declared_block_is_returned_as_written():
    declared = source_backfill({"backfill": {"supported": True, "method": "script",
                                             "script": "scripts/backfill_esma_library.py"}})
    assert declared["supported"] is True
    assert declared["method"] == "script"


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------

def test_supported_script_must_name_a_script_that_exists():
    problem = _source_backfill_error(
        {"backfill": {"supported": True, "method": "script", "script": "scripts/nope.py"}}
    )
    assert "does not exist" in problem


def test_supported_script_without_a_script_key_is_an_error():
    problem = _source_backfill_error({"backfill": {"supported": True, "method": "script"}})
    assert "needs a 'script:'" in problem


def test_real_backfill_script_path_is_accepted():
    assert _source_backfill_error(
        {"backfill": {"supported": True, "method": "script",
                      "script": "scripts/backfill_esma_library.py"}}
    ) is None


def test_unsupported_cannot_declare_a_method_other_than_none():
    problem = _source_backfill_error({"backfill": {"supported": False, "method": "paged_api"}})
    assert "cannot declare method" in problem


def test_unknown_method_is_rejected():
    assert "invalid backfill.method" in _source_backfill_error(
        {"backfill": {"supported": True, "method": "magic"}}
    )


def test_supported_true_needs_a_real_method():
    assert "needs a method" in _source_backfill_error({"backfill": {"supported": True}})


def test_volume_estimate_needs_an_as_of_date():
    assert "as_of" in _source_backfill_error({"volume_estimate": {"first_run": 100}})


def test_volume_estimate_figures_must_be_whole_numbers():
    problem = _source_backfill_error({"volume_estimate": {"first_run": "lots", "as_of": "2026-09-10"}})
    assert "whole number" in problem


def test_bad_declaration_in_the_registry_exits_at_load(tmp_path, caplog):
    (tmp_path / "regulators").mkdir()
    (tmp_path / "regulators" / "bad.yaml").write_text(
        "- id: xx-bad\n  jurisdiction: xx\n  sources:\n"
        "  - {label: news, type: rss, backfill: {supported: true, method: script, script: scripts/nope.py}}\n"
    )
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        load_regulators(str(tmp_path / "regulators"))
    assert exc.value.code == 1
    assert "xx-bad" in caplog.text and "nope.py" in caplog.text


# ---------------------------------------------------------------------------
# The refusal
# ---------------------------------------------------------------------------

def test_refusal_message_names_source_reason_and_alternative():
    source = {"backfill": {"supported": False, "method": "none",
                           "note": "the feed retains only the 10 most recent items, "
                                   "so there is no history to fetch"}}
    with pytest.raises(BackfillNotSupportedError) as exc:
        require_backfill_supported("us-occ", "news", source)
    message = str(exc.value)
    assert message == (
        "us-occ/news does not support backfill: the feed retains only the 10 most "
        "recent items, so there is no history to fetch. "
        "Use on_enable: new_only - you will get everything published from now on."
    )


def test_refusal_without_a_note_still_explains_itself():
    with pytest.raises(BackfillNotSupportedError) as exc:
        require_backfill_supported("xx-y", "feed", {})
    message = str(exc.value)
    assert message.startswith("xx-y/feed does not support backfill: ")
    assert "no history to fetch" in message
    assert "new_only" in message


def test_supported_source_is_not_refused():
    require_backfill_supported("xx-deep", "api", {"backfill": {"supported": True, "method": "paged_api"}})


@pytest.mark.parametrize("on_enable", ["backfill_all", "backfill_from"])
def test_backfill_request_against_an_unsupported_source_is_a_hard_error(config, caplog, on_enable):
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        _load(config, f"version: 1\ndefaults:\n  on_enable: {on_enable}\nmodules:\n  - id: xx-occ\n")
    assert exc.value.code == 1
    assert (
        "xx-occ/news does not support backfill: the feed retains only the 10 most "
        "recent items, so there is no history to fetch. "
        "Use on_enable: new_only - you will get everything published from now on."
    ) in caplog.text


def test_backfill_request_against_a_supported_source_is_allowed(config):
    regulators = _load(config, "version: 1\ndefaults:\n  on_enable: backfill_all\nmodules:\n  - id: xx-deep\n")
    assert [r["id"] for r in regulators] == ["xx-deep"]


def test_new_only_never_refuses_an_undeclared_source(config):
    """The refusal is about backfill, not about collecting: `new_only` against
    a ten-item feed is exactly the right thing to do."""
    regulators = _load(config, "version: 1\ndefaults:\n  on_enable: new_only\nmodules:\n  - id: xx-occ\n")
    assert [s["label"] for s in regulators[0]["sources"]] == ["news", "undeclared"]


def test_undeclared_source_is_refused_like_an_explicit_false(config, caplog):
    with caplog.at_level("ERROR"), pytest.raises(SystemExit):
        _load(config, "version: 1\nmodules:\n  - id: xx-occ\n    on_enable: backfill_all\n    sources: [undeclared]\n")
    assert "xx-occ/undeclared does not support backfill" in caplog.text


def test_refused_backfill_writes_nothing(config, tmp_path, caplog):
    """Never a silent no-op, never a partial success: the refusal lands before
    a run row exists or an item is inserted."""
    engine = init_db(str(tmp_path / "test.db"))
    with caplog.at_level("ERROR"), pytest.raises(SystemExit) as exc:
        _load(config, "version: 1\ndefaults:\n  on_enable: backfill_all\nmodules:\n  - id: xx-occ\n")
    assert exc.value.code == 1
    with engine.connect() as conn:
        assert conn.execute(sa_text("SELECT COUNT(*) FROM runs")).scalar() == 0
        assert conn.execute(sa_text("SELECT COUNT(*) FROM items")).scalar() == 0


# ---------------------------------------------------------------------------
# The real registry
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config", "regulators")
    return {r["id"]: r for r in load_regulators(path)}


def _source(registry, regulator_id, label):
    return next(s for s in registry[regulator_id]["sources"] if s["label"] == label)


@pytest.mark.parametrize("regulator_id,label,first_run", [
    ("us-occ", "news", 10),
    ("us-nmls", "state_licensing", 50),
    ("ae-fsra", "announcements", 1113),
    ("uk-fca", "notices_and_decisions", 5980),
    ("uk-fca", "policy_and_guidance", 3743),
    ("esma", "library", 7260),
    ("sg-mas", "media_releases", 1824),
])
def test_researched_volume_figures_are_declared_with_a_date(registry, regulator_id, label, first_run):
    estimate = _source(registry, regulator_id, label)["volume_estimate"]
    assert estimate["first_run"] == first_run
    assert estimate["as_of"] is not None


@pytest.mark.parametrize("regulator_id,label", [
    ("us-federal-register", "federal_register"),
    ("sg-mas", "media_releases"),
    ("ae-fsra", "announcements"),
    ("uk-fca", "notices_and_decisions"),
    ("esma", "library"),
])
def test_researched_sources_declare_backfill_support(registry, regulator_id, label):
    assert source_backfill(_source(registry, regulator_id, label))["supported"] is True


def test_occ_refusal_reads_like_the_issue_wrote_it(registry):
    with pytest.raises(BackfillNotSupportedError) as exc:
        require_backfill_supported("us-occ", "news", _source(registry, "us-occ", "news"))
    assert str(exc.value) == (
        "us-occ/news does not support backfill: the feed retains only the 10 most "
        "recent items, so there is no history to fetch. "
        "Use on_enable: new_only - you will get everything published from now on."
    )


def test_undeclared_registry_sources_default_to_unsupported(registry):
    """Most of the registry is undeclared, and that has to stay honest rather
    than optimistic."""
    undeclared = [
        (r["id"], s["label"])
        for r in registry.values() for s in r.get("sources", []) or []
        if "backfill" not in s
    ]
    assert undeclared, "expected the bulk of the registry to carry no declaration"
    for regulator_id, label in undeclared:
        with pytest.raises(BackfillNotSupportedError):
            require_backfill_supported(regulator_id, label, _source(registry, regulator_id, label))


def test_every_script_backed_declaration_names_a_script_that_exists(registry):
    declared = [
        (r["id"], s["label"], source_backfill(s))
        for r in registry.values() for s in r.get("sources", []) or []
        if source_backfill(s)["method"] == "script"
    ]
    assert declared
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for regulator_id, label, block in declared:
        assert os.path.isfile(os.path.join(root, block["script"])), f"{regulator_id}/{label}"


def test_script_existence_is_not_asserted_where_scripts_are_not_shipped(tmp_path, monkeypatch):
    """The webui and mcp images deliberately omit scripts/ - they never run a
    backfill. Enforcing the file's existence there made every registry load
    fail in both services, which took the module view and the empty-state
    explanations down with it. The check is meaningful only where backfill
    scripts actually live.
    """
    import regradar.registry as registry
    source = {"label": "x", "type": "rss",
              "backfill": {"supported": True, "method": "script", "script": "scripts/nope.py"}}

    monkeypatch.setattr(registry, "_REPO_ROOT", str(tmp_path))       # no scripts/ dir
    assert registry._source_backfill_error(source) is None

    (tmp_path / "scripts").mkdir()                                    # scripts/ present
    assert "does not exist" in (registry._source_backfill_error(source) or "")
