"""Loads config/activities.yaml and profile files, and implements the
profile expansion algorithm (architecture doc §5) shared by every consumer:
mcpserver query tools, the webui, and later the exporter.

`document_types` on a profile names the "document_type" taxonomy axis
directly, matching the existing convention of referring to it by name in
webui/app.py and mcpserver/server.py docstrings. The "instrument" axis has
no name anywhere in config (activities map to instruments without saying
so), so it is named exactly once, here, as INSTRUMENT_AXIS - nothing else
in the codebase should add a second literal for it.
"""
import os

import yaml

INSTRUMENT_AXIS = "instrument"
DOCUMENT_TYPE_AXIS = "document_type"

# Default only - callers (mcpserver/server.py, webui/app.py) resolve the
# real path from PROFILES_DIR the same way they resolve TAXONOMY_PATH etc.,
# so this module doesn't read the environment itself.
DEFAULT_PROFILES_DIR = "config/profiles"


class ProfileError(ValueError):
    """Raised when activities.yaml or a profile file is structurally
    invalid, or references an unknown jurisdiction, activity or instrument
    id."""


def _load_yaml(path: str):
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise ProfileError(f"{path}: file not found")
    except yaml.YAMLError as e:
        raise ProfileError(f"{path}: invalid YAML - {e}")


def load_activities(path: str, taxonomy) -> dict:
    """Loads config/activities.yaml: id -> {"name": str, "instruments":
    [ids]}. Every instrument id must be a valid value of the taxonomy's
    instrument axis (architecture doc §5.1) - unknown ones are a hard
    error, not a warning."""
    data = _load_yaml(path)
    if not isinstance(data, list) or not data:
        raise ProfileError(f"{path}: must be a non-empty list")
    activities: dict = {}
    for raw in data:
        aid = raw.get("id") if isinstance(raw, dict) else None
        if not aid:
            raise ProfileError(f"{path}: activity entry missing 'id'")
        if aid in activities:
            raise ProfileError(f"{path}: duplicate activity id '{aid}'")
        instruments = raw.get("instruments") or []
        for inst in instruments:
            if not taxonomy.is_valid(INSTRUMENT_AXIS, inst):
                raise ProfileError(f"{path}: activity '{aid}' references unknown instrument '{inst}'")
        activities[aid] = {"name": raw.get("name", aid), "instruments": list(instruments)}
    return activities


def _resolve_profile_path(name_or_path: str, profiles_dir: str) -> str:
    """config/profiles/<name>.yaml first, then a literal path (architecture
    doc §5.3: "A user's own profile lives wherever they like and is passed
    by path")."""
    shipped = os.path.join(profiles_dir, f"{name_or_path}.yaml")
    return shipped if os.path.exists(shipped) else name_or_path


def load_profile(name_or_path: str, taxonomy, activities: dict, profiles_dir: str = DEFAULT_PROFILES_DIR) -> dict:
    """Loads and validates one profile file (architecture doc §5.2).
    Raises ProfileError naming the file on any unknown jurisdiction,
    activity, instrument or document_type id."""
    path = _resolve_profile_path(name_or_path, profiles_dir)
    data = _load_yaml(path)
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: must be a mapping")

    jurisdiction_entries = list(data.get("jurisdictions") or [])
    known_jurisdictions = taxonomy.jurisdiction_ids()
    for entry in jurisdiction_entries:
        base = entry[:-2] if entry.endswith("/*") else entry
        if base.lower() not in known_jurisdictions:
            raise ProfileError(f"{path}: unknown jurisdiction '{entry}'")

    activity_entries = list(data.get("activities") or [])
    for aid in activity_entries:
        if aid not in activities:
            raise ProfileError(f"{path}: unknown activity '{aid}'")

    instrument_entries = list(data.get("instruments") or [])
    exclude_entries = list(data.get("exclude_instruments") or [])
    for inst in [*instrument_entries, *exclude_entries]:
        if not taxonomy.is_valid(INSTRUMENT_AXIS, inst):
            raise ProfileError(f"{path}: unknown instrument '{inst}'")

    document_type_entries = list(data.get("document_types") or [])
    for dt in document_type_entries:
        if not taxonomy.is_valid(DOCUMENT_TYPE_AXIS, dt):
            raise ProfileError(f"{path}: unknown document_type '{dt}'")

    return {
        "name": data.get("name", name_or_path),
        "jurisdictions": jurisdiction_entries,
        "activities": activity_entries,
        "instruments": instrument_entries,
        "exclude_instruments": exclude_entries,
        "document_types": document_type_entries,
        "regulators": list(data.get("regulators") or []),
        "languages": list(data.get("languages") or []),
        "include_unclassified": data.get("include_unclassified", True),
    }


class ExpandedProfile:
    """jurisdiction_ids, instrument_ids, document_types, regulators,
    languages (all sets), include_unclassified (bool) - the output of
    expand_profile(), architecture doc §5.3.

    No exclude_instrument_ids: §5.2's "hard exclude" is applied once during
    expansion (the excluded ids are already subtracted from
    instrument_ids), so carrying the set forward only invited a second,
    divergent exclusion at query time."""

    __slots__ = (
        "jurisdiction_ids", "instrument_ids",
        "document_types", "regulators", "languages", "include_unclassified",
    )

    def __init__(self, jurisdiction_ids, instrument_ids,
                 document_types, regulators, languages, include_unclassified):
        self.jurisdiction_ids = jurisdiction_ids
        self.instrument_ids = instrument_ids
        self.document_types = document_types
        self.regulators = regulators
        self.languages = languages
        self.include_unclassified = include_unclassified


def expand_profile(profile: dict, taxonomy, activities: dict) -> ExpandedProfile:
    """Architecture doc §5.3 steps 1-2 (step 3, the per-item match test, is
    applied at query time by mcpserver.queries._profile_filter_conditions,
    against these already-expanded sets).

    1. Jurisdiction set = union of each selected jurisdiction and its
       ancestors; a "<id>/*" entry also adds every descendant of <id>.
    2. Instrument set = union of instruments from selected activities plus
       explicit `instruments`, minus `exclude_instruments`.
    """
    jurisdiction_ids = set()
    for entry in profile["jurisdictions"]:
        wildcard = entry.endswith("/*")
        base = (entry[:-2] if wildcard else entry).lower()
        jurisdiction_ids.add(base)
        jurisdiction_ids.update(taxonomy.ancestors(base))
        if wildcard:
            jurisdiction_ids.update(taxonomy.descendants(base))

    instrument_ids = set()
    for aid in profile["activities"]:
        instrument_ids.update(activities[aid]["instruments"])
    instrument_ids.update(profile["instruments"])
    exclude_instrument_ids = set(profile["exclude_instruments"])
    instrument_ids -= exclude_instrument_ids

    return ExpandedProfile(
        jurisdiction_ids=jurisdiction_ids,
        instrument_ids=instrument_ids,
        document_types=set(profile["document_types"]),
        regulators=set(profile["regulators"]),
        languages=set(profile["languages"]),
        include_unclassified=bool(profile["include_unclassified"]),
    )


# Cache parsed+expanded profiles per process - invalidated only by restart
# (docs/issues/05-*.md design notes). A profile is a config file, same as
# taxonomy/activities, which mcpserver.server and webui.app already cache
# as process-global singletons.
_expanded_cache: dict = {}


def get_expanded_profile(name_or_path: str, taxonomy, activities: dict,
                          profiles_dir: str = DEFAULT_PROFILES_DIR) -> ExpandedProfile:
    cache_key = (profiles_dir, name_or_path)
    if cache_key not in _expanded_cache:
        profile = load_profile(name_or_path, taxonomy, activities, profiles_dir=profiles_dir)
        _expanded_cache[cache_key] = expand_profile(profile, taxonomy, activities)
    return _expanded_cache[cache_key]


def list_shipped_profiles(profiles_dir: str = DEFAULT_PROFILES_DIR) -> list:
    """Names of shipped profile templates under config/profiles/ - the id
    to pass as `profile` for the web UI's picker (populated at startup)."""
    if not os.path.isdir(profiles_dir):
        return []
    return sorted(
        os.path.splitext(f)[0] for f in os.listdir(profiles_dir) if f.endswith(".yaml")
    )
