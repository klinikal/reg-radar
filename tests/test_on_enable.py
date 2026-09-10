"""`on_enable` watermarks and page_watch baselines (issue #51).

The two that matter are test_reenabling_an_unchanged_page_watch_emits_nothing
and test_first_ever_enable_establishes_a_baseline_and_reports_no_change: a
page_watch source's whole state is a hash of a fragment, so a lost or reset
baseline reports a change to a page that never changed - a false positive in
a tool whose entire value is that its alerts are real.
"""
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import text as sa_text

from collector.main import (
    ModulesConfigError,
    apply_on_enable,
    load_regulators,
    run_collection,
)
from collector.schema import Item, make_item_id, make_page_watch_item_id
from collector.storage import (
    get_module_enable_state,
    insert_item,
    get_page_watch_baseline,
    init_db,
)

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


def _resolve(config, modules_yaml):
    (config / "modules.yaml").write_text(modules_yaml)
    return load_regulators(
        str(config / "regulators"),
        modules_path=str(config / "modules.yaml"),
        jurisdictions_path=str(config / "jurisdictions.yaml"),
    )


@pytest.fixture
def engine():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    yield init_db(db_path)
    os.unlink(db_path)


def _cfg():
    return {"user_agent": "test/1.0", "timeout": 10, "retries": 1, "flaresolverr_url": None}


def _reg(module=None, sources=None):
    return {
        "id": "xx-deep", "name": "Deep", "enabled": True,
        "sources": sources or [{"label": "api", "url": "http://example.com/api", "type": "json_api"}],
        **({"module": module} if module else {}),
    }


def _rss_item(published_at, url="http://example.com/a"):
    return Item(
        id=make_item_id("xx-deep", url, published_at),
        regulator_id="xx-deep", source_label="api", title="A", url=url,
        published_at=published_at, fetched_at=datetime.now(timezone.utc),
        date_inferred=False, language="en", content_snippet="x", source_type="json_api",
    )


def _watched_page(text, label="statute", url="http://example.com/statute"):
    """The item a page_watch fetch produces for a given fragment - the id is
    a pure function of the content, exactly as collector.pipeline builds it."""
    now = datetime.now(timezone.utc)
    return Item(
        id=make_page_watch_item_id("xx-watch", url, text),
        regulator_id="xx-watch", source_label=label, title=label, url=url,
        published_at=now, fetched_at=now, date_inferred=True, language="en",
        content_snippet=text, source_type="page_watch",
    )


def _watch_reg():
    return {
        "id": "xx-watch", "name": "Watch", "enabled": True,
        "sources": [{"label": "statute", "url": "http://example.com/statute", "type": "page_watch"}],
    }


def _item_count(engine):
    with engine.connect() as conn:
        return conn.execute(sa_text("SELECT count(*) FROM items")).scalar()


# ---------------------------------------------------------------------------
# on_enable: the transition
# ---------------------------------------------------------------------------

def test_new_only_records_a_watermark_before_any_fetch(engine):
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "new_only"})])
    state = get_module_enable_state(engine, "xx-deep")
    assert state["on_enable"] == "new_only"
    assert state["collect_from"] >= before


def test_no_module_block_defaults_to_new_only(engine):
    apply_on_enable(engine, [_reg()])
    assert get_module_enable_state(engine, "xx-deep")["on_enable"] == "new_only"


def test_backfill_all_records_no_lower_bound(engine):
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "backfill_all"})])
    state = get_module_enable_state(engine, "xx-deep")
    assert state["on_enable"] == "backfill_all"
    assert state["collect_from"] is None


def test_backfill_from_records_the_given_date(engine):
    module = {"id": "xx-deep", "on_enable": "backfill_from", "backfill_from": datetime(2026, 1, 1)}
    apply_on_enable(engine, [_reg(module)])
    assert get_module_enable_state(engine, "xx-deep")["collect_from"] == datetime(2026, 1, 1)


def test_on_enable_is_consulted_once_not_on_every_run(engine):
    """A module already collecting normally keeps the watermark it was
    given - editing on_enable afterwards must not re-cut its history."""
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "new_only"})])
    first = get_module_enable_state(engine, "xx-deep")["collect_from"]

    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "backfill_all"})])
    state = get_module_enable_state(engine, "xx-deep")
    assert state["collect_from"] == first
    assert state["on_enable"] == "new_only"


def test_dropping_a_module_from_the_selection_records_a_disable(engine):
    apply_on_enable(engine, [_reg()])
    apply_on_enable(engine, [])
    assert get_module_enable_state(engine, "xx-deep")["enabled"] is False


def test_reenabling_after_a_gap_is_a_fresh_transition(engine):
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "new_only"})])
    first = get_module_enable_state(engine, "xx-deep")["collect_from"]
    apply_on_enable(engine, [])
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "backfill_all"})])
    state = get_module_enable_state(engine, "xx-deep")
    assert state["enabled"] is True
    assert state["collect_from"] is None and state["collect_from"] != first


# ---------------------------------------------------------------------------
# on_enable: what the watermark actually collects
# ---------------------------------------------------------------------------

def test_new_only_collects_only_what_is_published_after_the_watermark(engine):
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "new_only"})])
    old = _rss_item(datetime(2020, 1, 1, tzinfo=timezone.utc), url="http://example.com/old")
    new = _rss_item(datetime.now(timezone.utc) + timedelta(minutes=5), url="http://example.com/new")

    with patch("collector.main.fetch_source", return_value=[old, new]):
        run_collection(engine, [_reg()], _cfg())

    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(sa_text("SELECT url FROM items"))] == ["http://example.com/new"]


def test_backfill_all_collects_everything_the_source_offers(engine):
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "backfill_all"})])
    items = [_rss_item(datetime(2020, 1, 1, tzinfo=timezone.utc), url="http://example.com/old"),
             _rss_item(datetime(2026, 1, 1, tzinfo=timezone.utc), url="http://example.com/new")]

    with patch("collector.main.fetch_source", return_value=items):
        run_collection(engine, [_reg()], _cfg())

    assert _item_count(engine) == 2


def test_backfill_from_collects_back_to_the_given_date(engine):
    module = {"id": "xx-deep", "on_enable": "backfill_from", "backfill_from": datetime(2023, 1, 1)}
    apply_on_enable(engine, [_reg(module)])
    items = [_rss_item(datetime(2020, 1, 1, tzinfo=timezone.utc), url="http://example.com/too-old"),
             _rss_item(datetime(2024, 6, 1, tzinfo=timezone.utc), url="http://example.com/in-range")]

    with patch("collector.main.fetch_source", return_value=items):
        run_collection(engine, [_reg()], _cfg())

    with engine.connect() as conn:
        assert [r[0] for r in conn.execute(sa_text("SELECT url FROM items"))] == ["http://example.com/in-range"]


def test_a_failed_first_run_leaves_the_watermark_at_the_enable_time(engine):
    """The watermark is the moment the module was switched on, never the
    newest item a partial fetch happened to see - so a retry after a failed
    first run still collects everything published since."""
    apply_on_enable(engine, [_reg({"id": "xx-deep", "on_enable": "new_only"})])
    watermark = get_module_enable_state(engine, "xx-deep")["collect_from"]

    with patch("collector.main.fetch_source", side_effect=RuntimeError("network boom")):
        run_collection(engine, [_reg()], _cfg())
    assert get_module_enable_state(engine, "xx-deep")["collect_from"] == watermark

    later = _rss_item(datetime.now(timezone.utc) + timedelta(minutes=5), url="http://example.com/after")
    with patch("collector.main.fetch_source", return_value=[later]):
        run_collection(engine, [_reg()], _cfg())
    assert _item_count(engine) == 1


def test_no_module_state_at_all_collects_exactly_as_before(engine):
    """Every caller with no module state - the backfill scripts, a test,
    an install predating #51 - keeps its old behaviour."""
    with patch("collector.main.fetch_source", return_value=[_rss_item(datetime(2019, 1, 1, tzinfo=timezone.utc))]):
        run_collection(engine, [_reg()], _cfg())
    assert _item_count(engine) == 1


# ---------------------------------------------------------------------------
# on_enable: the refusal (#50) must reach the operator, never degrade
# ---------------------------------------------------------------------------

def test_backfill_from_without_a_date_on_a_supported_source_is_refused(config):
    with pytest.raises(SystemExit):
        _resolve(config, "version: 1\nmodules:\n  - id: xx-deep\n    on_enable: backfill_from\n")


def test_backfill_from_date_resolves_through_defaults(config):
    regulators = _resolve(
        config,
        "version: 1\ndefaults:\n  on_enable: backfill_from\n  backfill_from: 2026-01-01\n"
        "modules:\n  - id: xx-deep\n",
    )
    assert regulators[0]["module"]["backfill_from"] == datetime(2026, 1, 1)


def test_an_unparseable_backfill_from_is_refused(config):
    with pytest.raises(SystemExit):
        _resolve(config, "version: 1\nmodules:\n  - id: xx-deep\n    on_enable: backfill_from\n"
                         "    backfill_from: last tuesday\n")


# ---------------------------------------------------------------------------
# page_watch baselines
# ---------------------------------------------------------------------------

def test_first_ever_enable_establishes_a_baseline_and_reports_no_change(engine, caplog):
    caplog.set_level(logging.INFO, logger="collector.main")
    with patch("collector.main.fetch_source", return_value=[_watched_page("the statute, as it stands")]):
        run_collection(engine, [_watch_reg()], _cfg())

    assert _item_count(engine) == 0
    assert get_page_watch_baseline(engine, "xx-watch", "statute") is not None
    messages = caplog.text
    assert "baseline established" in messages
    assert "change detected" not in messages


def test_an_unchanged_page_reports_nothing_on_the_next_run(engine):
    page = "the statute, as it stands"
    with patch("collector.main.fetch_source", return_value=[_watched_page(page)]):
        run_collection(engine, [_watch_reg()], _cfg())
        run_collection(engine, [_watch_reg()], _cfg())
    assert _item_count(engine) == 0


def test_a_changed_page_reports_exactly_one_item(engine, caplog):
    caplog.set_level(logging.INFO, logger="collector.main")
    with patch("collector.main.fetch_source", return_value=[_watched_page("before")]):
        run_collection(engine, [_watch_reg()], _cfg())
    with patch("collector.main.fetch_source", return_value=[_watched_page("after")]):
        run_collection(engine, [_watch_reg()], _cfg())

    assert _item_count(engine) == 1
    assert "change detected" in caplog.text


def test_disabling_a_module_does_not_delete_its_page_watch_baseline(engine):
    """The regression this issue exists for: the baseline has to survive the
    module being switched off, or the first run after re-enabling reports a
    change to a page that never changed."""
    with patch("collector.main.fetch_source", return_value=[_watched_page("unchanged")]):
        run_collection(engine, [_watch_reg()], _cfg())
    baseline = get_page_watch_baseline(engine, "xx-watch", "statute")

    apply_on_enable(engine, [_watch_reg()])
    apply_on_enable(engine, [])                       # module switched off
    assert get_page_watch_baseline(engine, "xx-watch", "statute") == baseline

    apply_on_enable(engine, [_watch_reg()])           # and back on
    assert get_page_watch_baseline(engine, "xx-watch", "statute") == baseline


def test_reenabling_an_unchanged_page_watch_emits_nothing(engine, caplog):
    caplog.set_level(logging.INFO, logger="collector.main")
    with patch("collector.main.fetch_source", return_value=[_watched_page("unchanged")]):
        run_collection(engine, [_watch_reg()], _cfg())
    apply_on_enable(engine, [_watch_reg()])
    apply_on_enable(engine, [])
    apply_on_enable(engine, [_watch_reg()])
    caplog.clear()

    with patch("collector.main.fetch_source", return_value=[_watched_page("unchanged")]):
        run_collection(engine, [_watch_reg()], _cfg())

    assert _item_count(engine) == 0
    assert "change detected" not in caplog.text


def test_reenabling_a_page_that_changed_while_off_emits_one_item(engine):
    """(A) over (B): a page that genuinely changed during the disabled
    window is a correct alert, not a spurious one - suppressing the first
    post-enable emission would throw it away."""
    with patch("collector.main.fetch_source", return_value=[_watched_page("before the gap")]):
        run_collection(engine, [_watch_reg()], _cfg())
    apply_on_enable(engine, [_watch_reg()])
    apply_on_enable(engine, [])
    apply_on_enable(engine, [_watch_reg()])

    with patch("collector.main.fetch_source", return_value=[_watched_page("changed while off")]):
        run_collection(engine, [_watch_reg()], _cfg())

    assert _item_count(engine) == 1


def test_a_missing_fragment_leaves_the_baseline_untouched(engine):
    """A selector that stops matching returns zero items (adapter parse()),
    which is not evidence the page changed - resetting the baseline there
    would report a spurious change on the run after it recovers."""
    with patch("collector.main.fetch_source", return_value=[_watched_page("unchanged")]):
        run_collection(engine, [_watch_reg()], _cfg())
    baseline = get_page_watch_baseline(engine, "xx-watch", "statute")

    with patch("collector.main.fetch_source", return_value=[]):
        run_collection(engine, [_watch_reg()], _cfg())
    assert get_page_watch_baseline(engine, "xx-watch", "statute") == baseline

    with patch("collector.main.fetch_source", return_value=[_watched_page("unchanged")]):
        run_collection(engine, [_watch_reg()], _cfg())
    assert _item_count(engine) == 0


def test_an_install_with_history_but_no_baseline_adopts_it(engine):
    """The upgrade path: a page watched for months already has an item row
    but no baseline. Adopting the last item we recorded is what stops the
    first post-upgrade run from either swallowing a real change or
    reporting one that never happened."""
    from collector.storage import insert_item

    insert_item(engine, _watched_page("as last recorded"))
    with patch("collector.main.fetch_source", return_value=[_watched_page("as last recorded")]):
        run_collection(engine, [_watch_reg()], _cfg())
    assert _item_count(engine) == 1  # nothing new

    with patch("collector.main.fetch_source", return_value=[_watched_page("genuinely different")]):
        run_collection(engine, [_watch_reg()], _cfg())
    assert _item_count(engine) == 2


# --- upgrading an install that was already collecting ---------------------

def test_an_install_already_collecting_gets_no_watermark(engine):
    """#48 promises an upgraded install collects exactly what it collected
    before. #51 added a store-time filter that did not exist when that
    promise was written, so a straight new_only at migration would stamp a
    watermark at "now" and silently drop anything published earlier that had
    not been collected yet - a feed's backlog, or the window since the last
    successful run. A regulator that already has items was already being
    collected, so it gets no lower bound.
    """
    insert_item(engine, _rss_item(datetime(2026, 9, 1, tzinfo=timezone.utc)))

    apply_on_enable(engine, [{"id": "xx-deep", "module": {"id": "xx/*", "on_enable": "new_only"}}])

    state = get_module_enable_state(engine, "xx-deep")
    assert state["enabled"]
    assert state["collect_from"] is None, "an already-collecting module must not be date-bounded"


def test_a_genuinely_new_module_still_gets_its_watermark(engine):
    """The other half: a regulator with no history is a real first enable,
    and new_only must still stop it pulling a whole archive."""
    insert_item(engine, _rss_item(datetime(2026, 9, 1, tzinfo=timezone.utc)))

    apply_on_enable(engine, [{"id": "xx-occ", "module": {"id": "xx/*", "on_enable": "new_only"}}])

    assert get_module_enable_state(engine, "xx-occ")["collect_from"] is not None
