"""One-off migration: converts `config/alerts.yaml`'s flat `keyword`/`tag`
pairs into `config/rules.yaml` entries (architecture doc §4.3, docs/issues/
04-rules-engine-write-interface-llm-classifier-skill.md).

Each alert rule is mapped onto the *nearest* taxonomy (axis, value) pair, in
this priority order:

1. `tag` matches a taxonomy value id exactly (e.g. tag "mica" -> instrument
   value "mica").
2. `keyword` matches one of a value's `aliases` exactly, case-insensitive
   (e.g. keyword "travel rule" -> instrument value
   "transfer_of_funds_regulation", which lists that exact alias).
3. `tag` is a case-insensitive prefix of a value id, or vice versa (e.g.
   tag "mifid" -> instrument value "mifid2").

A rule with no match under any of these is left out of rules.yaml and
printed (not silently dropped) for manual handling - it named something the
taxonomy doesn't model as a legal instrument or document type (e.g. a
company name, or a structural label like "casp"/"vasp" that architecture
doc §5.1 treats as an activity, not an objective classification label).

Every migrated rule gets weight 0.7 (the same weight the alias-derived
implicit rules for the destination value already carry) and matches both
title and body, case-insensitive - a straight re.escape() of the original
keyword text, not a hand-tuned pattern; tightening any of them is a
follow-on config change, not part of this migration.

Run from the repository root:

    .venv/bin/python -m scripts.migrate_alerts_to_rules --dry-run
    .venv/bin/python -m scripts.migrate_alerts_to_rules
"""
import argparse
import logging
import os
import re

import yaml

from regradar.rules import DEFAULT_MIN_CONFIDENCE, write_raw_rules
from regradar.taxonomy import build_taxonomy

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_legacy_alert_rules(path: str) -> list[dict]:
    """Reads alerts.yaml's old flat `keyword`/`tag` list - inlined here
    (rather than importing collector.keyword_alerts, which this issue
    removes along with the feature it implemented) since this migration is
    the only remaining reader of that format."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        return []
    return [
        {"keyword": entry["keyword"], "tag": entry["tag"]}
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("keyword"), str) and isinstance(entry.get("tag"), str)
    ]


def find_target(keyword: str, tag: str, taxonomy) -> tuple | None:
    """Returns (axis, value_id) for the nearest taxonomy match, or None."""
    tag_lower = tag.lower()
    keyword_lower = keyword.lower()

    for axis in taxonomy.axes():
        for av in taxonomy.axis_values(axis):
            if tag_lower == av.id.lower():
                return axis, av.id

    for axis in taxonomy.axes():
        for av in taxonomy.axis_values(axis):
            for alias in av.aliases:
                if alias.lower() == keyword_lower:
                    return axis, av.id

    for axis in taxonomy.axes():
        for av in taxonomy.axis_values(axis):
            id_lower = av.id.lower()
            if id_lower.startswith(tag_lower) or tag_lower.startswith(id_lower):
                return axis, av.id

    return None


def migrate(alert_rules: list[dict], taxonomy) -> tuple[list[dict], list[dict]]:
    """Returns (migrated_rules, unmapped_alert_rules)."""
    migrated = []
    unmapped = []
    for rule in alert_rules:
        target = find_target(rule["keyword"], rule["tag"], taxonomy)
        if target is None:
            unmapped.append(rule)
            continue
        axis, value = target
        migrated.append({
            "axis": axis,
            "value": value,
            "match": [{"pattern": re.escape(rule["keyword"]), "weight": 0.7, "flags": "i"}],
            "fields": ["title", "body"],
        })
    return migrated, unmapped


def run(alerts_path: str, rules_path: str, taxonomy_path: str, jurisdictions_path: str, dry_run: bool) -> int:
    taxonomy = build_taxonomy(taxonomy_path, jurisdictions_path)
    alert_rules = load_legacy_alert_rules(alerts_path)
    migrated, unmapped = migrate(alert_rules, taxonomy)

    logger.info("Mapped %d/%d alert rule(s) to rules.yaml entries", len(migrated), len(alert_rules))
    for rule in alert_rules:
        target = find_target(rule["keyword"], rule["tag"], taxonomy)
        if target:
            logger.info("  mapped: keyword=%r tag=%r -> axis=%r value=%r", rule["keyword"], rule["tag"], *target)
    for rule in unmapped:
        logger.warning("  UNMAPPED (needs manual handling): keyword=%r tag=%r", rule["keyword"], rule["tag"])

    if not dry_run:
        write_raw_rules(rules_path, DEFAULT_MIN_CONFIDENCE, migrated)
        logger.info("Wrote %d rule(s) to %s", len(migrated), rules_path)
    else:
        logger.info("[DRY RUN] Would write %d rule(s) to %s", len(migrated), rules_path)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alerts-path", default=os.getenv("ALERTS_PATH", "config/alerts.yaml"))
    parser.add_argument("--rules-path", default=os.getenv("RULES_PATH", "config/rules.yaml"))
    parser.add_argument("--taxonomy-path", default=os.getenv("TAXONOMY_PATH", "config/taxonomy.yaml"))
    parser.add_argument("--jurisdictions-path", default=os.getenv("JURISDICTIONS_PATH", "config/jurisdictions.yaml"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.alerts_path, args.rules_path, args.taxonomy_path, args.jurisdictions_path, args.dry_run)


if __name__ == "__main__":
    main()
