"""Module state for the web UI (issue #53).

Two filters exist and they are not the same thing:

  * `config/modules.yaml` decides what is **fetched**. Coarse, and not
    freely reversible - a module that was off for six months leaves a
    six-month hole, and many feeds cannot be backfilled.
  * profiles decide what is **shown**. Fine-grained and instantly
    reversible.

The webui reads results out of the database, so it only ever sees the
second filter. Everything the first filter excluded looks exactly like a
regulator that has been quiet - which is the confusion this module exists
to remove. It reads the registry (`config/regulators/`) and the operator's
selection (`config/modules.yaml`) and hands the views enough to say *which*
of the two produced an empty page.

Selection resolution is deliberately not reimplemented here: it reuses
collector.main's `_load_modules_file` / `_resolve_modules`, which are the
single definition of the resolution order and of the registry's
`enabled: false` override. The private names are the price of not owning a
second, divergent copy of that logic.
"""
import logging
import os

from collector.main import (
    ModulesConfigError,
    _load_modules_file,
    _resolve_modules,
    load_regulators,
)

logger = logging.getLogger(__name__)

# Loader states, mirroring collector.main.load_regulators()'s three-way
# branch plus the "your file is broken" case the collector handles by
# exiting (a web server must not).
SELECTION = "selection"          # config/modules.yaml present, collect its selection
COLLECT_ALL = "collect_all"      # absent + REGRADAR_COLLECT_ALL=true
UNCONFIGURED = "unconfigured"    # absent, no env var - collecting nothing at all
INVALID = "invalid"              # present but structurally broken
UNKNOWN = "unknown"              # the registry itself could not be read - state is unknowable

# The one-line statement of the two-filter model. Shown wherever module
# state and profile state meet, because knowing which knob you just turned
# is the whole point.
TWO_FILTER_LINE = (
    "Modules decide what is fetched; profiles decide what is shown. "
    "Changing a profile is instant and reversible - changing a module is not, "
    "because nothing is collected while it is off and most feeds cannot be backfilled."
)


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


class ModuleState:
    """The registry, the operator's selection over it, and which of the
    three loader states the install is in."""

    def __init__(self, mode, registry, selected, modules_path, error=None):
        self.mode = mode
        self.registry = registry                    # list[dict], every regulator in the catalogue
        self.selected = selected                    # reg_id -> resolved module block
        self.modules_path = modules_path
        self.error = error
        self.by_id = {r["id"]: r for r in registry}

    def registry_enabled(self, reg_id: str) -> bool:
        """False when the registry marks the regulator `enabled: false` -
        a maintainer statement that the source is broken or blocked, never
        a statement about what the operator wants."""
        reg = self.by_id.get(reg_id)
        return bool(reg) and reg.get("enabled", True)

    def is_selected(self, reg_id: str) -> bool:
        return reg_id in self.selected

    def collecting(self, reg_id: str) -> bool:
        return self.is_selected(reg_id) and self.registry_enabled(reg_id)

    def module_for(self, reg_id: str) -> dict | None:
        return self.selected.get(reg_id)


def load_module_state(regulators_path: str, modules_path: str, jurisdictions_path: str) -> ModuleState:
    """Never exits and never raises: a broken config/modules.yaml is
    reported as INVALID and rendered, because killing the web server over
    a typo in a user-owned file is a worse failure than saying so on the
    page. The collector still exits on the same file - it has to, it would
    otherwise run a half-configured collection."""
    # No modules_path: this call is the plain registry load, which is what
    # every non-collector caller wants (see load_regulators' docstring).
    # It sys.exit()s on a missing or unreadable registry, which is right for
    # the collector and fatal for a web server - a config directory that
    # isn't mounted must degrade to "module state unknown", not a dead
    # process, and certainly not a confident wrong explanation.
    try:
        registry = load_regulators(regulators_path)
    except SystemExit:
        logger.warning("Cannot read the regulator registry at %s - module state unavailable",
                       regulators_path)
        return ModuleState(UNKNOWN, [], {}, modules_path)

    if not os.path.exists(modules_path):
        mode = COLLECT_ALL if _env_flag("REGRADAR_COLLECT_ALL") else UNCONFIGURED
        # COLLECT_ALL selects the whole registry; UNCONFIGURED selects
        # nothing, which is what a brand-new install sees on first load.
        selected = (
            {r["id"]: {"id": r["id"], "schedule": None, "on_enable": None} for r in registry}
            if mode == COLLECT_ALL else {}
        )
        return ModuleState(mode, registry, selected, modules_path)

    try:
        doc = _load_modules_file(modules_path)
        resolved = _resolve_modules(registry, doc, jurisdictions_path, modules_path)
    except ModulesConfigError as e:
        return ModuleState(INVALID, registry, {}, modules_path, error=str(e))

    return ModuleState(SELECTION, registry, {r["id"]: r["module"] for r in resolved}, modules_path)


def enable_hint(jurisdiction: str, unselected_ids: set[str], registry_ids_in_jurisdiction: set[str]) -> str:
    """The id to name in the "turn this on" instruction: the jurisdiction
    wildcard when the whole jurisdiction is off, otherwise the regulators
    themselves - so the instruction is never wider than what is missing."""
    if unselected_ids >= registry_ids_in_jurisdiction:
        return f"{jurisdiction}/*"
    return ", ".join(sorted(unselected_ids))


def diagnose(state: ModuleState, scope_ids, taxonomy, has_query_filter: bool) -> dict:
    """Why is this result set empty? Returns a dict the template renders.

    `scope_ids` is the set of registry regulator ids the current view asks
    for (profile jurisdictions/regulators intersected with the form's
    regulator and jurisdiction filters), or None for "no regulator
    constraint at all". `has_query_filter` is true when a text search or a
    date range is also narrowing the result, which changes the ordinary
    empty message from "nothing collected yet" to "nothing matches".

    The four reasons are kept apart deliberately: they look identical to a
    user and blaming the wrong one is worse than saying nothing.
    """
    scope = set(scope_ids) if scope_ids is not None else {r["id"] for r in state.registry}

    if state.mode == UNKNOWN:
        # Nothing is known about module state, so nothing about it can be
        # claimed. Falls back to the plain "nothing matches" wording.
        return _ordinary_empty(state, set(), has_query_filter)

    if state.mode == INVALID:
        return {"reason": "modules_invalid", "modules_path": state.modules_path, "error": state.error}

    if state.mode == UNCONFIGURED:
        # Highest priority: nothing has ever been collected for anything,
        # so no per-jurisdiction story would be true. This is what a fresh
        # install sees before `reg-radar setup` has ever run.
        return {"reason": "no_modules_file", "modules_path": state.modules_path}

    collecting = {r for r in scope if state.collecting(r)}
    if collecting or not scope:
        return _ordinary_empty(state, collecting, has_query_filter)

    disabled = {r for r in scope if state.is_selected(r) and not state.registry_enabled(r)}
    if disabled and disabled >= scope:
        # Every regulator asked for is one the operator did select and the
        # registry force-disabled. Their configuration is not the problem.
        return {
            "reason": "registry_disabled",
            "regulators": _describe(state, sorted(disabled), taxonomy),
            "modules_path": state.modules_path,
        }

    unselected = scope - collecting - disabled
    return {
        "reason": "not_selected",
        "jurisdictions": _group_by_jurisdiction(state, unselected, taxonomy),
        "modules_path": state.modules_path,
        "also_registry_disabled": _describe(state, sorted(disabled), taxonomy),
    }


def _ordinary_empty(state: ModuleState, collecting: set, has_query_filter: bool) -> dict:
    """Modules are on and the registry is happy - the filter genuinely
    matches nothing. Surfaces the `new_only` caveat so a short backlog
    reads as expected rather than broken."""
    new_only = sorted(
        r for r in collecting
        if (state.module_for(r) or {}).get("on_enable") in (None, "new_only")
    )
    return {
        "reason": "no_matching_items",
        "has_query_filter": has_query_filter,
        "new_only_regulators": new_only,
        "collecting_count": len(collecting),
    }


def _describe(state: ModuleState, ids, taxonomy) -> list[dict]:
    out = []
    for reg_id in ids:
        reg = state.by_id.get(reg_id, {})
        jurisdiction = (reg.get("jurisdiction") or "").lower()
        out.append({
            "id": reg_id,
            "name": reg.get("name") or reg_id,
            "jurisdiction": jurisdiction,
            "jurisdiction_name": taxonomy.jurisdiction_name(jurisdiction) or jurisdiction,
        })
    return out


def _group_by_jurisdiction(state: ModuleState, reg_ids, taxonomy) -> list[dict]:
    by_jurisdiction: dict[str, set[str]] = {}
    for reg_id in reg_ids:
        jurisdiction = (state.by_id.get(reg_id, {}).get("jurisdiction") or "").lower()
        by_jurisdiction.setdefault(jurisdiction, set()).add(reg_id)

    all_in_jurisdiction: dict[str, set[str]] = {}
    for reg in state.registry:
        all_in_jurisdiction.setdefault((reg.get("jurisdiction") or "").lower(), set()).add(reg["id"])

    out = []
    for jurisdiction in sorted(by_jurisdiction):
        unselected = by_jurisdiction[jurisdiction]
        out.append({
            "id": jurisdiction,
            "name": taxonomy.jurisdiction_name(jurisdiction) or jurisdiction,
            "regulators": [
                {"id": r, "name": state.by_id.get(r, {}).get("name") or r} for r in sorted(unselected)
            ],
            "enable_id": enable_hint(jurisdiction, unselected, all_in_jurisdiction.get(jurisdiction, set())),
        })
    return out
