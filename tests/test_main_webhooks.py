import tempfile
from unittest.mock import patch

from collector.main import _dispatch_webhooks, run_collection_locked
from collector.storage import init_db


def _make_engine():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    return init_db(db_path), db_path


def _cfg(webhooks_path):
    return {
        "user_agent": "test/1.0", "timeout": 10, "retries": 1, "flaresolverr_url": None,
        "webhooks_path": webhooks_path,
    }


def test_dispatch_webhooks_noop_when_config_missing(tmp_path):
    engine, _ = _make_engine()
    # Should not raise, and should not attempt to load activities/taxonomy.
    _dispatch_webhooks(engine, _cfg(str(tmp_path / "missing.yaml")), taxonomy=object())


def test_dispatch_webhooks_noop_when_taxonomy_none(tmp_path):
    engine, _ = _make_engine()
    webhooks_path = tmp_path / "webhooks.yaml"
    webhooks_path.write_text("[]")
    # Should not raise even though taxonomy failed to load upstream.
    _dispatch_webhooks(engine, _cfg(str(webhooks_path)), taxonomy=None)


def test_dispatch_webhooks_calls_dispatch_all_when_config_present(tmp_path):
    engine, _ = _make_engine()
    webhooks_path = tmp_path / "webhooks.yaml"
    webhooks_path.write_text("[]")
    cfg = _cfg(str(webhooks_path))
    cfg["activities_path"] = "config/activities.yaml"
    cfg["profiles_dir"] = "config/profiles"

    from regradar.taxonomy import build_taxonomy
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")

    with patch("regradar.webhooks.dispatch_all") as mock_dispatch:
        _dispatch_webhooks(engine, cfg, taxonomy)
    mock_dispatch.assert_called_once()


def test_run_collection_locked_dispatches_webhooks_after_run_collection(tmp_path):
    engine, db_path = _make_engine()
    lock_path = str(tmp_path / ".collector.lock")
    cfg = _cfg(str(tmp_path / "missing.yaml"))

    with patch("collector.main.run_collection") as mock_run, patch("collector.main._dispatch_webhooks") as mock_dispatch:
        run_collection_locked(engine, [], cfg, lock_path)

    mock_run.assert_called_once()
    mock_dispatch.assert_called_once()


def test_dispatch_webhooks_malformed_yaml_logs_error_and_does_not_raise(tmp_path, caplog):
    from regradar.taxonomy import build_taxonomy
    engine, _ = _make_engine()
    webhooks_path = tmp_path / "webhooks.yaml"
    webhooks_path.write_text("- profile: eu-casp\n  url: [unterminated\n")
    cfg = _cfg(str(webhooks_path))
    cfg["activities_path"] = "config/activities.yaml"
    cfg["profiles_dir"] = "config/profiles"
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")

    with caplog.at_level("ERROR"):
        _dispatch_webhooks(engine, cfg, taxonomy)  # must not raise

    assert str(webhooks_path) in caplog.text


def test_run_collection_locked_survives_malformed_webhooks_yaml(tmp_path):
    # A YAML syntax error in config/webhooks.yaml must never crash a
    # scheduled collection run - only webhook delivery is skipped.
    from regradar.taxonomy import build_taxonomy
    engine, db_path = _make_engine()
    lock_path = str(tmp_path / ".collector.lock")
    webhooks_path = tmp_path / "webhooks.yaml"
    webhooks_path.write_text("- profile: eu-casp\n  url: [unterminated\n")
    cfg = _cfg(str(webhooks_path))
    cfg["activities_path"] = "config/activities.yaml"
    cfg["profiles_dir"] = "config/profiles"
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")

    with patch("collector.main.run_collection") as mock_run:
        run_collection_locked(engine, [], cfg, lock_path, taxonomy=taxonomy)  # must not raise

    mock_run.assert_called_once()
