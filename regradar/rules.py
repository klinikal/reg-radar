"""The rules engine (architecture doc §4.3): compiles `config/rules.yaml`
plus implicit alias rules from `taxonomy.yaml` into `CompiledRule`s once per
run, and scores an item's title/body against them.

Only `RulesEngine.score()` computes anything - it never touches the
database. Writing a rule's score as a `classifications` row (by: "rule") is
the caller's job (collector.main after insert_item(), or `regradar
reclassify --by rule`), matching how `by: source_default` rows are written
today (collector.main._apply_source_label_defaults).
"""
import hashlib
import logging
import re
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

_VALID_FIELDS = {"title", "body"}
DEFAULT_MIN_CONFIDENCE = 0.5
# Alias-derived implicit rules (architecture doc §4.3): a taxonomy value's
# `aliases` compile into a rule at this weight, word-boundary matched,
# case-insensitive, over both title and body.
ALIAS_RULE_WEIGHT = 0.7


@dataclass
class CompiledRule:
    axis: str
    value: str
    patterns: list[tuple]  # (compiled re.Pattern, weight)
    fields: list[str] = field(default_factory=lambda: ["title", "body"])
    title_multiplier: float = 1.0


def _compile_pattern(pattern: str, flags: str, path: str, context: str):
    re_flags = re.IGNORECASE if flags and "i" in flags else 0
    try:
        return re.compile(pattern, re_flags)
    except re.error as e:
        logger.warning("%s: skipping invalid pattern in %s (%r): %s", path, context, pattern, e)
        return None


def _compile_explicit_rules(path: str, raw_rules, taxonomy) -> list[CompiledRule]:
    compiled = []
    for i, raw in enumerate(raw_rules or []):
        context = f"rules[{i}]"
        if not isinstance(raw, dict):
            logger.warning("%s: skipping non-mapping rule entry at %s", path, context)
            continue
        axis, value = raw.get("axis"), raw.get("value")
        if not axis or not value:
            logger.warning("%s: skipping %s - missing axis/value", path, context)
            continue
        if not taxonomy.is_valid(axis, value):
            logger.warning("%s: skipping %s - unknown axis/value %r/%r", path, context, axis, value)
            continue
        fields = raw.get("fields") or ["title", "body"]
        if not isinstance(fields, list) or not set(fields) <= _VALID_FIELDS:
            logger.warning("%s: skipping %s - fields must be a subset of %s", path, context, sorted(_VALID_FIELDS))
            continue
        patterns = []
        for m in raw.get("match", []) or []:
            if not isinstance(m, dict) or "pattern" not in m or "weight" not in m:
                logger.warning("%s: skipping a match entry in %s - needs 'pattern' and 'weight'", path, context)
                continue
            compiled_re = _compile_pattern(m["pattern"], m.get("flags", ""), path, context)
            if compiled_re is not None:
                patterns.append((compiled_re, float(m["weight"])))
        if not patterns:
            logger.warning("%s: skipping %s - no usable patterns", path, context)
            continue
        compiled.append(CompiledRule(
            axis=axis, value=value, patterns=patterns, fields=fields,
            title_multiplier=float(raw.get("title_multiplier", 1.0)),
        ))
    return compiled


def _compile_alias_rules(taxonomy) -> list[CompiledRule]:
    """One implicit CompiledRule per taxonomy value that has aliases,
    word-boundary matched, case-insensitive (architecture doc §4.3)."""
    compiled = []
    for axis in taxonomy.axes():
        for axis_value in taxonomy.axis_values(axis):
            if not axis_value.aliases:
                continue
            patterns = [
                (re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE), ALIAS_RULE_WEIGHT)
                for alias in axis_value.aliases
            ]
            compiled.append(CompiledRule(axis=axis, value=axis_value.id, patterns=patterns))
    return compiled


def _score_rule(rule: CompiledRule, title: str, body: str) -> float:
    """Design notes (see ../wiki/content/component-rules.md): for each field, a pattern
    contributes its weight once if it matches at all in that field (not
    multiplied by match count), times title_multiplier on the title field.
    Contributions sum across all the rule's patterns and fields, capped at
    1.0."""
    total = 0.0
    for field_name in rule.fields:
        text = title if field_name == "title" else body
        if not text:
            continue
        multiplier = rule.title_multiplier if field_name == "title" else 1.0
        for regex, weight in rule.patterns:
            if regex.search(text):
                total += weight * multiplier
    return min(total, 1.0)


class RulesEngine:
    def __init__(self, rules: list[CompiledRule], min_confidence: float, classifier_id: str):
        self.rules = rules
        self.min_confidence = min_confidence
        self.classifier_id = classifier_id

    @classmethod
    def load(cls, rules_path: str, taxonomy) -> "RulesEngine":
        try:
            with open(rules_path, "rb") as f:
                raw_bytes = f.read()
        except FileNotFoundError:
            raw_bytes = b""
        try:
            data = yaml.safe_load(raw_bytes) or {}
        except yaml.YAMLError as e:
            logger.error("Invalid YAML in rules config %s: %s - explicit rules disabled for this run", rules_path, e)
            data = {}
        if not isinstance(data, dict):
            logger.error("Rules config %s must be a mapping with top-level 'rules' - explicit rules disabled", rules_path)
            data = {}

        min_confidence = data.get("min_confidence", DEFAULT_MIN_CONFIDENCE)
        try:
            min_confidence = float(min_confidence)
        except (TypeError, ValueError):
            min_confidence = DEFAULT_MIN_CONFIDENCE

        rules = _compile_explicit_rules(rules_path, data.get("rules"), taxonomy)
        rules.extend(_compile_alias_rules(taxonomy))

        classifier_id = hashlib.sha256(raw_bytes + str(taxonomy.version()).encode()).hexdigest()
        return cls(rules=rules, min_confidence=min_confidence, classifier_id=classifier_id)

    def score(self, title: str, body: str) -> dict:
        """Returns {(axis, value): confidence} for every (axis, value) whose
        combined score reaches min_confidence. Scores sum per (axis, value)
        across every rule targeting that pair - an explicit rule plus its
        alias-derived counterpart stack - capped at 1.0 (architecture doc
        §4.3: "Scores sum per (axis, value), capped at 1.0")."""
        totals: dict = {}
        for rule in self.rules:
            s = _score_rule(rule, title, body)
            if s <= 0:
                continue
            key = (rule.axis, rule.value)
            totals[key] = min(1.0, totals.get(key, 0.0) + s)
        return {k: v for k, v in totals.items() if v >= self.min_confidence}


_RULES_HEADER_COMMENT = """# Deterministic, weighted, regex-capable classification rules (architecture
# doc §4.3), replacing the old alerts.yaml keyword tagger. Each entry scores
# an (axis, value) pair from config/taxonomy.yaml against an item's title
# and/or body; a match writes a `by: rule` classification row when the
# summed score reaches min_confidence below.
#
# Alias lists on taxonomy.yaml values are compiled into implicit rules at
# weight 0.7 automatically - most instruments need no entry here at all.
#
# Edit this file directly, or manage rules via the web UI at /rules.
#
"""


def load_raw_rules(path: str) -> tuple:
    """Returns (min_confidence, rules) as plain dicts - the shape the /rules
    web UI reads and writes back, distinct from RulesEngine.load() which
    compiles patterns for scoring."""
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return DEFAULT_MIN_CONFIDENCE, []
    except yaml.YAMLError as e:
        logger.error("Invalid YAML in rules config %s: %s - rules disabled for this run", path, e)
        return DEFAULT_MIN_CONFIDENCE, []
    if not isinstance(data, dict):
        return DEFAULT_MIN_CONFIDENCE, []
    try:
        min_confidence = float(data.get("min_confidence", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        min_confidence = DEFAULT_MIN_CONFIDENCE
    rules = data.get("rules") or []
    return min_confidence, rules if isinstance(rules, list) else []


def write_raw_rules(path: str, min_confidence: float, rules: list) -> None:
    with open(path, "w") as f:
        f.write(_RULES_HEADER_COMMENT)
        yaml.safe_dump(
            {"min_confidence": min_confidence, "rules": rules},
            f, default_flow_style=False, sort_keys=False,
        )
