# Profiles and activities: config-driven filtering across UI, MCP, API and export

**Size:** S to M  **Depends on:** #5 taxonomy and classification store  **Design doc:** docs/design/regradar-architecture.md §5

## Why

RegRadar's taxonomy tells you what a document *is* (jurisdiction, instrument, document type). It says nothing about who should care. Today every consumer sees the whole corpus and filters it themselves, which is the crypto-only framing v2 is dropping. A profile is the missing layer: a named, versionable filter turning "everything the taxonomy knows" into "what a UK equities dealer, or an EU CASP, needs to read." Without it, a more accurate taxonomy is still no more useful than v1's flat tags.

## Scope

- `config/activities.yaml`: the shipped activity-to-instrument mapping, exactly as specified in design doc §5.1 (`equities_dealing`, `casp`, `emi`, `bank`, each an id, name and `instruments` list).
- Profile file schema and loader, per design doc §5.2: `name`, `jurisdictions`, `activities`, `instruments`, `exclude_instruments`, `document_types`, `regulators`, `languages`, `include_unclassified` (default `true`).
- The expansion algorithm from design doc §5.3, implemented once and shared by every consumer.
- Four shipped templates under `config/profiles/`: `uk-equities-dealer.yaml` (verbatim from §5.2), `eu-casp.yaml`, `eu-emi.yaml`, `eu-bank.yaml`.
- A `profile` parameter on every existing MCP query tool that filters items: `search_items_tool`, `get_recent_tool`, `get_warnings_tool`, `count_items_tool`, `get_month_facets_tool`, `get_items_since_tool`.
- The same `profile` parameter on the equivalent JSON API routes (`/api/items`, `/api/months`) and the HTML view (`/`), plus a profile picker in the web UI templates.
- Validation on load: unknown jurisdiction, activity or instrument ids in a profile are a hard error, matching the fail-loudly convention already used for regulator jurisdictions (design doc §4.2).

## Non-goals

- The exporter's `--profile` flag (design doc §6): a separate issue building on this one.
- A profile editor or builder UI. The web UI only selects an existing profile (shipped or path-based); authoring one is still "edit a YAML file".
- Any relevance scoring beyond the boolean match in §5.3.
- Migrating v1's `alerts.yaml` keyword rules: that content becomes `rules.yaml` and `content_tags` import, both issue #6.

## Design notes

Add a single module, `regradar/profiles.py` (package layout per design doc §6a), imported by `mcpserver/`, `webui/` and later the exporter, so the algorithm exists exactly once:

```python
def load_profile(name_or_path: str, taxonomy, activities, jurisdictions) -> dict: ...
def expand_profile(profile: dict, activities, jurisdictions) -> ExpandedProfile
# jurisdiction_ids, instrument_ids, exclude_instrument_ids, document_types,
# regulators, languages (all sets), include_unclassified (bool)
```

`load_profile` checks `config/profiles/<name>.yaml` first, then treats the argument as a literal path, matching §5.2's "passed by path". Cache the parsed and expanded result per process.

Filtering intersects against the effective labels materialised by #5 (either the `item_labels` side table or `items.labels` JSON, whichever #5 chose; do not add a second mechanism). Add one shared `_profile_filter_conditions(expanded_profile, prefix="")` returning `(conditions, params)`, called from every function that builds a `WHERE` clause (`search_items`, `get_recent`, `get_warnings`, `search_items_advanced`, `get_items_since`). Expand jurisdiction ancestors, and descendants for `<id>/*` entries, on the profile side, once, and match the item's own `jurisdiction` column against that set.

`include_unclassified` needs "no instrument label" as its own queryable condition (the key absent or empty), not merely a non-matching LIKE, so it is never conflated with a genuine mismatch.

MCP tool changes are additive, one optional parameter each, following the existing docstring convention. For the web UI, add a `profile` query param alongside the existing `regulator_id`/`date_from` pattern in `index`, `api_items` and `api_months`, threaded through `_search`, and populate the picker from `config/profiles/*.yaml` at startup.

## Acceptance criteria

- [ ] `config/activities.yaml` exists with the four activities from §5.1 and loads without error.
- [ ] `config/profiles/{uk-equities-dealer,eu-casp,eu-emi,eu-bank}.yaml` exist, each valid against the loader.
- [ ] `expand_profile` implements §5.3 steps 1-3, with unit tests covering jurisdiction ancestor union, `eu/*` descendant expansion, instrument union/exclusion, and both `include_unclassified` branches.
- [ ] `eu-casp` with `jurisdictions: ["eu/*"]` matches both an ESMA item and a BaFin item; with `jurisdictions: [eu]` it matches only the ESMA item.
- [ ] Loading a profile referencing an unknown jurisdiction, activity or instrument id raises, not warns.
- [ ] All six named MCP tools accept an optional `profile` parameter and return correctly filtered results; omitting it leaves current behaviour unchanged.
- [ ] `/api/items` and `/api/months` accept `profile`; the web UI has a working profile picker that round-trips through page links like the existing filters do.
- [ ] An item with no live `instrument` classification appears under a profile with `include_unclassified: true` and is excluded when it is `false`.

## Open questions

- Resolved during drafting: plain `eu` matches EU-level output only, `eu/*` adds every member state (design doc §5.3). The three EU templates use `eu/*`.
- Should shipped templates' `regulators: []` stay empty, or should EU activity templates exclude regulators known to be irrelevant (e.g. insurance-only bodies from `eu-casp`)? Left empty here; tighten later by PR per principle 1.
