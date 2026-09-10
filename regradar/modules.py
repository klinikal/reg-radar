"""`reg-radar setup` and `reg-radar modules` (issue #52).

Both commands write one file - `config/modules.yaml`, the operator's own
selection over the shipped registry (issue #48). Nothing here reimplements
the schema, the resolution order or the backfill rules: the file is built,
then handed to collector.main's `_load_modules_file` / `_resolve_modules`
for validation *before* it is written, so anything the collector would
refuse at startup is refused here instead, with nothing on disk.

Two things this module is deliberately careful about.

**The file belongs to the user.** `setup` never overwrites an existing
`config/modules.yaml` without showing what is selected now and asking; every
rewrite leaves a `.bak` alongside. PyYAML has no round-trip mode and this
package takes no dependency for one, so comments in a hand-edited file do
not survive a `modules enable`/`disable` - the backup is what makes that
recoverable rather than merely regrettable.

**Turning a module off is not free to reverse.** A module off for six months
leaves a six-month hole, and a source the registry declares
`backfill.supported: false` cannot fill it at any price - the OCC's feed
holds ten items. `disable` says so, naming the source and the registry's own
reason, at the point of the decision rather than in the docs.
"""
import argparse
import os
import shutil
import sys
import tempfile
from datetime import date

import yaml

from collector.main import (
    ModulesConfigError,
    _load_modules_file,
    _resolve_modules,
    load_regulators,
    source_backfill,
)
from regradar.taxonomy import TaxonomyError, Taxonomy, load_jurisdictions

# The §4.4 cadence defaults, as the README's "Suggested cadences" table
# states them. `setup` writes the two exceptions per module and leaves
# everything else on the daily default, so the operator never has to know
# that the OCC needs four-hourly polling.
CADENCE_DAILY = "0 6 * * *"
CADENCE_FAST = "0 */4 * * *"      # tiny-retention feeds: slower loses documents
CADENCE_WEEKLY = "0 6 * * 1"      # page_watch statute trackers: change ~3x/year

# A feed this small that also declares no backfill is losing documents
# between polls, not merely quiet - the OCC's ten-item RSS is the case this
# exists for. # ASSUMPTION: 25 is a threshold, not a measured constant; the
# only source in the registry under it today is us-occ/news (10).
_TINY_RETENTION = 25

# Jurisdiction groups for `setup`'s first question. Each is a root id in
# config/jurisdictions.yaml plus its descendants; anything not covered gets
# its own group, so a jurisdiction added later shows up rather than
# vanishing.
# ASSUMPTION: the issue's "EEA / UK / US / APAC / Middle East /
# International" maps onto the roots this registry actually has (there is no
# separate `eea` id - EEA-EFTA states parent onto `eu`).
GROUP_ROOTS = [
    ("eu", "EU / EEA"),
    ("gb", "United Kingdom"),
    ("us", "United States"),
    ("sg", "Asia-Pacific"),
    ("ae", "Middle East"),
    ("intl", "International standard-setting bodies"),
]

_HEADER = """\
# Module selection - which regulators this install actually collects.
#
# Written by `reg-radar setup`. This file is yours: reg-radar only rewrites
# it when you run `reg-radar setup` or `reg-radar modules enable/disable`,
# and it is gitignored, so `git pull` never touches it. The previous version
# is kept alongside as modules.yaml.bak.
#
# Schema, resolution order and the on_enable/schedule keys: see
# config/modules.example.yaml and the README's "Modules" section.
#
# Turning a module off is not free to reverse - nothing is collected while
# it is off, and many sources cannot be backfilled at all.
"""


# --------------------------------------------------------------------------
# Registry helpers - pure, and the part worth testing directly.
# --------------------------------------------------------------------------

def load_context(args) -> tuple[list[dict], Taxonomy]:
    """The whole registry (never a module-filtered view - these commands
    exist to change the selection, so they have to see what is outside it)
    and the jurisdiction graph."""
    regulators = load_regulators(args.regulators_path)
    try:
        taxonomy = Taxonomy(0, {}, load_jurisdictions(args.jurisdictions_path))
    except TaxonomyError as e:
        raise ModulesConfigError(f"cannot read {args.jurisdictions_path}: {e}") from e
    return regulators, taxonomy


def group_jurisdictions(regulators: list[dict], taxonomy: Taxonomy) -> list[dict]:
    """[{id, name, jurisdictions, regulators}] - the registry split into the
    groups `setup` offers, in GROUP_ROOTS order, skipping empty ones."""
    by_jurisdiction: dict[str, list[dict]] = {}
    for reg in regulators:
        by_jurisdiction.setdefault((reg.get("jurisdiction") or "").lower(), []).append(reg)

    groups, claimed = [], set()
    for root, name in GROUP_ROOTS:
        members = [root, *taxonomy.descendants(root)]
        regs = [r for j in members for r in by_jurisdiction.get(j, [])]
        claimed.update(members)
        if regs:
            groups.append({"id": root, "name": name, "jurisdictions": members, "regulators": regs})
    for jurisdiction in sorted(set(by_jurisdiction) - claimed):
        groups.append({
            "id": jurisdiction,
            "name": taxonomy.jurisdiction_name(jurisdiction) or jurisdiction,
            "jurisdictions": [jurisdiction],
            "regulators": by_jurisdiction[jurisdiction],
        })
    return groups


def collectable_sources(reg: dict, labels: list[str] | None = None) -> list[dict]:
    """The sources a selection would actually fetch: registry-enabled, and
    within `labels` if one was given. `enabled: false` sources are dropped
    here because they are never collected however they were selected, so
    counting their volume or refusing over their backfill would both be
    lies."""
    return [
        s for s in (reg.get("sources") or [])
        if s.get("enabled", True) and (labels is None or s.get("label") in labels)
    ]


def cadence_for(reg: dict, labels: list[str] | None = None) -> str:
    """The §4.4 default cadence for one regulator, derived from what the
    registry declares about its sources rather than from a list of ids."""
    sources = collectable_sources(reg, labels)
    if not sources:
        return CADENCE_DAILY
    for source in sources:
        estimate = source.get("volume_estimate") or {}
        first_run = estimate.get("first_run")
        if first_run is not None and first_run <= _TINY_RETENTION and not source_backfill(source)["supported"]:
            return CADENCE_FAST
    if all(s.get("type") == "page_watch" for s in sources):
        return CADENCE_WEEKLY
    return CADENCE_DAILY


def volume_estimate(selection: list[tuple[dict, list[str] | None]]) -> dict:
    """Summed `volume_estimate.first_run` over a selection, with the sources
    that declare nothing counted as unknown rather than as zero.

    That split is the whole point (issue #50): "approximately 8,400 items,
    plus 3 sources with unknown volume" is honest, "8,400" is not - and in
    this registry the unknowns are the majority, so silently omitting them
    would understate a first run by an unbounded amount.
    """
    known = unknown = sources = 0
    as_of: list[date] = []
    for reg, labels in selection:
        for source in collectable_sources(reg, labels):
            sources += 1
            first_run = (source.get("volume_estimate") or {}).get("first_run")
            if first_run is None:
                unknown += 1
            else:
                known += first_run
                if (source.get("volume_estimate") or {}).get("as_of"):
                    as_of.append(source["volume_estimate"]["as_of"])
    return {
        "items": known,
        "sources": sources,
        "known_sources": sources - unknown,
        "unknown_sources": unknown,
        "as_of": min(as_of) if as_of else None,
    }


def unbackfillable(reg: dict, labels: list[str] | None = None) -> list[tuple[str, str]]:
    """[(label, reason)] for every collectable source the registry says
    cannot be backfilled. The reason is the registry's own `backfill.note`,
    which is where a retention limit like the OCC's ten items is written
    down."""
    out = []
    for source in collectable_sources(reg, labels):
        declared = source_backfill(source)
        if not declared["supported"]:
            out.append((
                source.get("label"),
                (declared["note"] or "no verified way to page back through its history").strip().rstrip("."),
            ))
    return out


def describe_volume(estimate: dict, on_enable: str) -> list[str]:
    """The lines shown before anything is written."""
    lines = [f"  {estimate['sources']} source(s) across the selection."]
    if estimate["known_sources"]:
        lines.append(
            f"  Estimated first run: approximately {estimate['items']:,} items from "
            f"{estimate['known_sources']} source(s) that declare a volume estimate"
            + (f" (as of {estimate['as_of']})" if estimate["as_of"] else "")
            + (f", plus {estimate['unknown_sources']} source(s) of unknown volume."
               if estimate["unknown_sources"] else ".")
        )
    else:
        lines.append(
            f"  Estimated first run: unknown - none of the {estimate['sources']} selected source(s) "
            "declares a volume estimate."
        )
    if on_enable == "new_only":
        lines.append(
            "  Those figures are what a full history would be. With on_enable: new_only the first "
            "run records the moment you enabled each module and takes only what is published after "
            "it, so it will be far smaller."
        )
    return lines


# --------------------------------------------------------------------------
# Reading, building and writing config/modules.yaml.
# --------------------------------------------------------------------------

def read_doc(path: str) -> dict | None:
    """The parsed modules.yaml, or None when there isn't one. Structural
    errors are raised as ModulesConfigError by _load_modules_file - a
    command that is about to rewrite a broken file must say so, not
    silently replace it."""
    return _load_modules_file(path) if os.path.exists(path) else None


def resolve(regulators: list[dict], doc: dict, args) -> list[dict]:
    return _resolve_modules(regulators, doc, args.jurisdictions_path, args.modules_path)


def build_doc(entries: list[dict], on_enable: str = "new_only", backfill_from=None) -> dict:
    """A complete modules.yaml document. `entries` are already-shaped module
    blocks; `defaults` carries the daily cadence and the chosen on_enable so
    each entry only has to state its exceptions."""
    defaults = {"schedule": CADENCE_DAILY, "on_enable": on_enable}
    if backfill_from:
        defaults["backfill_from"] = str(backfill_from)
    return {"version": 1, "defaults": defaults, "modules": entries}


def dump_doc(doc: dict) -> str:
    return _HEADER + "\n" + yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, width=100)


def write_doc(path: str, doc: dict, regulators: list[dict], args) -> list[dict]:
    """Validates the document exactly as the collector will at startup, then
    writes it - so a rejected selection leaves the existing file untouched.
    Validation runs against a temp copy because _load_modules_file/
    _resolve_modules take a path, and this must not be the first time the
    real path holds the new content."""
    text = dump_doc(doc)
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(text)
        probe = f.name
    try:
        resolved = _resolve_modules(
            regulators, _load_modules_file(probe), args.jurisdictions_path, args.modules_path
        )
    finally:
        os.unlink(probe)

    if os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return resolved


def entry_for(reg_or_id, labels: list[str] | None = None, regulator: dict | None = None) -> dict:
    """One `modules:` entry. `schedule:` is written only when it differs
    from the daily default, so the file stays readable at 62 regulators and
    a cadence exception is visible as an exception."""
    entry: dict = {"id": reg_or_id}
    if labels is not None:
        entry["sources"] = list(labels)
    if regulator is not None:
        cadence = cadence_for(regulator, labels)
        if cadence != CADENCE_DAILY:
            entry["schedule"] = cadence
    return entry


# --------------------------------------------------------------------------
# `reg-radar modules list`
# --------------------------------------------------------------------------

def _cmd_list(args, regulators, taxonomy) -> int:
    doc = read_doc(args.modules_path)
    if doc is None:
        print(f"No module selection at {args.modules_path} - this install collects nothing.")
        print("Run `reg-radar setup` to choose, or `reg-radar modules enable <id|jurisdiction/*>`.")
        return 0

    selected = {r["id"]: r for r in resolve(regulators, doc, args)}
    by_id = {r["id"]: r for r in regulators}
    print(f"{args.modules_path}: {len(selected)} of {len(regulators)} registry regulator(s) selected\n")
    print(f"{'REGULATOR':<20} {'JURIS':<8} {'CADENCE':<14} {'ON_ENABLE':<14} {'VIA':<12} STATE")
    forced = []
    for reg_id in sorted(selected):
        reg, module = selected[reg_id], selected[reg_id]["module"]
        collecting = by_id[reg_id].get("enabled", True)
        if not collecting:
            forced.append(reg_id)
        labels = [s.get("label") for s in reg.get("sources", [])]
        state = "collecting" if collecting else "REGISTRY-DISABLED"
        if collecting and len(labels) < len(by_id[reg_id].get("sources") or []):
            state += f" ({len(labels)} of {len(by_id[reg_id]['sources'])} sources)"
        print(
            f"{reg_id:<20} {(reg.get('jurisdiction') or ''):<8} "
            f"{(module.get('schedule') or 'SCHEDULE env'):<14} "
            f"{(module.get('on_enable') or 'new_only'):<14} {module['id']:<12} {state}"
        )

    for reg_id in forced:
        # #48's precedence rule, stated where the operator can act on it:
        # their configuration is right and there is nothing to change.
        print(
            f"\n{reg_id} is selected but the registry marks it `enabled: false`, so it is not "
            f"collected: {(by_id[reg_id].get('notes') or 'known broken or blocked').strip()}"
        )

    if args.all:
        unselected = [r for r in regulators if r["id"] not in selected]
        print(f"\nNot selected ({len(unselected)}):")
        for reg in sorted(unselected, key=lambda r: r["id"]):
            print(f"  {reg['id']:<20} {(reg.get('jurisdiction') or ''):<8} {reg.get('name') or ''}")
    return 0


# --------------------------------------------------------------------------
# `reg-radar modules enable` / `disable`
# --------------------------------------------------------------------------

def parse_on_enable(value: str) -> tuple[str, str | None]:
    """`new_only` | `backfill_all` | `backfill_from=<date>` -> (mode, date)."""
    mode, _, when = value.partition("=")
    if mode == "backfill_from":
        if not when:
            raise ValueError("--on-enable=backfill_from needs a date, e.g. --on-enable=backfill_from=2026-01-01")
        return mode, when
    if when:
        raise ValueError(f"--on-enable={mode} takes no date")
    if mode not in ("new_only", "backfill_all"):
        raise ValueError(f"unknown --on-enable value {value!r} - expected new_only, backfill_all "
                         "or backfill_from=<date>")
    return mode, None


def _cmd_enable(args, regulators, taxonomy) -> int:
    doc = read_doc(args.modules_path) or build_doc([])
    try:
        mode, when = parse_on_enable(args.on_enable) if args.on_enable else (None, None)
    except ValueError as e:
        print(f"reg-radar modules enable: {e}", file=sys.stderr)
        return 1

    by_id = {r["id"]: r for r in regulators}
    # Several ids at once because the web UI's empty state hands the user a
    # comma-separated list of exactly the regulators that are missing
    # (webui.modules.enable_hint) - that instruction has to be runnable.
    module_ids = [i.strip().rstrip(",") for i in args.module_ids if i.strip().rstrip(",")]
    for module_id in module_ids:
        entry = next((e for e in doc["modules"] if e["id"] == module_id), None)
        if entry is None:
            # A named regulator gets its §4.4 cadence for the same reason
            # setup writes them: nobody should have to know the OCC needs
            # four-hourly polling. A wildcard gets the default - the
            # regulators under it are not all one cadence.
            entry = entry_for(module_id, None, by_id.get(module_id))
            doc["modules"].append(entry)
        if mode:
            entry["on_enable"] = mode
            entry.pop("backfill_from", None)
            if when:
                entry["backfill_from"] = when

    try:
        # Validation is the whole safety net here: an unknown id, an unknown
        # jurisdiction and #50's backfill refusal all surface from this call,
        # before anything is written.
        written = write_doc(args.modules_path, doc, regulators, args)
    except ModulesConfigError as e:
        print(f"reg-radar modules enable: {e}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1

    resolved = [r for r in written if r["module"]["id"] in module_ids]
    estimate = volume_estimate([(r, [s.get("label") for s in r.get("sources", [])]) for r in resolved])
    print(f"Enabled {', '.join(module_ids)} in {args.modules_path}: {len(resolved)} regulator(s).")
    for line in describe_volume(estimate, mode or (doc["defaults"].get("on_enable") or "new_only")):
        print(line)
    return 0


def _cmd_disable(args, regulators, taxonomy) -> int:
    doc = read_doc(args.modules_path)
    if doc is None:
        print(f"No module selection at {args.modules_path} - nothing to disable.", file=sys.stderr)
        return 1

    resolved = {r["id"]: r for r in resolve(regulators, doc, args)}
    target = resolved.get(args.module_id)
    exact = next((e for e in doc["modules"] if e["id"] == args.module_id), None)
    if target is None and exact is None:
        print(f"{args.module_id} is not selected in {args.modules_path} - nothing to do.")
        return 0

    if exact is not None:
        doc["modules"] = [e for e in doc["modules"] if e["id"] != args.module_id]
        removed = [r for r in resolved.values() if r["module"]["id"] == args.module_id]
    else:
        # Selected through a wildcard. Removing one regulator from `eu/*`
        # is only expressible by expanding the wildcard, so do that and say
        # so - the alternative is refusing a request the schema can express.
        winner = target["module"]["id"]
        covered = [r for r in resolved.values() if r["module"]["id"] == winner]
        source = next(e for e in doc["modules"] if e["id"] == winner)
        replacement = [{**source, "id": r["id"]} for r in covered if r["id"] != args.module_id]
        doc["modules"] = [e for e in doc["modules"] if e["id"] != winner] + replacement
        removed = [target]
        print(f"{args.module_id} was selected through the wildcard {winner}, which has been expanded "
              f"into {len(replacement)} explicit module(s) so it can be dropped.")
        by_name = sum(1 for r in covered if r["id"] != args.module_id and not r.get("enabled", True))
        if by_name:
            # Expanding names every regulator the wildcard swept up, which
            # promotes the registry's `enabled: false` override from a DEBUG
            # line to a WARNING for each - the same override, no longer
            # silent. Said here rather than left to be discovered in the log.
            print(f"  {by_name} of them are marked `enabled: false` in the registry and are now named "
                  "explicitly, so the collector will log a WARNING for each at startup. They were "
                  "never collected either way; keeping them selected is what stops them silently "
                  "dropping out if the registry re-enables them.")

    blocked = 0
    for reg in removed:
        for label, reason in unbackfillable(reg, [s.get("label") for s in reg.get("sources", [])]):
            blocked += 1
            print(f"  ! {reg['id']}/{label} cannot be backfilled: {reason}.")
    print(
        f"Disabling {args.module_id} stops collection now. Everything already collected stays in the "
        "database, but nothing published while it is off will be collected."
    )
    print(
        f"{blocked} of those source(s) declare no way to backfill, so re-enabling later will not fill "
        "the gap - for a short-retention feed the hole is permanent."
        if blocked else
        "Every source here declares a backfill route, so a gap may be recoverable - but only by a "
        "deliberate `scripts/backfill_*.py` run, never automatically."
    )

    try:
        write_doc(args.modules_path, doc, regulators, args)
    except ModulesConfigError as e:
        print(f"reg-radar modules disable: {e}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1
    print(f"Disabled {args.module_id} in {args.modules_path}.")
    return 0


# --------------------------------------------------------------------------
# `reg-radar setup` - the prompting layer, kept thin over the functions above.
# --------------------------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    answer = input(f"{prompt} ").strip()
    return answer or default


def _confirm(prompt: str) -> bool:
    return _ask(f"{prompt} [y/N]").lower() in ("y", "yes")


def _pick(items: list, label) -> list:
    """Numbered multi-select. Blank or `all` takes everything, which is the
    answer most people want at 62 regulators."""
    for i, item in enumerate(items, 1):
        print(f"  {i:>2}. {label(item)}")
    while True:
        answer = _ask("Numbers (comma-separated), or blank/all for every one:", "all").lower()
        if answer in ("all", "*"):
            return list(items)
        try:
            chosen = [items[int(n) - 1] for n in answer.replace(",", " ").split()]
        except (ValueError, IndexError):
            print("  Not a valid selection - try again.")
            continue
        if chosen:
            return chosen


def _cmd_setup(args, regulators, taxonomy) -> int:
    if not sys.stdin.isatty():
        print("reg-radar setup is interactive. For scripted installs use "
              "`reg-radar modules enable <id|jurisdiction/*>`.", file=sys.stderr)
        return 1

    existing = read_doc(args.modules_path)
    previously: dict[str, dict] = {}
    if existing is not None:
        current = resolve(regulators, existing, args)
        previously = {r["id"]: r for r in current}
        print(f"{args.modules_path} already exists and selects {len(current)} regulator(s):")
        print("  " + ", ".join(sorted(r["id"] for r in current)) or "  (none)")
        print("Re-running setup replaces it (the current file is kept as modules.yaml.bak).")
        print("Anything you drop stops being collected from that moment - see the warning below "
              "before confirming at the end.")
        if not _confirm("Continue?"):
            print("Left unchanged.")
            return 0

    groups = group_jurisdictions(regulators, taxonomy)
    print("\nWhich jurisdictions do you want to collect?")
    chosen_groups = _pick(groups, lambda g: f"{g['name']} ({len(g['regulators'])} regulators)")

    selection: list[tuple[dict, list[str] | None]] = []
    entries: list[dict] = []
    for group in chosen_groups:
        print(f"\n{group['name']}: {len(group['regulators'])} regulator(s).")
        if _confirm("Take all of them?"):
            entries.append(entry_for(f"{group['id']}/*"))
            selection.extend((r, None) for r in group["regulators"])
            continue
        for reg in _pick(sorted(group["regulators"], key=lambda r: r["id"]),
                         lambda r: f"{r['id']:<18} {r.get('name') or ''}"):
            labels = [s.get("label") for s in collectable_sources(reg)]
            if len(labels) > 1 and _confirm(f"  Narrow {reg['id']}'s {len(labels)} sources?"):
                labels = _pick(labels, lambda label: label)
                entries.append(entry_for(reg["id"], labels, reg))
                selection.append((reg, labels))
            else:
                entries.append(entry_for(reg["id"], None, reg))
                selection.append((reg, None))

    if not entries:
        print("\nNothing selected - no file written.")
        return 0

    print("\nWhat should the first run after enabling collect?")
    print("  1. new_only      - only what is published from now on (default, cheap)")
    print("  2. backfill_from - everything published since a date you give")
    print("  3. backfill_all  - no date bound at all")
    answer = _ask("Choice [1]:", "1")
    on_enable = {"1": "new_only", "2": "backfill_from", "3": "backfill_all"}.get(answer, "new_only")
    backfill_from = _ask("From which date? [2026-01-01]:", "2026-01-01") if on_enable == "backfill_from" else None

    if on_enable != "new_only":
        # #50's refusal, turned into a per-module fallback rather than a
        # dead end: a regulator with any un-backfillable source gets an
        # explicit `on_enable: new_only`, which beats `defaults` by the
        # resolution order. Shown, never silent.
        blocked = {reg["id"]: unbackfillable(reg, labels) for reg, labels in selection}
        blocked = {k: v for k, v in blocked.items() if v}
        if blocked:
            print(f"\n{len(blocked)} regulator(s) have sources that cannot be backfilled. They will "
                  "collect new items only - the history does not exist to fetch:")
            for reg_id, reasons in sorted(blocked.items()):
                for label, reason in reasons:
                    print(f"  {reg_id}/{label}: {reason}.")
            by_id = {r["id"]: r for r in regulators}
            wildcards = {e["id"] for e in entries if e["id"].endswith("/*")}
            for reg_id in sorted(blocked):
                entry = next((e for e in entries if e["id"] == reg_id), None)
                if entry is None:
                    entry = entry_for(reg_id, None, by_id[reg_id])
                    entries.append(entry)
                entry["on_enable"] = "new_only"
            if wildcards:
                print("  (written as explicit `on_enable: new_only` entries, which override the "
                      "wildcard they sit under.)")

    doc = build_doc(entries, on_enable, backfill_from)
    print(f"\nThis will collect {len(selection)} regulator(s) via {len(entries)} module entry/entries:")
    for line in describe_volume(volume_estimate(selection), on_enable):
        print(line)
    cadences = sorted({e.get("schedule", CADENCE_DAILY) for e in entries})
    print(f"  Cadence: {CADENCE_DAILY} by default"
          + (f", with exceptions {', '.join(c for c in cadences if c != CADENCE_DAILY)}."
             if len(cadences) > 1 else "."))
    dropped = sorted(set(previously) - {reg["id"] for reg, _ in selection})
    if dropped:
        # The irreversibility, priced at the moment of the decision rather
        # than in the docs: these are the modules this run switches OFF.
        print(f"\n{len(dropped)} module(s) you collect today are NOT in this selection and will stop "
              f"being collected: {', '.join(dropped)}")
        for reg_id in dropped:
            for label, reason in unbackfillable(previously[reg_id],
                                                [s.get("label") for s in previously[reg_id].get("sources", [])]):
                print(f"  ! {reg_id}/{label} cannot be backfilled: {reason}.")

    print("\nCollection-time filtering is not free to reverse: nothing is collected for a module "
          "while it is off, and many sources cannot be backfilled at all. Choose what you will "
          "never want, not what you are unsure about.")

    if not _confirm(f"\nWrite {args.modules_path}?"):
        print("Nothing written.")
        return 0
    try:
        write_doc(args.modules_path, doc, regulators, args)
    except ModulesConfigError as e:
        print(f"reg-radar setup: {e}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 1

    print(f"\nWrote {args.modules_path}.")
    print("Next: the collector picks this up on its next start and runs each module on its cadence "
          f"(the default is {CADENCE_DAILY}). To collect immediately: "
          "`docker compose exec collector python -m collector.main --once`.")
    print("To change the selection later: `reg-radar modules list`, then `reg-radar modules "
          "enable <id|jurisdiction/*>` or `reg-radar modules disable <id>` - or re-run "
          "`reg-radar setup`.")
    return 0


# --------------------------------------------------------------------------
# Wiring (see regradar/cli.py's docstring for the convention).
# --------------------------------------------------------------------------

def _add_common(parser) -> None:
    parser.add_argument("--modules-path", default=os.getenv("MODULES_PATH", "config/modules.yaml"))
    parser.add_argument("--regulators-path", default=os.getenv("REGULATORS_PATH", "config/regulators"))
    parser.add_argument("--jurisdictions-path",
                        default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    setup = subparsers.add_parser(
        "setup", help="Choose interactively what this install collects, and write config/modules.yaml",
    )
    _add_common(setup)
    setup.set_defaults(func=run, modules_command="setup")

    modules = subparsers.add_parser("modules", help="Inspect and edit config/modules.yaml non-interactively")
    sub = modules.add_subparsers(dest="modules_command", required=True)

    listing = sub.add_parser("list", help="Show the current selection, cadence and on_enable")
    _add_common(listing)
    listing.add_argument("--all", action="store_true", help="Also list registry regulators that are not selected")
    listing.set_defaults(func=run)

    enable = sub.add_parser("enable", help="Select a regulator or a jurisdiction wildcard (us/*)")
    _add_common(enable)
    enable.add_argument("module_ids", nargs="+", metavar="module_id",
                        help="One or more regulator ids (uk-fca) or jurisdiction wildcards ('us/*'); "
                             "trailing commas are ignored, so the web UI's hint pastes straight in")
    enable.add_argument("--on-enable", help="new_only | backfill_all | backfill_from=<YYYY-MM-DD>")
    enable.set_defaults(func=run)

    disable = sub.add_parser("disable", help="Drop a regulator from the selection")
    _add_common(disable)
    disable.add_argument("module_id", help="A regulator id, or a jurisdiction wildcard already in the file")
    disable.set_defaults(func=run)


_COMMANDS = {"setup": _cmd_setup, "list": _cmd_list, "enable": _cmd_enable, "disable": _cmd_disable}


def run(args: argparse.Namespace) -> int:
    try:
        regulators, taxonomy = load_context(args)
        return _COMMANDS[args.modules_command](args, regulators, taxonomy)
    except ModulesConfigError as e:
        # A broken or refused config/modules.yaml, reported the way the
        # collector reports it - named, readable, and with nothing written.
        print(f"reg-radar {args.modules_command}: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled - nothing written.", file=sys.stderr)
        return 1
