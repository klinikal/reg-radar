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
