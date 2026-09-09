import fcntl
import glob
import logging
import os
import sys
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from collector.fetcher import fetch_source
from collector.storage import (
    check_db_integrity,
    init_db,
    insert_item,
    record_run_complete,
    record_run_start,
    record_source_empty_run,
    record_source_error,
    reset_source_empty_runs,
    reset_source_errors,
    upsert_regulator,
)
from regradar.classification import write_classification
from regradar.rules import RulesEngine
from regradar.taxonomy import load_and_exit_on_error

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    return {
        "db_path": os.getenv("DB_PATH", "/app/data/regradar.db"),
        "schedule": os.getenv("SCHEDULE", "0 6 * * *"),
        "user_agent": os.getenv("USER_AGENT", "regradar/2.0"),
        "timeout": int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
        "retries": int(os.getenv("REQUEST_RETRIES", "3")),
        "flaresolverr_url": os.getenv("FLARESOLVERR_URL", "").strip() or None,
        "rules_path": os.getenv("RULES_PATH", "/app/config/rules.yaml"),
        "taxonomy_path": os.getenv("TAXONOMY_PATH", "/app/config/taxonomy.yaml"),
        "jurisdictions_path": os.getenv("JURISDICTIONS_PATH", "/app/config/jurisdictions.yaml"),
        "webhooks_path": os.getenv("WEBHOOKS_PATH", "/app/config/webhooks.yaml"),
        "activities_path": os.getenv("ACTIVITIES_PATH", "/app/config/activities.yaml"),
        "profiles_dir": os.getenv("PROFILES_DIR", "/app/config/profiles"),
    }


def _load_regulators_file(path: str) -> list[dict]:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        logger.error("Invalid YAML in regulators config %s: %s", path, e)
        sys.exit(1)
    if not isinstance(data, list) or len(data) == 0:
        logger.error(
            "Regulators config %s must be a non-empty list; got %s",
            path,
            type(data).__name__ if data is not None else "empty",
        )
        sys.exit(1)
    return data


def load_regulators(config_path: str) -> list[dict]:
    """A directory loads and concatenates every `*.yaml` in it (the
    per-jurisdiction split, e.g. config/regulators/mt.yaml); a single file
    path still works for migration compatibility. Fails loudly (matching
    this module's existing sys.exit(1) convention) on a duplicate regulator
    id across files or a duplicate source label within a regulator - unknown
    jurisdictions are caught downstream by regradar.taxonomy against
    config/jurisdictions.yaml.
    """
    if os.path.isdir(config_path):
        # A leading underscore (config/regulators/_template.yaml) marks a
        # file as documentation, not a real entry - never loaded, matching
        # the common "_ = not a real file" convention CONTRIBUTING.md points
        # contributors at.
        paths = sorted(
            p for p in glob.glob(os.path.join(config_path, "*.yaml"))
            if not os.path.basename(p).startswith("_")
        )
        if not paths:
            logger.error("Regulators config directory %s has no *.yaml files", config_path)
            sys.exit(1)
    elif os.path.isfile(config_path):
        paths = [config_path]
    else:
        logger.error("Regulators config not found: %s", config_path)
        sys.exit(1)

    regulators: list[dict] = []
    seen_ids: dict[str, str] = {}
    for path in paths:
        for reg in _load_regulators_file(path):
            reg_id = reg.get("id") if isinstance(reg, dict) else None
            if not reg_id:
                logger.error("%s: regulator entry missing 'id'", path)
                sys.exit(1)
            if reg_id in seen_ids:
                logger.error(
                    "Duplicate regulator id '%s' in %s (already defined in %s)",
                    reg_id, path, seen_ids[reg_id],
                )
                sys.exit(1)
            seen_ids[reg_id] = path

            seen_labels = set()
            for source in reg.get("sources", []) or []:
                label = source.get("label") if isinstance(source, dict) else None
                if not label:
                    logger.error("%s: regulator '%s' has a source missing 'label'", path, reg_id)
                    sys.exit(1)
                if label in seen_labels:
                    logger.error("%s: regulator '%s' has duplicate source label '%s'", path, reg_id, label)
                    sys.exit(1)
                seen_labels.add(label)

            regulators.append(reg)
    return regulators


def _apply_source_label_defaults(engine, taxonomy, item_id: str, source_labels: dict) -> None:
    """Writes each of a source's configured `labels` (regulators.yaml
    sources[].labels, architecture doc §4.3) as a `by: source_default`
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
    docs/issues/04-rules-engine-write-interface-llm-classifier-skill.md)."""
    for (axis, value), confidence in (rule_labels or {}).items():
        try:
            write_classification(
                engine, taxonomy, item_id, axis, value,
                by="rule", confidence=confidence, classifier_id=rules_engine.classifier_id,
            )
        except (KeyError, ValueError) as e:
            logger.warning("Skipping invalid rule label for item %s: %s", item_id, e)


def run_collection(engine, regulators: list[dict], cfg: dict, taxonomy=None, rules_engine=None):
    run_id = record_run_start(engine)
    total_fetched = 0
    total_new = 0
    total_errors = 0
    error_detail = []
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
            "See 'Backups & recovery' in skills/regradar-operator/SKILL.md "
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
                    logger.warning(
                        "Regulator %s is cloudflare_protected but FLARESOLVERR_URL is not set — skipping",
                        reg.get("id", "<unknown>"),
                    )
                    continue

                for source in reg.get("sources", []):
                    if not source.get("enabled", True):
                        logger.debug("Skipping disabled source: %s/%s", reg.get("id", "<unknown>"), source.get("label", "<unknown>"))
                        continue
                    try:
                        items = fetch_source(source, reg["id"], flaresolverr_url, fetch_cfg, rules_engine=rules_engine)
                        total_fetched += len(items)
                        for item in items:
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
        if _exception is None:
            logger.info(
                "Run complete: fetched=%d new=%d errors=%d", total_fetched, total_new, total_errors
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


def run_collection_locked(engine, regulators: list[dict], cfg: dict, lock_path: str, taxonomy=None, rules_engine=None):
    """run_collection(), but skips entirely (rather than overlapping) if
    another collection run already holds the lock - a long FlareSolverr-
    heavy run (MFSA's ~10+ enabled categories at an 8s minimum interval
    each) can genuinely still be running when the next scheduled trigger or
    a manual --once invocation fires, and two processes writing to the same
    SQLite file concurrently is a real corruption risk, not just wasted
    work."""
    lock_file = _acquire_lock(lock_path)
    if lock_file is None:
        logger.warning(
            "Another collection run is already in progress (lock held at %s) - skipping this run rather than "
            "overlapping it.", lock_path,
        )
        return
    try:
        run_collection(engine, regulators, cfg, taxonomy=taxonomy, rules_engine=rules_engine)
        _dispatch_webhooks(engine, cfg, taxonomy)
    finally:
        _release_lock(lock_file)


def main():
    cfg = load_config()
    config_path = os.getenv("REGULATORS_PATH", "/app/config/regulators")

    logger.info("Initialising database at %s", cfg["db_path"])
    engine = init_db(cfg["db_path"])

    logger.info("Loading regulators from %s", config_path)
    regulators = load_regulators(config_path)

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

    schedule = cfg["schedule"]
    logger.info("Starting scheduler with cron: %s", schedule)
    scheduler = BlockingScheduler()
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    scheduler.add_job(
        run_collection_locked,
        CronTrigger.from_crontab(schedule),
        args=[engine, regulators, cfg, lock_path, taxonomy, rules_engine],
    )
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()


def _dispatch_webhooks(engine, cfg: dict, taxonomy) -> None:
    """Delivers to every configured config/webhooks.yaml subscriber after a
    collection run completes (architecture doc section 6). A no-op if the
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


if __name__ == "__main__":
    main()
