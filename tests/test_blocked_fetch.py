"""Blocked/challenged fetch detection and the `blocked` source state
(issue #39 B), plus the staleness assertion (issue #39 A).

The two failure modes this covers present identically to an operator today:
a WAF block and a silent decoy both look like "this source returned nothing
new", indistinguishable from a genuinely quiet regulator. The tests that
carry the most weight here are the two the issue calls out explicitly - a
blocked fetch never reaching the parser, and `blocked` being its own state
alongside the existing error/empty-run counters rather than folded into
either.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from collector.adapters.http import BlockedFetchError, detect_block, fetch_bytes, fetch_html
from collector.adapters.scrape import ScrapeAdapter
from collector.fetcher import LARGE_BODY_ZERO_ITEMS_BYTES, fetch_source
from collector.main import run_collection
from collector.storage import (
    SourceErrorRow,
    init_db,
    record_source_blocked,
    record_source_empty_run,
    record_source_error,
    reset_source_blocked,
    upsert_regulator,
)
from mcpserver.queries import get_blocked_sources, get_health, list_regulators
from scripts.source_health import (
    assert_contract,
    check_source,
    check_staleness,
    parse_fixture,
    staleness_thresholds,
)

FIXTURES = Path(__file__).parent / "fixtures" / "blocked"

SCRAPE_SOURCE = {
    "label": "publications",
    "type": "scrape",
    "url": "https://example-authority.example/publications",
    "scrape": {
        "item_selector": ".publication-item",
        "title_selector": ".title",
        "link_selector": "a",
        "date_selector": ".date",
    },
}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def _engine():
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return init_db(f.name)


def _mock_get_client(status_code: int, headers: dict, body: str):
    """A patched httpx.Client whose .get() returns one canned response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers
    resp.text = body
    resp.content = body.encode("utf-8")
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.get.return_value = resp
    return client


def _mock_stream_client(status_code: int, headers: dict, body: bytes):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers
    resp.iter_bytes.return_value = [body]
    resp.raise_for_status = MagicMock()
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.stream.return_value.__enter__ = MagicMock(return_value=resp)
    client.stream.return_value.__exit__ = MagicMock(return_value=False)
    return client


# --- detect_block: every signature in the issue's table ------------------

def test_detect_block_cf_mitigated_challenge_header():
    signature = detect_block(403, {"cf-mitigated": "challenge"}, _fixture("cloudflare_403.html"))
    assert signature is not None and "cf-mitigated" in signature


def test_detect_block_sucuri_geo02_names_the_code():
    # GEO02 is a geo-block no workaround fixes, so the signature has to
    # carry the code - otherwise an operator burns a day on a new UA.
    signature = detect_block(403, {"x-sucuri-block": "GEO02"}, _fixture("sucuri_geo02_403.html"))
    assert signature is not None and "GEO02" in signature


def test_detect_block_server_cloudflare_on_non_200():
    signature = detect_block(403, {"server": "cloudflare"}, "<html>nope</html>")
    assert signature is not None and "cloudflare" in signature


def test_detect_block_server_cloudflare_on_200_is_not_a_block():
    # Plenty of regulators sit behind Cloudflare and serve real content
    # through it. Only a non-200 makes the server header suspicious.
    assert detect_block(200, {"server": "cloudflare"}, "<html><body>real content</body></html>") is None


def test_detect_block_interstitial_marker_in_a_200_body():
    signature = detect_block(200, {}, _fixture("cloudflare_challenge_200.html"))
    assert signature is not None and "interstitial" in signature


def test_detect_block_genuine_404_is_not_a_block():
    # A real 404 must stay a real 404 - misreporting it as blocked sends an
    # operator hunting a WAF that isn't there (the SEC case in the issue is
    # the reverse: a 403 masking a real 404, which is why status alone is
    # never the signal).
    assert detect_block(404, {"server": "nginx"}, "<html>Not Found</html>") is None


def test_detect_block_tolerates_unreadable_headers():
    assert detect_block(200, None, "<html>fine</html>") is None


# --- the block reaches the fetch layer, not the parser -------------------

def test_fetch_html_raises_on_cf_mitigated_403():
    with patch("httpx.Client", return_value=_mock_get_client(
            403, {"cf-mitigated": "challenge", "server": "cloudflare"}, _fixture("cloudflare_403.html"))):
        with pytest.raises(BlockedFetchError, match="cf-mitigated"):
            fetch_html("https://example.com/x", "ua", 10, 3)


def test_fetch_html_does_not_retry_a_block():
    # Retrying a WAF block only deepens it, and #39 is explicit that this
    # layer surfaces blocks rather than routing around them.
    client = _mock_get_client(403, {"x-sucuri-block": "GEO02"}, _fixture("sucuri_geo02_403.html"))
    with patch("httpx.Client", return_value=client):
        with pytest.raises(BlockedFetchError):
            fetch_html("https://example.com/x", "ua", 10, 5)
    assert client.get.call_count == 1


def test_fetch_bytes_raises_on_sucuri_geo02():
    with patch("httpx.Client", return_value=_mock_stream_client(
            403, {"x-sucuri-block": "GEO02"}, _fixture("sucuri_geo02_403.html").encode())):
        with pytest.raises(BlockedFetchError, match="GEO02"):
            fetch_bytes("https://example.com/feed.xml", "ua", 10, 3)


def test_fetch_bytes_raises_on_200_challenge_interstitial():
    with patch("httpx.Client", return_value=_mock_stream_client(
            200, {}, _fixture("cloudflare_challenge_200.html").encode())):
        with pytest.raises(BlockedFetchError, match="interstitial"):
            fetch_bytes("https://example.com/feed.xml", "ua", 10, 3)


@pytest.mark.parametrize("status,headers", [
    (403, {"cf-mitigated": "challenge", "server": "cloudflare"}),
    (200, {}),
])
def test_challenge_page_never_reaches_the_parser(status, headers):
    """THE test for #39's fourth requirement.

    The challenge fixture deliberately contains three `.publication-item`
    blocks - exactly what this source's item_selector matches. If
    classification happened after parsing, three plausible-looking garbage
    rows ("Verify you are human", "Ray ID: ...") would land in the corpus.
    Garbage in the corpus is worse than collecting nothing, so parse() must
    never be called at all.
    """
    body = _fixture("cloudflare_challenge_200.html")

    # Sanity-check the fixture itself: if it stopped matching the selector,
    # this test would pass for the wrong reason.
    assert len(ScrapeAdapter().parse(body, SCRAPE_SOURCE, "xx-example", {})) == 3

    def _never(*args, **kwargs):
        raise AssertionError("parse() was called on a blocked response")

    with patch("httpx.Client", return_value=_mock_get_client(status, headers, body)), \
         patch.object(ScrapeAdapter, "parse", side_effect=_never):
        with pytest.raises(BlockedFetchError):
            fetch_source(SCRAPE_SOURCE, "xx-example", None, {"user_agent": "ua", "timeout": 10, "retries": 1})


def test_blocked_challenge_page_writes_zero_items_to_the_database():
    """Same guarantee, asserted end to end through a real collection run:
    zero rows in `items`, and the source recorded as blocked."""
    engine = _engine()
    reg = {"id": "xx-example", "name": "Example", "enabled": True, "sources": [SCRAPE_SOURCE]}
    cfg = {"user_agent": "ua", "timeout": 10, "retries": 1, "flaresolverr_url": None}

    with patch("httpx.Client", return_value=_mock_get_client(
            200, {}, _fixture("cloudflare_challenge_200.html"))):
        run_collection(engine, [reg], cfg)

    with engine.connect() as conn:
        assert conn.execute(sa_text("SELECT COUNT(*) FROM items")).scalar() == 0

    with Session(engine) as session:
        row = session.query(SourceErrorRow).filter_by(regulator_id="xx-example", source_label="publications").one()
        assert row.consecutive_blocked_runs == 1
        # Blocked is its own state: the fetch didn't error and the site
        # wasn't quiet, so neither of the other counters moved.
        assert (row.consecutive_failures or 0) == 0
        assert (row.consecutive_empty_runs or 0) == 0


# --- large-body-zero-items decoy -----------------------------------------

def _padded_decoy(size: int) -> str:
    body = _fixture("mas_decoy_200.html")
    padding = "<!-- " + ("maintenance " * 64) + " -->\n"
    while len(body.encode("utf-8")) < size:
        body = body.replace("<!--PADDING-->", padding + "<!--PADDING-->", 1)
    return body


def test_large_body_yielding_zero_items_is_treated_as_blocked():
    # The MAS case: HTTP 200, no challenge marker, no WAF header - just
    # 853,817 bytes of the wrong content. Nothing but "large body AND zero
    # parsed items" catches it.
    decoy = _padded_decoy(LARGE_BODY_ZERO_ITEMS_BYTES + 1024)
    assert ScrapeAdapter().parse(decoy, SCRAPE_SOURCE, "xx-example", {}) == []

    with patch("httpx.Client", return_value=_mock_get_client(200, {}, decoy)):
        with pytest.raises(BlockedFetchError, match="0 items"):
            fetch_source(SCRAPE_SOURCE, "xx-example", None, {"user_agent": "ua", "timeout": 10, "retries": 1})


def test_large_body_that_parses_is_not_blocked():
    # The signal is large body AND zero items, never size alone: MAS's real
    # JSON is ~114KB and parses fine. A size-only threshold would flag the
    # working source and the decoy identically.
    body = _padded_decoy(LARGE_BODY_ZERO_ITEMS_BYTES + 1024).replace(
        "<!--PADDING-->",
        '<div class="publication-item"><a class="title" href="/p/1">Real notice</a>'
        '<span class="date">2026-09-01</span></div>',
        1,
    )
    with patch("httpx.Client", return_value=_mock_get_client(200, {}, body)):
        items = fetch_source(SCRAPE_SOURCE, "xx-example", None, {"user_agent": "ua", "timeout": 10, "retries": 1})
    assert len(items) == 1


def test_small_body_with_zero_items_is_not_blocked():
    # A quiet listing page is not a block - it is the empty-run counter's
    # job, and flagging it here would drown the real alarm in noise.
    with patch("httpx.Client", return_value=_mock_get_client(200, {}, "<html><body>Nothing today.</body></html>")):
        assert fetch_source(SCRAPE_SOURCE, "xx-example", None,
                            {"user_agent": "ua", "timeout": 10, "retries": 1}) == []


# --- the `blocked` source state ------------------------------------------

def test_record_source_blocked_is_independent_of_the_other_counters():
    engine = _engine()
    record_source_error(engine, "esma", "all_news", "timeout")
    record_source_empty_run(engine, "esma", "all_news")
    record_source_blocked(engine, "esma", "all_news", "cf-mitigated: challenge")
    record_source_blocked(engine, "esma", "all_news", "cf-mitigated: challenge")

    with Session(engine) as session:
        row = session.query(SourceErrorRow).filter_by(regulator_id="esma", source_label="all_news").one()
        assert row.consecutive_blocked_runs == 2
        assert row.consecutive_failures == 1
        assert row.consecutive_empty_runs == 1
        assert row.last_blocked_reason == "cf-mitigated: challenge"
        assert row.last_blocked_at is not None


def test_reset_source_blocked_clears_only_the_blocked_counter():
    engine = _engine()
    record_source_error(engine, "esma", "all_news", "timeout")
    record_source_blocked(engine, "esma", "all_news", "blocked")
    reset_source_blocked(engine, "esma", "all_news")

    with Session(engine) as session:
        row = session.query(SourceErrorRow).filter_by(regulator_id="esma", source_label="all_news").one()
        assert row.consecutive_blocked_runs == 0
        assert row.consecutive_failures == 1


def test_init_db_adds_blocked_columns_to_a_pre_existing_database():
    from sqlalchemy import create_engine
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    legacy = create_engine(f"sqlite:///{db_path}")
    with legacy.connect() as conn:
        conn.execute(sa_text(
            "CREATE TABLE source_errors (id INTEGER PRIMARY KEY, regulator_id TEXT, source_label TEXT, "
            "consecutive_failures INTEGER, last_error TEXT, last_attempt_at DATETIME)"
        ))
        conn.commit()
    legacy.dispose()

    engine = init_db(db_path)
    with engine.connect() as conn:
        cols = [r[1] for r in conn.execute(sa_text("PRAGMA table_info(source_errors)")).fetchall()]
    assert "consecutive_blocked_runs" in cols
    assert "last_blocked_reason" in cols
    os.unlink(db_path)


def test_run_summary_log_names_the_blocked_sources():
    engine = _engine()
    reg = {"id": "xx-example", "name": "Example", "enabled": True, "sources": [SCRAPE_SOURCE]}
    cfg = {"user_agent": "ua", "timeout": 10, "retries": 1, "flaresolverr_url": None}

    with patch("httpx.Client", return_value=_mock_get_client(
            403, {"cf-mitigated": "challenge"}, _fixture("cloudflare_403.html"))), \
            patch("collector.main.logger") as mock_logger:
        run_collection(engine, [reg], cfg)

    summary = " ".join(str(c) for c in mock_logger.error.call_args_list + mock_logger.info.call_args_list)
    assert "BLOCKED" in summary
    assert "xx-example/publications" in summary
    assert "blocked=%d" in summary or "blocked=1" in summary


def test_a_clean_fetch_clears_a_previous_block():
    engine = _engine()
    record_source_blocked(engine, "xx-example", "publications", "cf-mitigated: challenge")
    reg = {"id": "xx-example", "name": "Example", "enabled": True, "sources": [SCRAPE_SOURCE]}
    cfg = {"user_agent": "ua", "timeout": 10, "retries": 1, "flaresolverr_url": None}
    good = ('<html><body><div class="publication-item">'
            '<a class="title" href="/p/1">Real notice</a><span class="date">2026-09-01</span>'
            "</div></body></html>")

    with patch("httpx.Client", return_value=_mock_get_client(200, {}, good)):
        run_collection(engine, [reg], cfg)

    with Session(engine) as session:
        row = session.query(SourceErrorRow).filter_by(regulator_id="xx-example", source_label="publications").one()
        assert row.consecutive_blocked_runs == 0


# --- surfacing: queries / webui / health ---------------------------------

def test_get_blocked_sources_reports_reason_and_count():
    engine = _engine()
    record_source_blocked(engine, "xx-example", "publications", "x-sucuri-block: GEO02")
    blocked = get_blocked_sources(engine)
    assert len(blocked) == 1
    assert blocked[0]["regulator_id"] == "xx-example"
    assert blocked[0]["last_blocked_reason"] == "x-sucuri-block: GEO02"
    assert blocked[0]["consecutive_blocked_runs"] == 1


def test_list_regulators_flags_a_blocked_regulator_for_the_webui():
    engine = _engine()
    upsert_regulator(engine, {"id": "xx-example", "name": "Example", "full_name": "Example", "jurisdiction": "eu"})
    upsert_regulator(engine, {"id": "yy-fine", "name": "Fine", "full_name": "Fine", "jurisdiction": "eu"})
    record_source_blocked(engine, "xx-example", "publications", "cf-mitigated: challenge")

    by_id = {r["id"]: r for r in list_regulators(engine)}
    # One blocked run is already a failed workaround, not a quiet day - no
    # threshold to wait out before an operator sees it.
    assert by_id["xx-example"]["blocked"] is True
    assert by_id["yy-fine"]["blocked"] is False


def test_get_health_surfaces_blocked_sources():
    engine = _engine()
    record_source_blocked(engine, "xx-example", "publications", "cf-mitigated: challenge")
    health = get_health(engine)
    assert [b["regulator_id"] for b in health["blocked_sources"]] == ["xx-example"]


# --- surfacing: webhook / digest -----------------------------------------

def _taxonomy_and_activities():
    from regradar.profiles import load_activities
    from regradar.taxonomy import build_taxonomy
    taxonomy = build_taxonomy("config/taxonomy.yaml", "config/jurisdictions.yaml")
    return taxonomy, load_activities("config/activities.yaml", taxonomy)


def test_webhook_delivers_blocked_sources_even_with_no_new_items(tmp_path, monkeypatch):
    import yaml
    from collector.storage import get_webhook_cursor
    from regradar.webhooks import dispatch_all

    engine = _engine()
    upsert_regulator(engine, {"id": "esma", "name": "ESMA", "full_name": "ESMA", "jurisdiction": "eu"})
    record_source_blocked(engine, "esma", "all_news", "cf-mitigated: challenge")
    monkeypatch.setenv("WEBHOOK_SECRET_EU", "s1")
    config_path = tmp_path / "webhooks.yaml"
    config_path.write_text(yaml.safe_dump([{
        "profile": "eu-casp", "url": "https://hooks.example/ok", "secret_env": "WEBHOOK_SECRET_EU",
        "max_retries": 1, "backoff_seconds": 0,
    }]))

    delivered = []

    def handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(200)

    taxonomy, activities = _taxonomy_and_activities()
    client = httpx.Client(transport=httpx.MockTransport(handler))
    dispatch_all(engine, str(config_path), taxonomy, activities, "config/profiles", client=client)

    # "No new items" is exactly what a blocked source looks like, so the
    # old "nothing new -> send nothing" short-circuit made this alarm
    # unreachable through the webhook path.
    assert len(delivered) == 1
    assert delivered[0]["items"] == []
    assert delivered[0]["blocked_sources"][0]["regulator_id"] == "esma"

    from regradar.webhooks import subscriber_id
    sid = subscriber_id({"profile": "eu-casp", "url": "https://hooks.example/ok"})
    # A blocked-only delivery must not advance the cursor past items that
    # haven't arrived yet.
    assert get_webhook_cursor(engine, sid) is None


def test_digest_renders_a_blocked_section():
    from regradar.digest import render_html, render_markdown

    blocked = [{"regulator_id": "xx-example", "source_label": "publications",
                "last_blocked_reason": "x-sucuri-block: GEO02", "consecutive_blocked_runs": 3}]
    markdown = render_markdown("eu-casp", [], blocked)
    assert "blocked source" in markdown
    assert "xx-example/publications" in markdown
    assert "GEO02" in markdown

    html = render_html("eu-casp", [], blocked)
    assert "blocked source" in html
    assert "GEO02" in html


def test_digest_without_blocked_sources_is_unchanged():
    from regradar.digest import render_markdown
    assert "blocked" not in render_markdown("eu-casp", [], [])


# --- staleness assertion (#39 A) ------------------------------------------

def _stale_items():
    source = {"label": "news", "type": "rss", "url": "https://example-authority.example/news"}
    return source, parse_fixture(source, "xx-example", (FIXTURES / "stale_but_valid.xml").read_bytes())


def test_stale_but_valid_feed_passes_the_contract_and_fails_staleness():
    # Well-formed, 200, items present, every existing assertion satisfied -
    # newest entry July 2020. FINRA's shape: only the staleness assertion
    # catches it.
    source, items = _stale_items()
    assert len(items) == 2
    assert_contract(items)  # does not raise

    error, warning = check_staleness(items, source)
    assert error is not None and "365-day error threshold" in error


def test_staleness_defaults_are_90_warn_365_error():
    assert staleness_thresholds({"label": "news"}) == (90, 365)


def test_staleness_warns_between_the_thresholds():
    source, items = _stale_items()
    now = max(i.published_at for i in items) + __import__("datetime").timedelta(days=120)
    error, warning = check_staleness(items, source, now=now)
    assert error is None
    assert warning is not None and "90-day warn threshold" in warning


def test_per_source_staleness_override_suppresses_the_error():
    # The FATF grey list fires roughly three times a year - the default
    # would be pure noise there, so the override has to actually work.
    source, items = _stale_items()
    source = {**source, "staleness": {"warn_days": 200, "error_days": 5000}}
    assert staleness_thresholds(source) == (200, 5000)

    error, warning = check_staleness(items, source)
    assert error is None
    assert warning is not None


def test_check_source_returns_the_staleness_error_and_collects_warnings():
    source, items = _stale_items()
    regulator = {"id": "xx-example"}
    with patch("scripts.source_health.fetch_raw", return_value=(FIXTURES / "stale_but_valid.xml").read_bytes()):
        assert "stale" in check_source(regulator, source, {})

        warnings = []
        overridden = {**source, "staleness": {"error_days": 5000}}
        assert check_source(regulator, overridden, {}, warnings=warnings) is None
        assert warnings and "stale" in warnings[0]


def test_staleness_is_silent_when_no_item_carries_a_real_date():
    # An inferred date is our own fetch-time guess, not the source's claim -
    # asserting on it would fire on every date-less source every night.
    source, items = _stale_items()
    for item in items:
        item.published_at = None
        item.date_inferred = True
    assert check_staleness(items, source) == (None, None)


def test_a_blocked_item_pdf_does_not_discard_the_listing_items():
    # Regression guard for the block raise itself: the scrape adapter
    # fetches each matched item's PDF, and a block on one attachment must
    # not throw away the items the listing already yielded. The listing
    # fetch is what decides whether the source is blocked.
    from collector.adapters.http import fetch_bytes as real_fetch_bytes  # noqa: F401

    source = {**SCRAPE_SOURCE, "scrape": {**SCRAPE_SOURCE["scrape"], "extract_pdf_text": True}}
    listing = ('<html><body><div class="publication-item">'
               '<a class="title" href="/p/1.pdf">Real notice</a><span class="date">2026-09-01</span>'
               "</div></body></html>")

    def _client(*args, **kwargs):
        # First call is the listing (client.get), the PDF goes through
        # client.stream and is Sucuri-blocked.
        client = _mock_get_client(200, {}, listing)
        blocked = _mock_stream_client(403, {"x-sucuri-block": "GEO02"}, b"blocked")
        client.stream = blocked.stream
        return client

    with patch("httpx.Client", side_effect=_client):
        items = fetch_source(source, "xx-example", None, {"user_agent": "ua", "timeout": 10, "retries": 1})
    assert len(items) == 1


# --- cloudflare_protected with no FlareSolverr configured -----------------

def test_cloudflare_protected_without_flaresolverr_is_blocked_not_skipped():
    """Found while shipping FATF (#47), which collects only through
    FlareSolverr. run_collection used to `continue` past a
    cloudflare_protected regulator when FLARESOLVERR_URL was unset, before
    any fetch: nothing recorded, no counter moved, and the run still
    finished "success". A green run plus one WARNING is precisely the
    invisible failure #39 exists to kill, so a misconfigured install has to
    be as loud as a blocked one.
    """
    engine = _engine()
    reg = {
        "id": "intl-fatf", "name": "FATF", "enabled": True,
        "cloudflare_protected": True,
        "sources": [SCRAPE_SOURCE, {**SCRAPE_SOURCE, "label": "disabled_one", "enabled": False}],
    }
    cfg = {"user_agent": "ua", "timeout": 10, "retries": 1, "flaresolverr_url": None}

    run_collection(engine, [reg], cfg)

    with Session(engine) as session:
        row = session.query(SourceErrorRow).filter_by(
            regulator_id="intl-fatf", source_label="publications").one()
        assert row.consecutive_blocked_runs == 1
        assert "FLARESOLVERR_URL" in (row.last_blocked_reason or "")
        # A disabled source is not a blocked one - it was never meant to run.
        assert session.query(SourceErrorRow).filter_by(
            regulator_id="intl-fatf", source_label="disabled_one").one_or_none() is None

    with engine.connect() as conn:
        status, errors = conn.execute(sa_text(
            "SELECT status, total_errors FROM runs ORDER BY started_at DESC LIMIT 1")).one()
    assert status == "partial", "a regulator that collected nothing must not read as success"
    assert errors == 1
