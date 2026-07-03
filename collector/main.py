import fcntl
import logging
import os
import sys
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from collector.fetcher import fetch_source
from collector.keyword_alerts import load_alert_rules, match_keywords
from collector.storage import (
    init_db,
    insert_item,
    record_run_complete,
    record_run_start,
    record_source_error,
    reset_source_errors,
    upsert_regulator,
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_config():
    return {
        "db_path": os.getenv("DB_PATH", "/app/data/casp_radar.db"),
        "schedule": os.getenv("SCHEDULE", "0 6 * * *"),
        "user_agent": os.getenv("USER_AGENT", "casp-radar/1.0"),
        "timeout": int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
        "retries": int(os.getenv("REQUEST_RETRIES", "3")),
        "flaresolverr_url": os.getenv("FLARESOLVERR_URL", "").strip() or None,
        "alerts_path": os.getenv("ALERTS_PATH", "/app/config/alerts.yaml"),
    }


def load_regulators(config_path: str) -> list[dict]:
    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("Regulators config not found: %s", config_path)
        sys.exit(1)
    except yaml.YAMLError as e:
        logger.error("Invalid YAML in regulators config %s: %s", config_path, e)
        sys.exit(1)
    if not isinstance(data, list) or len(data) == 0:
        logger.error(
            "Regulators config %s must be a non-empty list; got %s",
            config_path,
            type(data).__name__ if data is not None else "empty",
        )
        sys.exit(1)
    return data


def run_collection(engine, regulators: list[dict], cfg: dict, alert_rules: list[dict] = None):
    run_id = record_run_start(engine)
    total_fetched = 0
    total_new = 0
    total_errors = 0
    error_detail = []
    _exception = None

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
                        items = fetch_source(source, reg["id"], flaresolverr_url, fetch_cfg)
                        total_fetched += len(items)
                        for item in items:
                            if alert_rules:
                                combined_text = f"{item.title} {item.content_snippet} {item.raw_content}"
                                matched_tags = match_keywords(combined_text, alert_rules)
                                if matched_tags:
                                    item.tags = sorted(set(item.tags) | set(matched_tags))
                            if insert_item(engine, item):
                                total_new += 1
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
        record_run_complete(engine, run_id, total_fetched, total_new, total_errors, error_detail, status=status)
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


def run_collection_locked(engine, regulators: list[dict], cfg: dict, lock_path: str, alert_rules: list[dict] = None):
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
        run_collection(engine, regulators, cfg, alert_rules=alert_rules)
    finally:
        _release_lock(lock_file)


def main():
    cfg = load_config()
    config_path = os.getenv("REGULATORS_PATH", "/app/config/regulators.yaml")

    logger.info("Initialising database at %s", cfg["db_path"])
    engine = init_db(cfg["db_path"])

    logger.info("Loading regulators from %s", config_path)
    regulators = load_regulators(config_path)

    for reg in regulators:
        upsert_regulator(engine, {
            k: reg.get(k)
            for k in ("id", "name", "full_name", "jurisdiction", "jurisdiction_type", "website", "mica_competent_authority", "enabled")
        })

    alert_rules = load_alert_rules(cfg["alerts_path"])
    logger.info("Loaded %d keyword alert rule(s) from %s", len(alert_rules), cfg["alerts_path"])

    lock_path = os.path.join(os.path.dirname(cfg["db_path"]) or ".", ".collector.lock")

    if "--once" in sys.argv:
        logger.info("Running single collection pass")
        run_collection_locked(engine, regulators, cfg, lock_path, alert_rules=alert_rules)
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
        args=[engine, regulators, cfg, lock_path, alert_rules],
    )
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
