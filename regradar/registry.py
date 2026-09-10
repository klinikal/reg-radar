"""Loads config/regulators (the registry) and config/modules.yaml (the
operator's selection over it) - architecture doc §4.4 / issue #48.

Split out of collector/main.py (issue #25 item 5): every caller here needs
only the registry and modules-config logic, never the collector's own
scheduling - and collector/main.py imports apscheduler.schedulers.blocking /
apscheduler.triggers.cron at module level for that scheduling. Before the
split, `regradar reclassify` (and every backfill script, webui/modules.py,
etc.) dragged in and initialised apscheduler just by importing
`load_regulators`. This module has no apscheduler import at module level -
_validate_module_keys imports CronTrigger lazily, only when it actually
validates a `schedule:` string in modules.yaml, which no caller here does
unless it's resolving modules.yaml.

collector/main.py re-imports everything from here (for its own use, and so
`from collector.main import load_regulators` etc. keeps working for every
existing caller) - it also imports apscheduler for real, so nothing is lost
by importing this module first.
"""
import glob
import logging
import os
import sys
from datetime import date, datetime, timezone

import yaml

from regradar.taxonomy import Taxonomy, TaxonomyError, load_jurisdictions

logger = logging.getLogger(__name__)


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

                problem = _source_backfill_error(source) or _source_staleness_error(source)
                if problem:
                    logger.error(
                        "%s: regulator '%s' source '%s': %s", path, reg_id, label, problem
                    )
                    sys.exit(1)

            regulators.append(reg)

    if modules_path is None:
        return regulators

    if os.path.exists(modules_path):
        try:
            doc = _load_modules_file(modules_path)
            return _resolve_modules(regulators, doc, jurisdictions_path, modules_path)
        except BackfillNotSupportedError as e:
            logger.error("%s", e)
            sys.exit(1)
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


def _parse_backfill_from(where: str, value) -> datetime:
    """The `backfill_from:` date as a naive-UTC datetime, matching how every
    other datetime is stored (see storage.db_now). YAML parses an unquoted
    2026-01-01 to a date object on its own; a quoted string is accepted too
    so a hand-edited file that quotes it is not refused over punctuation."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        try:
            return _parse_backfill_from(where, datetime.fromisoformat(value.strip()))
        except ValueError:
            pass
    raise ModulesConfigError(
        f"{where}: invalid backfill_from {value!r} - expected a date like 2026-01-01"
    )


class ModulesConfigError(Exception):
    """A structurally invalid config/modules.yaml. Always carries the file
    and the offending entry in its message - the operator has to be able to
    find it without reading this code."""


BACKFILL_METHODS = ("script", "paged_api", "none")

# Resolves `backfill.script:` paths, which are written relative to the repo
# root the way the README and CONTRIBUTING.md quote them
# (scripts/backfill_esma_library.py).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class BackfillNotSupportedError(ModulesConfigError):
    """A backfill was asked for against a source the registry says cannot be
    backfilled (issue #50). Subclasses ModulesConfigError so the config-load
    path already treats it as fatal; the message is written for the operator,
    not for the log, and always names the source, the reason and the
    alternative."""


def source_backfill(source: dict) -> dict:
    """The normalised backfill declaration for one registry source.

    A source with no `backfill:` block is `supported: false` - the safe
    default, since asserting backfill nobody verified is exactly the failure
    this field exists to close."""
    block = source.get("backfill") or {}
    supported = bool(block.get("supported", False))
    return {
        "supported": supported,
        "method": block.get("method") or (None if supported else "none"),
        "script": block.get("script"),
        "note": block.get("note"),
    }


def _source_staleness_error(source: dict) -> str | None:
    """`staleness:` on a page_watch source can never fire, so it is rejected
    rather than accepted and silently ignored.

    The freshness assertion reads the dates a source supplies. A page_watch
    item carries a fetch-time date, not the source's, so its age is always
    zero days and no threshold is ever crossed. FATF shipped exactly such an
    override - warn 200 / error 550 on the grey list - which read as
    considered configuration while asserting nothing at all. Answering "has
    this page changed lately?" needs collection history, which the nightly
    check has no database for; until that exists, the honest state is no
    assertion, said out loud here rather than implied by a dead setting.
    """
    st = source.get("staleness")
    if st is None:
        return None
    if not isinstance(st, dict):
        return "staleness: must be a mapping with warn_days / error_days"
    if source.get("type") == "page_watch":
        return (
            "staleness: cannot apply to a page_watch source - its items carry a "
            "fetch-time date, so the age checked here is always zero and the "
            "thresholds can never fire. Remove it."
        )
    for key in ("warn_days", "error_days"):
        if key in st and not isinstance(st[key], int):
            return f"staleness.{key} must be a whole number of days"
    warn, err = st.get("warn_days"), st.get("error_days")
    if isinstance(warn, int) and isinstance(err, int) and warn > err:
        return f"staleness.warn_days ({warn}) is above error_days ({err}) - the warning could never fire first"
    return None


def _source_backfill_error(source: dict) -> str | None:
    """Registry validation for the two optional per-source fields. Returns a
    readable problem description, or None when the source is valid."""
    block = source.get("backfill")
    if block is not None:
        if not isinstance(block, dict):
            return f"'backfill' must be a mapping, got {type(block).__name__}"
        if not isinstance(block.get("supported", False), bool):
            return "'backfill.supported' must be true or false"
        method = block.get("method")
        if method is not None and method not in BACKFILL_METHODS:
            return f"invalid backfill.method {method!r} - expected one of {', '.join(BACKFILL_METHODS)}"
        supported = bool(block.get("supported", False))
        if supported:
            if method in (None, "none"):
                return "backfill.supported: true needs a method of 'script' or 'paged_api'"
            if method == "script":
                script = block.get("script")
                if not isinstance(script, str) or not script.strip():
                    return "backfill.method: script needs a 'script:' naming the backfill script"
                # Only assert the file exists where backfill scripts are
                # actually shipped. `scripts/` is in the collector image and
                # the source tree, but deliberately not in the webui or mcp
                # images - they never run a backfill. Enforcing it there made
                # every registry load fail in those two services, which took
                # the module view and the empty-state explanations down with
                # it. The check still catches a typo everywhere it can be
                # meaningful: the collector, the CLI, and the test suite.
                scripts_dir = os.path.join(_REPO_ROOT, "scripts")
                if os.path.isdir(scripts_dir) and not os.path.isfile(os.path.join(_REPO_ROOT, script)):
                    return f"backfill.script {script!r} does not exist"
        elif method not in (None, "none"):
            return (
                f"backfill.supported: false cannot declare method {method!r} - "
                "use 'none', or drop the block entirely"
            )

    estimate = source.get("volume_estimate")
    if estimate is not None:
        if not isinstance(estimate, dict):
            return f"'volume_estimate' must be a mapping, got {type(estimate).__name__}"
        for key in ("first_run", "per_week"):
            value = estimate.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                return f"'volume_estimate.{key}' must be a non-negative whole number, got {value!r}"
        if estimate.get("as_of") is None:
            # Without a date the figure is unfalsifiable - `as_of` is what
            # makes a stale estimate visible rather than invisible.
            return "'volume_estimate' needs an 'as_of' date"
    return None


def require_backfill_supported(regulator_id: str, label: str, source: dict) -> None:
    """Raise unless the registry says this source can actually be backfilled.

    Called at the point the operator *asks* - `on_enable: backfill_all` /
    `backfill_from` in modules.yaml, and the entry points under scripts/ -
    so the refusal lands before a run is recorded or an item written. The
    alternative it names is the honest one: a source whose feed retains ten
    items has no history to fetch at any price, but everything published from
    now on is still yours."""
    declared = source_backfill(source)
    if declared["supported"]:
        return
    reason = (declared["note"] or "").strip()
    if not reason:
        reason = "the registry declares no verified way to page back through its history, so there is no history to fetch"
    if not reason.endswith("."):
        reason += "."
    raise BackfillNotSupportedError(
        f"{regulator_id}/{label} does not support backfill: {reason} "
        f"Use on_enable: new_only - you will get everything published from now on."
    )


def _validate_module_keys(where: str, entry: dict) -> None:
    """Structural validation of the three per-module keys - `schedule`
    (#MOD2), `on_enable` and its `backfill_from:` date (#MOD4). Whether a
    `backfill_from:` is actually *present* for an `on_enable: backfill_from`
    cannot be decided here: either key may be inherited from `defaults` or a
    wildcard, so that check belongs in _resolve_modules() where the whole
    resolution has happened."""
    schedule = entry.get("schedule")
    if schedule is not None:
        if not isinstance(schedule, str):
            raise ModulesConfigError(f"{where}: 'schedule' must be a cron string, got {type(schedule).__name__}")
        # Imported here, not at module level: this is the only place in this
        # module that needs apscheduler at all, and it is only reached when
        # modules.yaml actually declares a `schedule:` to validate. Every
        # other caller of load_regulators() (reclassify, the backfill
        # scripts, source_health, webui) never touches this function, so
        # importing apscheduler eagerly here would reintroduce the exact
        # cost issue #25 item 5 removed.
        from apscheduler.triggers.cron import CronTrigger
        try:
            CronTrigger.from_crontab(schedule)
        except (ValueError, TypeError) as e:
            raise ModulesConfigError(f"{where}: invalid cron schedule {schedule!r} ({e})") from e
    on_enable = entry.get("on_enable")
    if on_enable is not None and on_enable not in MODULE_ON_ENABLE_VALUES:
        raise ModulesConfigError(
            f"{where}: invalid on_enable {on_enable!r} - expected one of {', '.join(MODULE_ON_ENABLE_VALUES)}"
        )
    # ASSUMPTION: the date for `on_enable: backfill_from` is a sibling key
    # (`backfill_from: 2026-01-01`), not a value inside on_enable - #48
    # already shipped on_enable as a plain enum string and its tests assert
    # a bare `on_enable: backfill_from` parses.
    if entry.get("backfill_from") is not None:
        _parse_backfill_from(where, entry["backfill_from"])


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
        for key in ("schedule", "on_enable", "backfill_from"):
            value = defaults.get(key)
            if matched_wildcard is not None and matched_wildcard.get(key) is not None:
                value = matched_wildcard[key]
            if reg_id in exact and exact[reg_id].get(key) is not None:
                value = exact[reg_id][key]
            # `backfill_from` only exists when someone set one: an absent
            # key rather than a None keeps the resolved block the same
            # shape it has always had for everything that reads it.
            if key != "backfill_from" or value is not None:
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

        if resolved.get("on_enable") in ("backfill_all", "backfill_from"):
            # The refusal fires here, at load, because this is where the
            # operator asked - before init_db, before a run row, before an
            # item. `enabled: false` sources are never collected, so they are
            # never backfilled either and are not worth refusing over.
            for source in sources:
                if source.get("enabled", True):
                    require_backfill_supported(reg_id, source.get("label"), source)

        # After the refusal above, deliberately: a source that cannot be
        # backfilled at all is told so, rather than being asked for a date
        # that would change nothing.
        if resolved.get("on_enable") == "backfill_from":
            if resolved.get("backfill_from") is None:
                raise ModulesConfigError(
                    f"{modules_path}: module '{entry['id']}' asks for on_enable: backfill_from but "
                    f"no 'backfill_from:' date is set for it - add e.g. `backfill_from: 2026-01-01` "
                    f"to the module or to defaults"
                )
            resolved["backfill_from"] = _parse_backfill_from(
                f"{modules_path}: module '{entry['id']}'", resolved["backfill_from"]
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
