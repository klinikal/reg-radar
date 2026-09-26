import fcntl
import glob
import logging
import os
import sys
import time
from datetime import date, datetime, timezone

import yaml
from sqlalchemy import text as sa_text
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from collector.detail import fetch_detail_body
from collector.fetcher import fetch_source
from collector.storage import (
    regulator_has_items,
    check_db_integrity,
    get_module_enable_state,
    get_page_watch_baseline,
    init_db,
    insert_item,
    item_exists,
    latest_item_id,
    mark_modules_disabled,
    record_module_enabled,
    record_run_complete,
    record_run_start,
    record_source_blocked,
    record_source_empty_run,
    record_source_error,
    reset_source_blocked,
    reset_source_empty_runs,
    reset_source_errors,
    set_page_watch_baseline,
    upsert_regulator,
)
from collector.adapters.http import BlockedFetchError
from regradar.classification import write_classification
from regradar.registry import (
    BACKFILL_METHODS,
    MODULE_ON_ENABLE_VALUES,
    BackfillNotSupportedError,
    ModulesConfigError,
    _env_flag,
    _load_modules_file,
    _load_regulators_file,
    _parse_backfill_from,
    _resolve_modules,
    _REPO_ROOT,
    _source_backfill_error,
    _source_staleness_error,
    _validate_module_keys,
    load_regulators,
    require_backfill_supported,
    source_backfill,
)
from regradar.rules import RulesEngine
from regradar.taxonomy import load_and_exit_on_error
from collector.adapters.http import DEFAULT_USER_AGENT

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    return {
        "db_path": os.getenv("DB_PATH", "/app/data/reg_radar.db"),
        "schedule": os.getenv("SCHEDULE", "0 6 * * *"),
        "user_agent": os.getenv("USER_AGENT", DEFAULT_USER_AGENT),
        "timeout": int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
        "retries": int(os.getenv("REQUEST_RETRIES", "3")),
        "flaresolverr_url": os.getenv("FLARESOLVERR_URL", "").strip() or None,
        "rules_path": os.getenv("RULES_PATH", "/app/config/rules.yaml"),
        "taxonomy_path": os.getenv("TAXONOMY_PATH", "/app/config/taxonomy.yaml"),
        "jurisdictions_path": os.getenv("JURISDICTIONS_PATH", "/app/config/jurisdictions.yaml"),
        "webhooks_path": os.getenv("WEBHOOKS_PATH", "/app/config/webhooks.yaml"),
        "digests_path": os.getenv("DIGESTS_PATH", "/app/config/digests.yaml"),
        "activities_path": os.getenv("ACTIVITIES_PATH", "/app/config/activities.yaml"),
        "profiles_dir": os.getenv("PROFILES_DIR", "/app/config/profiles"),
        "modules_path": os.getenv("MODULES_PATH", "/app/config/modules.yaml"),
    }


DEFAULT_ON_ENABLE = "new_only"


def _watermark_for(on_enable: str, module: dict) -> datetime | None:
    """The lower bound this `on_enable` puts on what a module collects.

    One mechanism covers all three values, which is why there is no branch
    on on_enable anywhere else: `new_only` is "nothing older than now",
    `backfill_from` is "nothing older than that date", `backfill_all` is
    "no bound at all" (None).
    """
    if on_enable == "backfill_all":
        return None
    if on_enable == "backfill_from":
        return module["backfill_from"]
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_backfill_route(reg: dict, on_enable: str) -> None:
    """Names, per source, what will actually fetch the history the operator
    just asked for.

    ASSUMPTION: nothing in the daily collection path pages backwards - every
    adapter reads the current page only, and every historic fetch in this
    repo is a deliberate scripts/backfill_*.py run. The collector's own fetch only ever reads page 1 of a
    feed - `backfill_all` on its own lifts the watermark, it does not walk
    an archive - so staying quiet here would be the silent partial that
    #50's refusal exists to prevent, one step further along."""
    for source in reg.get("sources", []):
        if not source.get("enabled", True):
            continue
        declared = source_backfill(source)
        if declared["method"] == "script":
            logger.info(
                "%s/%s: on_enable: %s stops older items being filtered out, but the collector itself "
                "only ever reads the current page - run `python -m %s` (--dry-run first) to fetch the "
                "archive.",
                reg["id"], source["label"], on_enable,
                declared["script"].removesuffix(".py").replace("/", "."),
            )
        else:
            logger.info(
                "%s/%s: on_enable: %s stops older items being filtered out; there is no dedicated "
                "backfill script for this source, so what arrives is what it currently offers. %s",
                reg["id"], source["label"], on_enable, declared["note"] or "",
            )


# ASSUMPTION: enable state is per *regulator*, not per modules.yaml entry.
# A wildcard like `eu/*` enables 30 regulators, and a regulator added to the
# registry later must get its own transition rather than inheriting the
# wildcard's original enable date.
def apply_on_enable(engine, regulators: list[dict]) -> None:
    """Consumes `on_enable` for every module that is enabled *and was not
    collecting before*, and records the disable of every module that has
    dropped out of the selection (issue #51).

    Runs once per process, before any fetch, so a first run that fails
    leaves a watermark that is honest rather than half-set: it is the
    moment the module was switched on, not the newest item a partial fetch
    happened to see. A module already collecting normally is skipped
    entirely - `on_enable` is a transition rule, not a per-run one, and
    editing it later must not silently re-cut an install's history.
    """
    for reg_id in mark_modules_disabled(engine, {r["id"] for r in regulators}):
        logger.info(
            "Module %s is no longer selected - it will not be collected. Everything already "
            "collected for it stays in the database, including its page_watch baselines.",
            reg_id,
        )

    for reg in regulators:
        state = get_module_enable_state(engine, reg["id"])
        if state and state["enabled"]:
            continue  # already collecting - on_enable is not consulted again
        module = reg.get("module") or {}
        on_enable = module.get("on_enable") or DEFAULT_ON_ENABLE
        if state is None and regulator_has_items(engine, reg["id"]):
            # Not a new module at all: this install was already collecting
            # this regulator before module state existed, which is every
            # regulator on an install upgrading through #48's migration.
            # Treating it as a fresh enable would stamp a watermark at "now"
            # and silently drop anything published earlier that has not been
            # collected yet - a feed's existing backlog, or the window since
            # the last successful run. #48's guarantee is that an upgraded
            # install collects exactly what it collected before, so a module
            # that was already collecting gets no lower bound.
            watermark = None
            on_enable = "backfill_all"
        else:
            watermark = _watermark_for(on_enable, module)
        record_module_enabled(engine, reg["id"], module.get("id") or reg["id"], on_enable, watermark)
        logger.info(
            "Module %s enabled (%s): collecting %s.",
            reg["id"], on_enable,
            "everything the sources offer, with no date bound" if watermark is None
            else f"items published after {watermark.isoformat(sep=' ', timespec='seconds')}",
        )
        if on_enable in ("backfill_all", "backfill_from"):
            _log_backfill_route(reg, on_enable)


MODULES_MIGRATION_YAML = """\
# Written automatically when this install was upgraded to the modular
# collection model (issue #48). It reproduces the pre-upgrade behaviour
# exactly: every EU/EEA regulator, plus the UK.
#
# This file is yours - reg-radar never rewrites it once it exists, and it is
# gitignored so `git pull` never touches it. See config/modules.example.yaml
# and the README's "Modules" section for the full schema.
version: 1

defaults:
  schedule: "0 6 * * *"
  on_enable: new_only

modules:
  - id: eu/*
  - id: gb/*
"""


def _migrate_modules_file(engine, modules_path: str) -> None:
    """Writes the eu/* + gb/* modules.yaml on an existing install's first run
    after upgrading, so nobody silently stops collecting. Deliberately
    visible (a real file the operator can read and edit) rather than a
    deprecation-warning grace period - it teaches the new model on first
    contact.

    "Existing install" is "the database already holds items". A fresh install
    has none, and gets the new default instead: collect nothing until
    `reg-radar setup` runs. If the file can't be written (read-only config
    mount), warn and leave it - the caller then falls through to whichever
    state applies, and the operator has a message naming the problem."""
    if os.path.exists(modules_path):
        return
    if _env_flag("REGRADAR_COLLECT_ALL"):
        return  # an explicit operator choice already covers this install
    try:
        with engine.connect() as conn:
            has_items = conn.execute(sa_text("SELECT 1 FROM items LIMIT 1")).first() is not None
    except Exception as e:
        logger.warning("Could not check for existing items while migrating to modules: %s", e)
        return
    if not has_items:
        return
    try:
        os.makedirs(os.path.dirname(modules_path) or ".", exist_ok=True)
        with open(modules_path, "w") as f:
            f.write(MODULES_MIGRATION_YAML)
    except OSError as e:
        logger.warning(
            "This install has collected items but has no %s, and it could not be written (%s). "
            "Collection now requires a module selection - create that file (see "
            "config/modules.example.yaml) or set REGRADAR_COLLECT_ALL=true.",
            modules_path, e,
        )
        return
    logger.info(
        "Upgraded to modular collection: wrote %s selecting eu/* and gb/*, which reproduces exactly "
        "what this install collected before. Edit it to add or drop regulators; see the README's "
        "Modules section.",
        modules_path,
    )


def _apply_source_label_defaults(engine, taxonomy, item_id: str, source_labels: dict) -> None:
    """Writes each of a source's configured `labels` (regulators.yaml
    sources[].labels, see ../wiki/content/component-rules.md) as a `by: source_default`
    classification for a newly-inserted item, at that axis's configured
    source_default_confidence (config/taxonomy.yaml - see Taxonomy.
    source_default_confidence()). Code deliberately doesn't branch on axis
    name here; per-axis confidence is a config concern."""
    for axis, axis_value in (source_labels or {}).items():
        values = axis_value if isinstance(axis_value, list) else [axis_value]
        for value in values:
            try:
                confidence = taxonomy.source_default_confidence(axis)
                write_classification(
                    engine, taxonomy, item_id, axis, value,
                    by="source_default", confidence=confidence, classifier_id="collector",
                )
            except (KeyError, ValueError) as e:
                logger.warning("Skipping invalid source default label for item %s: %s", item_id, e)


def _apply_rule_labels(engine, taxonomy, rules_engine, item_id: str, rule_labels: dict) -> None:
    """Writes each (axis, value): confidence pair scored by the rules
    engine (collector.pipeline.classify(), regradar.rules.RulesEngine) for a
    newly-inserted item as a `by: rule` classification, classifier_id set to
    the engine's classifier_id (sha256 of rules.yaml + taxonomy version -
    ../wiki/content/component-rules.md)."""
    for (axis, value), confidence in (rule_labels or {}).items():
        try:
            write_classification(
                engine, taxonomy, item_id, axis, value,
                by="rule", confidence=confidence, classifier_id=rules_engine.classifier_id,
            )
        except (KeyError, ValueError) as e:
            logger.warning("Skipping invalid rule label for item %s: %s", item_id, e)


def _ordering_key(item) -> datetime:
    """What a watermark compares against: the item's published date where
    the source gives one, its fetch time otherwise (an undated item is new
    to us the moment we see it, and dropping it would be worse than
    collecting it twice)."""
    when = item.published_at or item.fetched_at
    return when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when


def _page_watch_changes(engine, regulator_id: str, label: str, items: list) -> list:
    """The page_watch items that represent an actual change to the watched
    fragment, updating the stored baseline as it goes.

    A page_watch source has no history: its whole state is the hash of a
    fragment, carried in the item id. So the first observation is a
    baseline, not a change - reporting it would mean telling a user a
    statute page changed on the day they started watching it. Equally, a
    baseline that reset would report a change that never happened, so the
    row is written once, never deleted on disable, and adopted from
    existing collection history on an install that predates it.

    Zero items (the fragment is missing, or the selector stopped matching -
    PageWatchAdapter.parse returns [] for both) leaves the baseline exactly
    as it was: an unreadable page is not evidence that the page changed,
    and it is already surfaced as an empty run by the caller."""
    changed = []
    for item in items:
        baseline = get_page_watch_baseline(engine, regulator_id, label)
        if baseline is None:
            baseline = latest_item_id(engine, regulator_id, label)
            if baseline is not None:
                # Pre-#51 history: the last item we recorded IS the last
                # thing we saw, so adopt it rather than treating a page
                # watched for months as brand new and swallowing a change.
                set_page_watch_baseline(engine, regulator_id, label, baseline)
        if baseline is None:
            set_page_watch_baseline(engine, regulator_id, label, item.id)
            logger.info(
                "page_watch baseline established for %s/%s - recorded the current content of the "
                "watched fragment. This is the first observation, not a change; nothing is reported.",
                regulator_id, label,
            )
            continue
        if baseline == item.id:
            continue  # unchanged since the baseline
        set_page_watch_baseline(engine, regulator_id, label, item.id)
        logger.info(
            "page_watch change detected for %s/%s - the watched fragment differs from the stored baseline.",
            regulator_id, label,
        )
        changed.append(item)
    return changed


def _collectable(engine, regulator_id: str, source: dict, items: list) -> list:
    """The fetched items this run should actually write - `items` itself is
    left alone so fetch counts and the empty-run/selector-drift detectors
    keep seeing what the source really returned.

    Two filters, both from issue #51: the module's `on_enable` watermark
    (absent for any caller with no module state, e.g. the backfill scripts,
    which then behave exactly as they did before), and the page_watch
    baseline comparison."""
    state = get_module_enable_state(engine, regulator_id)
    collect_from = (state or {}).get("collect_from")
    if collect_from is not None:
        items = [i for i in items if _ordering_key(i) > collect_from]
    if source.get("type") == "page_watch":
        items = _page_watch_changes(engine, regulator_id, source["label"], items)
    return items


def _fill_detail_body(item, reg: dict, flaresolverr_url: str | None, fetch_cfg: dict, detail_fetches_done: int) -> int:
    """Issue #109: for a brand-new RSS item from a regulator with
    `detail_selector` configured, fetch item.url's own detail page and
    replace the feed-summary body (rss_body_markdown) with the full article,
    through the same fetch/select/convert path as scripts/reprocess_bodies.py
    --refetch (collector/detail.py). Mutates `item` in place on success.

    On any failure (blocked, 404, selector miss, other fetch error) the item
    keeps today's RSS-summary body untouched - a detail fetch is a best-
    effort enrichment, never a reason to drop or fail an item that the feed
    itself already delivered successfully. Deliberately does not call
    record_source_error/record_source_blocked or touch this run's
    error/total counters: those drive source-health tracking
    (consecutive_failures/consecutive_blocked_runs) and this optional,
    per-item enrichment failing is not evidence the *source* is unhealthy.
    Logged at warning level only - see the PR description for why that
    (rather than a run-log field) was the choice here.

    Returns the updated detail_fetches_done counter (the delay is skipped
    before the first detail fetch in a run).
    """
    if detail_fetches_done > 0:
        time.sleep(_DETAIL_FETCH_DELAY_SECONDS)

    try:
        result = fetch_detail_body(
            item.url, reg["detail_selector"], reg.get("detail_exclude"),
            fetch_cfg["user_agent"], fetch_cfg["timeout"], fetch_cfg["retries"], flaresolverr_url,
        )
    except Exception as e:
        # An unexpected error (conversion, SSRF refusal, parser) must not
        # escape into the source-level handler and skip the rest of the feed.
        result = {"status": "error", "reason": f"{type(e).__name__}: {e}"}
    if result["status"] == "recovered":
        item.body_md = result["body_md"]
        item.extractor = result["extractor"]
        item.raw_content = result["raw_content"]
        item.raw_bytes = result["content_html"].encode("utf-8")
        item.mime = "text/html"
    else:
        logger.warning(
            "Detail fetch failed for new item %s (%s): %s - keeping RSS summary",
            item.id, reg.get("id", "<unknown>"), result["reason"],
        )
    return detail_fetches_done + 1


# Seconds between successive detail-page fetches within one run (issue
# #109) - politeness for the plain-HTTP path, matching --refetch's own
# default delay; FlareSolverr fetches are already paced by
# collector.adapters.http.throttle_flaresolverr. Never applied before the
# first detail fetch, since most runs have none or one new item.
_DETAIL_FETCH_DELAY_SECONDS = 2.0


def run_collection(engine, regulators: list[dict], cfg: dict, taxonomy=None, rules_engine=None):
    run_id = record_run_start(engine)
    total_fetched = 0
    total_new = 0
    total_errors = 0
    detail_fetches_done = 0
    error_detail = []
    blocked_detail = []
    _exception = None

    # Checked once per run, before fetching anything - corruption doesn't
    # stop the collector from writing, only from being read back cleanly
    # later, so nothing else in this run would ever notice on its own. A
    # user hitting it first via a search failure (rather than this check)
    # is exactly the silent-failure pattern this exists to prevent.
    db_integrity_ok = check_db_integrity(engine)
    if not db_integrity_ok:
        logger.critical(
            "DATABASE INTEGRITY CHECK FAILED - the SQLite file at %s may be "
            "corrupted. Search and other read paths may already be broken. "
            "See 'Backups & recovery' in skills/reg-radar-operator/SKILL.md "
            "to restore from the Litestream backup.",
            cfg.get("db_path", "<unknown>"),
        )

    fetch_cfg = {
        "user_agent": cfg["user_agent"],
        "timeout": cfg["timeout"],
        "retries": cfg["retries"],
    }

    try:
        for reg in regulators:
            try:
                if not reg.get("enabled", True):
                    logger.debug("Skipping disabled regulator: %s", reg.get("id", "<unknown>"))
                    continue

                cloudflare_protected = reg.get("cloudflare_protected", False)
                flaresolverr_url = cfg["flaresolverr_url"] if cloudflare_protected else None

                if cloudflare_protected and not cfg["flaresolverr_url"]:
                    # Skipping silently here marked the whole run "success"
                    # while collecting nothing from this regulator - a green
                    # run and one WARNING is exactly the invisible failure
                    # #39 exists to kill, and FATF is the source it was
                    # written for. A misconfigured install must be as loud as
                    # a blocked one, so record every source as blocked and
                    # count it, rather than passing over the regulator.
                    reason = "cloudflare_protected but FLARESOLVERR_URL is not set"
                    logger.error("Regulator %s: %s — skipping", reg.get("id", "<unknown>"), reason)
                    for source in reg.get("sources", []):
                        if not source.get("enabled", True):
                            continue
                        label = source.get("label", "<unknown>")
                        record_source_blocked(engine, reg["id"], label, reason)
                        total_errors += 1
                        msg = f"{reg['id']}/{label}: BLOCKED - {reason}"
                        error_detail.append(msg)
                        blocked_detail.append(msg)
                    continue

                for source in reg.get("sources", []):
                    if not source.get("enabled", True):
                        logger.debug("Skipping disabled source: %s/%s", reg.get("id", "<unknown>"), source.get("label", "<unknown>"))
                        continue
                    try:
                        items = fetch_source(source, reg["id"], flaresolverr_url, fetch_cfg, rules_engine=rules_engine)
                        total_fetched += len(items)
                        for item in _collectable(engine, reg["id"], source, items):
                            if (
                                source.get("type") == "rss"
                                and reg.get("detail_selector")
                                and not item_exists(engine, item.id)
                            ):
                                detail_fetches_done = _fill_detail_body(
                                    item, reg, flaresolverr_url, fetch_cfg, detail_fetches_done,
                                )
                            if insert_item(engine, item):
                                total_new += 1
                                if taxonomy is not None:
                                    _apply_source_label_defaults(engine, taxonomy, item.id, source.get("labels"))
                                    if rules_engine is not None:
                                        _apply_rule_labels(engine, taxonomy, rules_engine, item.id, item.rule_labels)
                        # fetch_source() returns [] both when a fetch genuinely
                        # fails (network error, blocked, etc.) and when it
                        # succeeds but the parser finds nothing - those look
                        # identical to a caller. For a scrape source specifically,
                        # 0 items is suspicious: every scrape source here only
                        # ever looks at page 1 of a listing, which in normal
                        # operation always has *something* (recent history) -
                        # a broken selector (site redesign, wrong CSS) also
                        # returns [] with no exception, and used to look exactly
                        # like a quiet, uneventful day. Treat it as a source
                        # error so it surfaces in consecutive_failures instead
                        # of silently resetting a real failure streak.
                        # Getting here at all means the fetch came back
                        # clean - no block signature, no decoy heuristic -
                        # so any previous block is over.
                        reset_source_blocked(engine, reg["id"], source["label"])

                        if source.get("type") == "scrape" and len(items) == 0:
                            error_msg = f"{reg['id']}/{source['label']}: scrape returned 0 items - possible broken selector or blocked fetch"
                            total_errors += 1
                            error_detail.append(error_msg)
                            logger.error(error_msg)
                            record_source_error(
                                engine, reg["id"], source["label"],
                                "scrape returned 0 items - possible broken selector or blocked fetch",
                            )
                        else:
                            reset_source_errors(engine, reg["id"], source["label"])

                        # Stall detection is independent of the scrape-only
                        # zero-items error above: an RSS/page_watch source
                        # returning 0 items on one run is normal (no new
                        # entries today), but a *string* of empty runs across
                        # any source type despite a successful fetch is the
                        # "200 OK, selectors matching nothing" case that a
                        # single-run check can't tell apart from a quiet day.
                        if len(items) == 0:
                            record_source_empty_run(engine, reg["id"], source["label"])
                        else:
                            reset_source_empty_runs(engine, reg["id"], source["label"])
                    except BlockedFetchError as e:
                        # Distinct from a fetch failure on purpose: the site
                        # answered, it just answered with a block or a decoy.
                        # Recorded against its own counter so a failed
                        # workaround is as visible as a source that 500s,
                        # instead of looking like a quiet day.
                        total_errors += 1
                        key = f"{reg['id']}/{source['label']}"
                        error_msg = f"{key}: BLOCKED - {e}"
                        error_detail.append(error_msg)
                        blocked_detail.append(error_msg)
                        logger.error("Blocked fetch for %s: %s", key, e)
                        record_source_blocked(engine, reg["id"], source["label"], str(e))
                    except Exception as e:
                        total_errors += 1
                        error_msg = f"{reg['id']}/{source['label']}: {e}"
                        error_detail.append(error_msg)
                        logger.error("Error fetching %s: %s", error_msg, e)
                        record_source_error(engine, reg["id"], source["label"], str(e))

            except Exception as e:
                logger.error(
                    "Unexpected error processing regulator %s — skipping: %s",
                    reg.get("id", "<unknown>") if isinstance(reg, dict) else repr(reg),
                    e,
                )
                total_errors += 1
                error_detail.append(f"regulator error: {e}")

    except Exception as e:
        _exception = e
        raise

    finally:
        # Match storage.py's documented status vocabulary (running/success/
        # partial/failed) exactly - this used to hardcode "ok"/"failed",
        # which both used a status the column comment doesn't even list, and
        # (more importantly) meant a run with per-source errors that didn't
        # escape the loop was marked "ok" instead of "partial", masking
        # exactly the kind of partial failure this status field exists to
        # surface.
        if _exception is not None:
            status = "failed"
        else:
            status = "success" if total_errors == 0 else "partial"
        record_run_complete(engine, run_id, total_fetched, total_new, total_errors, error_detail, status=status, db_integrity_ok=db_integrity_ok)
        if blocked_detail:
            # Logged at error level, separately from the counts line: a
            # blocked source is the failure mode an operator most needs to
            # see in the run summary, and it is invisible in "errors=N".
            logger.error(
                "%d source(s) BLOCKED this run - a workaround (UA, FlareSolverr) has failed or "
                "the site's protection posture changed:\n  %s",
                len(blocked_detail), "\n  ".join(blocked_detail),
            )
        if _exception is None:
            logger.info(
                "Run complete: fetched=%d new=%d errors=%d blocked=%d",
                total_fetched, total_new, total_errors, len(blocked_detail),
            )


def _acquire_lock(lock_path: str):
    """Returns an open file handle holding an exclusive, non-blocking lock,
    or None if another process already holds it. Caller must keep the
    handle alive for the run and release it via _release_lock() when done.

    File-based (not in-process) because the scheduled cron job and a manual
    `docker compose run --rm collector ... --once` are separate processes/
    containers entirely - apscheduler's own max_instances guard only
    prevents overlap *within* one process, not across two. Both share the
    same DB_PATH volume, so a lockfile next to the database is visible to
    both."""
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except OSError:
        lock_file.close()
        return None


def _release_lock(lock_file) -> None:
    fcntl.flock(lock_file, fcntl.LOCK_UN)
    lock_file.close()


def run_collection_locked(engine, regulators: list[dict], cfg: dict, lock_path: str, taxonomy=None,
                          rules_engine=None, cadence: str | None = None):
    """run_collection(), but skips entirely (rather than overlapping) if
    another collection run already holds the lock - a long FlareSolverr-
    heavy run (MFSA's ~10+ enabled categories at an 8s minimum interval
    each) can genuinely still be running when the next scheduled trigger or
    a manual --once invocation fires, and two processes writing to the same
    SQLite file concurrently is a real corruption risk, not just wasted
    work.

    `cadence` is the cron expression of the job that fired, passed only so a
    lock-skip names which cadence lost and which regulators it was carrying
    (issue #49) - one global lock now serialises several cadences, so a skip
    has to be attributable.
    """
    # ponytail: one global lock across all cadences; if a slow module starves a
    # fast one (OCC's 4h cadence repeatedly skipped behind a long MFSA run),
    # go per-module locks keyed on regulator id.
    lock_file = _acquire_lock(lock_path)
    if lock_file is None:
        logger.warning(
            "Another collection run is already in progress (lock held at %s) - skipping this run rather than "
            "overlapping it. Cadence: %s. Regulators skipped: %s",
            lock_path, cadence or cfg.get("schedule", "<unknown>"),
            ", ".join(r.get("id", "<unknown>") for r in regulators) or "<none>",
        )
        return
    try:
        run_collection(engine, regulators, cfg, taxonomy=taxonomy, rules_engine=rules_engine)
        try:
            _dispatch_webhooks(engine, cfg, taxonomy)
        except Exception:
            # Same contract as _schedule_digests: an outbound-delivery
            # problem is not a collection failure, and must not make
            # `--once` exit non-zero on a run that collected fine.
            logger.exception("Webhook dispatch raised unexpectedly - continuing")
    finally:
        _release_lock(lock_file)


def main():
    cfg = load_config()
    config_path = os.getenv("REGULATORS_PATH", "/app/config/regulators")

    logger.info("Initialising database at %s", cfg["db_path"])
    engine = init_db(cfg["db_path"])

    _migrate_modules_file(engine, cfg["modules_path"])

    logger.info("Loading regulators from %s", config_path)
    regulators = load_regulators(
        config_path, modules_path=cfg["modules_path"], jurisdictions_path=cfg["jurisdictions_path"],
    )
    # Before anything is fetched, and before the empty-selection exit below,
    # so switching everything off is recorded as a disable rather than as
    # silence (issue #51).
    apply_on_enable(engine, regulators)

    if not regulators:
        # Not an error: either a fresh install that hasn't run `reg-radar
        # setup` yet, or a modules.yaml that selects nothing. load_regulators()
        # has already logged the one line explaining which. Exit 0 - a new
        # install must not look like a crash.
        return

    logger.info("Loading taxonomy from %s and %s", cfg["taxonomy_path"], cfg["jurisdictions_path"])
    taxonomy = load_and_exit_on_error(
        cfg["taxonomy_path"], cfg["jurisdictions_path"], regulators=regulators, regulators_path=config_path,
    )

    for reg in regulators:
        upsert_regulator(engine, {
            k: reg.get(k)
            for k in ("id", "name", "full_name", "jurisdiction", "jurisdiction_type", "website", "mica_competent_authority", "enabled")
        })

    rules_engine = RulesEngine.load(cfg["rules_path"], taxonomy)
    logger.info("Loaded %d classification rule(s) from %s", len(rules_engine.rules), cfg["rules_path"])

    lock_path = os.path.join(os.path.dirname(cfg["db_path"]) or ".", ".collector.lock")

    if "--once" in sys.argv:
        logger.info("Running single collection pass")
        run_collection_locked(engine, regulators, cfg, lock_path, taxonomy=taxonomy, rules_engine=rules_engine)
        return

    from apscheduler.events import EVENT_JOB_ERROR

    def _on_job_error(event):
        logger.error(
            "Scheduled job %s raised an unhandled exception: %s",
            event.job_id,
            event.exception,
        )

    scheduler = BlockingScheduler()
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    _schedule_collection(scheduler, engine, regulators, cfg, lock_path, taxonomy, rules_engine)
    try:
        _schedule_digests(scheduler, engine, cfg, taxonomy)
    except Exception:
        logger.exception("Digest scheduling raised unexpectedly - continuing without it")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()



def _group_by_cadence(regulators: list[dict], default_schedule: str) -> dict[str, list[dict]]:
    """Groups selected regulators by their resolved cron expression, keeping
    registry order within a group. A regulator with no `module.schedule`
    (either no modules.yaml at all, or nothing set it anywhere in #48's
    defaults -> jurisdiction wildcard -> module id order) falls back to
    SCHEDULE, so an install with no cadence opinions gets exactly one job
    with exactly today's expression."""
    groups: dict[str, list[dict]] = {}
    for reg in regulators:
        schedule = (reg.get("module") or {}).get("schedule") or default_schedule
        groups.setdefault(schedule, []).append(reg)
    return groups


def _schedule_collection(scheduler: BlockingScheduler, engine, regulators: list[dict], cfg: dict,
                         lock_path: str, taxonomy, rules_engine) -> None:
    """Adds one collection job per *distinct* cron expression rather than one
    per module, so the job count tracks the number of cadences in use, not
    the size of the registry (issue #49). Each job collects only the
    regulators bound to its expression; the groups are disjoint, so two
    cadences never fetch or write the same source, and the single file lock
    in run_collection_locked() serialises them if they ever overlap in time.

    An unusable expression logs and skips that group only, following
    _schedule_digests' precedent - one bad cadence must never stop the
    scheduler. The SCHEDULE fallback is the exception: it is the whole
    install's cadence, and a broken one has always been a hard failure."""
    default_schedule = cfg["schedule"]
    for schedule, group in _group_by_cadence(regulators, default_schedule).items():
        try:
            trigger = CronTrigger.from_crontab(schedule)
        except (ValueError, TypeError) as e:
            if schedule == default_schedule:
                raise
            logger.error(
                "Invalid cron schedule %r (%s) for module(s) %s - skipping those; other cadences still run.",
                schedule, e, ", ".join(r.get("id", "<unknown>") for r in group),
            )
            continue
        scheduler.add_job(
            run_collection_locked,
            trigger,
            args=[engine, group, cfg, lock_path, taxonomy, rules_engine, schedule],
            id=f"collect:{schedule}",
        )
        logger.info(
            "Scheduled collection with cron: %s (%d regulator(s): %s)",
            schedule, len(group), ", ".join(r.get("id", "<unknown>") for r in group),
        )

def _dispatch_webhooks(engine, cfg: dict, taxonomy) -> None:
    """Delivers to every configured config/webhooks.yaml subscriber after a
    collection run completes (see ../wiki/content/concept-export-sync-contract.md). A no-op if the
    file at cfg["webhooks_path"] doesn't exist - webhooks are opt-in - or if
    taxonomy failed to load (profiles can't be expanded without it).
    """
    webhooks_path = cfg.get("webhooks_path", "/app/config/webhooks.yaml")
    if not os.path.exists(webhooks_path):
        return
    if taxonomy is None:
        logger.warning("Webhook config found at %s but taxonomy failed to load - skipping dispatch", webhooks_path)
        return

    from regradar.profiles import load_activities
    from regradar.webhooks import dispatch_all

    activities_path = cfg.get("activities_path", "/app/config/activities.yaml")
    try:
        activities = load_activities(activities_path, taxonomy)
    except Exception as e:
        logger.error("Webhook dispatch: could not load activities from %s: %s - skipping", activities_path, e)
        return
    dispatch_all(engine, webhooks_path, taxonomy, activities, cfg.get("profiles_dir", "/app/config/profiles"))


def _schedule_digests(scheduler: BlockingScheduler, engine, cfg: dict, taxonomy) -> None:
    """Adds one APScheduler cron job per config/digests.yaml entry, each
    calling the same code path as `reg-radar digest --send --name <name>`
    (issue #13's ruling: no second scheduling mechanism - reuse the
    collector's own scheduler, independent of its own SCHEDULE). A missing
    digests.yaml is a no-op - digests are opt-in, matching webhooks.yaml's
    own precedent; an invalid one is logged and skipped entirely rather
    than raised, so a bad config file never crashes the collector. `reg-radar
    digest --send` can also be run from an external cron instead of this
    scheduler - see the README's Digests section.
    """
    digests_path = cfg.get("digests_path", "/app/config/digests.yaml")
    if not os.path.exists(digests_path):
        return
    if taxonomy is None:
        logger.warning("Digest config found at %s but taxonomy failed to load - skipping digest scheduling", digests_path)
        return

    from regradar.digest import DigestConfigError, load_digests, send_digest
    from regradar.profiles import load_activities

    try:
        digests = load_digests(digests_path)
    except DigestConfigError as e:
        logger.error("Digest config error: %s - skipping digest scheduling", e)
        return

    activities_path = cfg.get("activities_path", "/app/config/activities.yaml")
    try:
        activities = load_activities(activities_path, taxonomy)
    except Exception as e:
        logger.error("Digest scheduling: could not load activities from %s: %s - skipping", activities_path, e)
        return

    for entry in digests:
        try:
            trigger = CronTrigger.from_crontab(entry["schedule"])
        except ValueError as e:
            logger.error("Digest '%s': invalid schedule %r (%s) - skipping", entry["name"], entry["schedule"], e)
            continue
        scheduler.add_job(
            send_digest,
            trigger,
            args=[engine, entry, taxonomy, activities, cfg.get("profiles_dir", "/app/config/profiles")],
            id=f"digest:{entry['name']}",
        )
        logger.info("Scheduled digest '%s' with cron: %s", entry["name"], entry["schedule"])


if __name__ == "__main__":
    main()
