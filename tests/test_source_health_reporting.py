"""The nightly check must survive being unable to report.

This job sat broken for an unknown number of nights: ISSUE_LABEL is
`source-health`, the repo had no such label, and `gh issue create --label
source-health` exits 1. The CalledProcessError was unhandled, so the sweep
aborted on the FIRST failing source, having checked about a fifth of the
registry, and filed nothing. The alarm for every regulator was mute, and the
only symptom was a red workflow that looked like an ordinary source failure.
"""
import subprocess
from unittest.mock import patch

import pytest

from scripts import source_health


def _err(returncode=1, stderr="could not add label: 'source-health' not found"):
    return subprocess.CalledProcessError(returncode, ["gh"], stderr=stderr)


def test_a_failed_issue_create_does_not_raise():
    """The regression. Previously this propagated and killed the run."""
    with patch.object(source_health, "find_open_issue", return_value=None):
        with patch.object(source_health, "_gh", side_effect=_err()):
            assert source_health.open_or_update_issue("es-cnmv", "press_releases", "boom") is False


def test_a_successful_report_says_so():
    with patch.object(source_health, "find_open_issue", return_value=None):
        with patch.object(source_health, "_gh"):
            assert source_health.open_or_update_issue("es-cnmv", "press_releases", "boom") is True


def test_a_failed_close_does_not_raise():
    with patch.object(source_health, "find_open_issue", side_effect=_err()):
        assert source_health.close_issue_if_open("es-cnmv", "press_releases") is False


def test_the_sweep_continues_past_a_source_it_cannot_report(monkeypatch):
    """The property that actually matters: one unreportable source must not
    stop the remaining sources being checked."""
    regulators = [{"id": "r1", "enabled": True, "sources": [
        {"label": "a", "type": "rss", "enabled": True},
        {"label": "b", "type": "rss", "enabled": True},
        {"label": "c", "type": "rss", "enabled": True},
    ]}]
    monkeypatch.setattr(source_health, "check_source", lambda *a, **k: "broken")
    monkeypatch.setattr(source_health, "ensure_issue_label", lambda: None)
    monkeypatch.setattr(source_health, "open_or_update_issue", lambda *a, **k: False)

    unreported = []
    results = source_health.run_all_checks(regulators, {}, file_issues=True, report_failures=unreported)

    assert len(results) == 3, "every source must still be checked"
    assert unreported == ["r1/a", "r1/b", "r1/c"]


def test_ensure_label_tolerates_an_existing_label(caplog):
    done = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="label already exists")
    with patch.object(source_health, "_gh", return_value=done):
        source_health.ensure_issue_label()
    assert "could not ensure" not in caplog.text


def test_ensure_label_warns_when_it_cannot_create_one(caplog):
    done = subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="HTTP 403 forbidden")
    with patch.object(source_health, "_gh", return_value=done):
        with caplog.at_level("WARNING"):
            source_health.ensure_issue_label()
    assert "could not ensure" in caplog.text
