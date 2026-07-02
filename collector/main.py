import logging
import os
import sys
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from collector.fetcher import fetch_source
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


def run_collection(engine, regulators: list[dict], cfg: dict):
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
                            if insert_item(engine, item):
                                total_new += 1
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
        status = "failed" if _exception is not None else "ok"
        record_run_complete(engine, run_id, total_fetched, total_new, total_errors, error_detail, status=status)
        if _exception is None:
            logger.info(
                "Run complete: fetched=%d new=%d errors=%d", total_fetched, total_new, total_errors
            )


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

    if "--once" in sys.argv:
        logger.info("Running single collection pass")
        run_collection(engine, regulators, cfg)
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
        run_collection,
        CronTrigger.from_crontab(schedule),
        args=[engine, regulators, cfg],
    )
    try:
        scheduler.start()
    except KeyboardInterrupt:
        scheduler.shutdown()


if __name__ == "__main__":
    main()
