import fcntl
import glob
import logging
import os
import sys
import yaml
from sqlalchemy import text as sa_text
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from collector.fetcher import fetch_source
from collector.storage import (
    check_db_integrity,
    init_db,
    insert_item,
    record_run_complete,
    record_run_start,
    record_source_blocked,
    record_source_empty_run,
    record_source_error,
    reset_source_blocked,
    reset_source_empty_runs,
    reset_source_errors,
    upsert_regulator,
)
from collector.adapters.http import BlockedFetchError
from regradar.classification import write_classification
from regradar.rules import RulesEngine
from regradar.taxonomy import Taxonomy, TaxonomyError, load_and_exit_on_error, load_jurisdictions
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


def load_regulators(config_path: str, modules_path: str | None = None,
                    jurisdictions_path: str = "/app/config/jurisdictions.yaml") -> list[dict]:
    """A directory loads and concatenates every `*.yaml` in it (the
    per-jurisdiction split, e.g. config/regulators/mt.yaml); a single file
    path still works for migration compatibility. Fails loudly (matching
    this module's existing sys.exit(1) convention) on a duplicate regulator
    id across files or a duplicate source label within a regulator - unknown
    jurisdictions are caught downstream by regradar.taxonomy against
    config/jurisdictions.yaml.

    `modules_path` opts into user-owned module selection (issue #48). It is
    None for every caller that wants the whole registry regardless of what
    the operator collects - the backfill scripts, source_health, reclassify -
    and only the collector daemon passes it. With it set, three states:

    | modules.yaml present | collect exactly what it selects              |
    | absent, REGRADAR_COLLECT_ALL=true | whole registry (pre-#48 behaviour) |
    | absent, no env var   | collect nothing, one line pointing at setup  |
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

    if modules_path is None:
        return regulators

    if os.path.exists(modules_path):
        try:
            doc = _load_modules_file(modules_path)
            return _resolve_modules(regulators, doc, jurisdictions_path, modules_path)
        except ModulesConfigError as e:
            # Same contract as every other bad config file in this module:
            # a readable message naming the offending entry and a clean
            # exit, never a traceback and never a half-configured run.
            logger.error("Invalid module configuration: %s", e)
            sys.exit(1)

    if _env_flag("REGRADAR_COLLECT_ALL"):
        logger.info(
            "No %s and REGRADAR_COLLECT_ALL is set - collecting every regulator in the registry",
            modules_path,
        )
        return regulators

    logger.info(
        "No module selection at %s, so nothing will be collected. Run `reg-radar setup` to choose "
        "which regulators to collect, or set REGRADAR_COLLECT_ALL=true to collect the whole registry. "
        "See the README's Modules section.",
        modules_path,
    )
    return []


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


MODULE_ON_ENABLE_VALUES = ("new_only", "backfill_from", "backfill_all")


class ModulesConfigError(Exception):
    """A structurally invalid config/modules.yaml. Always carries the file
    and the offending entry in its message - the operator has to be able to
    find it without reading this code."""


def _validate_module_keys(where: str, entry: dict) -> None:
    """`schedule` and `on_enable` are validated here but not acted on:
    per-module cadence is #MOD2 and on_enable semantics are #MOD4. Carrying
    them through the schema now means an operator's file doesn't have to be
    rewritten when those land."""
    schedule = entry.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, str):
            raise ModulesConfigError(f"{where}: 'schedule' must be a cron string, got {type(schedule).__name__}")
        try:
            CronTrigger.from_crontab(schedule)
        except (ValueError, TypeError) as e:
            raise ModulesConfigError(f"{where}: invalid cron schedule {schedule!r} ({e})") from e
    on_enable = entry.get("on_enable")
    if on_enable is not None and on_enable not in MODULE_ON_ENABLE_VALUES:
        raise ModulesConfigError(
            f"{where}: invalid on_enable {on_enable!r} - expected one of {', '.join(MODULE_ON_ENABLE_VALUES)}"
        )


def _load_modules_file(path: str) -> dict:
    """Parses and structurally validates config/modules.yaml. Does not know
    anything about the registry - id/label existence is checked in
    _resolve_modules(), which has the regulators to check against."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ModulesConfigError(f"{path}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ModulesConfigError(f"{path}: must be a mapping with 'version' and 'modules' keys")
    if data.get("version") != 1:
        raise ModulesConfigError(f"{path}: unsupported version {data.get('version')!r} - expected 1")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ModulesConfigError(f"{path}: 'defaults' must be a mapping")
    _validate_module_keys(f"{path}: defaults", defaults)

    modules = data.get("modules")
    if modules is None:
        modules = []
    if not isinstance(modules, list):
        raise ModulesConfigError(f"{path}: 'modules' must be a list")

    seen: set[str] = set()
    for i, entry in enumerate(modules):
        where = f"{path}: modules[{i}]"
        if not isinstance(entry, dict):
            raise ModulesConfigError(f"{where}: must be a mapping, got {type(entry).__name__}")
        module_id = entry.get("id")
        if not isinstance(module_id, str) or not module_id.strip():
            raise ModulesConfigError(f"{where}: missing 'id'")
        if module_id in seen:
            raise ModulesConfigError(f"{where}: duplicate module id {module_id!r}")
        seen.add(module_id)
        sources = entry.get("sources")
        if sources is not None and not (isinstance(sources, list) and all(isinstance(s, str) for s in sources)):
            raise ModulesConfigError(f"{where} ({module_id}): 'sources' must be a list of source labels")
        _validate_module_keys(f"{where} ({module_id})", entry)

    return {"version": 1, "defaults": defaults, "modules": modules}


def _resolve_modules(regulators: list[dict], doc: dict, jurisdictions_path: str,
                     modules_path: str) -> list[dict]:
    """Applies the resolution order - defaults -> jurisdiction wildcard ->
    explicit module id -> `sources:` - narrower always winning, and returns
    shallow copies of the selected regulators with their `sources` trimmed
    to the selection and a resolved `module` block attached.

    The registry wins on `false`: a regulator or source the registry marks
    `enabled: false` is never collected, however explicitly the operator
    selected it. Registry `enabled: false` is a maintainer statement that
    the source is broken or blocked, not a user statement about wanting it.
    Actually skipping it stays in run_collection() as before; this only
    surfaces the override, and only when the operator asked for that thing
    by name - a wildcard sweeping up the 50-odd deliberately disabled MFSA
    circular categories is not an overridden request and stays at DEBUG.
    """
    try:
        # Only the jurisdiction graph is needed (descendants/ancestors), and
        # this runs before the real taxonomy is built in main() - the axes
        # would be dead weight here.
        taxonomy = Taxonomy(0, {}, load_jurisdictions(jurisdictions_path))
    except TaxonomyError as e:
        raise ModulesConfigError(f"cannot expand jurisdiction wildcards: {e}") from e

    by_id = {reg["id"]: reg for reg in regulators}
    known_jurisdictions = taxonomy.jurisdiction_ids()

    exact: dict[str, dict] = {}
    wildcard: dict[str, dict] = {}
    for entry in doc["modules"]:
        module_id = entry["id"].strip()
        if module_id.endswith("/*"):
            base = module_id[:-2].lower()
            if base not in known_jurisdictions:
                raise ModulesConfigError(
                    f"{modules_path}: module {module_id!r} names unknown jurisdiction {base!r} "
                    f"(known ids are in {jurisdictions_path})"
                )
            wildcard[base] = entry
        elif module_id not in by_id:
            raise ModulesConfigError(
                f"{modules_path}: unknown module id {module_id!r} - no regulator with that id in the registry"
            )
        else:
            exact[module_id] = entry

    defaults = doc["defaults"]
    selected: list[dict] = []
    labels_seen: dict[str, set[str]] = {}

    for reg in regulators:
        reg_id = reg["id"]
        jurisdiction = (reg.get("jurisdiction") or "").lower()
        # ancestors() is nearest-first, so the first wildcard that matches is
        # the narrowest one: `fr/*` beats `eu/*` for the AMF.
        matched_wildcard = next(
            (wildcard[base] for base in (jurisdiction, *taxonomy.ancestors(jurisdiction)) if base in wildcard),
            None,
        )
        entry = exact.get(reg_id) or matched_wildcard
        if entry is None:
            continue

        resolved = {"id": entry["id"]}
        for key in ("schedule", "on_enable"):
            value = defaults.get(key)
            if matched_wildcard is not None and matched_wildcard.get(key) is not None:
                value = matched_wildcard[key]
            if reg_id in exact and exact[reg_id].get(key) is not None:
                value = exact[reg_id][key]
            resolved[key] = value

        all_sources = reg.get("sources", []) or []
        # Recorded against every entry that *matched*, not just the one that
        # won, so a wildcard's `sources:` list isn't reported as unknown just
        # because a narrower entry overrode it for the regulator that has
        # that label.
        for matched in (entry, matched_wildcard):
            if matched is not None:
                labels_seen.setdefault(matched["id"], set()).update(s.get("label") for s in all_sources)
        wanted = entry.get("sources")
        sources = all_sources if wanted is None else [s for s in all_sources if s.get("label") in wanted]

        if not reg.get("enabled", True):
            log = logger.warning if reg_id in exact else logger.debug
            log(
                "Module '%s' selects regulator %s, but the registry marks it enabled: false "
                "(known broken or blocked) - it will not be collected.",
                entry["id"], reg_id,
            )
        if wanted is not None:
            for source in sources:
                if not source.get("enabled", True):
                    logger.warning(
                        "Module '%s' explicitly selects source %s/%s, but the registry marks it "
                        "enabled: false (known broken or blocked) - it will not be collected.",
                        entry["id"], reg_id, source.get("label"),
                    )

        selected.append({**reg, "sources": sources, "module": resolved})

    for entry in doc["modules"]:
        wanted = entry.get("sources")
        if not wanted or entry["id"] not in labels_seen:
            continue
        unknown = [label for label in wanted if label not in labels_seen[entry["id"]]]
        if unknown:
            raise ModulesConfigError(
                f"{modules_path}: module '{entry['id']}' lists unknown source label(s) "
                f"{', '.join(repr(u) for u in unknown)} - available: "
                f"{', '.join(sorted(l for l in labels_seen[entry['id']] if l))}"
            )

    logger.info(
        "Module selection from %s: %d regulator(s), %d source(s) matched "
        "(before registry `enabled: false` filtering)",
        modules_path, len(selected), sum(len(r["sources"]) for r in selected),
    )
    return selected


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
