import logging

import yaml

logger = logging.getLogger(__name__)


def load_alert_rules(path: str) -> list[dict]:
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return []
    except yaml.YAMLError as e:
        logger.error("Invalid YAML in alerts config %s: %s — keyword tagging disabled for this run", path, e)
        return []

    if not data:
        return []
    if not isinstance(data, list):
        logger.error("Alerts config %s must be a list — keyword tagging disabled for this run", path)
        return []

    rules = []
    for entry in data:
        if not isinstance(entry, dict) or "keyword" not in entry or "tag" not in entry:
            logger.warning("Skipping invalid alert rule entry (needs 'keyword' and 'tag'): %r", entry)
            continue
        if not isinstance(entry["keyword"], str) or not isinstance(entry["tag"], str):
            logger.warning("Skipping alert rule entry with non-string 'keyword'/'tag': %r", entry)
            continue
        rules.append({"keyword": entry["keyword"], "tag": entry["tag"]})
    return rules


def match_keywords(text: str, rules: list[dict]) -> list[str]:
    if not text or not rules:
        return []
    lowered = text.lower()
    matched = {rule["tag"] for rule in rules if rule["keyword"].lower() in lowered}
    return sorted(matched)


_ALERTS_HEADER_COMMENT = """#
# Keyword tagging (like a self-hosted Google Alerts). Each entry checks every
# newly-collected item's title and content for `keyword` (case-insensitive
# substring match) and, if found, adds `tag` to that item's content_tags -
# filterable via the same tag search everywhere else in the app already works.
#
# This file is optional - delete it or leave it empty to disable keyword
# tagging entirely; collection is unaffected either way.
#
# Edit this file directly, or manage rules via the web UI at /alerts.
#
"""


def write_alert_rules(path: str, rules: list[dict]) -> None:
    with open(path, "w") as f:
        f.write(_ALERTS_HEADER_COMMENT)
        yaml.safe_dump(rules, f, default_flow_style=False, sort_keys=False)
