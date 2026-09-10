---
title: Profiles
type: component
sources: [regradar/profiles.py]
last-verified: 2026-09-10
---

## Purpose

Loads `config/activities.yaml` and individual profile files, and expands a
profile into the concrete jurisdiction/instrument sets used to filter items.
Shared by the mcpserver query tools, the webui, and the exporter.

## How it works

`load_activities(path, taxonomy)` reads `config/activities.yaml`, a list of
`{id, name, instruments}` entries, into a dict keyed by activity id. Every
listed instrument must be a valid value of the taxonomy's `instrument` axis
(`INSTRUMENT_AXIS`), otherwise `ProfileError` is raised naming the activity.

`load_profile(name_or_path, taxonomy, activities, profiles_dir)` resolves the
profile: `config/profiles/<name>.yaml` if it exists under `profiles_dir`,
otherwise the literal path passed in (so a user's own profile can live
anywhere). It validates every `jurisdictions`, `activities`, `instruments`,
`exclude_instruments` and `document_types` entry against the taxonomy or the
loaded activities dict, raising `ProfileError` naming the file on any unknown
id.

`expand_profile(profile, taxonomy, activities)` returns an `ExpandedProfile`
with two computed sets:

1. Jurisdiction set: union of each selected jurisdiction and its ancestors
   (`taxonomy.ancestors`); a `"<id>/*"` entry also adds every descendant
   (`taxonomy.descendants`).
2. Instrument set: union of instruments from selected activities plus
   explicit `instruments`, minus `exclude_instruments` (the exclusion is
   applied once here, not carried forward as a separate set).

The per-item match test itself (step 3) is applied later at query time by
`mcpserver.queries._profile_filter_conditions`, against these already-
expanded sets.

`get_expanded_profile(...)` wraps load + expand with a process-level cache
(`_expanded_cache`, keyed by `(profiles_dir, name_or_path)`), invalidated
only by process restart.

`list_shipped_profiles(profiles_dir)` returns the sorted list of profile
names shipped under `profiles_dir` (each `.yaml` file's stem), used to
populate the webui's profile picker.

## Inputs

- `config/activities.yaml`: a non-empty YAML list of `{id, name,
  instruments}`.
- A profile YAML file (shipped under `config/profiles/` or a user-supplied
  path): `name`, `jurisdictions`, `activities`, `instruments`,
  `exclude_instruments`, `document_types`, `regulators`, `languages`,
  `include_unclassified`.
- A taxonomy object providing `is_valid(axis, value)`, `jurisdiction_ids()`,
  `ancestors(id)`, `descendants(id)`.

## Outputs and side effects

- `load_activities` returns `{id: {"name": str, "instruments": [ids]}}`.
- `load_profile` returns a validated dict with all fields normalised (lists,
  with defaults applied; `include_unclassified` defaults to `True`).
- `expand_profile` returns an `ExpandedProfile` (a `__slots__` class):
  `jurisdiction_ids`, `instrument_ids`, `document_types`, `regulators`,
  `languages` (all sets), `include_unclassified` (bool).
- No file writes. `get_expanded_profile` mutates the in-process
  `_expanded_cache` dict.

## How to run or invoke

Not invoked directly; called from `mcpserver/server.py`, `webui/app.py`, and
`regradar.exporter`, each of which resolves `profiles_dir` from
`PROFILES_DIR` the same way they resolve `TAXONOMY_PATH`. The module's own
default is `DEFAULT_PROFILES_DIR = "config/profiles"`.

## How to test

[UNVERIFIED] No test command is stated in this source file; check
`code/tests/` for a `test_profiles.py`-style file exercising
`load_activities`, `load_profile`, and `expand_profile`.

## Boundaries

- Never treats an unknown jurisdiction, activity, instrument or
  document_type id as a warning, always a hard `ProfileError` naming the
  offending file.
- Never re-applies `exclude_instruments` at query time; the exclusion is
  final once `expand_profile` runs, by design (see the `ExpandedProfile`
  docstring in source).
- Never reads the environment itself to resolve `profiles_dir`; that is each
  caller's responsibility.

## Related pages

[Profiles Config](config-profiles.md)
[Profiles and Activities](concept-profiles-activities.md)
