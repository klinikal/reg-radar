from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from collector.schema import Item
from scripts.source_health import (
    MAX_FIXTURE_BYTES,
    assert_contract,
    capture,
    check_source,
    close_issue_if_open,
    find_open_issue,
    fixture_extension,
    fixture_path,
    issue_title,
    iter_enabled_sources,
    open_or_update_issue,
    run_all_checks,
)


def _make_item(title="A title", url="https://example.com/1", published_at=None, date_inferred=False):
    now = datetime.now(timezone.utc)
    return Item(
        id="x", regulator_id="esma", source_label="all_news", title=title, url=url,
        published_at=published_at, fetched_at=now, date_inferred=date_inferred,
        language="en", content_snippet="", source_type="rss",
    )


def test_assert_contract_passes_with_resolved_published_at():
    assert_contract([_make_item(published_at=datetime.now(timezone.utc))])


def test_assert_contract_passes_with_date_inferred():
    assert_contract([_make_item(date_inferred=True)])


def test_assert_contract_fails_on_empty_list():
    with pytest.raises(AssertionError, match="0 items"):
        assert_contract([])


def test_assert_contract_fails_when_no_item_has_title_url_and_date():
    # published_at is None and date_inferred is False - neither condition is met.
    with pytest.raises(AssertionError):
        assert_contract([_make_item(published_at=None, date_inferred=False)])


def test_assert_contract_fails_on_empty_title():
    with pytest.raises(AssertionError):
        assert_contract([_make_item(title="", date_inferred=True)])


def test_fixture_extension_rss_and_atom_are_xml():
    assert fixture_extension("rss") == "xml"
    assert fixture_extension("atom") == "xml"


def test_fixture_extension_scrape_and_page_watch_are_html():
    assert fixture_extension("scrape") == "html"
    assert fixture_extension("page_watch") == "html"


def test_fixture_path_uses_regulator_id_and_label():
    path = fixture_path("esma", {"label": "all_news", "type": "rss"})
    assert path.parts[-2:] == ("esma", "all_news.xml")


def test_iter_enabled_sources_skips_disabled_regulator():
    regulators = [{"id": "r1", "enabled": False, "sources": [{"label": "s1", "type": "rss"}]}]
    assert list(iter_enabled_sources(regulators)) == []


def test_iter_enabled_sources_skips_disabled_source():
    regulators = [{"id": "r1", "sources": [
        {"label": "s1", "type": "rss", "enabled": False},
        {"label": "s2", "type": "rss"},
    ]}]
    results = list(iter_enabled_sources(regulators))
    assert len(results) == 1
    assert results[0][1]["label"] == "s2"


def test_iter_enabled_sources_defaults_enabled_true():
    regulators = [{"id": "r1", "sources": [{"label": "s1", "type": "rss"}]}]
    results = list(iter_enabled_sources(regulators))
    assert len(results) == 1


def test_check_source_returns_none_on_success():
    regulator = {"id": "esma", "cloudflare_protected": False}
    source = {"label": "all_news", "type": "rss", "url": "https://example.com/rss"}
    with patch("scripts.source_health.fetch_raw", return_value=b"<rss></rss>"), \
         patch("scripts.source_health.parse_fixture", return_value=[_make_item(date_inferred=True)]):
        assert check_source(regulator, source, {}) is None


def test_check_source_returns_error_string_on_fetch_failure():
    regulator = {"id": "esma", "cloudflare_protected": False}
    source = {"label": "all_news", "type": "rss", "url": "https://example.com/rss"}
    with patch("scripts.source_health.fetch_raw", side_effect=RuntimeError("failed to fetch")):
        error = check_source(regulator, source, {})
    assert error == "failed to fetch"


def test_check_source_returns_error_string_on_zero_items():
    regulator = {"id": "esma", "cloudflare_protected": False}
    source = {"label": "all_news", "type": "rss", "url": "https://example.com/rss"}
    with patch("scripts.source_health.fetch_raw", return_value=b"<rss></rss>"), \
         patch("scripts.source_health.parse_fixture", return_value=[]):
        error = check_source(regulator, source, {})
    assert "0 items" in error


def test_issue_title_is_stable_for_the_same_source():
    assert issue_title("esma", "all_news") == issue_title("esma", "all_news")
    assert issue_title("esma", "all_news") != issue_title("esma", "library")


def _mock_gh_result(stdout=""):
    result = MagicMock()
    result.stdout = stdout
    return result


def test_find_open_issue_returns_number_on_title_match():
    payload = '[{"number": 42, "title": "source-health: esma/all_news failing"}]'
    with patch("scripts.source_health._gh", return_value=_mock_gh_result(payload)):
        assert find_open_issue("esma", "all_news") == "42"


def test_find_open_issue_returns_none_when_no_title_matches():
    payload = '[{"number": 42, "title": "some other issue"}]'
    with patch("scripts.source_health._gh", return_value=_mock_gh_result(payload)):
        assert find_open_issue("esma", "all_news") is None


def test_open_or_update_issue_creates_when_none_open():
    with patch("scripts.source_health.find_open_issue", return_value=None), \
         patch("scripts.source_health._gh") as mock_gh:
        open_or_update_issue("esma", "all_news", "boom")
    args = mock_gh.call_args[0]
    assert args[0] == "issue" and args[1] == "create"


def test_open_or_update_issue_comments_when_already_open():
    with patch("scripts.source_health.find_open_issue", return_value="42"), \
         patch("scripts.source_health._gh") as mock_gh:
        open_or_update_issue("esma", "all_news", "boom")
    args = mock_gh.call_args[0]
    assert args[0] == "issue" and args[1] == "comment" and args[2] == "42"


def test_close_issue_if_open_closes_existing_issue():
    with patch("scripts.source_health.find_open_issue", return_value="42"), \
         patch("scripts.source_health._gh") as mock_gh:
        close_issue_if_open("esma", "all_news")
    args = mock_gh.call_args[0]
    assert args[0] == "issue" and args[1] == "close" and args[2] == "42"


def test_close_issue_if_open_noop_when_nothing_open():
    with patch("scripts.source_health.find_open_issue", return_value=None), \
         patch("scripts.source_health._gh") as mock_gh:
        close_issue_if_open("esma", "all_news")
    mock_gh.assert_not_called()


def test_run_all_checks_files_issue_on_failure_and_closes_on_recovery():
    regulators = [{"id": "esma", "sources": [
        {"label": "broken", "type": "rss", "url": "https://example.com/broken"},
        {"label": "healthy", "type": "rss", "url": "https://example.com/healthy"},
    ]}]

    def fake_check(regulator, source, cfg):
        return "boom" if source["label"] == "broken" else None

    with patch("scripts.source_health.check_source", side_effect=fake_check), \
         patch("scripts.source_health.open_or_update_issue") as mock_open, \
         patch("scripts.source_health.close_issue_if_open") as mock_close:
        results = run_all_checks(regulators, {})

    assert results == {"esma/broken": "boom", "esma/healthy": None}
    mock_open.assert_called_once_with("esma", "broken", "boom")
    mock_close.assert_called_once_with("esma", "healthy")


def test_capture_writes_fixture_file(tmp_path):
    regulator = {"id": "esma", "cloudflare_protected": False}
    source = {"label": "all_news", "type": "rss", "url": "https://example.com/rss"}
    with patch("scripts.source_health.fetch_raw", return_value=b"<rss>content</rss>"), \
         patch("scripts.source_health.FIXTURES_DIR", tmp_path):
        path, size = capture(regulator, source, {})
    assert path.read_bytes() == b"<rss>content</rss>"
    assert size == len(b"<rss>content</rss>")


def test_capture_dry_run_does_not_write_file(tmp_path):
    regulator = {"id": "esma", "cloudflare_protected": False}
    source = {"label": "all_news", "type": "rss", "url": "https://example.com/rss"}
    with patch("scripts.source_health.fetch_raw", return_value=b"<rss>content</rss>"), \
         patch("scripts.source_health.FIXTURES_DIR", tmp_path):
        path, size = capture(regulator, source, {}, dry_run=True)
    assert not path.exists()
    assert size == len(b"<rss>content</rss>")


def test_capture_raises_and_does_not_write_when_over_size_cap():
    regulator = {"id": "esma", "cloudflare_protected": False}
    source = {"label": "all_news", "type": "rss", "url": "https://example.com/rss"}
    oversized = b"x" * (MAX_FIXTURE_BYTES + 1)
    with patch("scripts.source_health.fetch_raw", return_value=oversized):
        with pytest.raises(ValueError, match="exceeds"):
            capture(regulator, source, {})


def test_run_all_checks_skips_issue_filing_when_disabled():
    regulators = [{"id": "esma", "sources": [{"label": "broken", "type": "rss", "url": "https://example.com/broken"}]}]
    with patch("scripts.source_health.check_source", return_value="boom"), \
         patch("scripts.source_health.open_or_update_issue") as mock_open, \
         patch("scripts.source_health.close_issue_if_open") as mock_close:
        run_all_checks(regulators, {}, file_issues=False)
    mock_open.assert_not_called()
    mock_close.assert_not_called()
